"""Gameplay and character-selection command dispatch."""

from __future__ import annotations

import time
import traceback
import sys
import threading

from dso_server.runtime.context import *
from dso_server.protocol.trade_protocol import TRADE_CLIENT_COMMANDS
from dso_server.protocol.food_recycling_protocol import SALVAGE_COOK_ITEM_COMMAND
from dso_server.protocol.prop_protocol import (
    PROP_INTERACTION_COMMAND,
    QUEST_TRIGGER_SIGNAL_COMMAND,
)
from dso_server.protocol.quest_protocol import (
    QUEST_COMMAND,
    QUEST_MONSTER_INDICATION_REQUEST_COMMAND,
    QUEST_TRIGGER_INDICATION_REQUEST_COMMAND,
    TALK_COMMAND,
    DELIVER_COMMAND,
    PAY_COMMAND,
    DEFEND_COMMAND,
)
from dso_server.runtime.transport import Session
from dso_server.protocol.daily_challenge_protocol import DAILY_CHALLENGE_SELECTION_COMMAND
from dso_server.protocol.item_customization_protocol import APPLY_ITEM_CUSTOMIZATION_COMMAND
from dso_server.protocol.ranking_protocol import RANKING_REQUEST_COMMANDS
from dso_server.protocol.wanted_thief_protocol import (
    GET_WANTED_THIEF_INFO_COMMAND, ACCEPT_WANTED_THIEF_MISSION_COMMAND,
    REFRESH_WANTED_THIEF_MISSION_COMMAND,
)
from dso_server.protocol.season_pass_protocol import SEASON_PASS_COMMANDS
from dso_server.protocol.social_protocol import SOCIAL_MANAGEMENT_COMMANDS
from dso_server.protocol.seasonal_activity_protocol import SEASONAL_ACTIVITY_COMMANDS
from dso_server.protocol.pvp_match_protocol import PVP_OBJECTIVE_COMMANDS
from dso_server.protocol.gameplay_protocol import (
    CLIENT_CURRENT_POSITION_FRAME_INFO_COMMAND, LOGOUT_COMMAND, decode_logout,
)


_GAMEPLAY_BULLET_SKILL_COMMANDS = frozenset({
    BULLET_SKILL_COMMAND,
    TARGET_POINT_BULLET_SKILL_COMMAND,
    TARGET_BULLET_SKILL_COMMAND,
})
_GAMEPLAY_SPATIAL_SKILL_COMMANDS = frozenset({
    SKILL_COMMAND,
    TARGET_SKILL_COMMAND,
    SHIFTED_SKILL_COMMAND,
})
_GAMEPLAY_MOVE_COMMAND = 0x005F
_GAMEPLAY_SKILL_DISPATCH_BATCH_LIMIT = 8
_GAMEPLAY_SKILL_DISPATCH_QUEUE_LIMIT = 512
_GAMEPLAY_SKILL_DISPATCH_STALL_SECONDS = 0.500
_GAMEPLAY_SKILL_DISPATCH_SLOW_COMMAND_SECONDS = 0.050
_GAMEPLAY_SKILL_DISPATCH_STALL_LOG_INTERVAL_SECONDS = 2.0


def _spatial_input(command_id):
    return (command_id == _GAMEPLAY_MOVE_COMMAND
            or command_id in _GAMEPLAY_BULLET_SKILL_COMMANDS
            or command_id in _GAMEPLAY_SPATIAL_SKILL_COMMANDS)


class DispatchService:
    """Gameplay and character-selection command dispatch."""

    @staticmethod
    def _gameplay_skill_dispatch_context(s: Session) -> tuple:
        """Identify the map and arena phase in which input was received."""

        match = getattr(s, "gameplay_match", None)
        return (
            id(getattr(s, "gameplay_combat_state", None)),
            str(getattr(s, "gameplay_map", "") or ""),
            id(match) if match is not None else None,
            getattr(match, "round_index", None),
            getattr(match, "phase", None),
        )

    def _gameplay_skill_dispatch_session_is_current(
            self, s: Session, context: tuple, command_id=None) -> bool:
        """Reject queued work after disconnect, replacement, or map change."""

        if (
            not getattr(s, "connected", False)
            or getattr(s, "retired", False)
            or getattr(s, "transport_failure_reason", None)
            or not getattr(s, "gameplay_mode", False)
            or getattr(s, "gameplay_exit_transition_started", False)
        ):
            return False
        current = self._gameplay_skill_dispatch_context(s)
        if (current != context if command_id is None or _spatial_input(command_id)
                else current[:3] != context[:3]):
            return False
        is_current = getattr(self, "is_session_current", None)
        return is_current is None or bool(is_current(s))

    @staticmethod
    def _reserve_gameplay_skill_dispatch_locked(s: Session) -> int:
        """Claim the session's one outstanding queue-drain callback."""

        s.gameplay_skill_dispatch_next_token += 1
        token = s.gameplay_skill_dispatch_next_token
        s.gameplay_skill_dispatch_scheduled_token = token
        s.gameplay_skill_dispatch_token_reserved_at = time.perf_counter()
        return token

    def _schedule_gameplay_skill_dispatch(
            self, s: Session, token: int) -> None:
        """Schedule one bounded queue slice without holding a state lock."""

        try:
            timer = s.gameplay_ingress_timer(
                0.0,
                self._drain_gameplay_skill_dispatch,
                args=(s, token),
            )
            timer.daemon = True
            timer.start()
        except Exception as exc:
            dropped = 0
            with s.gameplay_skill_dispatch_lock:
                if s.gameplay_skill_dispatch_scheduled_token == token:
                    s.gameplay_skill_dispatch_scheduled_token = None
                    dropped = len(s.gameplay_skill_dispatch_queue)
                    s.gameplay_skill_dispatch_queue.clear()
            print(
                f"[{GAMEPLAY_SERVICE_NAME}] ERROR: could not schedule "
                f"gameplay skill ingress; dropped={dropped}: "
                f"{type(exc).__name__}: {exc}"
            )
            retire = getattr(self, "request_session_retirement", None)
            if retire is not None:
                retire(s, "gameplay input scheduler unavailable")

    def _enqueue_gameplay_skill_command(
            self, s: Session, command_id: int, payload: bytes) -> bool:
        """Queue ordered gameplay input away from the UDP receive thread.

        The historical field/method names mention skills, but movement belongs
        in the same per-session FIFO. A movement update performs vicinity,
        loot, presence, and combat reconciliation; running it inline made the
        sole receive thread wait behind projectile/status workers and delayed
        RakNet ACKs and connected-ping replies by hundreds of milliseconds.

        Adjacent movement commands are latest-state updates and may coalesce.
        Any command between two moves remains a FIFO boundary. Inventory,
        social and travel commands share that boundary so durable writes and
        content queries cannot block packet intake or overtake earlier casts.
        """

        context = self._gameplay_skill_dispatch_context(s)
        if not self._gameplay_skill_dispatch_session_is_current(s, context, command_id):
            return False
        bit_length = getattr(s, "current_message_bit_length", None)
        if bit_length is None:
            bit_length = len(payload) * 8
        schedule_token = None
        dropped = False
        log_overflow = False
        stall_snapshot = None
        now = time.perf_counter()
        with s.gameplay_skill_dispatch_lock:
            if s.gameplay_skill_dispatch_context != context:
                # Preserve only input for the current map and arena phase.
                # Countdown movement must never execute after the start. An
                # already scheduled drain remains the sole queue owner and
                # will observe this incremented generation.
                previous_context = s.gameplay_skill_dispatch_context
                retained = (
                    [entry for entry in s.gameplay_skill_dispatch_queue
                     if not _spatial_input(entry[2])]
                    if previous_context is not None and previous_context[:3] == context[:3]
                    else ()
                )
                s.gameplay_skill_dispatch_queue.clear()
                s.gameplay_skill_dispatch_context = context
                s.gameplay_skill_dispatch_generation += 1
                # Arena phase changes invalidate old movement/casts, but an
                # acknowledged inventory or social request must still execute.
                s.gameplay_skill_dispatch_queue.extend(
                    (s.gameplay_skill_dispatch_generation, *entry[1:])
                    for entry in retained
                )
            generation = s.gameplay_skill_dispatch_generation
            queued_entry = (
                generation,
                context,
                int(command_id),
                bytes(payload),
                int(bit_length),
                now,
            )
            coalesced_move = bool(
                int(command_id) == _GAMEPLAY_MOVE_COMMAND
                and s.gameplay_skill_dispatch_queue
                and s.gameplay_skill_dispatch_queue[-1][0] == generation
                and s.gameplay_skill_dispatch_queue[-1][1] == context
                and s.gameplay_skill_dispatch_queue[-1][2]
                == _GAMEPLAY_MOVE_COMMAND
            )
            if coalesced_move:
                s.gameplay_skill_dispatch_queue[-1] = queued_entry
            elif len(s.gameplay_skill_dispatch_queue) >= (
                    _GAMEPLAY_SKILL_DISPATCH_QUEUE_LIMIT):
                dropped = True
                s.gameplay_skill_dispatch_overflow_count += 1
                now = time.perf_counter()
                if (
                    now - s.gameplay_skill_dispatch_last_overflow_log_at
                    >= 1.0
                ):
                    s.gameplay_skill_dispatch_last_overflow_log_at = now
                    log_overflow = True
            else:
                s.gameplay_skill_dispatch_queue.append(queued_entry)
            if not dropped:
                if s.gameplay_skill_dispatch_scheduled_token is None:
                    schedule_token = (
                        self._reserve_gameplay_skill_dispatch_locked(s)
                    )
                elif (
                    now - s.gameplay_skill_dispatch_token_reserved_at
                    >= _GAMEPLAY_SKILL_DISPATCH_STALL_SECONDS
                    and now - s.gameplay_skill_dispatch_last_progress_at
                    >= _GAMEPLAY_SKILL_DISPATCH_STALL_SECONDS
                    and now - s.gameplay_skill_dispatch_last_stall_log_at
                    >= _GAMEPLAY_SKILL_DISPATCH_STALL_LOG_INTERVAL_SECONDS
                ):
                    s.gameplay_skill_dispatch_last_stall_log_at = now
                    stall_snapshot = (
                        len(s.gameplay_skill_dispatch_queue),
                        now - s.gameplay_skill_dispatch_token_reserved_at,
                        now - s.gameplay_skill_dispatch_last_progress_at,
                        s.gameplay_skill_dispatch_current_command,
                        s.gameplay_skill_dispatch_worker_ident,
                    )
        if log_overflow:
            print(
                f"[{GAMEPLAY_SERVICE_NAME}] WARNING: gameplay ingress "
                f"capacity {_GAMEPLAY_SKILL_DISPATCH_QUEUE_LIMIT} reached "
                f"for {getattr(s, 'addr', '<unknown>')}"
            )
        if dropped:
            retire = getattr(self, "request_session_retirement", None)
            if retire is not None:
                retire(s, "gameplay input capacity exceeded")
        if schedule_token is not None:
            self._schedule_gameplay_skill_dispatch(s, schedule_token)
        if stall_snapshot is not None:
            depth, token_age, progress_age, active_command, worker_ident = (
                stall_snapshot
            )
            print(
                f"[{GAMEPLAY_SERVICE_NAME}] WARNING: gameplay ingress stalled "
                f"for {getattr(s, 'addr', '<unknown>')}; depth={depth}, "
                f"token_age={token_age:.3f}s, progress_age={progress_age:.3f}s, "
                f"active_command={active_command!r}, worker={worker_ident!r}"
            )
            frame = (
                sys._current_frames().get(worker_ident)
                if worker_ident is not None else None
            )
            if frame is not None:
                print(
                    f"[{GAMEPLAY_SERVICE_NAME}] stalled ingress worker stack:\n"
                    + "".join(traceback.format_stack(frame))
                )
        return not dropped

    def _dispatch_gameplay_skill_command_now(
            self, s: Session, command_id: int, payload: bytes) -> None:
        """Execute one queued command through its authoritative handler."""

        self._dispatch_char_op_now(s, payload)

    def _finish_gameplay_skill_dispatch_slice(
            self, s: Session, token: int) -> None:
        """Release or fairly reschedule the session's sole drain callback."""

        next_token = None
        with s.gameplay_skill_dispatch_lock:
            if s.gameplay_skill_dispatch_scheduled_token != token:
                return
            while s.gameplay_skill_dispatch_queue:
                next_context = s.gameplay_skill_dispatch_queue[0][1]
                if self._gameplay_skill_dispatch_session_is_current(
                        s, next_context, s.gameplay_skill_dispatch_queue[0][2]):
                    next_token = (
                        self._reserve_gameplay_skill_dispatch_locked(s)
                    )
                    break
                else:
                    s.gameplay_skill_dispatch_queue.popleft()
            else:
                s.gameplay_skill_dispatch_scheduled_token = None
        if next_token is not None:
            self._schedule_gameplay_skill_dispatch(s, next_token)

    def _drain_gameplay_skill_dispatch(
            self, s: Session, token: int) -> None:
        """Execute an ordered, bounded slice and isolate command failures."""

        processed = 0
        try:
            while processed < _GAMEPLAY_SKILL_DISPATCH_BATCH_LIMIT:
                with s.gameplay_skill_dispatch_lock:
                    if s.gameplay_skill_dispatch_scheduled_token != token:
                        return
                    if not s.gameplay_skill_dispatch_queue:
                        break
                    (
                        queued_generation,
                        context,
                        command_id,
                        payload,
                        bit_length,
                        queued_at,
                    ) = s.gameplay_skill_dispatch_queue.popleft()
                    generation = s.gameplay_skill_dispatch_generation
                if (
                    queued_generation != generation
                    or not self._gameplay_skill_dispatch_session_is_current(
                        s, context, command_id
                    )
                ):
                    processed += 1
                    continue
                previous_bit_length = s.current_message_bit_length
                s.current_message_bit_length = bit_length
                s.current_message_received_at = queued_at
                started_at = time.perf_counter()
                with s.gameplay_skill_dispatch_lock:
                    s.gameplay_skill_dispatch_running_since = started_at
                    s.gameplay_skill_dispatch_worker_ident = (
                        threading.get_ident()
                    )
                    s.gameplay_skill_dispatch_current_command = command_id
                try:
                    # Trades reserve both participants' mutation gates. This
                    # gate is distinct from flow_lock: handlers may take short
                    # state snapshots and wait for persistence without holding
                    # a guarded state lock across either operation.
                    with s.gameplay_mutation_lock:
                        if self._gameplay_skill_dispatch_session_is_current(s, context, command_id):
                            self._dispatch_gameplay_skill_command_now(
                                s, command_id, payload
                            )
                except Exception as exc:
                    print(
                        f"[{GAMEPLAY_SERVICE_NAME}] ERROR: queued gameplay "
                        f"input 0x{command_id:04x} failed for "
                        f"{getattr(s, 'addr', '<unknown>')}: "
                        f"{type(exc).__name__}: {exc}"
                    )
                    traceback.print_exc()
                finally:
                    s.current_message_bit_length = previous_bit_length
                    s.current_message_received_at = None
                    finished_at = time.perf_counter()
                    with s.gameplay_skill_dispatch_lock:
                        s.gameplay_skill_dispatch_last_progress_at = finished_at
                        s.gameplay_skill_dispatch_running_since = 0.0
                        s.gameplay_skill_dispatch_worker_ident = None
                        s.gameplay_skill_dispatch_current_command = None
                    runtime = finished_at - started_at
                    queue_age = started_at - queued_at
                    log_slow_command = False
                    if runtime >= _GAMEPLAY_SKILL_DISPATCH_SLOW_COMMAND_SECONDS:
                        with s.gameplay_skill_dispatch_lock:
                            if (
                                finished_at
                                - s.gameplay_skill_dispatch_last_slow_log_at
                                >= 1.0
                            ):
                                s.gameplay_skill_dispatch_last_slow_log_at = (
                                    finished_at
                                )
                                log_slow_command = True
                    if log_slow_command:
                        print(
                            f"[{GAMEPLAY_SERVICE_NAME}] WARNING: gameplay "
                            f"input 0x{command_id:04x} took {runtime:.3f}s "
                            f"after {queue_age:.3f}s queued for "
                            f"{getattr(s, 'addr', '<unknown>')}"
                        )
                processed += 1
        finally:
            self._finish_gameplay_skill_dispatch_slice(s, token)

    @staticmethod
    def _handle_simple_gameplay_command(
            s: Session, payload: bytes, command_id: int) -> None:
        decoded = decode_simple_gameplay_command(payload)
        with s.flow_lock:
            s.gameplay_client_command_state[command_id] = decoded
        CharacterService._accept_gameplay_state_command(s, command_id)
        print(
            f"[{GAMEPLAY_SERVICE_NAME}] decoded and applied client "
            f"command 0x{command_id:04x}: {decoded!r}"
        )

    def _schedule_local_player_initialization(self, s: Session) -> bool:
        """Run gameplay-ready initialization off the UDP receive thread.

        ``_send_local_player_initialization`` costs 100-600 ms with a warm
        encounter cache and more than a second when the destination's monster
        population still has to be generated (PERFORMANCE.md section 13).
        The receive thread is the only reader of the service socket: while
        it ran this inline, no datagram from *any* client was read or
        acknowledged, so every player's ping rose by the full amount each
        time anyone entered a map.  The delayed 0x8d fallback already runs
        the same function from a timer thread, so the work itself was never
        bound to the receive thread; only the explicit 0x006f path was.

        Returns whether the work was handed to a worker.  A bare service with
        no scheduler (protocol-level tests) still runs it inline.
        """

        scheduler = getattr(self, "gameplay_initialization_scheduler", None)
        if scheduler is None:
            self._send_local_player_initialization(s)
            return False
        queued_at = time.perf_counter()

        def run() -> None:
            waited = time.perf_counter() - queued_at
            if waited >= 0.5:
                print(
                    f"[{GAMEPLAY_SERVICE_NAME}] initialization for {s.addr} "
                    f"waited {waited:.3f}s behind earlier map entries"
                )
            is_current = getattr(self, "is_session_current", None)
            if not s.connected or (
                is_current is not None and not is_current(s)
            ):
                return
            self._send_local_player_initialization(s)

        handle = scheduler.timer(0.0, run)
        handle.start()
        return True

    def _on_char_op(self, s: Session, payload: bytes):
        command_id = (
            int.from_bytes(payload[1:3], 'little') if len(payload) >= 3 else None
        )
        hot_input = _spatial_input(command_id)
        if (
            s.gameplay_mode and command_id is not None and command_id != 0x006F
            and (hot_input or getattr(self, "gameplay_command_offload", False))
        ):
            self._enqueue_gameplay_skill_command(s, command_id, payload)
            return
        self._dispatch_char_op_now(s, payload)

    def _dispatch_char_op_now(self, s: Session, payload: bytes):
        command_id = (
            int.from_bytes(payload[1:3], 'little') if len(payload) >= 3 else None
        )
        if command_id is None:
            return
        if VERBOSE_PACKET_LOGGING or not s.gameplay_mode:
            print(
                f"[DrasaCharacterService] 0x8b raw={payload.hex()} "
                f"length={len(payload)} command="
                f"{f'0x{command_id:04x}' if command_id is not None else 'short'}"
            )
        if NPC_TRACE and command_id in (
            0x0029, 0x0066, TALK_COMMAND, DELIVER_COMMAND, PAY_COMMAND, DEFEND_COMMAND,
            QUEST_COMMAND,
        ):
            print(
                f"[NPC-TRACE] received cmd=0x{command_id:04x} "
                f"map={s.gameplay_map!r} gameplay_mode={s.gameplay_mode} "
                f"initialized={s.gameplay_initialization_sent} "
                f"presence_ready={s.gameplay_presence_ready}"
            )
        if s.gameplay_mode:
            if s.gameplay_presence_ready and CharacterService._refresh_premium_bags(s):
                CharacterService._request_account_save(s, "premium bags reconciled", flush=True)
            if command_id == 0x006F:
                # UpdateLockedExits() requires localPlayerActor to be valid.
                # In the captured gameplay flow, this client command arrives
                # after map/UI readiness and is the safe point to create it.
                print(
                    f"[{GAMEPLAY_SERVICE_NAME}] accepted gameplay-ready "
                    "cmd 0x006f; sending local-player initialization"
                )
                self._handle_simple_gameplay_command(s, payload, command_id)
                self._schedule_local_player_initialization(s)
            elif command_id == 0x0025:
                self._accept_gameplay_state_command(s, command_id)
            elif command_id == UPDATE_SHOW_PLAYER_OVERHEAD_ICONS_COMMAND:
                self._handle_update_show_player_overhead_icons(s, payload)
            elif command_id == _GAMEPLAY_MOVE_COMMAND:
                self._handle_gameplay_move(s, payload)
            elif command_id in _GAMEPLAY_BULLET_SKILL_COMMANDS:
                self._handle_bullet_skill(s, payload)
            elif command_id in _GAMEPLAY_SPATIAL_SKILL_COMMANDS:
                self._handle_spatial_skill(s, payload)
            elif command_id == 0x0055:
                self._handle_inventory_item_action(s, payload)
            elif command_id == APPLY_ITEM_CUSTOMIZATION_COMMAND:
                self._handle_item_customization(s, payload)
            elif command_id == PICKUP_ITEM_COMMAND:
                self._handle_pickup_item(s, payload)
            elif command_id == 0x0060:
                self._handle_map_exit(s, payload)
            elif command_id == 0x0061:
                self._handle_map_travel(s, payload)
            elif command_id == 0x0062:
                self._handle_respawn(s, payload)
            elif command_id == USE_TRAVEL_ITEM_COMMAND:
                self._handle_use_travel_item(s, payload)
            elif command_id == ACTIVE_EVENTS_COMMAND:
                self._handle_active_events_request(s, payload)
            elif command_id == SET_EVENT_PARTICIPATION_COMMAND:
                self._handle_set_event_participation(s, payload)
            elif command_id == 0x0063:
                self._handle_logout(s, payload)
            elif command_id == 0x001C:
                self._handle_actor_request(s, payload)
            elif command_id == 0x012B:
                self._handle_gauntlet_notification(s, payload)
            elif command_id == 0x00B4:
                self._handle_player_query(s, payload)
            elif command_id in {
                CREATE_GUILD_COMMAND,
                GUILD_LIST_REQUEST_COMMAND,
                GUILD_LEAVE_COMMAND,
            }:
                self._handle_guild_command(s, payload, command_id)
            elif command_id in {
                CREATE_GROUP_COMMAND,
                GROUP_STATUS_REQUEST_COMMAND,
                GROUP_LEAVE_COMMAND,
            }:
                self._handle_group_command(s, payload, command_id)
            elif command_id in TRADE_CLIENT_COMMANDS:
                # DSOR player trade (services/trade.py).
                self._handle_trade_command(s, payload)
            elif command_id in {
                GROUP_INVITE_COMMAND,
                GUILD_INVITE_COMMAND,
            }:
                self._handle_social_invite(s, payload, command_id)
            elif command_id in {
                GROUP_INVITATION_RESPONSE_COMMAND,
                GUILD_INVITATION_RESPONSE_COMMAND,
            }:
                self._handle_invitation_response(
                    s, payload, command_id
                )
            elif command_id == GROUP_FINDER_QUERY_COMMAND:
                self._handle_group_finder(s, payload)
            elif command_id == GROUP_MEMBER_QUERY_COMMAND:
                self._handle_group_member_query(s, payload)
            elif command_id == BUDDY_LIST_COMMAND:
                self._handle_buddy_list(s, payload)
            elif command_id == 0x0059:
                self._handle_request_offer(s, payload)
            elif command_id == 0x010B:
                self._handle_ocp_offer_request(s, payload)
            elif command_id == OCP_BOOKING_COMMAND:
                self._handle_ocp_booking(s, payload)
            elif command_id == ACCEPTED_TIER_SPENDER_COMMAND:
                self._handle_tier_spender_accept(s, payload)
            elif command_id == SHUFFLE_TIER_SPENDER_DEALS_COMMAND:
                self._handle_tier_spender_shuffle(s, payload)
            elif command_id == CLIENT_NOTIFICATION_COMMAND:
                self._handle_notification_action(s, payload)
            elif command_id == ACTIVITY_SIGNIN_PROTOCOL_COMMAND:
                self._handle_activity_signin(s, payload)
            elif command_id == TRANSACTION_COMMAND:
                self._handle_transaction(s, payload)
            elif command_id == SALVAGE_COMMAND:
                self._handle_salvage(s, payload)
            elif command_id == SALVAGE_COOK_ITEM_COMMAND:
                self._handle_food_recycling(s, payload)
            elif command_id == CRAFTING_COMMAND:
                self._handle_crafting(s, payload)
            elif command_id == CRAFTING_RESULT_ACCEPTANCE_COMMAND:
                # Retail sends this after consuming a CraftingResult. It is an
                # acknowledgement only; enchantment transfer sends its full
                # inventory refresh in the same server batch as CraftingResult.
                with s.flow_lock:
                    s.gameplay_client_command_state[
                        CRAFTING_RESULT_ACCEPTANCE_COMMAND
                    ] = bytes(payload)
                CharacterService._accept_gameplay_state_command(
                    s, CRAFTING_RESULT_ACCEPTANCE_COMMAND
                )
            elif command_id == 0x0029:
                self._handle_npc_request(s, payload)
            elif command_id == 0x0066:
                self._handle_npc_encounter(s, payload)
            elif command_id == 0x0038:
                self._handle_prop_request(s, payload)
            elif command_id == 0x0135:
                self._handle_use_stickerbook_item(s, payload)
            elif command_id == 0x0051:
                self._handle_quick_slots(s, payload)
            elif command_id == ADD_STICKER_BOOK_FROM_INVENTORY_COMMAND:
                self._handle_add_stickerbook_from_inventory(s, payload)
            elif command_id == SET_STICKER_BOOK_FAVORITE_COMMAND:
                self._handle_stickerbook_favorite(s, payload)
            elif command_id == UPGRADE_STICKER_BOOK_ITEM_COMMAND:
                self._handle_upgrade_stickerbook_item(s, payload)
            elif command_id == SELECT_TALENT_COMMAND:
                self._handle_select_talent(s, payload)
            elif command_id == SELECT_TALENT_PRESELECTION_COMMAND:
                self._handle_select_talent_preselection(s, payload)
            elif command_id == DESELECT_TALENT_COMMAND:
                self._handle_deselect_talent(s, payload)
            elif command_id == UNLOCK_SKILL_TALENT_PRESELECTION_COMMAND:
                self._handle_unlock_skill_talent_preselection(s, payload)
            elif command_id == RESET_TALENT_PRESELECTION_COMMAND:
                self._handle_reset_talent_preselection(s, payload)
            elif command_id == RESET_TALENT_CATEGORY_COMMAND:
                self._handle_reset_talent_category(s, payload)
            elif command_id == PVP_FLAG_COMMAND:
                self._handle_pvp_flag(s, payload)
            elif command_id == JOIN_MATCH_QUEUE_COMMAND:
                self._handle_join_match_queue(s, payload)
            elif command_id == ABORT_MATCH_QUEUE_COMMAND:
                self._handle_abort_match_queue(s, payload)
            elif command_id == JOIN_BATTLEGROUND_RESPONSE_COMMAND:
                self._handle_join_battleground_response(s, payload)
            elif command_id == LEAVE_PVP_COMMAND:
                self._handle_leave_pvp(s, payload)
            elif command_id in SEASON_PASS_COMMANDS:
                self._handle_season_pass_command(s, payload)
            elif command_id in {
                GET_WANTED_THIEF_INFO_COMMAND, ACCEPT_WANTED_THIEF_MISSION_COMMAND,
                REFRESH_WANTED_THIEF_MISSION_COMMAND,
            }:
                self._handle_wanted_thief_command(s, payload)
            elif command_id == DAILY_CHALLENGE_SELECTION_COMMAND:
                self._handle_daily_challenge_selection(s, payload)
            elif command_id in RANKING_REQUEST_COMMANDS:
                self._handle_ranking_request(s, payload)
            elif command_id == SET_TITLE_COMMAND:
                self._handle_set_title(s, payload)
            elif command_id == PROP_INTERACTION_COMMAND:
                self._handle_prop_interaction(s, payload)
            elif command_id == QUEST_TRIGGER_SIGNAL_COMMAND:
                self._handle_quest_trigger_signal(s, payload)
            elif command_id == 0x0113:
                self._handle_staging_signal(s, payload)
            elif command_id == QUEST_COMMAND:
                self._handle_quest_command(s, payload)
            elif command_id == DELIVER_COMMAND:
                self._handle_deliver_command(s, payload)
            elif command_id == PAY_COMMAND:
                self._handle_pay_command(s, payload)
            elif command_id == TALK_COMMAND:
                self._handle_talk_command(s, payload)
            elif command_id == DEFEND_COMMAND:
                self._handle_defend_command(s, payload)
            elif command_id == QUEST_MONSTER_INDICATION_REQUEST_COMMAND:
                self._handle_quest_monster_indication_request(s, payload)
            elif command_id == QUEST_TRIGGER_INDICATION_REQUEST_COMMAND:
                self._handle_quest_trigger_indication_request(s, payload)
            elif command_id == 0x0170:
                self._accept_gameplay_state_command(s, command_id)
                self._send_fractured_planes_effect_choices(s)
            elif command_id == 0x0174:
                self._handle_fractured_planes_first_clear_acceptance(
                    s, payload
                )
            elif command_id in SOCIAL_MANAGEMENT_COMMANDS:
                self._handle_social_management(s, payload, command_id)
            elif command_id in SEASONAL_ACTIVITY_COMMANDS:
                self._handle_seasonal_activity(s, payload, command_id)
            elif command_id in PVP_OBJECTIVE_COMMANDS:
                self._handle_pvp_objective_command(s, payload, command_id)
            elif command_id in {
                0x00BB, 0x00E2, 0x00E8,
                0x00EA, CLIENT_CURRENT_POSITION_FRAME_INFO_COMMAND,
            }:
                self._handle_simple_gameplay_command(s, payload, command_id)
            else:
                print(
                    f"[{GAMEPLAY_SERVICE_NAME}] ERROR: unimplemented gameplay "
                    f"command 0x{command_id:04x}; retained raw payload="
                    f"{payload.hex()}"
                )
            return
        if command_id == 0x0086:
            self._handle_character_generation(s, payload)
        elif command_id == 0x006F:
            print("[DrasaCharacterService] accepted character-screen completion request")
            self._send_post_selection_config(s)
        elif command_id == 0x010B:
            with s.flow_lock:
                generation_active = s.character_generation_active
                s.post_selection_client_ready = True
                ready = s.post_selection_character_request
            if generation_active:
                print(
                    "[DrasaCharacterService] accepted character-generation "
                    "cmd 0x010b readiness"
                )
                self._send_character_generation_ocp(s)
            else:
                print(
                    "[DrasaCharacterService] accepted client cmd "
                    "0x010b readiness"
                )
            if ready and not generation_active:
                self._send_post_selection_tail(s)
        elif command_id == 0x0087 and len(payload) >= 9 and payload[3] == 0x03:
            character_id = int.from_bytes(payload[5:9], 'little')
            # Checked before arming, not only before responding: arming is
            # what actually puts a character into the world.
            if not self._require_character_ownership(
                s, character_id, action="play"
            ):
                return
            # Client operation 3 requests entry with the selected character.
            # Server operation 4 is the *denial* path (its deny_reason is
            # always rendered as an error, including value zero).  Operation
            # 5 is the success path and requires a non-zero active ID.
            selected = next(
                (
                    entry for entry in self.character_selection.characters
                    if entry.character_id == character_id
                ),
                None,
            )
            if selected is not None:
                gameplay_map = self._persisted_gameplay_map(
                    selected,
                    selected.map_id or DEFAULT_GAMEPLAY_MAP,
                )
                template_row = self.player_template_rows.get(
                    selected.class_id
                )
                self.arm_gameplay(
                    s.addr[0], gameplay_map, selected, template_row
                )
                if self.gameplay_router is not None:
                    arm_login_handoff = getattr(
                        self.gameplay_router,
                        'arm_gameplay_handoff',
                        self.gameplay_router.arm_gameplay,
                    )
                    arm_login_handoff(
                        s.addr[0], gameplay_map, selected, template_row
                    )

            if self._send_character_operation_response(s, 5, character_id):
                with s.flow_lock:
                    s.selected_character_id = character_id
                # Operation 5 clears the client's stored destination while it
                # enters the black transition state. The captured command
                # 0x0070 supplies the next host and causes
                # CharacterSelectionStateHandler to enter ConnectState.  Its
                # final bool must be false here: true explicitly clears the
                # client's game-map flag and sends it back to
                # CharacterSelectionState after connecting.
                gameplay_addr = (
                    f'{self.advertised_host}:{getattr(self.gameplay_router, "port", 2190)}'
                    if self.gameplay_router is not None
                    else f'{self.advertised_host}:{self.port}'
                )
                redirect = switch_map_payload(
                    gameplay_addr,
                    enter_character_selection=False,
                )
                s.send_reliable_ordered(
                    redirect,
                    track=True,
                    label='switch-map-gameplay',
                )
                print(
                    "[DrasaCharacterService] accepted Play character request; "
                    f"sent SwitchMapCommand to {gameplay_addr}; next connection "
                    f"is armed as {GAMEPLAY_SERVICE_NAME}"
                )
        elif command_id == 0x0087 and len(payload) >= 9 and payload[3] == 0x06:
            # Client operation 6: the selection screen's delete button
            # (Messaging::DeleteCharacter) naming one of the listed characters.
            character_id = int.from_bytes(payload[5:9], 'little')
            self._handle_character_delete(s, character_id)
        elif command_id == 0x0087 and len(payload) >= 9 and payload[3] == 0x08:
            character_id = int.from_bytes(payload[5:9], 'little')
            # An equipment listing is a read the client issues from its own
            # cached selection, so it is refused rather than treated as
            # tampering.
            if not self._require_character_ownership(
                s, character_id, action="an equipment listing", drop=False
            ):
                return
            if self._send_character_equipment(s, character_id):
                with s.flow_lock:
                    s.selected_character_id = character_id
        elif command_id == 0x0087 and len(payload) >= 4 and payload[3] == 0x02:
            with s.flow_lock:
                s.post_selection_character_request = True
                ready = s.post_selection_client_ready
                already_sent = s.post_selection_tail_sent
                leaving_editor = s.character_generation_active
                s.character_generation_active = False
            print("[DrasaCharacterService] accepted character cmd 0x0087 operation 2")
            if already_sent:
                # The client asks again whenever it (re)enters the selection
                # screen, in particular through the creation editor's back
                # button.  It rebuilds the screen from the same tail; without
                # it the player is left on a black screen.
                print(
                    "[DrasaCharacterService] re-sending the selection tail "
                    + ("after the creation editor" if leaving_editor else "on request")
                )
                self._send_post_selection_tail(s, again=True)
            elif ready:
                self._send_post_selection_tail(s)
        elif command_id == LOGOUT_COMMAND:
            try:
                decode_logout(payload)
            except ValueError as exc:
                print(f"[DrasaCharacterService] rejected malformed LogoutCommand: {exc}")
                return
            # 0x0063 is LogoutCommand, including the captured 8b630040.
            # Treating it as Play Now armed a new route when the old
            # selection connection closed. A quick relog could then skip the
            # selection/Play transition and create InventoryProperty before
            # IngameWidget existed. Only CharacterSelection operation 3 (or
            # character creation) may start gameplay from this screen.
            # Leave any legitimate handoff already in flight untouched.
            s.send_reliable_ordered(b'\x15')
            print(
                "[DrasaCharacterService] accepted character-screen logout"
            )
        else:
            print("[DrasaCharacterService] ignored unrelated 0x8b command")
