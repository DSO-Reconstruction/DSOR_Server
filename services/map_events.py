"""Map-local scripted events: signals, event transitions, props, triggers.

The static data is described in ``docs/GAME_EVENTS.md`` (map-local section)
and ``docs/PROP_AND_TRIGGER_COMMANDS.md``.  One :class:`MapEventRuntime` is
shared by every session on a map instance, keyed like the combat state.
World happenings reach this service either directly (client commands) or
through the presence callback (``("map_signal", ...)`` and
``("monsters_killed", ...)``) from static combat and movement code.
"""

from __future__ import annotations

import math
import random
import sqlite3
import time
from functools import lru_cache
from pathlib import Path

from dso_server.runtime.context import *
from dso_server.runtime.transport import Session
from dso_server.gameplay.local_map_state import (
    LOCAL_NPC_ACTOR_BASE,
    active_local_map_event_flags,
    load_local_map_exits,
    load_local_npc_definitions,
    local_npc_actor_id,
    local_npc_info,
)
from dso_server.gameplay.boss_encounter_policy import select_boss_spawn_rows
from dso_server.gameplay.event_spawn_areas import event_area_spawn_rows
from dso_server.gameplay.local_monster_state import (
    _grounded_spawn_position,
    _load_monster,
    _load_runtime_definition,
    _spawn_group,
    load_local_player_only_encounter,
)
from dso_server.gameplay.map_collision_terrain import CollisionTerrainError
from dso_server.gameplay.map_event_state import (
    CLIENT_REPORTED_SIGNAL_TYPES,
    GatedSpawnRow,
    WIRE_PROP_OPERATION,
    MapEventRuntime,
    MapLocalEventDefinition,
    PropRuntimeState,
    SignalContext,
    load_map_event_content,
    load_map_signal_catalog,
    map_key,
)
from dso_server.gameplay.pathengine_navigation import NavigationError
from dso_server.protocol.character_selection import CLASS_NAMES
from dso_server.protocol.gameplay_protocol import (
    PROP_INFO_COMMAND,
    ActorsEnterVicinity,
    ActorsEnterVicinityBatch,
    ActorPlacement,
    Message,
    MessageArgument,
    NPCBatch,
    NewNPC,
    PropInfo,
    PropInfoEntry,
    encode_actors_enter_vicinity_batch,
    encode_npc_batch,
    encode_message_batch,
    encode_screen_message,
)
from dso_server.protocol.player_init_protocol import (
    EXIT_INFO_COMMAND,
    KICK_OUT_TO_HUB_LIMIT_TIME_LEFT_COMMAND,
    EventUpdate,
    EventUpdateEntry,
    ExitInfo,
    ExitInfoEntry,
    KickOutToHubLimitTimeLeft,
    ServerCommand,
    encode_event_update,
    encode_server_command_batch,
)
from dso_server.protocol.prop_protocol import (
    PROP_INTERACTION_COMMAND,
    PROP_STATE_ACTIVE,
    PROP_STATE_USED,
    QUEST_TRIGGER_SIGNAL_COMMAND,
    PropUpdate,
    decode_prop_interaction,
    decode_quest_trigger_signal,
    encode_discard_prop_batch,
    encode_prop_update_batch,
)
from dso_server.protocol.commerce_protocol import (
    _new_item,
    load_purchase_item_definition,
)
from dso_server.protocol.world_loot_protocol import WorldLootDrop
from dso_server.gameplay.quest_content import load_quest_catalog
from dso_server.paths import DEFAULT_QUESTS_PATH


@lru_cache(maxsize=8192)
def _monster_kill_score(static_database_path: str, template_id: str) -> int:
    """Return the static score granted when a monster template is killed."""

    try:
        with sqlite3.connect(f"{static_database_path}?mode=ro", uri=True) as database:
            row = database.execute(
                "SELECT AddScoreForKill FROM _Template_Monster WHERE Id=? LIMIT 1",
                (template_id,),
            ).fetchone()
    except (OSError, sqlite3.Error) as exc:
        _log(
            f"kill-score lookup failed for {template_id!r}: "
            f"{type(exc).__name__}: {exc}"
        )
        return 0
    if row is None:
        _log(f"kill-score template {template_id!r} is missing from static data")
        return 0
    return max(0, int(row[0] or 0))


MAP_EVENT_LOG_PREFIX = f"[{GAMEPLAY_SERVICE_NAME}] map-event"
# How far a gated spawn transform may be moved to reach walkable ground.
EVENT_SPAWN_SNAP_DISTANCE = 4.0
# Authored rows whose template already stands within this distance are the
# same actor, materialized by the initial population (boss rooms bypass the
# event gate so the boss exists on entry).
EVENT_SPAWN_DUPLICATE_RADIUS = 1.5
_FORCE_ANNOUNCE_EVENT_ACTORS = frozenset({
    "a0001_champion_undead_mage_01",
    "a0005_champion_grimmag_01_q_waytogrimmagstone_03",
})
_TUTORIAL_CULTIST_TEMPLATE = "a0001_champion_undead_mage_01"
_BLOOD_CHEST_GEM_TEMPLATES = (
    "gem_amethyst_e", "gem_cyanit_e", "gem_diamond_e",
    "gem_onyx_e", "gem_ruby_e",
)
_BLOOD_CHEST_LOOT_LIST = "chest_lootlist_bloodshed"


def _log(message: str) -> None:
    print(f"{MAP_EVENT_LOG_PREFIX} {message}")


def _prop_info_entry(
    state, *, game_tick: int = 0, operation: str | None = None,
) -> PropInfoEntry:
    """One PropInfo record in the retail shape (docs/PROP_AND_TRIGGER_COMMANDS.md §2).

    ``operation`` is the quest switch id the viewer sees the prop under
    (``""`` for a plain prop); by default the definition's own switch id.
    """

    definition = state.definition
    if operation is None:
        operation = definition.quest_switch_id
    indication = 0xFF
    if operation:
        catalog = load_quest_catalog(DEFAULT_QUESTS_PATH)
        binding = next((b for b in catalog.switches_for_prop(definition.instance_id)
                        if b.switch.switch_id == operation), None)
        quest = catalog.quest(binding.quest_id) if binding else None
        indication = quest.quest_type_id if quest else 6
    return PropInfoEntry(
        prop_id=definition.prop_id,
        template_id=definition.template_id,
        instance_guid=definition.instance_guid,
        field_20=0xFFFFFFFF,
        field_24=int(state.state) & 0xFF,
        field_28=int(game_tick) & 0xFFFFFFFF,
        position=definition.position,
        rotation=definition.rotation,
        # Quest targets carry their switch id and indication 6, as retail's
        # tutorial props did (docs/QUEST_PROTOCOL.md §8).
        field_48=operation or WIRE_PROP_OPERATION,
        field_50="",
        field_58=0,
        field_5c=indication,
        field_60=0xFFFFFFFF,
    )


def _session_game_tick(s: Session) -> int:
    return int(getattr(s, "gameplay_chase_server_tick", 0) or 0) & 0xFFFFFFFF


def _send_prop_info(viewer: Session, states, *, label: str) -> None:
    """Retail creates and refreshes mid-map props with PropInfo, never NewProp.

    Quest-controlled props are filtered per viewer (services/quests.py).
    """

    personalized = tuple(_prop_state_for_viewer(viewer, state) for state in states)
    _send_prop_info_entries(
        viewer, CharacterService._quest_prop_entries(viewer, personalized), label=label,
    )


def _prop_state_for_viewer(viewer: Session, state: PropRuntimeState) -> PropRuntimeState:
    """Return this player's view of an individually used prop."""

    definition = state.definition
    if not definition.individual_usage:
        return state
    runtime = getattr(viewer, "gameplay_map_event_runtime", None)
    if runtime is None:
        return state
    key = (
        _individual_prop_player_id(viewer), int(definition.prop_id),
    )
    with runtime.lock:
        used = key in runtime.individual_prop_usage
    return (
        PropRuntimeState(definition, PROP_STATE_USED)
        if used else state
    )


def _individual_prop_player_id(viewer: Session) -> int:
    """Stable per-character key, including across a reconnect to the instance."""

    character_id = getattr(viewer, "selected_character_id", None)
    if character_id is None:
        character = getattr(viewer, "gameplay_character", None)
        character_id = getattr(character, "character_id", None)
    if character_id is None:
        character_id = getattr(viewer, "gameplay_remote_actor_id", 0)
    if character_id:
        return int(character_id)
    return id(viewer)


def _send_prop_info_entries(viewer: Session, pairs, *, label: str) -> None:
    """Send ``(state, operation)`` pairs as PropInfo records, 16 per command."""

    tick = _session_game_tick(viewer)
    entries = tuple(
        _prop_info_entry(state, game_tick=tick, operation=operation)
        for state, operation in pairs
    )
    for start in range(0, len(entries), 16):
        chunk = entries[start:start + 16]
        vicinity_packet, _ = encode_actors_enter_vicinity_batch(
            ActorsEnterVicinityBatch(
                ActorsEnterVicinity(tuple(entry.prop_id for entry in chunk)),
                actor_id=0,
                commands=(),
            )
        )
        CharacterService._send_world_loot_state_batch(
            viewer, vicinity_packet, len(vicinity_packet) * 8,
            label=f"{label}-actor-vicinity",
        )
        packet, packet_bits = encode_server_command_batch(
            (
                ServerCommand(
                    PROP_INFO_COMMAND,
                    PropInfo(chunk),
                    LOCAL_PLAYER_ACTOR_ID,
                ),
            ),
            include_terminator=True,
        )
        CharacterService._send_world_loot_state_batch(
            viewer, packet, packet_bits, label=label
        )


class MapEventService:
    """Signal evaluation and event-set transitions for one map instance."""

    # ------------------------------------------------------------------
    # Runtime registry
    # ------------------------------------------------------------------

    def _map_event_registry(self) -> dict:
        registry = getattr(self, "gameplay_map_event_runtimes", None)
        if registry is None:
            registry = {}
            self.gameplay_map_event_runtimes = registry
        return registry

    def _attach_map_event_runtime(
            self, s: Session, *, map_id: str, initial_flags,
            monster_level, scaling_difficulty: int,
            fresh_instance: bool) -> MapEventRuntime | None:
        """Join the instance's runtime, or start one for a new instance."""

        try:
            content = load_map_event_content(self.static_database_path, map_id)
            catalog = load_map_signal_catalog(self.static_database_path)
        except Exception as exc:
            _log(
                f"content unavailable for {map_id!r}: "
                f"{type(exc).__name__}: {exc}"
            )
            with s.flow_lock:
                s.gameplay_map_event_runtime = None
                s.gameplay_trigger_volumes_inside = set()
                s.gameplay_trigger_volumes_last_position = None
            s.gameplay_quest_props_visible = set()
            return None
        key = CharacterService._gameplay_instance_key(s)
        if getattr(s, "gameplay_fractured_planes_run", False):
            key = (*key, "fracturedplanes")
        flags = active_local_map_event_flags(map_id, frozenset(initial_flags))
        with self.gameplay_world_lock:
            registry = self._map_event_registry()
            runtime = registry.get(key)
            if runtime is None or fresh_instance:
                runtime = MapEventRuntime(
                    content,
                    catalog,
                    instance_key=key,
                    initial_flags=flags,
                    monster_level=monster_level,
                    scaling_difficulty=int(scaling_difficulty),
                )
                runtime.scheduler = s.gameplay_deadline_scheduler
                registry[key] = runtime
        with s.flow_lock:
            s.gameplay_map_event_runtime = runtime
            s.gameplay_trigger_volumes_inside = set()
            s.gameplay_trigger_volumes_last_position = None
            s.gameplay_quest_props_visible = set()
        return runtime

    def _map_event_runtime_is_live(self, runtime: MapEventRuntime) -> bool:
        with self.gameplay_world_lock:
            return self._map_event_registry().get(runtime.instance_key) is runtime

    def _map_event_sessions(self, runtime: MapEventRuntime) -> tuple[Session, ...]:
        """Every initialized session that shares ``runtime``."""

        return tuple(
            session
            for session in self._map_session_candidates(runtime.map_id)
            if (
                session.connected
                and session.gameplay_mode
                and session.gameplay_initialization_sent
                and session.gameplay_presence_ready
                and not session.gameplay_presence_departed
                and getattr(session, "gameplay_map_event_runtime", None) is runtime
            )
        )

    @staticmethod
    def _map_event_prop_info(s: Session) -> PropInfo | None:
        """The instance's published props, in the PropInfo shape."""

        runtime = getattr(s, "gameplay_map_event_runtime", None)
        if runtime is None:
            return None
        with runtime.lock:
            states = runtime.published_props()
        states = tuple(_prop_state_for_viewer(s, state) for state in states)
        tick = _session_game_tick(s)
        return PropInfo(tuple(
            _prop_info_entry(state, game_tick=tick, operation=operation)
            for state, operation
            in CharacterService._quest_prop_entries(s, states)
        ))

    @staticmethod
    def _running_map_event_entries(s: Session) -> tuple[EventUpdateEntry, ...]:
        """EventUpdate entries for the instance's running map-local events."""

        runtime = getattr(s, "gameplay_map_event_runtime", None)
        if runtime is None:
            return ()
        with runtime.lock:
            definitions = runtime.running_definitions()
        # Retail sends every map-local entry with event id 0; the client
        # matches map-local events by name.
        return tuple(
            EventUpdateEntry(
                event_id=0,
                event_name=definition.event_id,
                flags=(True, True, False, False),
                starts_at=None,
                ends_at=None,
                tags=(),
            )
            for definition in definitions
        )

    @staticmethod
    def _map_event_encounter_fallback(static_database_path, map_id: str):
        """A player-only encounter for maps whose monsters all come from events."""

        try:
            content = load_map_event_content(static_database_path, map_id)
        except Exception:
            return None
        if not content.spawn_rows:
            return None
        return load_local_player_only_encounter(static_database_path, map_id)

    # ------------------------------------------------------------------
    # Signal evaluation
    # ------------------------------------------------------------------

    def _fire_map_signal(self, s: Session, event_type: str, params) -> None:
        """Evaluate every server signal rule of ``event_type`` for ``s``."""

        runtime = getattr(s, "gameplay_map_event_runtime", None)
        if runtime is None:
            return
        self._flush_due_map_event_starts(runtime)
        kind = str(event_type).casefold()
        character = s.gameplay_character
        class_name = (
            CLASS_NAMES.get(int(getattr(character, "class_id", -1)), "")
            if character is not None else ""
        )
        player_level = max(1, int(getattr(character, "level", 1) or 1))
        present_templates: frozenset[str] = frozenset()
        alive_templates: frozenset[str] = frozenset()
        dead_fraction = 0.0
        if kind in ("monsterseliminated", "monsterseliminatedpercentage",
                    "playerkilledmonster"):
            combat = s.gameplay_combat_state
            if combat is not None:
                with combat.lock:
                    present, alive = combat.monster_template_counts()
                present_templates = frozenset(present)
                alive_templates = frozenset(alive)
                total = sum(present.values())
                if total:
                    dead_fraction = 1.0 - sum(alive.values()) / total
        with runtime.lock:
            running = runtime.active_flags()
        quests_accepted, quests_solved, tasks_done = (
            CharacterService._quest_signal_facts(self, s)
        )
        if kind == "playerhittrigger":
            CharacterService._quest_on_triggers(self, s, params)
        with s.flow_lock:
            unlocked_maps = frozenset(s.gameplay_unlocked_maps)
        context = SignalContext(
            map_id=runtime.map_id,
            params=frozenset(str(value).casefold() for value in params),
            running_events=running,
            player_level=player_level,
            class_name=class_name,
            player_count=max(1, len(self._map_event_sessions(runtime))),
            present_templates=present_templates,
            alive_templates=alive_templates,
            dead_fraction=dead_fraction,
            quests_accepted=quests_accepted,
            quests_solved=quests_solved,
            tasks_done=tasks_done,
            unlocked_maps=unlocked_maps,
        )
        rules = runtime.catalog.evaluate(kind, context)
        if not rules:
            return
        for rule in rules:
            if rule.event_type in (
                "monsterseliminatedpercentage",
                "playerkillmonstergetscore",
            ):
                with runtime.lock:
                    if rule.signal_id in runtime.fired_once:
                        continue
                    runtime.fired_once.add(rule.signal_id)
            _log(
                f"{event_type} {sorted(context.params)!r} on "
                f"{runtime.map_id} fired signal {rule.signal_id!r}"
            )
            self._emit_map_signal(s, runtime, rule.signal_id)

    def _emit_map_signal(self, s: Session, runtime: MapEventRuntime,
                         signal_id: str) -> None:
        catalog = runtime.catalog
        key = str(signal_id).casefold()
        with runtime.lock:
            runtime.signal_log.append((time.time(), key))
        for event_key in catalog.events_by_end_signal.get(key, ()):
            definition = catalog.event(event_key)
            if definition is not None and definition.applies_to(runtime.map_id):
                self._stop_map_local_event(runtime, definition)
        for event_key in catalog.events_by_start_signal.get(key, ()):
            definition = catalog.event(event_key)
            if definition is not None and definition.applies_to(runtime.map_id):
                self._schedule_map_local_event_start(runtime, definition)
        CharacterService._quest_on_signal(self, s, signal_id)

    def _on_monsters_killed(self, s: Session, monsters) -> None:
        templates = tuple(dict.fromkeys(
            str(getattr(monster, "template_id", "")).casefold()
            for monster in monsters
            if str(getattr(monster, "template_id", ""))
        ))
        if not templates:
            return
        CharacterService._quest_on_monsters_killed(self, s, monsters)
        CharacterService._achievement_on_monsters_killed(self, s, monsters)
        CharacterService._on_map_companion_boss_defeated(self, s, monsters)
        runtime = getattr(s, "gameplay_map_event_runtime", None)
        if runtime is not None:
            score_rules = tuple(
                rule
                for rule in runtime.catalog.rules_by_event_type.get(
                    "playerkillmonstergetscore", ()
                )
                if not rule.maps or map_key(runtime.map_id) in rule.maps
            )
            if score_rules:
                static_path = Path(self.static_database_path).resolve().as_uri()
                scored_templates = tuple(
                    (
                        str(getattr(monster, "template_id", "")).casefold(),
                        _monster_kill_score(
                            static_path,
                            str(getattr(monster, "template_id", "")).casefold(),
                        ),
                    )
                    for monster in monsters
                    if str(getattr(monster, "template_id", ""))
                )
                score_gained = sum(score for _template, score in scored_templates)
                with runtime.lock:
                    runtime.kill_score += score_gained
                    current_score = runtime.kill_score
                thresholds = tuple(
                    int(value)
                    for rule in score_rules
                    for value in rule.params
                    if str(value).isdigit()
                )
                next_threshold = min(
                    (value for value in thresholds if value > current_score),
                    default=None,
                )
                target_text = (
                    str(next_threshold) if next_threshold is not None
                    else ",".join(map(str, sorted(set(thresholds))))
                )
                _log(
                    f"kill-score progress on {runtime.map_id}: "
                    f"gained={score_gained}, total={current_score}, "
                    f"next={target_text}, monsters={scored_templates!r}"
                )
                if score_gained:
                    self._fire_map_signal(
                        s, "PlayerKillMonsterGetScore", (str(current_score),)
                    )
                # Fractured Planes reuses Endless Tower's authored score gate.
                # Some dynamically selected tower monsters have no
                # AddScoreForKill value, so a fully cleared first floor can
                # otherwise remain below the authored 315 point threshold
                # forever. If every currently materialized monster is dead
                # and no delayed map event is still spawning the next wave,
                # resolve that floor's score gate as a completed clear.
                if (
                    getattr(s, "gameplay_fractured_planes_run", False)
                    and map_key(runtime.map_id)
                    == "h0009_endlesstower_01_sm_01"
                ):
                    combat = s.gameplay_combat_state
                    if combat is not None:
                        with combat.lock:
                            present, alive = combat.monster_template_counts()
                        fully_cleared = bool(present) and not alive
                    else:
                        fully_cleared = False
                    if fully_cleared:
                        with runtime.lock:
                            has_pending_spawns = bool(runtime.pending_start_tokens)
                            clear_rules = tuple(
                                rule for rule in score_rules
                                if rule.signal_id not in runtime.fired_once
                            )
                            if not has_pending_spawns:
                                runtime.fired_once.update(
                                    rule.signal_id for rule in clear_rules
                                )
                        if not has_pending_spawns:
                            for rule in clear_rules:
                                _log(
                                    f"Fractured Planes floor clear on "
                                    f"{runtime.map_id}: resolving "
                                    f"{rule.signal_id!r} after all spawned "
                                    f"monsters were defeated"
                                )
                                self._emit_map_signal(
                                    s, runtime, rule.signal_id
                                )
        self._fire_map_signal(s, "PlayerKilledMonster", templates)
        self._fire_map_signal(s, "MonstersEliminated", templates)
        self._fire_map_signal(s, "MonstersEliminatedPercentage", ())

    @staticmethod
    def _notify_monsters_killed(s: Session, monsters) -> None:
        """Raise the kill toward the server from static combat code."""

        callback = getattr(s, "gameplay_presence_callback", None)
        if callback is None or not monsters:
            return
        try:
            callback(s, ("monsters_killed", tuple(monsters)))
        except Exception as exc:
            _log(f"kill notification failed: {type(exc).__name__}: {exc}")

    def _restore_map_unlocks(self, s: Session, persisted_state) -> None:
        from dso_server.gameplay.map_unlocks import restored_map_unlocks, saved_quest_map_unlocks
        s.gameplay_unlocked_maps = restored_map_unlocks(persisted_state, (
            s.gameplay_map, s.gameplay_map_access_id, s.gameplay_map_access_group_id,
        )) | saved_quest_map_unlocks(self.static_database_path, persisted_state)
        s.gameplay_admin_all_maps = bool(getattr(persisted_state, "admin_all_maps", False))

    def _on_map_entered(self, s: Session) -> None:
        with s.flow_lock:
            discoveries = {
                map_key(value) for value in (
                    s.gameplay_map, s.gameplay_map_access_id,
                    s.gameplay_map_access_group_id,
                ) if value
            }
            newly_discovered = discoveries - s.gameplay_unlocked_maps
            s.gameplay_unlocked_maps.update(discoveries)
        if newly_discovered:
            CharacterService._request_account_save(s, "map discovered")
        runtime = getattr(s, "gameplay_map_event_runtime", None)
        if runtime is None:
            return
        if getattr(s, "gameplay_fractured_planes_run", False):
            with runtime.lock:
                if runtime.fractured_planes_limit_deadline is None:
                    runtime.fractured_planes_limit_deadline = (
                        getattr(
                            s, "gameplay_fractured_planes_deadline", None
                        )
                        or time.perf_counter() + 8 * 60
                    )
                s.gameplay_fractured_planes_deadline = (
                    runtime.fractured_planes_limit_deadline
                )
                seconds_left = max(
                    0,
                    math.ceil(
                        runtime.fractured_planes_limit_deadline
                        - time.perf_counter()
                    ),
                )
            packet, packet_bits = encode_server_command_batch((
                ServerCommand(
                    KICK_OUT_TO_HUB_LIMIT_TIME_LEFT_COMMAND,
                    KickOutToHubLimitTimeLeft(seconds_left),
                    1,
                ),
            ))
            CharacterService._send_world_loot_state_batch(
                s, packet, packet_bits, label="fractured-planes-limit-time"
            )
            self._schedule_fractured_planes_limit_tick(runtime)
        access_group = str(getattr(s, "gameplay_map_access_group_id", "") or "")
        access_id = str(getattr(s, "gameplay_map_access_id", "") or "")
        self._fire_map_signal(
            s, "PlayerEnteredMap", (runtime.map_id, access_group, access_id)
        )
        self._fire_map_signal(
            s, "PlayerEnteredMapId", (access_group, access_id, runtime.map_id)
        )
        CharacterService._quest_on_map_entered(self, s)
        CharacterService._achievement_on_map_entered(
            self, s, (runtime.map_id, access_group, access_id),
        )
        CharacterService._restore_map_companion_handoffs(self, s)
        if s.gameplay_last_move is not None:
            CharacterService._check_map_companions(s, s.gameplay_last_move.relative_position)

    # ------------------------------------------------------------------
    # Client-reported signals
    # ------------------------------------------------------------------

    def _spawn_map_chest_loot(self, s: Session, definition) -> int:
        """Materialize the supported Bloodshed chest rewards as world loot.

        The client export names CHEST_lootlist_BLOODSHED but does not include
        the server-side loot-list rows. Keep this fallback deliberately small
        and use item templates that are present in the shipped static DB.
        """

        loot_list_id = str(definition.loot_list_id or definition.chest_id).casefold()
        if loot_list_id != _BLOOD_CHEST_LOOT_LIST:
            _log(
                f"no local loot table for chest {definition.instance_id!r} "
                f"({loot_list_id!r}); no rewards spawned"
            )
            return 0
        try:
            specifications = [("money_rc", 10)]
            gem_count = random.SystemRandom().randint(4, 5)
            specifications.extend(
                (random.SystemRandom().choice(_BLOOD_CHEST_GEM_TEMPLATES), 1)
                for _ in range(gem_count)
            )
            drops = []
            for template_id, amount in specifications:
                item_definition = load_purchase_item_definition(
                    self.static_database_path, template_id,
                )
                item = _new_item(item_definition, 0, amount)
                item.field_100 = tuple(float(value) for value in definition.position)
                auto_pick_up = "All" if item_definition.item_category == "Currency" else "None"
                drops.append(WorldLootDrop(
                    item, item_definition.item_category, auto_pick_up,
                ))
        except (OSError, ValueError, sqlite3.Error) as exc:
            _log(
                f"cannot build rewards for chest {definition.instance_id!r}: "
                f"{type(exc).__name__}: {exc}"
            )
            return 0
        instance_key = CharacterService._gameplay_instance_key(s)
        map_id = str(s.gameplay_map or "")
        with self.gameplay_world_lock:
            counters = getattr(self, "gameplay_world_next_item_ids", None)
            if counters is None:
                counters = self.gameplay_world_next_item_ids = {}
            with s.flow_lock:
                first = max(
                    int(getattr(s, "gameplay_next_item_id", RUNTIME_ITEM_ID_BASE)),
                    int(counters.get(instance_key, RUNTIME_ITEM_ID_BASE)),
                )
            end = first + len(drops)
            counters[instance_key] = end
            counters[map_id] = max(end, int(counters.get(map_id, RUNTIME_ITEM_ID_BASE)))
            with s.flow_lock:
                for offset, drop in enumerate(drops):
                    item_id = first + offset
                    drop.item.item_id = item_id
                    drop.item.field_18 = item_id & 0xFFFF
                    s.gameplay_world_loot[item_id] = drop
                s.gameplay_next_item_id = max(
                    int(getattr(s, "gameplay_next_item_id", RUNTIME_ITEM_ID_BASE)),
                    end,
                )
        CharacterService._reconcile_world_loot_visibility(
            s, label="map-chest-loot",
        )
        _log(
            f"spawned {len(drops)} reward item(s) from "
            f"{definition.instance_id!r} for player session"
        )
        return len(drops)

    def _handle_staging_signal(self, s: Session, payload: bytes) -> None:
        CharacterService._handle_simple_gameplay_command(s, payload, 0x0113)
        decoded = s.gameplay_client_command_state.get(0x0113)
        signal_id = str(getattr(decoded, "signal_id", "") or "")
        runtime = getattr(s, "gameplay_map_event_runtime", None)
        if runtime is None or not signal_id:
            return
        rule = runtime.catalog.rule(signal_id)
        if rule is None or not rule.enabled:
            return
        if rule.signal_type not in CLIENT_REPORTED_SIGNAL_TYPES:
            _log(
                f"ignored client report of server signal {signal_id!r}"
            )
            return
        _log(f"client reported signal {rule.signal_id!r} on {runtime.map_id}")
        self._emit_map_signal(s, runtime, rule.signal_id)

    def _handle_quest_trigger_signal(self, s: Session, payload: bytes) -> None:
        request = decode_quest_trigger_signal(
            payload, getattr(s, "current_message_bit_length", None)
        )
        with s.flow_lock:
            s.gameplay_client_command_state[QUEST_TRIGGER_SIGNAL_COMMAND] = request
        CharacterService._accept_gameplay_state_command(
            s, QUEST_TRIGGER_SIGNAL_COMMAND
        )
        _log(f"client reported trigger {request.trigger_id!r}")
        self._fire_map_signal(s, "PlayerHitTrigger", (request.trigger_id,))

    @staticmethod
    def _check_trigger_volumes(s: Session, wire_position) -> None:
        """Detect trigger-volume entry from the player's own movement."""

        runtime = getattr(s, "gameplay_map_event_runtime", None)
        if runtime is None or not runtime.content.trigger_volumes:
            return
        frame = getattr(s, "gameplay_coordinate_frame", None)
        if frame is None:
            combat = getattr(s, "gameplay_combat_state", None)
            if combat is None:
                return
            frame = combat.encounter.coordinate_frame
        world = frame.wire_to_world(wire_position)
        with runtime.lock:
            volumes = runtime.active_trigger_volumes()
        inside = {
            volume.row_id for volume in volumes if volume.contains(world)
        }
        with s.flow_lock:
            previous = set(getattr(s, "gameplay_trigger_volumes_inside", ()))
            previous_position = getattr(
                s, "gameplay_trigger_volumes_last_position", None
            )
            if previous_position is None:
                # The first movement packet can already be beyond a trigger
                # volume (especially after a map transfer). Sweep the path
                # from the authored entry position so that the initial
                # tutorial and other entry triggers cannot be skipped.
                previous_position = getattr(s, "gameplay_spawn_position", None)
            s.gameplay_trigger_volumes_inside = inside
            s.gameplay_trigger_volumes_last_position = world
        entered = inside - previous
        if previous_position is not None:
            entered.update(
                volume.row_id
                for volume in volumes
                if volume.row_id not in previous
                and volume.intersects_segment(previous_position, world)
            )
        if not entered:
            return
        # Both the volume id (server signal rules) and its TriggeredEventId
        # (quest explore tasks) are raised.
        trigger_ids = tuple(dict.fromkeys(
            value
            for volume in volumes if volume.row_id in entered
            for value in (volume.trigger_id, volume.triggered_event_id)
            if value
        ))
        _log(f"player entered trigger volume(s) {trigger_ids!r}")
        callback = getattr(s, "gameplay_presence_callback", None)
        if callback is not None:
            callback(s, ("map_signal", "PlayerHitTrigger", trigger_ids))

    def _handle_prop_interaction(self, s: Session, payload: bytes) -> None:
        request = decode_prop_interaction(
            payload, getattr(s, "current_message_bit_length", None)
        )
        with s.flow_lock:
            s.gameplay_client_command_state[PROP_INTERACTION_COMMAND] = request
        CharacterService._accept_gameplay_state_command(
            s, PROP_INTERACTION_COMMAND
        )
        runtime = getattr(s, "gameplay_map_event_runtime", None)
        if runtime is None:
            _log(f"PropInteraction {request!r} without a map runtime")
            return
        with runtime.lock:
            existing = runtime.props.get(int(request.prop_id))
            definition = existing.definition if existing is not None else None
            if definition is not None and definition.individual_usage:
                usage_key = (
                    _individual_prop_player_id(s), int(definition.prop_id),
                )
                if (
                    existing.state == PROP_STATE_ACTIVE
                    and usage_key not in runtime.individual_prop_usage
                ):
                    runtime.individual_prop_usage.add(usage_key)
                    result = (
                        PropRuntimeState(definition, PROP_STATE_USED), True,
                    )
                else:
                    result = (
                        PropRuntimeState(definition, PROP_STATE_USED), False,
                    )
            else:
                result = runtime.interact(request.prop_id)
        if result is None:
            _log(
                f"PropInteraction for unknown prop {request.prop_id} "
                f"({request.operation!r}) on {runtime.map_id}"
            )
            return
        state, flipped = result
        definition = state.definition
        if CharacterService._quest_prop_operation(s, definition) is None:
            _log(
                f"PropInteraction on quest prop {definition.instance_id!r} "
                "that is not live for this player; ignored"
            )
            return
        _log(
            f"prop {definition.instance_id!r} (id {definition.prop_id}, "
            f"operation {request.operation!r}) used; state now {state.state}"
            f"{' (changed)' if flipped else ''}"
        )
        # Retail's reply to a click (docs/PROP_AND_TRIGGER_COMMANDS.md §6):
        # the clicking player's actor in the body, the new state, the game
        # tick, every flag false, trailing actor 0, then a one-record
        # PropInfo refreshing the same prop.
        remote_actor_id = int(getattr(s, "gameplay_remote_actor_id", 0) or 0)
        viewers = (
            (s,) if definition.individual_usage
            else self._map_event_sessions(runtime) or (s,)
        )
        for viewer in viewers:
            if CharacterService._quest_prop_operation(viewer, definition) is None:
                continue
            update = PropUpdate(
                prop_id=definition.prop_id,
                state=int(state.state),
                actor_id=LOCAL_PLAYER_ACTOR_ID if viewer is s else remote_actor_id,
                counter=_session_game_tick(viewer),
                state_changed=False,
                operation=WIRE_PROP_OPERATION,
                task_index=0,
                keep_visual_state=False,
            )
            packet, packet_bits = encode_prop_update_batch((update,), actor_id=0)
            viewer.send_reliable_ordered(
                (packet, packet_bits), track=True, label="prop-update"
            )
            _send_prop_info(viewer, (state,), label="prop-update-info")
        if flipped and (definition.loot_list_id or definition.chest_id):
            self._spawn_map_chest_loot(s, definition)
        if state.state == PROP_STATE_ACTIVE or flipped:
            CharacterService._quest_on_prop_interacted(
                self, s, definition.instance_id,
                request.operation or definition.quest_switch_id,
            )
            self._fire_map_signal(
                s, "PlayerInteractedProp",
                (definition.instance_id, definition.template_id),
            )
            CharacterService._achievement_on_prop_interacted(
                self, s, definition.instance_id, definition.template_id,
            )

    # ------------------------------------------------------------------
    # Event transitions
    # ------------------------------------------------------------------

    def _schedule_map_local_event_start(
            self, runtime: MapEventRuntime,
            definition: MapLocalEventDefinition) -> None:
        wave = definition.wave_label is not None
        delay = max(5.0, definition.start_delay) if wave else definition.start_delay
        if delay <= 0.0:
            self._start_map_local_event(runtime, definition)
            return
        scheduler = getattr(runtime, "scheduler", None)
        if scheduler is None:
            self._start_map_local_event(runtime, definition)
            return
        with runtime.lock:
            if definition.key in runtime.running or definition.key in runtime.pending:
                return
            token = object()
            deadline = time.perf_counter() + delay
            handle = scheduler.timer(
                delay,
                self._run_delayed_map_event_start,
                args=(runtime, definition, token),
            )
            handle.absolute_deadline = deadline
            runtime.pending[definition.key] = handle
            runtime.pending_start_deadlines[definition.key] = deadline
            runtime.pending_start_tokens[definition.key] = token
        _log(
            f"{definition.event_id} starts on {runtime.map_id} in "
            f"{delay:.1f}s"
        )
        handle.start()
        if wave:
            for seconds in range(5, 0, -1):
                remaining = deadline - seconds - time.perf_counter()
                if remaining <= 0:
                    self._run_wave_countdown(runtime, definition, token)
                else:
                    countdown = scheduler.timer(
                        remaining, self._run_wave_countdown,
                        args=(runtime, definition, token),
                    )
                    countdown.absolute_deadline = deadline - seconds
                    countdown.start()

    def _run_wave_countdown(self, runtime, definition, token) -> None:
        if not self._map_event_runtime_is_live(runtime):
            return
        with runtime.lock:
            if runtime.pending_start_tokens.get(definition.key) is not token:
                return
            # Read the real deadline so a delayed callback cannot replay old
            # digits or announce a wave that was cancelled and rescheduled.
            seconds = math.ceil(
                runtime.pending_start_deadlines[definition.key] - time.perf_counter()
            )
            if not 1 <= seconds <= 5:
                return
            if runtime.wave_countdown_digits.get(definition.key) == seconds:
                return
            runtime.wave_countdown_digits[definition.key] = seconds
        # The client's native countdown (0x1405e5c7c) displays this integer
        # without changing match state. No zero/"Go" PvP message is sent.
        self._broadcast_wave_message(
            runtime, encode_message_batch(
                Message(5, (MessageArgument(1, seconds),)), actor_id=0,
            ), label="map-wave-countdown",
        )

    def _schedule_fractured_planes_limit_tick(self, runtime) -> None:
        with runtime.lock:
            deadline = runtime.fractured_planes_limit_deadline
            scheduler = runtime.scheduler
            if (
                deadline is None
                or scheduler is None
                or runtime.fractured_planes_limit_timer is not None
            ):
                return
            delay = min(1.0, max(0.0, deadline - time.perf_counter()))
            handle = scheduler.timer(
                delay,
                self._run_fractured_planes_limit_tick,
                args=(runtime,),
            )
            handle.absolute_deadline = time.perf_counter() + delay
            runtime.fractured_planes_limit_timer = handle
        handle.start()

    def _run_fractured_planes_limit_tick(self, runtime) -> None:
        with runtime.lock:
            runtime.fractured_planes_limit_timer = None
            deadline = runtime.fractured_planes_limit_deadline
            if deadline is None:
                return
            seconds_left = max(0, math.ceil(deadline - time.perf_counter()))
        packet, packet_bits = encode_server_command_batch((
            ServerCommand(
                KICK_OUT_TO_HUB_LIMIT_TIME_LEFT_COMMAND,
                KickOutToHubLimitTimeLeft(seconds_left),
                1,
            ),
        ))
        for viewer in self._map_event_sessions(runtime):
            try:
                CharacterService._send_world_loot_state_batch(
                    viewer, packet, packet_bits,
                    label="fractured-planes-limit-time",
                )
            except Exception as exc:
                _log(
                    "fractured-planes limit timer send failed: "
                    f"{type(exc).__name__}: {exc}"
                )
        if seconds_left:
            self._schedule_fractured_planes_limit_tick(runtime)

    def _broadcast_wave_message(self, runtime, packet, *, label) -> None:
        for viewer in self._map_event_sessions(runtime):
            try:
                CharacterService._send_world_loot_state_batch(
                    viewer, *packet, label=label,
                )
            except Exception as exc:
                _log(f"{label} send failed: {type(exc).__name__}: {exc}")

    def _flush_due_map_event_starts(self, runtime: MapEventRuntime) -> None:
        # Reward conditions must see elapsed deadlines even if a scheduler is
        # busy or its original session disconnected. Never restart a timer for
        # a late joiner to an existing dungeon instance.
        now = time.perf_counter()
        with runtime.lock:
            due = tuple((key, runtime.pending_start_tokens.get(key))
                        for key, deadline in runtime.pending_start_deadlines.items()
                        if deadline <= now)
        for key, token in due:
            definition = runtime.catalog.event(key)
            if definition is not None:
                self._start_map_local_event(runtime, definition, pending_token=token)

    def _run_delayed_map_event_start(
            self, runtime: MapEventRuntime,
            definition: MapLocalEventDefinition, token: object) -> None:
        if not self._map_event_runtime_is_live(runtime):
            return
        try:
            self._start_map_local_event(runtime, definition, pending_token=token)
        except Exception as exc:
            _log(
                f"delayed start of {definition.event_id} failed: "
                f"{type(exc).__name__}: {exc}"
            )

    def _run_map_local_event_stop(
            self, runtime: MapEventRuntime,
            definition: MapLocalEventDefinition) -> None:
        if not self._map_event_runtime_is_live(runtime):
            return
        try:
            self._stop_map_local_event(runtime, definition)
        except Exception as exc:
            _log(
                f"timed stop of {definition.event_id} failed: "
                f"{type(exc).__name__}: {exc}"
            )

    def _start_map_local_event(
            self, runtime: MapEventRuntime,
            definition: MapLocalEventDefinition, *, pending_token=None) -> None:
        with runtime.lock:
            if (pending_token is not None
                    and runtime.pending_start_tokens.get(definition.key) is not pending_token):
                return
            transition = runtime.begin_event(definition)
        if transition is None:
            return
        _log(
            f"{definition.event_id} started on {runtime.map_id}: "
            f"enabled sets {transition.enabled_sets}, "
            f"disabled sets {transition.disabled_sets}"
        )
        self._apply_map_event_transition(runtime, transition)
        if definition.wave_label is not None:
            self._broadcast_wave_message(
                runtime, encode_screen_message(definition.wave_label),
                label="map-wave-start",
            )
        scheduler = getattr(runtime, "scheduler", None)
        if definition.duration > 0.0 and scheduler is not None:
            handle = scheduler.timer(
                definition.duration,
                self._run_map_local_event_stop,
                args=(runtime, definition),
            )
            handle.start()

    def _stop_map_local_event(
            self, runtime: MapEventRuntime,
            definition: MapLocalEventDefinition) -> None:
        with runtime.lock:
            transition = runtime.end_event(definition)
        if transition is None:
            return
        _log(
            f"{definition.event_id} stopped on {runtime.map_id}: "
            f"enabled sets {transition.enabled_sets}, "
            f"disabled sets {transition.disabled_sets}"
        )
        self._apply_map_event_transition(runtime, transition)

    def _apply_map_event_transition(self, runtime: MapEventRuntime,
                                    transition) -> None:
        sessions = self._map_event_sessions(runtime)
        if transition.announce:
            self._broadcast_map_event_update(runtime, sessions, transition)
        if transition.disabled_sets:
            self._retire_event_monsters(runtime, sessions, transition.disabled_sets)
            self._unpublish_event_props(runtime, sessions, transition.disabled_sets)
        if transition.enabled_sets:
            self._publish_event_props(runtime, sessions, transition.enabled_sets)
            self._spawn_event_monsters(runtime, sessions, transition.enabled_sets)
            self._spawn_event_npcs(runtime, sessions, transition.enabled_sets)
        if transition.enabled_sets or transition.disabled_sets:
            self._publish_map_exits(runtime, sessions)

    # ------------------------------------------------------------------
    # Effects
    # ------------------------------------------------------------------

    def _broadcast_map_event_update(self, runtime, sessions, transition) -> None:
        definition = transition.event
        # One entry, event id 0, the map-local flag: the exact shape retail
        # used for BOSS_spawn/BOSS_start/BOSS_death mid-map.
        update = EventUpdate((
            EventUpdateEntry(
                event_id=0,
                event_name=definition.event_id,
                flags=(bool(transition.active), True, False, False),
                starts_at=None,
                ends_at=None,
                tags=(),
            ),
        ))
        packet, packet_bits = encode_event_update(update)
        for viewer in sessions:
            CharacterService._send_world_loot_state_batch(
                viewer, packet, packet_bits, label="map-event-update"
            )

    def _publish_event_props(self, runtime, sessions, event_sets) -> None:
        with runtime.lock:
            published = runtime.publish_props(
                runtime.select_props(runtime.content.props_for_sets(event_sets))
            )
        if not published:
            return
        _log(
            f"revealed {len(published)} prop(s) on {runtime.map_id}: "
            + ", ".join(sorted({
                state.definition.template_id for state in published
            })[:6])
        )
        for viewer in sessions:
            _send_prop_info(viewer, published, label="map-event-prop-info")

    def _unpublish_event_props(self, runtime, sessions, event_sets) -> None:
        with runtime.lock:
            prop_ids = tuple(
                prop_id for prop_id, state in runtime.props.items()
                if state.definition.event_set_row in set(event_sets)
            )
            removed = runtime.unpublish_props(prop_ids)
        if not removed:
            return
        _log(f"hid {len(removed)} prop(s) on {runtime.map_id}")
        for viewer in sessions:
            packet, packet_bits = encode_discard_prop_batch(
                removed, actor_id=LOCAL_PLAYER_ACTOR_ID
            )
            CharacterService._send_world_loot_state_batch(
                viewer, packet, packet_bits, label="map-event-discard-prop"
            )

    def _publish_map_exits(self, runtime, sessions) -> None:
        with runtime.lock:
            flags = runtime.active_flags()
        try:
            definitions = tuple(
                definition
                for definition in load_local_map_exits(
                    self.static_database_path,
                    runtime.map_id,
                    active_event_flags=flags,
                )
                if definition.active and definition.destination_map_id
            )
        except Exception as exc:
            _log(f"exit refresh failed: {type(exc).__name__}: {exc}")
            return
        exit_info = ExitInfo(tuple(
            ExitInfoEntry(
                definition.name,
                definition.destination_map_id,
                int(definition.active),
                definition.instance_guid,
            )
            for definition in definitions
        ))
        packet, packet_bits = encode_server_command_batch(
            (ServerCommand(EXIT_INFO_COMMAND, exit_info, LOCAL_PLAYER_ACTOR_ID),),
            include_terminator=True,
        )
        for viewer in sessions:
            with viewer.flow_lock:
                if viewer.gameplay_map != runtime.map_id:
                    continue
                viewer.gameplay_map_exits = definitions
            CharacterService._send_world_loot_state_batch(
                viewer, packet, packet_bits, label="map-event-exit-info"
            )
        _log(
            f"published {len(definitions)} exit(s) on {runtime.map_id} to "
            f"{len(sessions)} session(s)"
        )

    def _map_event_combat_state(self, sessions):
        for session in sessions:
            combat = getattr(session, "gameplay_combat_state", None)
            if combat is not None:
                return combat
        return None

    def _spawn_event_npcs(self, runtime, sessions, event_sets) -> None:
        """Create the NPCs of newly enabled event sets.

        Retail created the tutorial's spellweaver with ``NewNPC`` and its
        ``NPCInfo`` when the ``a0001_start_spellweaver`` map-local event
        started (tutorial capture, 16:05:23); the map initialization only
        carries NPCs whose event set is enabled by the global event flags.
        The NPCInfo is decorated per viewer with that player's quest offers.
        """

        wanted = {int(value) for value in event_sets}
        try:
            definitions = tuple(
                definition
                for definition in load_local_npc_definitions(
                    self.static_database_path, runtime.map_id,
                    include_inactive_event_sets=True,
                )
                if int(getattr(definition, "event_set_row", -1)) in wanted
            )
        except Exception as exc:
            _log(f"event NPCs unavailable on {runtime.map_id}: {type(exc).__name__}: {exc}")
            return
        if not definitions:
            return
        _log(
            f"revealed {len(definitions)} NPC(s) on {runtime.map_id}: "
            + ", ".join(sorted({d.template_id for d in definitions})[:6])
        )
        for viewer in sessions:
            with viewer.flow_lock:
                announced = set(viewer.gameplay_announced_npc_actor_ids)
                viewer.gameplay_announced_npc_actor_ids.update(
                    local_npc_actor_id(definition)
                    for definition in definitions
                )
            commands = []
            new_actor_ids = []
            for definition in definitions:
                actor_id = local_npc_actor_id(definition)
                # ActorManager::RegisterActor() rejects a duplicate id and
                # DrasaClientHandler then drops the rest of this batch, which
                # would take the quest offers with it.
                if actor_id not in announced:
                    new_actor_ids.append(actor_id)
                    commands.append(NewNPC(
                        template_id=definition.template_id,
                        instance_guid=definition.instance_guid,
                        placement=ActorPlacement(
                            LOCAL_NPC_ACTOR_BASE, actor_id, 1, False, False
                        ),
                        rotation=definition.rotation,
                        position=definition.position,
                        enabled=True,
                        actor_id=actor_id,
                    ))
                commands.append(CharacterService._quest_decorate_npc_info(
                    self, viewer,
                    local_npc_info(definition, actor_id=actor_id),
                    definition.template_id,
                ))
            # Register dynamic NPC ids in their own reliable-ordered frame.
            # Map-entry actors use this same ordering; keeping registration
            # inside the NewNPC batch can leave late-created actors visible
            # without a client interaction target.
            if new_actor_ids:
                vicinity_packet, vicinity_bits = encode_actors_enter_vicinity_batch(
                    ActorsEnterVicinityBatch(
                        ActorsEnterVicinity(tuple(new_actor_ids)),
                        actor_id=0,
                        commands=(),
                    )
                )
                viewer.send_reliable_ordered(
                    vicinity_packet,
                    track=True,
                    label="map-event-npc-actor-vicinity",
                )
            packet, packet_bits = encode_npc_batch(
                NPCBatch(tuple(commands)), include_actor_vicinity=False,
            )
            CharacterService._send_world_loot_state_batch(
                viewer, packet, packet_bits, label="map-event-new-npc"
            )

    def _spawn_event_monsters(self, runtime, sessions, event_sets) -> None:
        rows = runtime.gated_spawn_rows(event_sets)
        authored_rotations = {row.row_id: row.rotation for row in rows}
        # An EventSet keeps one row per difficulty variant of its boss (the
        # Heredur arena's BOSS_spawn set holds ten ``boss_heredur_scaling``
        # rows and the spawner).  The load-time population already picks one
        # phase through the boss policy; the event path must too, or every
        # variant walks in at once.
        selected = select_boss_spawn_rows(
            self.static_database_path,
            runtime.map_id,
            tuple(
                (row.row_id, row.position, row.template_id, row.event_set_row)
                for row in rows
            ),
            scaling_difficulty=runtime.scaling_difficulty,
        )
        rows = tuple(
            GatedSpawnRow(
                int(r[0]), int(r[3]), str(r[2]), tuple(r[1]),
                authored_rotations.get(int(r[0]), 0.0),
            )
            for r in selected
        )
        combat = self._map_event_combat_state(sessions)
        if combat is None:
            _log(
                f"{len(rows)} gated spawn row(s) on {runtime.map_id} but no "
                "combat state on the instance"
            )
            return
        encounter = combat.encounter
        database_path = encounter.static_database_path
        rows += event_area_spawn_rows(runtime, encounter, event_sets)
        # The authored cultist animset maps Death to cultist_steal_staff.
        # Its idle is start_idle; the staff theft uses its scripted despawn
        # transition, not with an extra Grimmag/China scene carrier.
        if not rows:
            return
        with combat.lock:
            pending = tuple(
                row for row in rows
                if not (set(combat.find_monsters(
                    row.template_id, row.position,
                    radius=EVENT_SPAWN_DUPLICATE_RADIUS,
                )) & set(combat.alive_actor_ids))
            )
            actor_ids = combat.allocate_runtime_actor_ids(len(pending))
        if not pending:
            # A map-local event can start from a trigger while the player's
            # first 0x85 initialization is still being assembled. In that
            # window there are no ready sessions to receive the spawn, but
            # the event transition still needs to materialize its actors in
            # the shared combat state. The initialization path calls this
            # method again once PlayerReady has been queued; replay the
            # already materialized event groups to that viewer then.
            with runtime.lock:
                event_actor_ids = set().union(*(
                    runtime.event_set_actor_ids.get(int(event_set), set())
                    for event_set in event_sets
                )) if event_sets else set()
            groups = []
            seen_groups = set()
            for actor_id in sorted(event_actor_ids):
                group = combat.runtime_group_for_actor(actor_id)
                if group is None or id(group) in seen_groups:
                    continue
                seen_groups.add(id(group))
                groups.append(group)
            for viewer in sessions:
                with viewer.flow_lock:
                    ready = viewer.gameplay_presence_ready
                if ready:
                    for group in groups:
                        self._announce_runtime_group(viewer, combat, group)
            _log(
                f"{len(rows)} gated spawn row(s) on {runtime.map_id} already "
                "materialized"
            )
            return
        monsters = []
        definitions = []
        spawned_rows = []
        for row, actor_id in zip(pending, actor_ids, strict=True):
            position = row.position
            scripted_cultist = (
                row.template_id.casefold() == _TUTORIAL_CULTIST_TEMPLATE
            )
            # The cinematic's staff, portal and root motion are authored
            # against this exact transform, not a navigable combat position.
            if not scripted_cultist:
                try:
                    position = _grounded_spawn_position(
                        encounter.navigation,
                        encounter.terrain,
                        position,
                        reference_position=position,
                        max_horizontal_distance=EVENT_SPAWN_SNAP_DISTANCE,
                    )
                except (NavigationError, CollisionTerrainError, ValueError):
                    position = tuple(float(value) for value in row.position)
            runtime_definition = _load_runtime_definition(
                database_path, row.template_id, actor_id
            )
            if runtime_definition is None:
                _log(f"no runtime template for {row.template_id!r}; skipped")
                continue
            try:
                monster = _load_monster(
                    database_path,
                    row.template_id,
                    actor_id,
                    position,
                    monster_level=runtime.monster_level,
                    scaling_difficulty=runtime.scaling_difficulty,
                )
            except ValueError as exc:
                _log(f"cannot load {row.template_id!r}: {exc}")
                continue
            if scripted_cultist:
                monster = replace(monster, field_a0=float(row.rotation))
                _log(
                    f"tutorial cultist authored placement: "
                    f"position={monster.position!r}, yaw={row.rotation:.6f}"
                )
            monsters.append(monster)
            definitions.append(runtime_definition)
            spawned_rows.append(row)
        if not monsters:
            return
        group = _spawn_group(database_path, tuple(monsters))
        ammo = combat.load_ammo_vulnerabilities(
            database_path, group.spawn_batch.commands
        )
        with combat.lock:
            added = combat.add_runtime_monsters(
                group, definitions, ammo_vulnerabilities=ammo
            )
        with runtime.lock:
            for row, monster in zip(spawned_rows, monsters, strict=True):
                runtime.event_set_actor_ids.setdefault(
                    row.event_set_row, set()
                ).add(int(monster.actor_id))
        _log(
            f"spawned {len(added)} monster(s) on {runtime.map_id}: "
            + ", ".join(sorted({monster.template_id for monster in monsters}))
        )
        for viewer in sessions:
            with viewer.flow_lock:
                ready = viewer.gameplay_presence_ready
            if ready:
                self._announce_runtime_group(viewer, combat, group)

        scene_actor_ids = tuple(
            monster.actor_id
            for row, monster in zip(spawned_rows, monsters, strict=True)
            if row.template_id.casefold() == _TUTORIAL_CULTIST_TEMPLATE
        )
        if scene_actor_ids:
            scheduler = getattr(runtime, "scheduler", None)
            if scheduler is None:
                _log("cannot start tutorial cultist scene: map scheduler is unavailable")
            else:
                # Publish the authored actor before switching its animation
                # from start_idle to the authored staff-theft sequence.
                handle = scheduler.timer(
                    0.35,
                    self._start_tutorial_cultist_sequence,
                    args=(runtime, combat, tuple(sessions), scene_actor_ids),
                )
                handle.start()
                _log(
                    f"queued tutorial cultist despawn sequence after actor "
                    f"initialization for {scene_actor_ids!r}"
                )

    def _start_tutorial_cultist_sequence(
            self, runtime, combat, sessions, scene_actor_ids) -> None:
        if not self._map_event_runtime_is_live(runtime):
            return
        with combat.lock:
            tick = max(
                (
                    int(getattr(viewer, "gameplay_chase_server_tick", 0) or 0)
                    for viewer in sessions
                ),
                default=0,
            )
            # Native KillCommand +0x78 maps to Messaging::Kill::SetDespawn.
            # Despawn bypasses the client's ordinary Death clip and loads
            # despawn/a0001_cultist_death, whose hijacked Death track plays
            # cultist_steal_staff. No damage types: this is a scripted scene.
            scene_commands = tuple(
                KillServerCommand(
                    Kill(
                        server_tick=tick,
                        hit_tags=(),
                        animation_time=600,
                        field_4c=0,
                        killer_actor_id=0,
                        position=tuple(combat.monster_positions[actor_id]),
                        skill_id=0,
                        projectile_id=0xFFFFFFFF,
                        field_78=True,
                    ),
                    int(actor_id),
                )
                for actor_id in scene_actor_ids
                if actor_id in combat.monster_positions
            )
        if scene_commands:
            packet, packet_bits = encode_combat_batch(
                CombatBatch(scene_commands)
            )
            stun_effects = ()
            database_path = Path(combat.encounter.static_database_path)
            try:
                with sqlite3.connect(
                    f"file:{database_path.as_posix()}?mode=ro", uri=True
                ) as database:
                    stun_effects = combat._load_location_aura_effects(
                        database, "a0001_tutorial_sequence_stun_aura"
                    )
            except Exception as exc:
                _log(
                    "could not load tutorial cultist stun effect: "
                    f"{type(exc).__name__}: {exc}"
                )
            for viewer in sessions:
                with viewer.flow_lock:
                    if viewer.gameplay_combat_state is not combat:
                        continue
                CharacterService._send_world_loot_state_batch(
                    viewer, packet, packet_bits,
                    label="tutorial-cultist-scripted-despawn",
                )
                if stun_effects:
                    CharacterService._send_socket_actor_applications(
                        viewer,
                        ((LOCAL_PLAYER_ACTOR_ID, None, stun_effects),),
                        server_tick=tick,
                        label="tutorial-cultist-sequence-stun",
                    )
            if stun_effects:
                _log(
                    "applied authored tutorial cultist stun to "
                    f"{len(sessions)} session(s) for "
                    f"{max(effect.duration_ticks for effect in stun_effects)} "
                    "ticks"
                )
            else:
                _log("tutorial cultist stun aura resolved to no receiver effects")
        with combat.lock:
            retired_scene_ids = combat.retire_monsters(scene_actor_ids)
        with runtime.lock:
            for actor_ids_for_set in runtime.event_set_actor_ids.values():
                actor_ids_for_set.difference_update(retired_scene_ids)
        _log(
            f"started authored tutorial cultist despawn sequence for "
            f"actor(s) {retired_scene_ids!r}"
        )

    def _announce_runtime_group(self, viewer: Session, combat, group) -> None:
        """Offer a freshly spawned group to one viewer, like a MoveCommand does."""

        with viewer.flow_lock:
            if viewer.gameplay_combat_state is not combat:
                return
            encounter = viewer.gameplay_monster_encounter
            move = viewer.gameplay_last_move
            announced = set(viewer.gameplay_announced_monster_actor_ids)
        if encounter is None:
            return
        if move is not None:
            player_world = encounter.coordinate_frame.wire_to_world(
                move.relative_position
            )
        else:
            spawn = getattr(viewer, "gameplay_spawn_position", None)
            if spawn is None:
                return
            player_world = tuple(float(value) for value in spawn)
        with combat.lock:
            alive = set(combat.alive_actor_ids)
            positions = dict(combat.monster_positions)
            scripted_actor_ids = {
                actor_id
                for actor_id in group.actor_id_set & alive
                if (
                    combat.monsters.get(actor_id) is not None
                    and combat.monsters[actor_id].template_id.casefold()
                    in _FORCE_ANNOUNCE_EVENT_ACTORS
                )
            }
        candidates = (group.actor_id_set & alive) - announced
        forced = candidates & scripted_actor_ids
        groups = list(CharacterService._monster_groups_within_radius(
            None,
            world_position=player_world,
            actor_ids=candidates - forced,
            positions=positions,
            radius=MONSTER_VISIBILITY_ENTER_RADIUS,
            extra_groups=(group,),
        ))
        # Scripted story actors must appear when their event starts even if
        # the trigger volume is wider than the ordinary monster visibility
        # radius. Keep the exception template-scoped; ordinary event waves
        # retain the normal per-actor distance filter.
        if forced:
            scripted_group = group.runtime_subset(
                forced, positions=positions
            )
            if scripted_group is not None:
                groups.append(scripted_group)
        if not groups:
            return
        with viewer.flow_lock:
            if viewer.gameplay_combat_state is not combat:
                return
            for subset in groups:
                viewer.gameplay_announced_monster_actor_ids.update(subset.actor_ids)
            viewer.gameplay_vicinity_announced = True
            # Scripted story actors must not depend on the client's optional
            # ActorRequest round trip to become visible. Reserve them before
            # announcing vicinity so a simultaneous request cannot enqueue a
            # duplicate NewMonster packet.
            scripted_spawn_ids = forced.difference(
                viewer.gameplay_spawned_monster_actor_ids
            )
            viewer.gameplay_spawned_monster_actor_ids.update(
                scripted_spawn_ids
            )
            viewer.gameplay_requested_actor_ids.update(scripted_spawn_ids)
            if scripted_spawn_ids:
                viewer.gameplay_monsters_spawned = True
        scripted_group = None
        if scripted_spawn_ids:
            scripted_group = next(
                (
                    subset for subset in groups
                    if scripted_spawn_ids.issubset(subset.actor_id_set)
                ),
                None,
            )
        for subset in groups:
            CharacterService._send_monster_vicinity(
                viewer, combat, subset,
                label="map-event-monster-vicinity",
            )
        if scripted_spawn_ids and scripted_group is not None:
            spawn_group = scripted_group.runtime_subset(
                scripted_spawn_ids, positions=positions
            )
            if spawn_group is not None:
                packet, packet_bits = encode_monster_spawn_batch(
                    spawn_group.spawn_batch
                )
                if len(packet) > TAIL_FRAG_SIZE:
                    viewer.send_reliable_ordered_split(
                        packet,
                        total_bit_length=packet_bits,
                        track=True,
                        label_prefix="map-event-scripted-monster-spawn",
                    )
                else:
                    viewer.send_reliable_ordered(
                        (packet, packet_bits),
                        track=True,
                        label="map-event-scripted-monster-spawn",
                    )
                with combat.lock:
                    combat.activate_monsters(scripted_spawn_ids)
                    delivered_positions = {
                        actor_id: combat.monster_positions[actor_id]
                        for actor_id in scripted_spawn_ids
                        if actor_id in combat.monster_positions
                    }
                with viewer.flow_lock:
                    if viewer.gameplay_combat_state is combat:
                        viewer.gameplay_delivered_monster_positions.update(
                            delivered_positions
                        )
                _log(
                    f"sent scripted NewMonster state "
                    f"{tuple(sorted(scripted_spawn_ids))} to {viewer.addr!r}"
                )
        if forced:
            _log(
                f"announced scripted actor(s) {tuple(sorted(forced))} "
                f"to {viewer.addr!r}"
            )

    def _retire_event_monsters(self, runtime, sessions, event_sets) -> None:
        combat = self._map_event_combat_state(sessions)
        if combat is None:
            return
        wanted = set(int(value) for value in event_sets)
        rows = runtime.content.spawn_rows_for_sets(wanted)
        with runtime.lock:
            actor_ids = set()
            for event_set in wanted:
                actor_ids.update(runtime.event_set_actor_ids.pop(event_set, ()))
        with combat.lock:
            for row in rows:
                actor_ids.update(combat.find_monsters(
                    row.template_id, row.position,
                    radius=EVENT_SPAWN_DUPLICATE_RADIUS,
                ))
            retired = combat.retire_monsters(tuple(sorted(actor_ids)))
        if not retired:
            return
        _log(f"retired {len(retired)} monster(s) on {runtime.map_id}")
        retired_set = set(retired)
        for viewer in sessions:
            with viewer.flow_lock:
                if viewer.gameplay_combat_state is not combat:
                    continue
                discard = tuple(sorted(
                    retired_set & viewer.gameplay_spawned_monster_actor_ids
                ))
                viewer.gameplay_spawned_monster_actor_ids.difference_update(retired_set)
                viewer.gameplay_announced_monster_actor_ids.difference_update(retired_set)
                viewer.gameplay_requested_actor_ids.difference_update(retired_set)
                for actor_id in retired_set:
                    viewer.gameplay_delivered_monster_positions.pop(actor_id, None)
            if discard:
                viewer.send_reliable_ordered(
                    encode_discard_monster_batch(discard),
                    track=True,
                    label="map-event-discard-monster",
                )


__all__ = ["MapEventService"]
