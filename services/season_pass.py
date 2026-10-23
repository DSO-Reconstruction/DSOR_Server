"""Season Pass query handling for the commands used by the retail client."""

from __future__ import annotations

from datetime import datetime, timezone

from dso_server.gameplay.season_pass import (
    load_season_pass_catalog,
    normalize_season_pass_state,
)
from dso_server.protocol.player_init_protocol import (
    CURRENCY_CHANGED_COMMAND,
    INVENTORY_INFO_COMMAND,
    SEASON_PASS_MISSIONS_INFO_RESULT_COMMAND,
    SEASON_PASS_REWARDS_INFO_RESULT_COMMAND,
    SeasonPassMissionsInfoResult,
    SeasonPassRewardsInfoResult,
    ServerCommand,
    encode_server_command_batch,
)
from dso_server.protocol.season_pass_protocol import (
    ADD_SEASON_PASS_EXP_COMMAND,
    GET_SEASON_PASS_AWARD_COMMAND,
    GET_SEASON_PASS_MISSIONS_INFO_COMMAND,
    GET_SEASON_PASS_REWARDS_INFO_COMMAND,
    decode_season_pass_query,
)
from dso_server.services.base import CharacterService
from dataclasses import replace
from dso_server.runtime.context import LOCAL_PLAYER_ACTOR_ID
from dso_server.protocol.currency_protocol import GAME_CURRENCY_CHANGED_COMMAND
from dso_server.protocol.inventory_economy_protocol import (
    InventoryMutationBuilder,
    credit_currency_template,
)
from dso_server.protocol.commerce_protocol import load_purchase_item_definition


SEASON_PASS_AWARD_ID_STRIDE = 100
SEASON_PASS_DELIVERY_LEDGER_VERSION = 3


class SeasonPassService:
    def _season_pass_catalog(self):
        active_templates = CharacterService._event_catalog(
            self,
        ).active_template_ids()
        return load_season_pass_catalog(
            self.static_database_path,
            alternate_rewards=(
                "event_season_ticket_2026_temp" in active_templates
            ),
        )

    @staticmethod
    def _restore_season_pass_state(s, persisted_state) -> None:
        s.gameplay_season_pass = normalize_season_pass_state(
            getattr(persisted_state, "season_pass", None) or {}
        )

    @staticmethod
    def _season_pass_document(s) -> dict[str, object]:
        return normalize_season_pass_state(
            getattr(s, "gameplay_season_pass", None) or {}
        )

    def _handle_season_pass_awards(
            self, s, request, state, catalog, *, _retry_count: int = 0) -> None:
        # The dispatcher currently exposes the session's live state object.
        # Work on a detached copy so a failed optimistic commit cannot mark
        # awards delivered before inventory/currency changes are committed.
        state = normalize_season_pass_state(state)
        original_state = normalize_season_pass_state(state)
        max_level = max(catalog.reward_levels)
        pass_types = set(state["pass_types"])
        claims = set(state["claimed_awards"])
        # Older ledgers were populated from claim markers even when no reward
        # was granted. Treat those entries as undelivered and reconcile them
        # once using the current delivery-ledger version.
        awarded = (set(state["awarded_awards"])
                   if int(state["delivery_ledger_version"])
                   >= SEASON_PASS_DELIVERY_LEDGER_VERSION else set())
        client_requested_ids = set(request.award_ids)
        if len(client_requested_ids) == 1:
            # A right-click sends a single award ID. Keep that action scoped
            # to the selected tile; reconciliation of older claims belongs
            # to collect-all requests, which carry multiple IDs.
            requested_ids = tuple(sorted(client_requested_ids))
        else:
            requested_ids = tuple(sorted(
                client_requested_ids | (claims - awarded)
            ))
        inventory = getattr(s, "gameplay_inventory", None)
        original_inventory = inventory
        currency = s.gameplay_currency_state
        original_currency = currency
        game_currencies = dict(s.gameplay_game_currencies)
        original_game_currencies = dict(game_currencies)
        original_next_item_id = int(s.gameplay_next_item_id)
        if inventory is None:
            print("[DrasaOnlineMapServer] Season Pass claim rejected: inventory unavailable")
            return
        builder = InventoryMutationBuilder(inventory, original_next_item_id)
        changed_currency = False
        changed_game_currencies = {}
        accepted_ids = []
        skipped = {"already_delivered": 0, "locked": 0, "pass_required": 0, "unknown_lane": 0}
        granted_tracks = set()
        for claimed_id in awarded:
            claimed_lane = (claimed_id - 1) // SEASON_PASS_AWARD_ID_STRIDE
            claimed_level = (claimed_id - 1) % SEASON_PASS_AWARD_ID_STRIDE + 1
            if claimed_lane in (0, 1, 2, 3, 4):
                granted_tracks.add((
                    claimed_level, {3: 1, 4: 2}.get(claimed_lane, claimed_lane),
                ))
        try:
            for award_id in requested_ids:
                if award_id in awarded:
                    skipped["already_delivered"] += 1
                    continue
                lane = (award_id - 1) // SEASON_PASS_AWARD_ID_STRIDE
                level = (award_id - 1) % SEASON_PASS_AWARD_ID_STRIDE + 1
                if lane not in (0, 1, 2, 3, 4):
                    skipped["unknown_lane"] += 1
                    continue
                # Collect-all includes reward IDs from locked levels on some
                # client builds. Ignore those entries while keeping eligible
                # rewards in the same request claimable.
                if level > int(state["level"]) or level > max_level:
                    skipped["locked"] += 1
                    continue
                if lane in (1, 3) and not pass_types.intersection({1, 3}):
                    skipped["pass_required"] += 1
                    continue
                if lane in (2, 4) and not pass_types.intersection({2, 4}):
                    skipped["pass_required"] += 1
                    continue
                # The client emits separate IDs for each ticket entitlement:
                # lanes 1/3 are the gold track and lanes 2/4 the red track.
                # Static rewards define one reward per visible paid track.
                reward_lane = {3: 1, 4: 2}.get(lane, lane)
                track_key = (level, reward_lane)
                row = catalog.reward_levels.get(level)
                if track_key not in granted_tracks:
                    reward = row[reward_lane] if row is not None else ""
                    if not reward.startswith("Loot:"):
                        raise ValueError(f"reward {award_id} has no static loot entry")
                    item_id, raw_amount = reward[5:].rsplit(",", 1)
                    amount = int(raw_amount)
                    if not item_id or amount <= 0:
                        raise ValueError(f"reward {award_id} has invalid static loot")
                    # Pass types 3 and 4 are the double gold/red tickets.
                    # They share a visible reward track with types 1 and 2,
                    # so the extra award IDs must double the amount instead
                    # of being collapsed into a second copy of the same item.
                    double_pass_active = (
                        (reward_lane == 1 and 3 in pass_types)
                        or (reward_lane == 2 and 4 in pass_types)
                    )
                    if double_pass_active:
                        amount *= 2
                    credit = credit_currency_template(
                        self.static_database_path, currency, game_currencies,
                        item_id, amount, scale_by_item_value=False,
                    )
                    if credit is not None:
                        currency = credit.currency
                        game_currencies = dict(credit.game_currencies)
                        changed_currency = changed_currency or (
                            currency != s.gameplay_currency_state
                        )
                        if credit.game_currency_update is not None:
                            changed_game_currencies[
                                credit.game_currency_update.resource_id
                            ] = credit.game_currency_update
                    else:
                        definition = load_purchase_item_definition(
                            self.static_database_path, item_id,
                        )
                        character = s.gameplay_character
                        builder.add_item(
                            definition, amount,
                            item_level=max(1, int(getattr(character, "level", 1))),
                        )
                    granted_tracks.add(track_key)
                claims.add(award_id)
                awarded.add(award_id)
                accepted_ids.append(award_id)
        except (ValueError, KeyError) as exc:
            print(f"[DrasaOnlineMapServer] Season Pass claim rejected: {exc}")
            return

        mutation = builder.finish()
        state["claimed_awards"] = tuple(sorted(claims))
        state["awarded_awards"] = tuple(sorted(awarded))
        state["delivery_ledger_version"] = SEASON_PASS_DELIVERY_LEDGER_VERSION
        result = SeasonPassRewardsInfoResult(
            int(state["level"]), int(state["experience"]),
            tuple(state["pass_types"]), tuple(state["claimed_awards"]),
        )
        commands = []
        changed_ingredient_bag_items = tuple(
            int(item.item_id)
            for item in mutation.changed_items
            if int(item.item_id) in set(mutation.inventory.field_180)
        )
        if changed_ingredient_bag_items:
            # Ingredient-bag stacks have no ordinary primary storage slot.
            # The client asserts in LocateItem when these are supplied through
            # the changed-item delta path; resend one authoritative full view,
            # the same shape used during player initialization.
            commands.append(ServerCommand(
                INVENTORY_INFO_COMMAND, mutation.inventory,
                LOCAL_PLAYER_ACTOR_ID,
            ))
        elif mutation.changed_items:
            commands.append(ServerCommand(
                INVENTORY_INFO_COMMAND, mutation.delta, LOCAL_PLAYER_ACTOR_ID,
            ))
        if changed_currency:
            commands.append(ServerCommand(
                CURRENCY_CHANGED_COMMAND, currency, LOCAL_PLAYER_ACTOR_ID,
            ))
        commands.extend(ServerCommand(
            GAME_CURRENCY_CHANGED_COMMAND, update, LOCAL_PLAYER_ACTOR_ID,
        ) for update in changed_game_currencies.values())
        if mutation.changed_items and not changed_ingredient_bag_items:
            commands.append(ServerCommand(
                INVENTORY_INFO_COMMAND, mutation.final, LOCAL_PLAYER_ACTOR_ID,
            ))
        commands.append(ServerCommand(
            SEASON_PASS_REWARDS_INFO_RESULT_COMMAND, result, 0,
        ))
        packet, packet_bits = encode_server_command_batch(
            commands, include_terminator=True,
        )
        retry_state = None
        with s.flow_lock:
            current_state = normalize_season_pass_state(
                getattr(s, "gameplay_season_pass", None) or {}
            )
            if (current_state != original_state
                    or s.gameplay_inventory is not original_inventory
                    or s.gameplay_currency_state != original_currency
                    or dict(s.gameplay_game_currencies) != original_game_currencies
                    or int(s.gameplay_next_item_id) != original_next_item_id):
                if _retry_count < 3 and s.connected and s.gameplay_character is not None:
                    retry_state = current_state
                else:
                    print("[DrasaOnlineMapServer] Season Pass claim stopped after concurrent character change")
            else:
                s.gameplay_season_pass = state
                s.gameplay_inventory = mutation.inventory
                s.gameplay_next_item_id = mutation.next_item_id
                s.gameplay_currency_state = currency
                s.gameplay_game_currencies = game_currencies
        if retry_state is not None:
            print(
                "[DrasaOnlineMapServer] retrying Season Pass claim after "
                f"concurrent character change ({_retry_count + 1}/3)"
            )
            self._handle_season_pass_awards(
                s, request, retry_state, catalog,
                _retry_count=_retry_count + 1,
            )
            return
        with s.flow_lock:
            if s.gameplay_season_pass is not state:
                return
        s.send_reliable_ordered_split(
            packet, total_bit_length=packet_bits, track=True,
            label_prefix="season-pass-awards",
        )
        CharacterService._request_account_save(
            s, "Season Pass rewards claimed", flush=True,
        )
        print(
            f"[DrasaOnlineMapServer] Season Pass rewards claimed "
            f"for {s.addr}: requested={request.award_ids}, "
            f"granted={accepted_ids}, skipped={skipped}, "
            f"inventory_sync={'full' if changed_ingredient_bag_items else 'delta'}, "
            f"ingredient_bag_items={changed_ingredient_bag_items}, "
            f"level={state['level']} pass_types={state['pass_types']} "
            f"claimed={original_state['claimed_awards']} "
            f"delivered={original_state['awarded_awards']}"
        )

    def _season_pass_daily_login(self, s) -> None:
        if not s.connected or s.gameplay_character is None:
            return
        today = datetime.now(timezone.utc).date().isoformat()
        with s.flow_lock:
            state = normalize_season_pass_state(
                getattr(s, "gameplay_season_pass", None) or {}
            )
            if state["login_day"] == today:
                return
            state["login_day"] = today
            s.gameplay_season_pass = state
        self._season_pass_feed(s, "DailyLogin", notify=False)

    def _season_pass_feed(
            self, s, kind: str, *, value: str = "", count: int = 1,
            notify: bool = True) -> bool:
        if (s.gameplay_character is None or not s.connected
                or getattr(s, "retired", False)
                or s.gameplay_exit_transition_started or int(count) <= 0):
            return False
        catalog = self._season_pass_catalog()
        difficulty = max(
            int(getattr(s, "gameplay_map_difficulty", 0)),
            int(getattr(s, "gameplay_scaling_difficulty", 0)),
        )
        changed = False
        completed = False
        with s.flow_lock:
            state = normalize_season_pass_state(
                getattr(s, "gameplay_season_pass", None) or {}
            )
            progress = dict(state["mission_progress"])
            experience = int(state["experience"])
            for mission in catalog.missions.values():
                if mission.objective.casefold() != str(kind).casefold():
                    continue
                if mission.special and mission.special.casefold() != str(value).casefold():
                    continue
                if mission.difficulty.casefold() != "any":
                    try:
                        if difficulty < int(mission.difficulty):
                            continue
                    except ValueError:
                        if str(value).casefold() != mission.difficulty.casefold():
                            continue
                old_progress = min(
                    mission.target,
                    max(0, int(progress.get(mission.mission_id, 0))),
                )
                if old_progress >= mission.target:
                    continue
                new_progress = min(mission.target, old_progress + int(count))
                progress[mission.mission_id] = new_progress
                changed = True
                if new_progress >= mission.target:
                    experience += mission.experience
                    completed = True
            if not changed:
                return False
            state["mission_progress"] = progress
            state["experience"] = experience
            state["level"] = catalog.level_for_experience(experience)
            s.gameplay_season_pass = state
            mission_result = SeasonPassMissionsInfoResult(tuple(
                (mission_id, int(progress.get(mission_id, 0)))
                for mission_id in catalog.missions
            ))
            rewards_result = SeasonPassRewardsInfoResult(
                int(state["level"]), experience,
                tuple(state["pass_types"]), tuple(state["claimed_awards"]),
            )

        if notify:
            packet, packet_bits = encode_server_command_batch((
                ServerCommand(
                    SEASON_PASS_MISSIONS_INFO_RESULT_COMMAND, mission_result, 0,
                ),
                ServerCommand(
                    SEASON_PASS_REWARDS_INFO_RESULT_COMMAND, rewards_result, 0,
                ),
            ), include_terminator=True)
            s.send_reliable_ordered_split(
                packet,
                total_bit_length=packet_bits,
                track=True,
                label_prefix="season-pass-progress",
            )
        CharacterService._request_account_save(
            s, "Season Pass mission progress", flush=completed,
        )
        print(
            f"[DrasaOnlineMapServer] Season Pass progress {kind!r}: "
            f"{sum(value > 0 for value in progress.values())} active, "
            f"level={state['level']} xp={experience}"
        )
        return True

    def _handle_season_pass_command(self, s, payload: bytes) -> None:
        try:
            request = decode_season_pass_query(
                payload, getattr(s, "current_message_bit_length", None),
            )
        except ValueError as exc:
            print(f"[DrasaOnlineMapServer] rejected Season Pass query: {exc}")
            return

        with s.flow_lock:
            if (s.gameplay_character is None or not s.connected
                    or getattr(s, "retired", False)
                    or s.gameplay_exit_transition_started):
                return
            state = normalize_season_pass_state(
                getattr(s, "gameplay_season_pass", None) or {}
            )
            if request.command_id == ADD_SEASON_PASS_EXP_COMMAND:
                catalog = self._season_pass_catalog()
                current_experience = int(state["experience"])
                requested_experience = int(request.experience)
                maximum_experience = int(catalog.level_thresholds[-1][1])
                cost = requested_experience * 10
                currency = s.gameplay_currency_state
                # The retail UI prices a level purchase at 10 Andermant per
                # missing Season Pass XP. Bound the request by the catalog so
                # a forged packet cannot buy past the final reward level.
                if (requested_experience <= 0
                        or requested_experience > maximum_experience - current_experience
                        or cost > 0xFFFFFFFF
                        or currency is None
                        or int(currency.currency_type) < cost):
                    experience = current_experience
                    updated_currency = currency
                    accepted = False
                else:
                    experience = current_experience + requested_experience
                    updated_currency = replace(
                        currency,
                        currency_type=int(currency.currency_type) - cost,
                        notify=True,
                    )
                    state["experience"] = experience
                    state["level"] = catalog.level_for_experience(experience)
                    s.gameplay_season_pass = state
                    s.gameplay_currency_state = updated_currency
                    accepted = True
                result = SeasonPassRewardsInfoResult(
                    int(state["level"]), experience,
                    tuple(state["pass_types"]), tuple(state["claimed_awards"]),
                )
            else:
                accepted = False
                updated_currency = None
                if request.command_id == GET_SEASON_PASS_AWARD_COMMAND:
                    catalog = self._season_pass_catalog()
            s.gameplay_season_pass = state
            if request.command_id == GET_SEASON_PASS_MISSIONS_INFO_COMMAND:
                catalog = self._season_pass_catalog()
                missions = tuple(
                    (mission_id, int(state["mission_progress"].get(mission_id, 0)))
                    for mission_id in catalog.missions
                )

        CharacterService._accept_gameplay_state_command(s, request.command_id)
        if request.command_id == GET_SEASON_PASS_AWARD_COMMAND:
            self._handle_season_pass_awards(s, request, state, catalog)
            return
        if request.command_id == ADD_SEASON_PASS_EXP_COMMAND:
            commands = []
            if updated_currency is not None:
                from dso_server.protocol.player_init_protocol import CURRENCY_CHANGED_COMMAND
                commands.append(ServerCommand(
                    CURRENCY_CHANGED_COMMAND, updated_currency,
                    LOCAL_PLAYER_ACTOR_ID,
                ))
            commands.append(ServerCommand(
                SEASON_PASS_REWARDS_INFO_RESULT_COMMAND, result, 0,
            ))
            packet, packet_bits = encode_server_command_batch(
                commands, include_terminator=True,
            )
            s.send_reliable_ordered_split(
                packet,
                total_bit_length=packet_bits,
                track=True,
                label_prefix="season-pass-level-purchase",
            )
            if accepted:
                CharacterService._request_account_save(
                    s, "Season Pass Andermant level purchase", flush=True,
                )
            print(
                f"[DrasaOnlineMapServer] Season Pass level purchase "
                f"{'accepted' if accepted else 'rejected'} to {s.addr}: "
                f"level={result.field_20} xp={result.field_24}"
            )
            return
        if request.command_id == GET_SEASON_PASS_REWARDS_INFO_COMMAND:
            result = SeasonPassRewardsInfoResult(
                int(state["level"]), int(state["experience"]),
                tuple(state["pass_types"]), tuple(state["claimed_awards"]),
            )
            response_id = SEASON_PASS_REWARDS_INFO_RESULT_COMMAND
            value = result
        else:
            response_id = SEASON_PASS_MISSIONS_INFO_RESULT_COMMAND
            value = SeasonPassMissionsInfoResult(missions)

        if response_id == 0xFFFF:
            print("[DrasaOnlineMapServer] Season Pass response is unavailable in the selected command table")
            return
        s.send_reliable_ordered(encode_server_command_batch((ServerCommand(
            response_id, value, 0,
        ),), include_terminator=True), track=True, label="season-pass-info")
        print(
            f"[DrasaOnlineMapServer] Season Pass reply 0x{response_id:04x} "
            f"to {s.addr}: level={state['level']} xp={state['experience']}"
        )
