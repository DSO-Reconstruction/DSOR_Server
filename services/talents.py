"""Talent presets, allocation, resets, modifiers, and PvP state."""

from __future__ import annotations

import sqlite3

from dso_server.runtime.context import *
from dso_server.runtime.transport import Session
from dso_server.protocol.membership_protocol import membership_is_active
from dso_server.protocol.player_init_protocol import (
    UNLOCK_SKILL_TALENT_PRESELECTION_INFO_COMMAND,
    UnlockSkillTalentPreselectionInfo,
)
from dso_server.protocol.talent_protocol import (
    DESELECT_TALENT_COMMAND,
    SKILL_TALENT_UNLOCKABLE_PRESELECTIONS,
    UNLOCK_SKILL_TALENT_PRESELECTION_COMMAND,
    decode_deselect_talent,
    decode_unlock_skill_talent_preselection,
    talent_preselection_available,
    talent_respec_cost,
)

# Timer key prefix of a running preset cooldown's removal.
TALENT_PRESET_COOLDOWN_TIMER = "talent-preset-cooldown"


class TalentService:
    """Talent presets, allocation, resets, modifiers, and PvP state."""

    @staticmethod
    def _talent_effect_key(category_id: int, talent_id: str) -> str:
        return (
            str(talent_id)
            if int(category_id) == 0
            else f"{int(category_id)}:{talent_id}"
        )

    @staticmethod
    def _talent_effect_key_is_category(
            effect_key: str, category_id: int) -> bool:
        prefix = f"{int(category_id)}:"
        if int(category_id) == 0:
            return ":" not in str(effect_key)
        return str(effect_key).startswith(prefix)

    @staticmethod
    def _talent_category_roots(
            database: TalentDatabase,
            selections: tuple[TalentSelectionEntry, ...] | list[
                TalentSelectionEntry
            ]) -> tuple[TalentSelectionEntry, ...]:
        """Rebuild the aggregate branch counters used by wisdom/group UIs."""

        ranks: dict[str, int] = {}
        for selection in selections:
            definition = database.definition(selection.resource_id)
            if definition.category_id not in (1, 2) or not definition.group_id:
                continue
            ranks.setdefault(definition.group_id, 0)
            ranks[definition.group_id] += int(selection.field_00)
        roots = []
        for group_id, rank in ranks.items():
            maximum = database.category_group_max_rank(group_id)
            if rank > maximum:
                raise ValueError(
                    f"talent group {group_id!r} exceeds its maximum "
                    f"rank {maximum}"
                )
            roots.append(TalentSelectionEntry(rank, 0, group_id))
        return tuple(roots)

    @staticmethod
    def _active_talent_rank(
            s: Session, category_id: int, talent_id: str) -> int:
        """A talent's rank in its tree's active preset (0 if not chosen)."""

        with s.flow_lock:
            categories = tuple(s.gameplay_talent_categories)
        if not 0 <= int(category_id) < len(categories):
            return 0
        return next(
            (
                int(entry.field_00)
                for entry in categories[int(category_id)].selected_skills
                if entry.resource_id == talent_id
            ),
            0,
        )

    @staticmethod
    def _talent_server_tick(s: Session) -> int:
        """The map clock now, for timed talent effects (flow_lock held)."""

        tick = int(s.gameplay_chase_server_tick)
        last_move = s.gameplay_last_move
        if last_move is not None:
            tick = max(tick, int(last_move.start_tick))
        return tick & 0xFFFFFFFF

    def _talent_map_is_hub(self, map_id: str) -> bool:
        """Hubs (ruleset 5) switch presets without a cooldown."""

        database = getattr(self, "static_database", None)
        if database is None or not map_id:
            return False
        try:
            return database.gameplay_map_ruleset(str(map_id)) == 5
        except (OSError, ValueError, sqlite3.Error):
            return False

    @staticmethod
    def _talent_spent_points(database: TalentDatabase, selections) -> int:
        """Points spent on the base ranks of these selections."""

        spent = 0
        for selection in selections:
            definition = database.definition(selection.resource_id)
            spent += sum(
                definition.point_cost_for_rank(rank)
                for rank in range(1, int(selection.field_00) + 1)
            )
        return spent

    def _talent_respec_price(
            self, s: Session, database: TalentDatabase,
            category_id: int, spent_points: int) -> int:
        """Gold (copper) a reset of these points costs; Honor is free."""

        if int(category_id) == 3 or int(spent_points) <= 0:
            return 0
        buff = database.respec_cost_effect_wire_id()
        with s.flow_lock:
            character = s.gameplay_character
            effects = list(s.gameplay_persistent_status_effects.values())
            effects.extend(
                value[0] if isinstance(value, tuple) else value
                for value in s.gameplay_runtime_status_effects.values()
            )
        if character is None:
            raise ValueError("local player is not initialized")
        # TalentRespecCostFactor = 1 + the talent_reset_costs_buff values.
        factor = 1.0 + sum(
            float(effect.values[0])
            for effect in effects
            if buff is not None
            and int(effect.effect_id) == buff
            and effect.has_details
            and effect.values
        )
        return talent_respec_cost(
            spent_points=spent_points,
            cost_per_point=database.respec_cost_per_point(),
            level_multiplier=database.respec_level_multiplier(
                int(character.level)
            ),
            factor=max(0.0, factor),
        )

    @staticmethod
    def _require_talent_gold(s: Session, price: int) -> None:
        if price <= 0:
            return
        with s.flow_lock:
            currency = s.gameplay_currency_state
        gold = int(currency.amount) if currency is not None else 0
        if gold < price:
            raise ValueError(
                f"the reset costs {price} copper; the character has {gold}"
            )

    @staticmethod
    def _charge_talent_gold(s: Session, price: int):
        """Take a reset's gold (flow_lock held); the new wallet or None."""

        currency = s.gameplay_currency_state
        if price <= 0 or currency is None:
            return None
        s.gameplay_currency_state = replace(
            currency,
            amount=max(0, int(currency.amount) - int(price)),
            notify=True,
        )
        return s.gameplay_currency_state

    @staticmethod
    def _send_talent_currency(s: Session, currency, *, label: str) -> None:
        if currency is None:
            return
        response, response_bits = encode_server_command_batch(
            (
                ServerCommand(
                    CURRENCY_CHANGED_COMMAND,
                    currency,
                    LOCAL_PLAYER_ACTOR_ID,
                ),
            ),
            include_terminator=True,
        )
        CharacterService._send_world_loot_state_batch(
            s, response, response_bits, label=label
        )

    @staticmethod
    def _schedule_talent_preset_cooldown(
            s: Session, entry: StatusEffectEntry, seconds: float) -> None:
        with s.flow_lock:
            generation = s.gameplay_chase_generation
            timer_key = (TALENT_PRESET_COOLDOWN_TIMER, int(entry.field_04))
            timer = s.gameplay_deadline_timer(
                max(0.0, float(seconds)) + 0.10,
                CharacterService._expire_talent_preset_cooldown,
                args=(s, generation, timer_key, entry),
            )
            timer.daemon = True
            previous = s.gameplay_projectile_timers.pop(timer_key, None)
            s.gameplay_projectile_timers[timer_key] = timer
        if previous is not None:
            previous.cancel()
        timer.start()

    @staticmethod
    def _expire_talent_preset_cooldown(
            s: Session, generation: int, timer_key,
            entry: StatusEffectEntry) -> None:
        """Lift a preset cooldown: the client keeps an effect until told.

        The removal is sent even when other code already dropped the expired
        entry from the session (consumables prune expired timed effects
        silently), or the client would show the cooldown forever.
        """

        with s.flow_lock:
            s.gameplay_projectile_timers.pop(timer_key, None)
            if generation != s.gameplay_chase_generation:
                return
            s.gameplay_persistent_status_effects.pop(entry.field_04, None)
            s.gameplay_timed_status_effect_expiries.pop(entry.field_04, None)
            server_tick = CharacterService._talent_server_tick(s)
        if not s.connected:
            return
        CharacterService._send_talent_effect_removals(
            s,
            (entry,),
            server_tick=server_tick,
            label="talent-preset-cooldown-expired",
        )
        CharacterService._request_account_save(
            s, "talent preset cooldown expired"
        )

    @staticmethod
    def _send_talent_effect_removals(
            s: Session, entries, *, server_tick: int, label: str) -> None:
        response, response_bits = encode_server_command_batch(
            (
                ServerCommand(
                    STATUS_EFFECT_COMMAND,
                    StatusEffect(
                        False,
                        tuple(
                            CharacterService._consumable_cleanup_entry(
                                entry, server_tick=server_tick
                            )
                            for entry in entries
                        ),
                    ),
                    LOCAL_PLAYER_ACTOR_ID,
                ),
            ),
            include_terminator=True,
        )
        CharacterService._send_world_loot_state_batch(
            s, response, response_bits, label=label
        )

    def _restore_talent_preset_cooldowns(self, s: Session) -> None:
        """Re-arm saved preset cooldowns after a map load; a hub lifts them
        ("...or you must enter an urban area")."""

        database = getattr(self, "talent_database", None)
        if database is None:
            return
        try:
            wire_ids = {
                cooldown[1]
                for category_id in range(3)
                if (cooldown := database.preset_cooldown(category_id))
            }
        except (OSError, ValueError, sqlite3.Error) as exc:
            print(
                f"[{GAMEPLAY_SERVICE_NAME}] talent preset cooldowns not "
                f"restored: {exc}"
            )
            return
        with s.flow_lock:
            gameplay_map = str(s.gameplay_map or "")
        in_hub = CharacterService._talent_map_is_hub(self, gameplay_map)
        now_epoch = time.time()
        lifted = []
        restored = []
        with s.flow_lock:
            for instance_id, entry in tuple(
                s.gameplay_persistent_status_effects.items()
            ):
                if int(entry.effect_id) not in wire_ids:
                    continue
                expires_at = s.gameplay_timed_status_effect_expiries.get(
                    instance_id
                )
                if in_hub or expires_at is None or expires_at <= now_epoch:
                    s.gameplay_persistent_status_effects.pop(
                        instance_id, None
                    )
                    s.gameplay_timed_status_effect_expiries.pop(
                        instance_id, None
                    )
                    lifted.append(entry)
                else:
                    restored.append((entry, float(expires_at) - now_epoch))
            server_tick = CharacterService._talent_server_tick(s)
        if lifted:
            CharacterService._send_talent_effect_removals(
                s,
                lifted,
                server_tick=server_tick,
                label="talent-preset-cooldown-lifted",
            )
            CharacterService._request_account_save(
                s, "talent preset cooldown lifted"
            )
        for entry, seconds in restored:
            CharacterService._schedule_talent_preset_cooldown(
                s, entry, seconds
            )

    def _handle_select_talent(
            self, s: Session, payload: bytes) -> None:
        """Apply the requested ranks in the active skill/wisdom/group/honor
        layout.

        The client sends one rank per click, but its bulk allocation sends the
        whole batch as a single request, so the rank delta is honoured as-is.
        """

        categories = None
        try:
            request = decode_select_talent(payload)
            database = getattr(self, 'talent_database', None)
            if database is None:
                raise ValueError("talent database is unavailable")
            definition = database.definition(request.talent_id)
            with s.flow_lock:
                character = copy.deepcopy(s.gameplay_character)
                categories = copy.deepcopy(s.gameplay_talent_categories)
                presets = copy.deepcopy(
                    s.gameplay_skill_talent_presets
                )
                category_presets = copy.deepcopy(
                    s.gameplay_category_talent_presets
                )
                category_points = tuple(
                    s.gameplay_talent_category_points
                )
                talent_effect_instance_ids = copy.deepcopy(
                    s.gameplay_talent_effect_instance_ids
                )
                persistent_effects = copy.deepcopy(
                    s.gameplay_persistent_status_effects
                )
                next_instance_id = (
                    s.gameplay_next_status_effect_instance_id
                )
                last_move = s.gameplay_last_move
                fallback_tick = s.gameplay_chase_server_tick
            if character is None:
                raise ValueError("local player is not initialized")
            class_name = CLASS_NAMES.get(int(character.class_id))
            if definition.class_name and definition.class_name != class_name:
                raise ValueError(
                    f"{request.talent_id!r} belongs to "
                    f"{definition.class_name}, not {class_name}"
                )
            category_id = int(definition.category_id)
            if category_id == 0:
                progression_level = int(character.level)
                granted_points = database.granted_skill_points(
                    progression_level
                )
            else:
                progression_level = database.progression_level(
                    category_id, category_points[category_id]
                )
                granted_points = database.granted_category_points(
                    category_id, category_points[category_id]
                )
            # Knowledge and Group UnlockLevels (30..55, 20) are character
            # levels: ClientTalentManager compares them with the character
            # level, and only Honor traits with their own tree's level.
            unlock_level = (
                int(character.level)
                if category_id in (0, 1, 2)
                else progression_level
            )
            if unlock_level < definition.unlock_level:
                # Name the progression value that produced the level: a
                # session that somehow lost its saved Honor XP is otherwise
                # indistinguishable from one that never earned any.
                source = (
                    f"character level {unlock_level}"
                    if category_id in (0, 1, 2)
                    else (
                        f"{TALENT_CATEGORY_NAMES[category_id]} level "
                        f"{progression_level} from "
                        f"{category_points[category_id]} XP"
                    )
                )
                raise ValueError(
                    f"{request.talent_id!r} unlocks at level "
                    f"{definition.unlock_level} in "
                    f"{TALENT_CATEGORY_NAMES[category_id]} progression; "
                    f"this character is at {source}"
                )
            if len(categories) != 4:
                raise ValueError("player talent snapshot is incomplete")

            talent_category = categories[category_id]
            active_preselection = int(talent_category.preselection)
            active_presets = (
                presets
                if category_id == 0
                else category_presets.setdefault(category_id, {})
            )
            combined = list(
                active_presets.setdefault(
                    active_preselection,
                    talent_category.selected_skills,
                )
            )
            current_entry = next(
                (
                    entry for entry in combined
                    if entry.resource_id == request.talent_id
                ),
                None,
            )
            current_rank = (
                int(current_entry.field_00)
                if current_entry is not None
                else 0
            )
            if category_id == 0 and definition.group_id:
                # One talent per GroupId in a skill preset (e.g. the four
                # <class>TalentElemental masteries).
                rival = next(
                    (
                        entry.resource_id
                        for entry in combined
                        if entry.resource_id != request.talent_id
                        and int(entry.field_00) > 0
                        and database.definition(entry.resource_id).group_id
                        == definition.group_id
                    ),
                    None,
                )
                if rival is not None:
                    raise ValueError(
                        f"{request.talent_id!r} shares group "
                        f"{definition.group_id!r} with selected {rival!r}"
                    )
            target_rank = current_rank + int(request.rank_delta)
            if target_rank > definition.max_rank:
                raise ValueError(
                    f"{request.talent_id!r} cannot go from rank "
                    f"{current_rank} to rank {target_rank}; its maximum "
                    f"rank is {definition.max_rank}"
                )

            used_points = 0
            for entry in combined:
                selected_definition = database.definition(entry.resource_id)
                if selected_definition.category_id != category_id:
                    raise ValueError(
                        f"preset {active_preselection} mixes talent "
                        "categories"
                    )
                used_points += sum(
                    selected_definition.point_cost_for_rank(rank)
                    for rank in range(1, int(entry.field_00) + 1)
                )
            # Bulk allocation raises the rank by more than one step, so every
            # rank crossed has to be paid for -- charging only the final rank
            # would sell the intermediate ones for free.
            rank_cost = sum(
                definition.point_cost_for_rank(rank)
                for rank in range(current_rank + 1, target_rank + 1)
            )
            if used_points + rank_cost > granted_points:
                raise ValueError(
                    f"not enough {TALENT_CATEGORY_NAMES[category_id]} "
                    f"talent points: "
                    f"{granted_points - used_points} available, "
                    f"{rank_cost} required"
                )

            replacement = TalentSelectionEntry(
                target_rank, 0, request.talent_id
            )
            if current_entry is None:
                combined.append(replacement)
            else:
                combined = [
                    (
                        replacement
                        if entry.resource_id == request.talent_id
                        else entry
                    )
                    for entry in combined
                ]
            roots = CharacterService._talent_category_roots(
                database, combined
            )
            updated_category = TalentCategorySelection(
                active_preselection, tuple(combined), roots
            )
            mutable_categories = list(categories)
            mutable_categories[category_id] = updated_category
            updated_categories = tuple(mutable_categories)
            server_tick = (
                int(last_move.start_tick)
                if last_move is not None
                else max(
                    int(fallback_tick),
                    int(server_time() // 40),
                )
            )
            new_effects = database.status_effect_entries(
                definition,
                target_rank=target_rank,
                actor_id=LOCAL_PLAYER_ACTOR_ID,
                first_instance_id=next_instance_id,
                server_tick=server_tick,
            )
            talent_modifiers = database.modifiers_for_rank(
                definition, target_rank
            )
            previous_effects = tuple(
                persistent_effects[instance_id]
                for instance_id in talent_effect_instance_ids.get(
                    CharacterService._talent_effect_key(
                        category_id, request.talent_id
                    ),
                    (),
                )
                if instance_id in persistent_effects
            )
            commands = []
            if previous_effects:
                commands.append(
                    ServerCommand(
                        STATUS_EFFECT_COMMAND,
                        StatusEffect(
                            False,
                            tuple(
                                replace(
                                    effect,
                                    has_details=False,
                                    values=(),
                                    shape_values=(),
                                )
                                for effect in previous_effects
                            ),
                        ),
                        LOCAL_PLAYER_ACTOR_ID,
                    )
                )
            commands.append(
                ServerCommand(
                    STATUS_EFFECT_COMMAND,
                    StatusEffect(False, new_effects),
                    LOCAL_PLAYER_ACTOR_ID,
                )
            )
            commands.append(
                ServerCommand(
                    TALENT_SELECTION_INFO_COMMAND,
                    (
                        TalentSelectionInfo(
                            3,
                            preselection=active_preselection,
                            # Branch 3 replaces the visible active spell
                            # preset; it is not a one-entry patch. Retail
                            # repeats every selection accumulated in the
                            # preset after each successful 0x0117.
                            selections=tuple(combined),
                        )
                        if category_id == 0
                        else TalentSelectionInfo(
                            0, updated_categories
                        )
                    ),
                    0,
                )
            )
            response, response_bits = encode_server_command_batch(
                tuple(commands),
                include_terminator=True,
            )
        except (OSError, ValueError) as exc:
            print(
                f"[{GAMEPLAY_SERVICE_NAME}] rejected SelectTalent: {exc}"
            )
            if categories is None:
                with s.flow_lock:
                    categories = copy.deepcopy(
                        s.gameplay_talent_categories
                    )
            if len(categories) == 4:
                CharacterService._send_talent_snapshot(
                    self,
                    s,
                    categories,
                    label="select-talent-reconcile",
                )
            return

        with s.flow_lock:
            s.gameplay_talent_categories = updated_categories
            active_presets[active_preselection] = tuple(combined)
            if category_id == 0:
                s.gameplay_skill_talent_presets = active_presets
            else:
                category_presets[category_id] = active_presets
                s.gameplay_category_talent_presets = category_presets
            for effect in previous_effects:
                s.gameplay_persistent_status_effects.pop(
                    effect.field_04, None
                )
            for effect in new_effects:
                s.gameplay_persistent_status_effects[
                    effect.field_04
                ] = effect
            effect_key = CharacterService._talent_effect_key(
                category_id, request.talent_id
            )
            s.gameplay_talent_effect_instance_ids[effect_key] = tuple(
                effect.field_04 for effect in new_effects
            )
            s.gameplay_next_status_effect_instance_id = (
                next_instance_id + len(new_effects)
            )
            s.gameplay_client_command_state[SELECT_TALENT_COMMAND] = request
        invalidate_player_combat_stats(s)
        CharacterService._refresh_admin_speed(s, static_database_path=self.static_database_path)
        CharacterService._accept_gameplay_state_command(
            s, SELECT_TALENT_COMMAND
        )
        CharacterService._send_world_loot_state_batch(
            s,
            response,
            response_bits,
            label='select-talent',
        )
        CharacterService._persist_gameplay_session(
            self, s, reason='talent selection', flush=True
        )
        player_effect_count = sum(
            modifier.skill_status_effect_scope == "player"
            for modifier in talent_modifiers
        )
        victim_effect_count = sum(
            modifier.skill_status_effect_scope == "victim"
            for modifier in talent_modifiers
        )
        print(
            f"[{GAMEPLAY_SERVICE_NAME}] selected "
            f"{request.talent_id!r} rank {target_rank} in preset "
            f"{active_preselection} "
            f"({TALENT_CATEGORY_NAMES[category_id]}); "
            f"spent {rank_cost} point(s), "
            f"{granted_points - used_points - rank_cost} remain; installed "
            f"{len(talent_modifiers)} modifier(s) "
            f"(player_effects={player_effect_count}, "
            f"victim_effects={victim_effect_count})"
        )

    def _handle_deselect_talent(
            self, s: Session, payload: bytes) -> None:
        """Take one rank off an Honor focus talent or switch a trait off.

        The client sends DeselectTalent for a focus talent's right-click and
        for a click on an active trait. The freed points return to the Honor
        pool by themselves: points are always recounted from the selections.
        """

        categories = None
        try:
            request = decode_deselect_talent(payload)
            database = getattr(self, 'talent_database', None)
            if database is None:
                raise ValueError("talent database is unavailable")
            definition = database.definition(request.talent_id)
            if definition.selection_kind not in (
                "_Template_FocusTalent", "_Template_Trait"
            ):
                raise ValueError(
                    f"{request.talent_id!r} is not an Honor focus talent "
                    "or trait"
                )
            category_id = int(definition.category_id)
            with s.flow_lock:
                categories = copy.deepcopy(s.gameplay_talent_categories)
                category_presets = copy.deepcopy(
                    s.gameplay_category_talent_presets
                )
                talent_effect_instance_ids = copy.deepcopy(
                    s.gameplay_talent_effect_instance_ids
                )
                persistent_effects = copy.deepcopy(
                    s.gameplay_persistent_status_effects
                )
                next_instance_id = (
                    s.gameplay_next_status_effect_instance_id
                )
                server_tick = CharacterService._talent_server_tick(s)
            if len(categories) != 4:
                raise ValueError("player talent snapshot is incomplete")
            talent_category = categories[category_id]
            active_preselection = int(talent_category.preselection)
            layouts = category_presets.setdefault(category_id, {})
            combined = list(layouts.setdefault(
                active_preselection, talent_category.selected_skills
            ))
            current_rank = next(
                (
                    int(entry.field_00) for entry in combined
                    if entry.resource_id == request.talent_id
                ),
                0,
            )
            if current_rank <= 0:
                raise ValueError(f"{request.talent_id!r} is not selected")
            target_rank = (
                current_rank - 1
                if definition.selection_kind == "_Template_FocusTalent"
                else 0
            )
            if target_rank > 0:
                combined = [
                    (
                        TalentSelectionEntry(
                            target_rank, 0, request.talent_id
                        )
                        if entry.resource_id == request.talent_id
                        else entry
                    )
                    for entry in combined
                ]
            else:
                combined = [
                    entry for entry in combined
                    if entry.resource_id != request.talent_id
                ]
            roots = CharacterService._talent_category_roots(
                database, combined
            )
            mutable_categories = list(categories)
            mutable_categories[category_id] = TalentCategorySelection(
                active_preselection, tuple(combined), roots
            )
            updated_categories = tuple(mutable_categories)
            effect_key = CharacterService._talent_effect_key(
                category_id, request.talent_id
            )
            previous_effects = tuple(
                persistent_effects[instance_id]
                for instance_id in talent_effect_instance_ids.get(
                    effect_key, ()
                )
                if instance_id in persistent_effects
            )
            new_effects = (
                database.status_effect_entries(
                    definition,
                    target_rank=target_rank,
                    actor_id=LOCAL_PLAYER_ACTOR_ID,
                    first_instance_id=next_instance_id,
                    server_tick=server_tick,
                )
                if target_rank > 0
                else ()
            )
            commands = []
            if previous_effects:
                commands.append(ServerCommand(
                    STATUS_EFFECT_COMMAND,
                    StatusEffect(
                        False,
                        self._talent_effect_removals(previous_effects),
                    ),
                    LOCAL_PLAYER_ACTOR_ID,
                ))
            if new_effects:
                commands.append(ServerCommand(
                    STATUS_EFFECT_COMMAND,
                    StatusEffect(False, new_effects),
                    LOCAL_PLAYER_ACTOR_ID,
                ))
            commands.append(ServerCommand(
                TALENT_SELECTION_INFO_COMMAND,
                TalentSelectionInfo(0, updated_categories),
                0,
            ))
            response, response_bits = encode_server_command_batch(
                tuple(commands), include_terminator=True
            )
        except (OSError, ValueError) as exc:
            print(
                f"[{GAMEPLAY_SERVICE_NAME}] rejected DeselectTalent: {exc}"
            )
            if categories is None:
                with s.flow_lock:
                    categories = copy.deepcopy(
                        s.gameplay_talent_categories
                    )
            if len(categories) == 4:
                CharacterService._send_talent_snapshot(
                    self,
                    s,
                    categories,
                    label="deselect-talent-reconcile",
                )
            return

        with s.flow_lock:
            s.gameplay_talent_categories = updated_categories
            layouts[active_preselection] = tuple(combined)
            category_presets[category_id] = layouts
            s.gameplay_category_talent_presets = category_presets
            for effect in previous_effects:
                s.gameplay_persistent_status_effects.pop(
                    effect.field_04, None
                )
            for effect in new_effects:
                s.gameplay_persistent_status_effects[
                    effect.field_04
                ] = effect
            if new_effects:
                s.gameplay_talent_effect_instance_ids[effect_key] = tuple(
                    effect.field_04 for effect in new_effects
                )
            else:
                s.gameplay_talent_effect_instance_ids.pop(effect_key, None)
            s.gameplay_next_status_effect_instance_id = (
                next_instance_id + len(new_effects)
            )
            s.gameplay_client_command_state[DESELECT_TALENT_COMMAND] = request
        invalidate_player_combat_stats(s)
        CharacterService._refresh_admin_speed(s, static_database_path=self.static_database_path)
        CharacterService._accept_gameplay_state_command(
            s, DESELECT_TALENT_COMMAND
        )
        CharacterService._send_world_loot_state_batch(
            s,
            response,
            response_bits,
            label="deselect-talent",
        )
        CharacterService._persist_gameplay_session(
            self, s, reason="talent deselection", flush=True
        )
        print(
            f"[{GAMEPLAY_SERVICE_NAME}] deselected {request.talent_id!r}: "
            f"rank {current_rank} -> {target_rank}"
        )

    def _handle_unlock_skill_talent_preselection(
            self, s: Session, payload: bytes) -> None:
        """Buy the next Skill preset (5th..8th) with Andermant."""

        try:
            request = decode_unlock_skill_talent_preselection(payload)
            database = getattr(self, 'talent_database', None)
            if database is None:
                raise ValueError("talent database is unavailable")
            prices = database.unlock_preselection_costs()
            with s.flow_lock:
                unlocked = int(s.gameplay_unlocked_skill_presets)
            if unlocked >= min(
                len(prices), SKILL_TALENT_UNLOCKABLE_PRESELECTIONS
            ):
                raise ValueError("every Skill preset is already bought")
            if int(request.unlocked) != unlocked + 1:
                raise ValueError(
                    f"asked for {request.unlocked} bought preset(s); "
                    f"{unlocked} are bought"
                )
            price = int(prices[unlocked])
            with s.flow_lock:
                if int(s.gameplay_unlocked_skill_presets) != unlocked:
                    raise ValueError("a purchase is already in progress")
                currency = s.gameplay_currency_state
                if currency is None:
                    raise ValueError("the wallet is not initialized")
                if int(currency.andermant) < price:
                    raise ValueError(
                        f"preset {unlocked + 1} costs {price} Andermant; "
                        f"the account has {currency.andermant}"
                    )
                # The shared wallet commits only the difference.
                s.gameplay_currency_state = replace(
                    currency,
                    currency_type=int(currency.andermant) - price,
                    notify=True,
                )
                currency = s.gameplay_currency_state
                s.gameplay_unlocked_skill_presets = unlocked + 1
                s.gameplay_client_command_state[
                    UNLOCK_SKILL_TALENT_PRESELECTION_COMMAND
                ] = request
        except (OSError, ValueError) as exc:
            print(
                f"[{GAMEPLAY_SERVICE_NAME}] rejected "
                f"UnlockSkillTalentPreselection: {exc}"
            )
            with s.flow_lock:
                unlocked = int(s.gameplay_unlocked_skill_presets)
            response, response_bits = encode_server_command_batch(
                (
                    ServerCommand(
                        UNLOCK_SKILL_TALENT_PRESELECTION_INFO_COMMAND,
                        UnlockSkillTalentPreselectionInfo(unlocked),
                        0,
                    ),
                ),
                include_terminator=True,
            )
            CharacterService._send_world_loot_state_batch(
                s,
                response,
                response_bits,
                label="unlock-skill-preset-reconcile",
            )
            return

        CharacterService._accept_gameplay_state_command(
            s, UNLOCK_SKILL_TALENT_PRESELECTION_COMMAND
        )
        response, response_bits = encode_server_command_batch(
            (
                ServerCommand(
                    CURRENCY_CHANGED_COMMAND,
                    currency,
                    LOCAL_PLAYER_ACTOR_ID,
                ),
                ServerCommand(
                    UNLOCK_SKILL_TALENT_PRESELECTION_INFO_COMMAND,
                    UnlockSkillTalentPreselectionInfo(unlocked + 1),
                    0,
                ),
            ),
            include_terminator=True,
        )
        CharacterService._send_world_loot_state_batch(
            s,
            response,
            response_bits,
            label="unlock-skill-preset",
        )
        CharacterService._persist_gameplay_session(
            self, s, reason="skill preset purchase", flush=True
        )
        print(
            f"[{GAMEPLAY_SERVICE_NAME}] bought Skill preset "
            f"{unlocked + 1} of {SKILL_TALENT_UNLOCKABLE_PRESELECTIONS} "
            f"for {price} Andermant"
        )

    @staticmethod
    def _talent_effect_removals(
        effects,
    ):
        return tuple(
            replace(
                effect,
                has_details=False,
                values=(),
                shape_values=(),
            )
            for effect in effects
        )

    def _send_talent_snapshot(
            self,
            s: Session,
            categories: tuple[TalentCategorySelection, ...],
            *,
            label: str) -> None:
        """Undo optimistic client edits with authoritative points and trees."""

        with s.flow_lock:
            category_points = tuple(s.gameplay_talent_category_points)
        if len(category_points) != 4:
            raise ValueError("player talent category points are incomplete")
        response, response_bits = encode_server_command_batch(
            (
                # The client spends a point optimistically when it emits
                # SelectTalent. A rejected Honor request therefore needs the
                # progression snapshot as well as the selection snapshot;
                # 0x011b alone restores ranks but leaves its cached available
                # point counter below zero.
                ServerCommand(
                    TALENT_CATEGORY_POINTS_INFO_COMMAND,
                    TalentCategoryPointsInfo(0xFF, category_points),
                    0,
                ),
                ServerCommand(
                    TALENT_SELECTION_INFO_COMMAND,
                    TalentSelectionInfo(0, tuple(categories)),
                    0,
                ),
            ),
            include_terminator=True,
        )
        CharacterService._send_world_loot_state_batch(
            s, response, response_bits, label=label
        )

    def _handle_select_talent_preselection(
            self, s: Session, payload: bytes) -> None:
        """Activate one saved layout in any talent category.

        Only the membership's presets (NumTalentSets) and bought Skill
        presets can be switched to. Outside a hub, a switch puts the tree's
        preset cooldown effect on the player and is refused while it runs.
        """

        categories = None
        try:
            request = decode_select_talent_preselection(payload)
            database = self.talent_database
            with s.flow_lock:
                membership = (
                    int(s.gameplay_membership_type),
                    bool(s.gameplay_membership_active),
                    str(s.gameplay_membership_expires_at),
                )
                unlocked_skill_presets = int(
                    s.gameplay_unlocked_skill_presets
                )
                gameplay_map = str(s.gameplay_map or "")
                cooldown_tick = CharacterService._talent_server_tick(s)
                expiries = dict(s.gameplay_timed_status_effect_expiries)
                categories = copy.deepcopy(s.gameplay_talent_categories)
                skill_presets = copy.deepcopy(
                    s.gameplay_skill_talent_presets
                )
                category_presets = copy.deepcopy(
                    s.gameplay_category_talent_presets
                )
                old_mapping = copy.deepcopy(
                    s.gameplay_talent_effect_instance_ids
                )
                persistent = copy.deepcopy(
                    s.gameplay_persistent_status_effects
                )
                next_instance_id = (
                    s.gameplay_next_status_effect_instance_id
                )
                last_move = s.gameplay_last_move
                fallback_tick = s.gameplay_chase_server_tick
            category_id = int(request.category)
            switching = request.preselection != int(
                categories[category_id].preselection
            )
            cooldown = None
            stale_cooldowns = ()
            if switching:
                talent_sets = database.talent_sets(
                    membership[0], membership_is_active(*membership)
                )
                if not talent_preselection_available(
                    category_id,
                    request.preselection,
                    talent_sets=talent_sets,
                    unlocked_skill_presets=unlocked_skill_presets,
                ):
                    raise ValueError(
                        f"{TALENT_CATEGORY_NAMES[category_id]} preset "
                        f"{request.preselection} is locked: the membership "
                        f"grants {talent_sets}, {unlocked_skill_presets} "
                        "Skill preset(s) are bought"
                    )
                if not CharacterService._talent_map_is_hub(
                    self, gameplay_map
                ):
                    cooldown = database.preset_cooldown(category_id)
            if cooldown is not None:
                now_epoch = time.time()
                stale_cooldowns = tuple(
                    effect for effect in persistent.values()
                    if int(effect.effect_id) == cooldown[1]
                )
                remaining = max(
                    (
                        float(expiries.get(effect.field_04, 0.0))
                        - now_epoch
                        for effect in stale_cooldowns
                    ),
                    default=0.0,
                )
                if remaining > 0.0:
                    raise ValueError(
                        f"the {TALENT_CATEGORY_NAMES[category_id]} preset "
                        f"cooldown has {math.ceil(remaining)} s left"
                    )
            presets = (
                skill_presets
                if category_id == 0
                else category_presets.setdefault(category_id, {})
            )
            selections = tuple(presets.get(request.preselection, ()))
            roots = CharacterService._talent_category_roots(
                database, selections
            )
            server_tick = (
                int(last_move.start_tick)
                if last_move is not None
                else max(int(fallback_tick), int(server_time() // 40))
            )
            previous_effects = tuple(
                persistent[instance_id]
                for effect_key, instance_ids in old_mapping.items()
                if CharacterService._talent_effect_key_is_category(
                    effect_key, category_id
                )
                for instance_id in instance_ids
                if instance_id in persistent
            )
            new_effects = []
            new_mapping = {
                effect_key: instance_ids
                for effect_key, instance_ids in old_mapping.items()
                if not CharacterService._talent_effect_key_is_category(
                    effect_key, category_id
                )
            }
            for selection in selections:
                definition = database.definition(selection.resource_id)
                if definition.category_id != category_id:
                    raise ValueError(
                        f"preset {request.preselection} contains a "
                        f"{TALENT_CATEGORY_NAMES[definition.category_id]} "
                        "talent"
                    )
                effects = database.status_effect_entries(
                    definition,
                    target_rank=int(selection.field_00),
                    actor_id=LOCAL_PLAYER_ACTOR_ID,
                    first_instance_id=next_instance_id,
                    server_tick=server_tick,
                )
                database.modifiers_for_rank(
                    definition, int(selection.field_00)
                )
                new_effects.extend(effects)
                effect_key = CharacterService._talent_effect_key(
                    category_id, selection.resource_id
                )
                new_mapping[effect_key] = tuple(
                    effect.field_04 for effect in effects
                )
                next_instance_id += len(effects)
            updated_category = TalentCategorySelection(
                request.preselection, selections, roots
            )
            cooldown_entry = None
            if cooldown is not None and cooldown[2] > 0.0:
                duration_ticks = max(1, int(math.ceil(
                    cooldown[2] * SERVER_TICKS_PER_SECOND
                )))
                cooldown_entry = StatusEffectEntry(
                    effect_id=cooldown[1],
                    field_04=next_instance_id,
                    field_0c=(cooldown_tick + duration_ticks) & 0xFFFFFFFF,
                    field_14=SERVER_TICKS_PER_SECOND,
                    field_08=cooldown_tick,
                    field_18=0,
                    field_10=duration_ticks,
                    field_1c=100,
                    field_20=LOCAL_PLAYER_ACTOR_ID,
                    field_25=False,
                    field_26=False,
                    field_39=False,
                    has_details=True,
                    values=(0.0, 0.0, 0.0, 0.0, 0.0),
                )
                next_instance_id += 1
            commands = []
            if previous_effects:
                commands.append(ServerCommand(
                    STATUS_EFFECT_COMMAND,
                    StatusEffect(
                        False,
                        self._talent_effect_removals(previous_effects),
                    ),
                    LOCAL_PLAYER_ACTOR_ID,
                ))
            if stale_cooldowns:
                commands.append(ServerCommand(
                    STATUS_EFFECT_COMMAND,
                    StatusEffect(
                        False,
                        tuple(
                            CharacterService._consumable_cleanup_entry(
                                effect, server_tick=cooldown_tick
                            )
                            for effect in stale_cooldowns
                        ),
                    ),
                    LOCAL_PLAYER_ACTOR_ID,
                ))
            if new_effects:
                commands.append(ServerCommand(
                    STATUS_EFFECT_COMMAND,
                    StatusEffect(False, tuple(new_effects)),
                    LOCAL_PLAYER_ACTOR_ID,
                ))
            if cooldown_entry is not None:
                commands.append(ServerCommand(
                    STATUS_EFFECT_COMMAND,
                    StatusEffect(False, (cooldown_entry,)),
                    LOCAL_PLAYER_ACTOR_ID,
                ))
            commands.append(ServerCommand(
                TALENT_SELECTION_INFO_COMMAND,
                (
                    TalentSelectionInfo(
                        3,
                        preselection=request.preselection,
                        selections=selections,
                    )
                    if category_id == 0
                    else TalentSelectionInfo(
                        0,
                        tuple(
                            updated_category
                            if index == category_id
                            else category
                            for index, category in enumerate(categories)
                        ),
                    )
                ),
                0,
            ))
            response, response_bits = encode_server_command_batch(
                tuple(commands), include_terminator=True
            )
        except (OSError, ValueError) as exc:
            print(
                f"[{GAMEPLAY_SERVICE_NAME}] rejected talent preset "
                f"selection: {exc}"
            )
            # The client highlighted the clicked button; the snapshot puts
            # the active preset back.
            if categories is None:
                with s.flow_lock:
                    categories = copy.deepcopy(
                        s.gameplay_talent_categories
                    )
            if len(categories) == 4:
                CharacterService._send_talent_snapshot(
                    self,
                    s,
                    categories,
                    label="select-talent-preset-reconcile",
                )
            return

        retired_timers = []
        with s.flow_lock:
            for effect in previous_effects:
                s.gameplay_persistent_status_effects.pop(
                    effect.field_04, None
                )
            for effect in new_effects:
                s.gameplay_persistent_status_effects[
                    effect.field_04
                ] = effect
            for effect in stale_cooldowns:
                s.gameplay_persistent_status_effects.pop(
                    effect.field_04, None
                )
                s.gameplay_timed_status_effect_expiries.pop(
                    effect.field_04, None
                )
                timer = s.gameplay_projectile_timers.pop(
                    (TALENT_PRESET_COOLDOWN_TIMER, effect.field_04), None
                )
                if timer is not None:
                    retired_timers.append(timer)
            if cooldown_entry is not None:
                s.gameplay_persistent_status_effects[
                    cooldown_entry.field_04
                ] = cooldown_entry
                s.gameplay_timed_status_effect_expiries[
                    cooldown_entry.field_04
                ] = time.time() + cooldown[2]
            s.gameplay_talent_effect_instance_ids = new_mapping
            s.gameplay_next_status_effect_instance_id = next_instance_id
            mutable_categories = list(categories)
            mutable_categories[category_id] = updated_category
            s.gameplay_talent_categories = tuple(mutable_categories)
            if category_id == 0:
                s.gameplay_skill_talent_presets = presets
            else:
                category_presets[category_id] = presets
                s.gameplay_category_talent_presets = category_presets
            s.gameplay_client_command_state[
                SELECT_TALENT_PRESELECTION_COMMAND
            ] = request
        invalidate_player_combat_stats(s)
        CharacterService._refresh_admin_speed(s, static_database_path=self.static_database_path)
        CharacterService._accept_gameplay_state_command(
            s, SELECT_TALENT_PRESELECTION_COMMAND
        )
        for timer in retired_timers:
            timer.cancel()
        CharacterService._send_world_loot_state_batch(
            s,
            response,
            response_bits,
            label="select-talent-preset",
        )
        if cooldown_entry is not None:
            CharacterService._schedule_talent_preset_cooldown(
                s, cooldown_entry, cooldown[2]
            )
        CharacterService._persist_gameplay_session(
            self, s, reason="talent preset selection", flush=True
        )
        print(
            f"[{GAMEPLAY_SERVICE_NAME}] activated "
            f"{TALENT_CATEGORY_NAMES[category_id]} talent preset "
            f"{request.preselection} with {len(selections)} selection(s)"
        )

    def _handle_reset_talent_category(
            self, s: Session, payload: bytes) -> None:
        """Reset one Wisdom/Group branch inside its active saved layout."""

        categories = None
        try:
            request = decode_reset_talent_category(payload)
            if request.category not in (1, 2):
                raise ValueError(
                    "ResetTalentCategory is valid only for Wisdom or Group"
                )
            database = self.talent_database
            with s.flow_lock:
                categories = copy.deepcopy(s.gameplay_talent_categories)
                category_presets = copy.deepcopy(
                    s.gameplay_category_talent_presets
                )
                mapping = copy.deepcopy(
                    s.gameplay_talent_effect_instance_ids
                )
                persistent = copy.deepcopy(
                    s.gameplay_persistent_status_effects
                )
            category_id = int(request.category)
            category = categories[category_id]
            if request.preselection != int(category.preselection):
                raise ValueError(
                    f"cannot reset branch in inactive preset "
                    f"{request.preselection}"
                )
            layouts = category_presets.setdefault(category_id, {})
            selections = tuple(layouts.setdefault(
                request.preselection, category.selected_skills
            ))
            removed_ids = {
                selection.resource_id
                for selection in selections
                if database.definition(selection.resource_id).group_id
                == request.category_id
            }
            if not removed_ids:
                raise ValueError(
                    f"active preset has no selections in branch "
                    f"{request.category_id!r}"
                )
            price = CharacterService._talent_respec_price(
                self,
                s,
                database,
                category_id,
                CharacterService._talent_spent_points(
                    database,
                    (
                        selection for selection in selections
                        if selection.resource_id in removed_ids
                    ),
                ),
            )
            CharacterService._require_talent_gold(s, price)
            retained_selections = tuple(
                selection
                for selection in selections
                if selection.resource_id not in removed_ids
            )
            roots = CharacterService._talent_category_roots(
                database, retained_selections
            )
            updated_category = TalentCategorySelection(
                request.preselection, retained_selections, roots
            )
            mutable_categories = list(categories)
            mutable_categories[category_id] = updated_category
            updated_categories = tuple(mutable_categories)
            removed_effect_keys = {
                CharacterService._talent_effect_key(
                    category_id, talent_id
                )
                for talent_id in removed_ids
            }
            previous_effects = tuple(
                persistent[instance_id]
                for effect_key in removed_effect_keys
                for instance_id in mapping.get(effect_key, ())
                if instance_id in persistent
            )
            commands = []
            if previous_effects:
                commands.append(ServerCommand(
                    STATUS_EFFECT_COMMAND,
                    StatusEffect(
                        False,
                        self._talent_effect_removals(previous_effects),
                    ),
                    LOCAL_PLAYER_ACTOR_ID,
                ))
            commands.append(ServerCommand(
                TALENT_SELECTION_INFO_COMMAND,
                TalentSelectionInfo(0, updated_categories),
                0,
            ))
            response, response_bits = encode_server_command_batch(
                tuple(commands), include_terminator=True
            )
        except (OSError, ValueError) as exc:
            print(
                f"[{GAMEPLAY_SERVICE_NAME}] rejected "
                f"ResetTalentCategory: {exc}"
            )
            if categories is None:
                with s.flow_lock:
                    categories = copy.deepcopy(
                        s.gameplay_talent_categories
                    )
            if len(categories) == 4:
                CharacterService._send_talent_snapshot(
                    self,
                    s,
                    categories,
                    label="reset-talent-category-reconcile",
                )
            return

        with s.flow_lock:
            for effect in previous_effects:
                s.gameplay_persistent_status_effects.pop(
                    effect.field_04, None
                )
            for effect_key in removed_effect_keys:
                s.gameplay_talent_effect_instance_ids.pop(
                    effect_key, None
                )
            layouts[request.preselection] = retained_selections
            category_presets[category_id] = layouts
            s.gameplay_category_talent_presets = category_presets
            s.gameplay_talent_categories = updated_categories
            currency = CharacterService._charge_talent_gold(s, price)
            s.gameplay_client_command_state[
                RESET_TALENT_CATEGORY_COMMAND
            ] = request
        invalidate_player_combat_stats(s)
        CharacterService._refresh_admin_speed(s, static_database_path=self.static_database_path)
        CharacterService._accept_gameplay_state_command(
            s, RESET_TALENT_CATEGORY_COMMAND
        )
        CharacterService._send_talent_currency(
            s, currency, label="reset-talent-category-currency"
        )
        CharacterService._send_world_loot_state_batch(
            s,
            response,
            response_bits,
            label="reset-talent-category",
        )
        CharacterService._persist_gameplay_session(
            self, s, reason="talent category reset", flush=True
        )
        print(
            f"[{GAMEPLAY_SERVICE_NAME}] reset "
            f"{TALENT_CATEGORY_NAMES[category_id]} branch "
            f"{request.category_id!r} in preset {request.preselection}; "
            f"removed {len(removed_ids)} selection(s) for {price} copper"
        )

    def _handle_reset_talent_preselection(
            self, s: Session, payload: bytes) -> None:
        """Reset one layout without overwriting another category/layout."""

        categories = None
        try:
            request = decode_reset_talent_preselection(payload)
            with s.flow_lock:
                categories = copy.deepcopy(s.gameplay_talent_categories)
                skill_presets = copy.deepcopy(
                    s.gameplay_skill_talent_presets
                )
                category_presets = copy.deepcopy(
                    s.gameplay_category_talent_presets
                )
                mapping = copy.deepcopy(
                    s.gameplay_talent_effect_instance_ids
                )
                persistent = copy.deepcopy(
                    s.gameplay_persistent_status_effects
                )
            category_id = int(request.category)
            presets = (
                skill_presets
                if category_id == 0
                else category_presets.setdefault(category_id, {})
            )
            if (
                request.preselection
                != categories[category_id].preselection
            ):
                raise ValueError(
                    f"cannot reset inactive preset {request.preselection}"
                )
            database = self.talent_database
            if database is None:
                raise ValueError("talent database is unavailable")
            price = CharacterService._talent_respec_price(
                self,
                s,
                database,
                category_id,
                CharacterService._talent_spent_points(
                    database,
                    presets.get(
                        request.preselection,
                        categories[category_id].selected_skills,
                    ),
                ),
            )
            CharacterService._require_talent_gold(s, price)
            previous_effects = tuple(
                persistent[instance_id]
                for effect_key, instance_ids in mapping.items()
                if CharacterService._talent_effect_key_is_category(
                    effect_key, category_id
                )
                for instance_id in instance_ids
                if instance_id in persistent
            )
            retained_mapping = {
                effect_key: instance_ids
                for effect_key, instance_ids in mapping.items()
                if not CharacterService._talent_effect_key_is_category(
                    effect_key, category_id
                )
            }
            commands = []
            if previous_effects:
                commands.append(ServerCommand(
                    STATUS_EFFECT_COMMAND,
                    StatusEffect(
                        False,
                        self._talent_effect_removals(previous_effects),
                    ),
                    LOCAL_PLAYER_ACTOR_ID,
                ))
            commands.append(ServerCommand(
                TALENT_SELECTION_INFO_COMMAND,
                (
                    TalentSelectionInfo(
                        3,
                        preselection=request.preselection,
                        selections=(),
                    )
                    if category_id == 0
                    else TalentSelectionInfo(
                        0,
                        tuple(
                            TalentCategorySelection(
                                request.preselection, (), ()
                            )
                            if index == category_id
                            else category
                            for index, category in enumerate(categories)
                        ),
                    )
                ),
                0,
            ))
            response, response_bits = encode_server_command_batch(
                tuple(commands), include_terminator=True
            )
        except (OSError, ValueError) as exc:
            print(
                f"[{GAMEPLAY_SERVICE_NAME}] rejected talent preset reset: "
                f"{exc}"
            )
            if categories is None:
                with s.flow_lock:
                    categories = copy.deepcopy(
                        s.gameplay_talent_categories
                    )
            if len(categories) == 4:
                CharacterService._send_talent_snapshot(
                    self,
                    s,
                    categories,
                    label="reset-talent-preset-reconcile",
                )
            return

        with s.flow_lock:
            for effect in previous_effects:
                s.gameplay_persistent_status_effects.pop(
                    effect.field_04, None
                )
            presets[request.preselection] = ()
            if category_id == 0:
                s.gameplay_skill_talent_presets = presets
            else:
                category_presets[category_id] = presets
                s.gameplay_category_talent_presets = category_presets
            s.gameplay_talent_effect_instance_ids = retained_mapping
            mutable_categories = list(categories)
            mutable_categories[category_id] = TalentCategorySelection(
                request.preselection, (), ()
            )
            s.gameplay_talent_categories = tuple(mutable_categories)
            currency = CharacterService._charge_talent_gold(s, price)
            s.gameplay_client_command_state[
                RESET_TALENT_PRESELECTION_COMMAND
            ] = request
        invalidate_player_combat_stats(s)
        CharacterService._refresh_admin_speed(s, static_database_path=self.static_database_path)
        CharacterService._accept_gameplay_state_command(
            s, RESET_TALENT_PRESELECTION_COMMAND
        )
        CharacterService._send_talent_currency(
            s, currency, label="reset-talent-preset-currency"
        )
        CharacterService._send_world_loot_state_batch(
            s,
            response,
            response_bits,
            label="reset-talent-preset",
        )
        CharacterService._persist_gameplay_session(
            self, s, reason="talent preset reset", flush=True
        )
        print(
            f"[{GAMEPLAY_SERVICE_NAME}] reset "
            f"{TALENT_CATEGORY_NAMES[category_id]} talent preset "
            f"{request.preselection} for {price} copper"
        )

    def _handle_pvp_flag(self, s: Session, payload: bytes) -> None:
        """Acknowledge one global-PvP toggle and retain it across handoffs."""

        try:
            request = decode_pvp_flag(payload, 57)
        except ValueError as exc:
            print(f"[{GAMEPLAY_SERVICE_NAME}] rejected PVPFlag: {exc}")
            return
        with s.flow_lock:
            s.gameplay_pvp_enabled = bool(request.enabled)
            enabled = s.gameplay_pvp_enabled
            s.gameplay_client_command_state[PVP_FLAG_COMMAND] = request
        CharacterService._accept_gameplay_state_command(s, PVP_FLAG_COMMAND)
        response = encode_pvp_flag_response(
            request, actor_id=LOCAL_PLAYER_ACTOR_ID
        )
        s.send_reliable_ordered(
            response,
            track=True,
            label="pvp-flag",
        )
        if s.gameplay_presence_callback is not None:
            s.gameplay_presence_callback(
                s, ("pvp_flag", enabled, request.server_tick)
            )
        CharacterService._request_account_save(
            s, "global PvP flag changed", flush=True
        )
        print(
            f"[{GAMEPLAY_SERVICE_NAME}] global PvP mode "
            f"{'enabled' if enabled else 'disabled'}; echoed request "
            f"tick {request.server_tick}"
        )
