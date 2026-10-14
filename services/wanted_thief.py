"""Wanted mission board requests backed by character state and static content."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

from dso_server.gameplay.wanted_thief import WantedState, load_wanted_catalog
from dso_server.protocol.wanted_thief_protocol import (
    GET_WANTED_THIEF_INFO_COMMAND, ACCEPT_WANTED_THIEF_MISSION_COMMAND,
    REFRESH_WANTED_THIEF_MISSION_COMMAND,
    WantedThiefInfo,
    decode_wanted_thief_request, encode_wanted_thief_info,
)
from dso_server.services.base import CharacterService


WANTED_EVENT = "event_wanted_thief_silent"


class WantedThiefService:
    @staticmethod
    def _restore_wanted_thief_state(s, persisted_state) -> None:
        s.gameplay_wanted_thief = WantedState.from_document(
            getattr(persisted_state, "wanted_thief", None) or {})

    @staticmethod
    def _wanted_thief_document(s) -> dict[str, object]:
        state = getattr(s, "gameplay_wanted_thief", None)
        return state.to_document() if state is not None else {}

    @staticmethod
    def _wanted_now() -> datetime:
        return datetime.now(timezone.utc)

    def _wanted_catalog(self):
        return load_wanted_catalog(self.static_database_path)

    def _wanted_board(self, s, catalog, now: datetime) -> WantedState:
        """Called under flow_lock; returns a detached immutable board."""
        day = now.date().isoformat()
        state = getattr(s, "gameplay_wanted_thief", None)
        if state is None:
            return WantedState(
                day, catalog.base_times, catalog.vip_times,
                catalog.roll((0, 0, 0, 0), (0, 1, 2, 3)), (0, 0, 0, 0),
            )
        if state.day < day:
            # The native timer counts down to midnight UTC. Keep accepted
            # missions through the reset; daily counters belong to the board,
            # while quest progress belongs to the persisted quest log.
            state = replace(
                state, day=day, remaining=catalog.base_times,
                special_remaining=catalog.vip_times,
            )
        return state

    @staticmethod
    def _send_wanted_thief_info(s, state: WantedState) -> None:
        expiry = (
            datetime.fromtimestamp(state.vip_expires_at, timezone.utc)
            if state.vip_expires_at > 0 else None
        )
        s.send_reliable_ordered(encode_wanted_thief_info(WantedThiefInfo(
            state.remaining, state.special_remaining, state.mission_ids,
            state.statuses, expiry,
        )), track=True, label="wanted-thief-info")
        print(
            f"[DrasaOnlineMapServer] Wanted Thief reply 0x0165 to {s.addr}: "
            f"remaining={state.remaining}/{state.special_remaining}, "
            f"missions={state.mission_ids}, statuses={state.statuses}"
        )

    def _handle_wanted_thief_command(self, s, payload: bytes) -> None:
        try:
            request = decode_wanted_thief_request(payload, s.current_message_bit_length)
        except ValueError as exc:
            print(f"[DrasaOnlineMapServer] rejected Wanted Thief request: {exc}")
            return
        catalog = self._wanted_catalog()
        now = self._wanted_now()
        with s.flow_lock:
            if (s.gameplay_character is None or not s.connected
                    or getattr(s, "retired", False)
                    or s.gameplay_exit_transition_started):
                return
            if request.command_id not in {
                GET_WANTED_THIEF_INFO_COMMAND, ACCEPT_WANTED_THIEF_MISSION_COMMAND,
                REFRESH_WANTED_THIEF_MISSION_COMMAND,
            }:
                return
            before = getattr(s, "gameplay_wanted_thief", None)
            state = self._wanted_board(s, catalog, now)
            s.gameplay_wanted_thief = state
            s.gameplay_client_command_state[request.command_id] = request
        CharacterService._accept_gameplay_state_command(s, request.command_id)
        accepted = False
        if request.command_id == ACCEPT_WANTED_THIEF_MISSION_COMMAND:
            accepted = self._accept_wanted_thief_mission(s, request.slot, state, catalog, now)
        if request.command_id == REFRESH_WANTED_THIEF_MISSION_COMMAND:
            accepted = self._refresh_wanted_thief_missions(s, request.slot, catalog, now)
        if not accepted and state != before:
            CharacterService._request_account_save(s, "Wanted mission board")
        with s.flow_lock:
            state = s.gameplay_wanted_thief
        self._send_wanted_thief_info(s, state)

    def _accept_wanted_thief_mission(self, s, slot, state, catalog, now) -> bool:
        # The 12:05 retail capture verifies only ordinary slots 1..3.
        # Fourth-slot purchase/entitlement rules still need a capture.
        if slot not in (1, 2, 3):
            print(f"[DrasaOnlineMapServer] unsupported Wanted acceptance slot {slot}; no charge")
            return False
        event = next((event for event in self._event_catalog().active_events(now)
                      if event.template.resource_id == WANTED_EVENT), None)
        if event is None:
            return False
        index = slot - 1
        mission = catalog.missions.get(state.mission_ids[index])
        if mission is None:
            return False

        def commit_accept():
            # Called by the quest service with flow_lock held. The counter,
            # slot status, and QuestProgress must enter snapshots together.
            character = s.gameplay_character
            if (character is None or not s.connected or getattr(s, "retired", False)
                    or s.gameplay_exit_transition_started
                    or s.gameplay_wanted_thief != state
                    or int(character.level) < max(10, event.template.minimum_level)
                    or int(character.level) > event.template.maximum_level
                    or str(s.gameplay_map).casefold() in event.template.excluded_maps
                    or state.remaining <= 0 or state.statuses[index] != 0):
                return False
            statuses = list(state.statuses)
            statuses[index] = 1
            s.gameplay_wanted_thief = replace(
                state, remaining=state.remaining - 1, statuses=tuple(statuses),
            )
            return True

        return self._quest_accept(
            s, mission.quest_id, offered=True, commit_accept=commit_accept,
        )

    def _refresh_wanted_thief_missions(self, s, slot, catalog, now):
        # Native refresh uses zero for the whole board; individual slots are
        # one based, like acceptance. Never replace an accepted mission.
        if slot not in range(5):
            return False
        event = next((e for e in self._event_catalog().active_events(now)
                      if e.template.resource_id == WANTED_EVENT), None)
        if event is None:
            return False
        from dso_server.protocol.player_init_protocol import (
            CURRENCY_CHANGED_COMMAND, ServerCommand, encode_server_command_batch)
        from dso_server.runtime.context import LOCAL_PLAYER_ACTOR_ID
        with s.flow_lock:
            state = s.gameplay_wanted_thief
            character = s.gameplay_character
            if (not s.connected or s.gameplay_exit_transition_started or character is None
                    or not max(10, event.template.minimum_level) <= character.level <= event.template.maximum_level
                    or str(s.gameplay_map).casefold() in event.template.excluded_maps):
                return False
            slots = tuple(i for i in (range(4) if slot == 0 else (slot - 1,))
                          if state.statuses[i] == 0)
            if not slots:
                return False
            cost = catalog.all_refresh_cost if slot == 0 else catalog.single_refresh_cost
            currency = s.gameplay_currency_state
            if currency is None or currency.currency_type < cost:
                return False
            from dso_server.gameplay.quest_content import QUEST_STATE_ACCEPTED
            active = {q.casefold() for q, p in getattr(s, 'gameplay_quest_log', {}).items()
                      if p.state == QUEST_STATE_ACCEPTED}
            excluded = frozenset(i for i, m in catalog.missions.items()
                                 if m.quest_id.casefold() in active)
            try:
                missions = catalog.roll(state.mission_ids, slots, excluded=excluded)
            except ValueError:
                return False
            s.gameplay_currency_state = replace(currency, currency_type=currency.currency_type - cost,
                                                notify=True)
            s.gameplay_wanted_thief = replace(state, mission_ids=missions)
            updated = s.gameplay_currency_state
        s.send_reliable_ordered(encode_server_command_batch((ServerCommand(
            CURRENCY_CHANGED_COMMAND, updated, LOCAL_PLAYER_ACTOR_ID),), include_terminator=True),
            track=True, label='wanted-refresh-currency')
        CharacterService._request_account_save(s, 'Wanted mission refresh')
        return True
