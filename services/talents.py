"""Talent presets, allocation, resets, modifiers, and PvP state."""

from __future__ import annotations

from dso_server.runtime.context import *
from dso_server.runtime.transport import Session


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
            if progression_level < definition.unlock_level:
                # Each category tracks its own level, so the character level
                # says nothing about a Knowledge/Group/Fame unlock. Name the
                # progression value that produced the level: a session that
                # somehow lost its saved category XP is otherwise
                # indistinguishable from one that never earned any.
                source = (
                    f"character level {progression_level}"
                    if category_id == 0
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
        """Activate one saved layout in any talent category."""

        try:
            request = decode_select_talent_preselection(payload)
            database = self.talent_database
            with s.flow_lock:
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
                    StatusEffect(False, tuple(new_effects)),
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
            return

        with s.flow_lock:
            for effect in previous_effects:
                s.gameplay_persistent_status_effects.pop(
                    effect.field_04, None
                )
            for effect in new_effects:
                s.gameplay_persistent_status_effects[
                    effect.field_04
                ] = effect
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
        CharacterService._send_world_loot_state_batch(
            s,
            response,
            response_bits,
            label="select-talent-preset",
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
            s.gameplay_client_command_state[
                RESET_TALENT_CATEGORY_COMMAND
            ] = request
        invalidate_player_combat_stats(s)
        CharacterService._refresh_admin_speed(s, static_database_path=self.static_database_path)
        CharacterService._accept_gameplay_state_command(
            s, RESET_TALENT_CATEGORY_COMMAND
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
            f"removed {len(removed_ids)} selection(s)"
        )

    def _handle_reset_talent_preselection(
            self, s: Session, payload: bytes) -> None:
        """Reset one layout without overwriting another category/layout."""

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
        except ValueError as exc:
            print(
                f"[{GAMEPLAY_SERVICE_NAME}] rejected talent preset reset: "
                f"{exc}"
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
            s.gameplay_client_command_state[
                RESET_TALENT_PRESELECTION_COMMAND
            ] = request
        invalidate_player_combat_stats(s)
        CharacterService._refresh_admin_speed(s, static_database_path=self.static_database_path)
        CharacterService._accept_gameplay_state_command(
            s, RESET_TALENT_PRESELECTION_COMMAND
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
            f"{request.preselection}"
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
