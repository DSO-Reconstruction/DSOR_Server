"""Map-local scripted events: signals, event definitions, and gated content.

Retail scripts a dungeon with three tables.  ``_Template_Signal`` in
``static.db4`` turns a world happening (a kill, a prop interaction, a
trigger-volume hit, a map entry) into a named signal when its conditions
hold.  ``_Template_Events`` rows with ``MapLocal=1`` consume those signals
through ``StartSignalId``/``EndSignalId``.  Each map database then gates its
placed spawn points, state objects, exits, and trigger volumes behind
``_Instance_EventSets`` flags such as ``BOSS_death=True;``.  Everything
below is read-only content; the mutable per-map-instance state lives in
:class:`MapEventRuntime`.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field, replace
from functools import lru_cache
import math
from pathlib import Path
import random
import re
import sqlite3
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from types import MappingProxyType
from typing import Iterable, Mapping

from dso_server.runtime import resident_content
from dso_server.runtime.file_revisions import observed_revision, resolved_path
from dso_server.gameplay.prop_loot import has_prop_loot
from dso_server.gameplay.local_map_state import (
    LOCAL_PROP_ID_BASE,
    active_client_instance_guid_overrides,
    effective_event_set_declarations,
    local_prop_actor_base,
    map_database_path,
)
from dso_server.protocol.prop_protocol import (
    PROP_STATE_ACTIVE,
    PROP_STATE_USED,
    NewProp,
    ZERO_GUID,
)


# The client addresses props by the **low 16 bits** of the prop id only:
# ``ClientPropManager::RegisterPropEntity`` (0x1402197c5) and the world's
# prop lookup (0x1409ec90e) both ``movzx`` the id's low word before indexing
# their arrays, so two props whose ids differ only above bit 15 overwrite each
# other on the client. ``_Instance_StateObjectSpawnPoint`` rows share rowids
# with scene state objects, so spawn-point props follow the scene rows in one
# sequence. Props also share the world actor-id space with NPCs: the first prop
# starts after the highest authored NPC id, including NPCs gated for later
# events. This matches the working DRASA path, which allocates prop ids after
# the map's NPC ids.
PROP_INDEX_LIMIT = 0x10000


def prop_index(prop_id: int) -> int:
    """The slot the client selects for a prop id."""

    return int(prop_id) & 0xFFFF


def spawn_prop_id(
    scene_row_limit: int,
    row_id: int,
    *,
    prop_actor_base: int = LOCAL_PROP_ID_BASE,
) -> int:
    """Prop id for spawn-point ``row_id`` placed after ``scene_row_limit`` rows."""

    index = int(scene_row_limit) + int(row_id) - 1
    if prop_index(prop_actor_base) + index >= PROP_INDEX_LIMIT:
        raise ValueError(
            f"prop index {index} exceeds the client's 16-bit prop slot range"
        )
    return int(prop_actor_base) + index
# Retail sends an empty operation string in every PropInfo, even for the
# crafting workbench and for eggs the player then clicks: the client keeps
# the template's own operation, and an empty string does not block clicking.
WIRE_PROP_OPERATION = ""

# Bloodshed is difficulty tier 8 in ``_Template_MapAccessGroup`` (the ``_08``
# access groups carry ``mapeffect_bloodshed`` and the ``BloodShedInfo`` text).
# The bloody sludges (``all_guardian_bloodshed_thing_blood_01``) sit in every
# dungeon's spawn-point table without an EventSet: their skills are the
# ``*_BLOODSHED`` variants, the ``bloodshed-challenge-01-n`` quest that clears
# them is activated only on ``*_08`` maps, and ``CLOSE_MAP_BLOODSHED`` fires on
# their kill.  Nothing in the map data gates the rows themselves, so the server
# gates them by tier.
BLOODSHED_DIFFICULTY = 8

# Spawn-point chest variants are authored on top of each other: one location
# holds a ``normal_*`` free chest (map-level loot, tier 0), one ``scaling_*``
# free chest per scaling tier, and the ``scaling_magic/rare/epic`` chests plus
# their treasure-goblin twins.  ``_Template_MapDifficulty`` carries the per-tier
# chance of each rarity; retail shows one chest per spot.
CHEST_LOCATION_RADIUS = 1.5
_CHEST_TIER_MARKERS = ("_epic_", "_rare_", "_magic_")
# Odds of a stacked amphora spot showing each variant, by keys per use:
# free small, one-key normal, five-key golden (not from retail data).
_CHEST_VARIANT_WEIGHTS = {0: 50, 1: 40, 5: 10}

# Authored per-difficulty reward chests in the parallel-world boss arenas.
PARALLEL_WORLD_REWARD_CHEST_INSTANCE_IDS = frozenset({
    "pw_run_lootchest", "pw_run_lootchest_gold",
})

SERVER_SIGNAL_TYPES = frozenset({"serversignal", "mapsignal"})
CLIENT_REPORTED_SIGNAL_TYPES = frozenset({"clienttoserversignal", "clientsignal"})

_DIFFICULTY_SUFFIX = re.compile(r"_diff_\d+$", re.IGNORECASE)


def _tokens(value: object) -> tuple[str, ...]:
    return tuple(
        token.strip()
        for token in str(value or "").replace(",", ";").split(";")
        if token.strip()
    )


def _folded_tokens(value: object) -> tuple[str, ...]:
    return tuple(token.casefold() for token in _tokens(value))


def bloodshed_only_template(template_id: object) -> bool:
    """Whether a spawn row belongs to the Bloodshed tier alone."""

    return "bloodshed" in str(template_id or "").casefold()


def map_key(value: object) -> str:
    """Normalize a map or access-group id the way signal rows name maps."""

    return _DIFFICULTY_SUFFIX.sub("", str(value or "").strip()).casefold()


@dataclass(frozen=True)
class SignalRule:
    """One ``_Template_Signal`` row."""

    signal_id: str
    signal_type: str
    event_type: str
    params: tuple[str, ...]
    maps: frozenset[str]
    min_level: int
    max_level: int
    classes: frozenset[str]
    player_amount: int
    events_running: tuple[str, ...]
    events_not_running: tuple[str, ...]
    requires_quest_state: bool
    enabled: bool
    quest_accepted: tuple[str, ...] = ()
    quest_not_accepted: tuple[str, ...] = ()
    quest_solved: tuple[str, ...] = ()
    quest_not_solved: tuple[str, ...] = ()
    quest_task_done: tuple[str, ...] = ()
    quest_task_not_done: tuple[str, ...] = ()
    maps_unlocked: tuple[str, ...] = ()
    maps_not_unlocked: tuple[str, ...] = ()

    @property
    def server_evaluated(self) -> bool:
        return self.signal_type in SERVER_SIGNAL_TYPES or (
            self.signal_type == "clientsignal"
            and self.event_type in {
                "playerenteredmap",
                # Quest turn-in is committed by the server and emits
                # PlayerClosedQuest. Map scripts use it to reveal follow-up
                # quest NPCs (for example Saving Souls' citizen in
                # a0401_oldchapel); without evaluating this signal, the
                # EventSet remains closed until some unrelated map-entry
                # path happens to reveal the actor.
                "playerclosedquest",
                "playerlevelup",
                # PlayerKilledMonster is also raised from the server's
                # authoritative combat callback. Evaluate ClientSignal rules
                # for that event there so boss-death gates do not depend on
                # the client reporting its own kill back as a staging signal.
                "playerkilledmonster",
                # Resolve prop signals from the server's authoritative prop
                # table instead of trusting a client StagingSignal payload.
                "playerinteractedprop",
            }
        )

    @property
    def fraction(self) -> float:
        try:
            return float(self.params[0]) if self.params else 1.0
        except ValueError:
            return 1.0

    def accepts(
        self,
        *,
        map_id: str,
        running_events: frozenset[str],
        player_level: int,
        class_name: str,
        player_count: int,
        quests_accepted: frozenset[str] = frozenset(),
        quests_solved: frozenset[str] = frozenset(),
        tasks_done: frozenset[str] = frozenset(),
        unlocked_maps: frozenset[str] = frozenset(),
    ) -> bool:
        if not self.enabled:
            return False
        if self.maps and map_key(map_id) not in self.maps:
            return False
        # Quest conditions read the character's log (services/quests.py):
        # "accepted" is any quest in the log that is not closed, "solved" a
        # closed one, and task tokens are ``<quest>:<task title>``.
        if any(q not in quests_accepted for q in self.quest_accepted):
            return False
        if any(q in quests_accepted for q in self.quest_not_accepted):
            return False
        if any(q not in quests_solved for q in self.quest_solved):
            return False
        if any(q in quests_solved for q in self.quest_not_solved):
            return False
        if any(t not in tasks_done for t in self.quest_task_done):
            return False
        if any(t in tasks_done for t in self.quest_task_not_done):
            return False
        if self.maps_unlocked or self.maps_not_unlocked:
            discovered = frozenset(map_key(value) for value in unlocked_maps)
            if any(value not in discovered for value in self.maps_unlocked):
                return False
            if any(value in discovered for value in self.maps_not_unlocked):
                return False
        if self.min_level and player_level < self.min_level:
            return False
        if self.max_level and player_level > self.max_level:
            return False
        if self.classes and class_name.casefold() not in self.classes:
            return False
        if self.player_amount > 0 and player_count < self.player_amount:
            return False
        if any(event not in running_events for event in self.events_running):
            return False
        if any(event in running_events for event in self.events_not_running):
            return False
        return True


@dataclass(frozen=True)
class MapLocalEventDefinition:
    """One ``_Template_Events`` row with ``MapLocal=1``."""

    event_id: str
    wire_id: int
    start_signals: tuple[str, ...]
    end_signals: tuple[str, ...]
    start_delay: float
    duration: float
    maps: frozenset[str]
    client_only: bool

    @property
    def key(self) -> str:
        return self.event_id.casefold()

    @property
    def wave_label(self) -> str | None:
        # Numbered waves also carry difficulty suffixes (_pw, _scaling,
        # _event, _normal_01) and sometimes A/B spawn variants. Presentation
        # events such as wave_countdown_01 and wave_done are not waves.
        match = re.search(r"(?:^|_)(?:monster)?wave_?(\d+)[a-z]?(?=_|$)", self.key)
        if match:
            return f"Wave {int(match.group(1))}"
        if self.key.endswith("_boss_wave"):
            return "Boss wave"
        if re.search(r"_monster_?waves?(?:_single)?$", self.key):
            return "Monster wave"
        return None

    def applies_to(self, map_id: str) -> bool:
        return not self.maps or map_key(map_id) in self.maps


@dataclass(frozen=True)
class SignalContext:
    """What happened, and the state the signal conditions read."""

    map_id: str
    params: frozenset[str]
    running_events: frozenset[str]
    player_level: int = 1
    class_name: str = ""
    player_count: int = 1
    present_templates: frozenset[str] = frozenset()
    alive_templates: frozenset[str] = frozenset()
    dead_fraction: float = 0.0
    quests_accepted: frozenset[str] = frozenset()
    quests_solved: frozenset[str] = frozenset()
    tasks_done: frozenset[str] = frozenset()
    unlocked_maps: frozenset[str] = frozenset()


@dataclass(frozen=True)
class MapSignalCatalog:
    rules_by_event_type: Mapping[str, tuple[SignalRule, ...]]
    rules_by_signal_id: Mapping[str, SignalRule]
    events: Mapping[str, MapLocalEventDefinition]
    events_by_start_signal: Mapping[str, tuple[str, ...]]
    events_by_end_signal: Mapping[str, tuple[str, ...]]

    def rule(self, signal_id: str) -> SignalRule | None:
        return self.rules_by_signal_id.get(str(signal_id).casefold())

    def event(self, event_id: str) -> MapLocalEventDefinition | None:
        return self.events.get(str(event_id).casefold())

    def evaluate(
        self, event_type: str, context: SignalContext,
    ) -> tuple[SignalRule, ...]:
        """Return the server-evaluated rules that fire for ``event_type``."""

        kind = str(event_type).casefold()
        fired = []
        for rule in self.rules_by_event_type.get(kind, ()):
            if not rule.accepts(
                map_id=context.map_id,
                running_events=context.running_events,
                player_level=context.player_level,
                class_name=context.class_name,
                player_count=context.player_count,
                quests_accepted=context.quests_accepted,
                quests_solved=context.quests_solved,
                tasks_done=context.tasks_done,
                unlocked_maps=context.unlocked_maps,
            ):
                continue
            if kind == "monsterseliminated":
                listed = frozenset(rule.params)
                present = listed & context.present_templates
                if (
                    not present
                    or not (listed & context.params)
                    or (listed & context.alive_templates)
                ):
                    continue
            elif kind == "monsterseliminatedpercentage":
                if context.dead_fraction + 1e-9 < rule.fraction:
                    continue
            elif kind == "playerkillmonstergetscore":
                try:
                    current_score = max(
                        int(value) for value in context.params
                    )
                    required_score = min(int(value) for value in rule.params)
                except (TypeError, ValueError):
                    if rule.params and not (frozenset(rule.params) & context.params):
                        continue
                else:
                    if current_score < required_score:
                        continue
            elif rule.params and not (frozenset(rule.params) & context.params):
                continue
            fired.append(rule)
        return tuple(fired)


def _load_signal_catalog(
    database_path: str, mtime_ns: int, size: int,
) -> MapSignalCatalog:
    del mtime_ns, size
    path = Path(database_path)
    rules_by_type: dict[str, list[SignalRule]] = {}
    rules_by_id: dict[str, SignalRule] = {}
    events: dict[str, MapLocalEventDefinition] = {}
    by_start: dict[str, list[str]] = {}
    by_end: dict[str, list[str]] = {}
    with sqlite3.connect(
        f"file:{path.as_posix()}?mode=ro", uri=True
    ) as database:
        database.row_factory = sqlite3.Row
        for row in database.execute("SELECT * FROM _Template_Signal"):
            signal_id = str(row["SignalId"] or "").strip()
            if not signal_id:
                continue
            rule = SignalRule(
                signal_id=signal_id,
                signal_type=str(row["SignalType"] or "").casefold(),
                event_type=str(row["EventType"] or "").casefold(),
                params=_folded_tokens(row["EventParam"]),
                maps=frozenset(map_key(value) for value in _tokens(row["Map"])),
                min_level=int(row["MinLevel"] or 0),
                max_level=int(row["MaxLevel"] or 0),
                classes=frozenset(_folded_tokens(row["CharClass"])),
                player_amount=int(row["PlayerAmount"] or 0),
                events_running=_folded_tokens(row["EventRunning"]),
                events_not_running=_folded_tokens(row["EventNotRunning"]),
                requires_quest_state=any(
                    _tokens(row[column])
                    for column in ("QuestAccepted", "QuestSolved", "QuestTaskDone")
                ),
                enabled=bool(int(row["Enabled"] or 0)),
                quest_accepted=_folded_tokens(row["QuestAccepted"]),
                quest_not_accepted=_folded_tokens(row["QuestNotAccepted"]),
                quest_solved=_folded_tokens(row["QuestSolved"]),
                quest_not_solved=_folded_tokens(row["QuestNotSolved"]),
                quest_task_done=_folded_tokens(row["QuestTaskDone"]),
                quest_task_not_done=_folded_tokens(row["QuestTaskNotDone"]),
                maps_unlocked=tuple(map_key(value) for value in _tokens(row["MapUnlocked"])),
                maps_not_unlocked=tuple(map_key(value) for value in _tokens(row["MapNotUnlocked"])),
            )
            rules_by_id[signal_id.casefold()] = rule
            if rule.server_evaluated:
                rules_by_type.setdefault(rule.event_type, []).append(rule)
        for row in database.execute(
            "SELECT rowid - 1 AS wire_id, * FROM _Template_Events "
            "WHERE COALESCE(MapLocal, 0)=1"
        ):
            event_id = str(row["Id"] or "").strip()
            if not event_id:
                continue
            definition = MapLocalEventDefinition(
                event_id=event_id,
                wire_id=int(row["wire_id"]),
                start_signals=_folded_tokens(row["StartSignalId"]),
                end_signals=_folded_tokens(row["EndSignalId"]),
                start_delay=max(0.0, float(row["MapEventStartDelay"] or 0.0)),
                duration=max(0.0, float(row["MapEventDuration"] or 0.0)),
                maps=frozenset(map_key(value) for value in _tokens(row["Map"])),
                client_only=bool(int(row["ClientOnly"] or 0)),
            )
            events[definition.key] = definition
            for signal in definition.start_signals:
                by_start.setdefault(signal, []).append(definition.key)
            for signal in definition.end_signals:
                by_end.setdefault(signal, []).append(definition.key)
    return MapSignalCatalog(
        rules_by_event_type=MappingProxyType({
            key: tuple(values) for key, values in rules_by_type.items()
        }),
        rules_by_signal_id=MappingProxyType(rules_by_id),
        events=MappingProxyType(events),
        events_by_start_signal=MappingProxyType({
            key: tuple(values) for key, values in by_start.items()
        }),
        events_by_end_signal=MappingProxyType({
            key: tuple(values) for key, values in by_end.items()
        }),
    )


_signal_catalog_cache = lru_cache(maxsize=4)(_load_signal_catalog)


def load_map_signal_catalog(static_database_path: str | Path) -> MapSignalCatalog:
    # A hot lookup (every signal, exit and reveal check): reuse the last
    # observed static.db4 revision instead of stat()ing it each call.
    path = resolved_path(str(static_database_path))
    revision = observed_revision(path)
    if revision is None:
        raise FileNotFoundError(path)
    return _signal_catalog_cache(str(path), *revision)


@dataclass(frozen=True)
class EventSetDefinition:
    row: int
    required: frozenset[str]
    forbidden: frozenset[str]

    @property
    def events(self) -> frozenset[str]:
        return self.required | self.forbidden

    def satisfied(self, active: frozenset[str]) -> bool:
        return (not self.required or bool(self.required & active)) and not (self.forbidden & active)


@dataclass(frozen=True)
class GatedSpawnRow:
    row_id: int
    event_set_row: int
    template_id: str
    position: tuple[float, float, float]
    rotation: float = 0.0
    # ``MapVariation`` family and difficulty tier (variationType 1) of the
    # spawn point's parent node; 0/0 when it belongs to no variation.
    variation_group: int = 0
    variation_difficulty: int = 0


def select_variation_spawn_rows(
    rows: Iterable[GatedSpawnRow], difficulty: int,
) -> tuple[GatedSpawnRow, ...]:
    """Keep, of each MapVariation family, only the instance's tier.

    The Herald's lair authors its boss_start broodlings once per tier (4 on
    tier 0, 6 on 1-3, 8 on 4-10: 78 rows under one EventSet); opening the set
    spawned them all at once. A family without the exact tier takes its
    highest tier below it, else none (``selected_variation_tier``):
    c2304_boss pairs a tier-0 normal family with a tier 1-10 scaling one
    under BOSS_spawn, and falling back to the lowest tier added four
    scaling gorgons to the normal instance's four.
    """

    rows = tuple(rows)
    tiers: dict[int, set[int]] = {}
    for row in rows:
        if row.variation_group:
            tiers.setdefault(row.variation_group, set()).add(
                row.variation_difficulty
            )
    chosen = {
        group: selected_variation_tier(values, difficulty)
        for group, values in tiers.items()
    }
    return tuple(
        row for row in rows
        if not row.variation_group
        or chosen[row.variation_group] == row.variation_difficulty
    )


@dataclass(frozen=True)
class MapPropDefinition:
    prop_id: int
    row_id: int
    kind: str
    instance_id: str
    template_id: str
    instance_guid: bytes
    position: tuple[float, float, float]
    rotation: float
    event_set_row: int
    operation: str
    one_shot: bool
    individual_usage: bool
    loot_list_id: str
    chest_id: str
    signal_referenced: bool
    workbench: bool
    quest_switch_id: str = ""
    # ``MapVariation`` node this scene object belongs to, if any, plus the
    # variation family and the difficulty tier that node selects
    # (variationType 1).
    parent_node_index: int = 0
    variation_group: int = 0
    variation_difficulty: int = 0
    variation_level: int = 0
    # StateObject KeyId/KeyAmount: what one use spends (amphora keys).
    key_id: str = ""
    key_amount: int = 0

    @property
    def always_present(self) -> bool:
        return self.event_set_row < 0

    @property
    def spends_keys(self) -> bool:
        """Whether one use costs ``key_amount`` x ``key_id``.

        Charged only for chests filled from data/catalogs/prop_loot.json
        (amphorae). Other keyed StateObjects -- lockpick chests, Balor's
        chests, shrines, quest altars -- open as before.
        """

        return bool(
            self.key_id and self.key_amount > 0
            and has_prop_loot(self.loot_list_id or self.chest_id)
        )

    @property
    def network_prop(self) -> bool:
        """Whether the server publishes this object as a prop at all.

        Scene objects that nothing listens to stay client-side; a dungeon
        holds hundreds of them and retail's own PropInfo carried only the
        interactive ones.
        """

        if self.workbench or self.signal_referenced or self.quest_switch_id:
            return True
        # Fractured Planes uses an otherwise plain scene rift as a clickable
        # map transition. Its EventSet controls when it appears; it still
        # needs a server prop id so PropInteraction can fire the map signal.
        if self.instance_id.casefold() == "fracturedplanes_rift":
            return True
        if self.always_present:
            # Retail published a scene amphora (PropInfo on approach) and
            # answered its click with loot; chests whose list the server can
            # fill are interactive the same way.
            return has_prop_loot(self.loot_list_id or self.chest_id)
        return bool(self.operation or self.loot_list_id or self.chest_id)

    def new_prop(self, state: int) -> NewProp:
        return NewProp(
            prop_id=self.prop_id,
            template_id=self.template_id,
            instance_guid=self.instance_guid,
            position=self.position,
            rotation=self.rotation,
            state=state,
            operation=WIRE_PROP_OPERATION,
        )


def selected_map_difficulty(player) -> int:
    """The access-group tier chosen for this player's map instance."""

    difficulty = int(getattr(player, "gameplay_map_difficulty", 0) or 0)
    if difficulty <= 0:
        runtime = getattr(player, "gameplay_map_event_runtime", None)
        difficulty = int(getattr(runtime, "scaling_difficulty", 0) or 0)
    return max(0, difficulty)


def selected_map_level(player) -> int:
    """Effective level used by level-based MapVariation nodes."""

    level = int(getattr(player, "gameplay_monster_level", 0) or 0)
    if level <= 0:
        character = getattr(player, "gameplay_character", None)
        level = int(getattr(character, "level", 0) or 0)
    if level <= 0:
        runtime = getattr(player, "gameplay_map_event_runtime", None)
        level = int(getattr(runtime, "monster_level", 0) or 0)
    return max(0, level)


def prop_visible_to_player(
    definition: MapPropDefinition, player,
) -> bool:
    """Whether a ``MapVariation`` prop belongs to the player's difficulty.

    The client merges PropInfo records into one scene.  Overlapping variants
    authored for different difficulty tiers therefore all appear unless the
    server publishes only the variation selected by the map-access group.
    """

    if not int(definition.variation_group or 0):
        return True
    required_level = int(definition.variation_level or 0)
    if required_level > 0:
        level = selected_map_level(player)
        runtime = getattr(player, "gameplay_map_event_runtime", None)
        siblings = {
            int(prop.variation_level)
            for prop in getattr(getattr(runtime, "content", None), "props", ())
            if int(prop.variation_group or 0) == int(definition.variation_group or 0)
            and int(prop.variation_level or 0) > 0
        }
        if level > 0:
            eligible = [value for value in siblings if value <= level]
            selected = max(eligible) if eligible else min(siblings or {required_level})
        else:
            selected = min(siblings or {required_level})
        return required_level == selected

    required = int(definition.variation_difficulty or 0)
    difficulty = selected_map_difficulty(player)
    if difficulty > 0:
        return required == difficulty
    # Sessions without map-access metadata (tests or direct map entry) still
    # see one deterministic variant instead of every overlapping copy.
    runtime = getattr(player, "gameplay_map_event_runtime", None)
    siblings = {
        int(prop.variation_difficulty or 0)
        for prop in getattr(getattr(runtime, "content", None), "props", ())
        if int(prop.variation_group or 0) == int(definition.variation_group or 0)
    }
    return required == min(siblings) if siblings else True


def _map_variations(path: Path) -> dict[int, tuple[int, int]]:
    """``MapVariation`` node index -> (family node, difficulty tier)."""

    try:
        root = ET.fromstring(resident_content.read_bytes(path.with_suffix(".mvr")))
    except (OSError, ET.ParseError):
        return {}
    variations: dict[int, tuple[int, int]] = {}
    for node in root.iter("LevelNode"):
        if str(node.get("variationType", "")).strip() != "1":
            continue
        try:
            index = int(node.get("nodeIndex", ""))
            group = int(node.get("parentNodeIndex", ""))
            value = int(node.get("variationValue", ""))
        except (TypeError, ValueError):
            continue
        variations[index] = (group, value)
    return variations


@lru_cache(maxsize=256)
def map_difficulty_variations(path: Path) -> Mapping[int, tuple[int, int]]:
    """Every level node -> (family, tier) of its nearest difficulty variation.

    Unlike ``_map_variations`` this also covers nodes nested below a
    ``variationType="1"`` node (c2303_dun's miniboss adds hang three levels
    under their tier node). Structural nodes carry no ``nodeIndex``; their
    index is the ``parentNodeIndex`` their own children name.
    """

    try:
        root = ET.fromstring(resident_content.read_bytes(Path(path).with_suffix(".mvr")))
    except (OSError, ET.ParseError):
        return MappingProxyType({})
    result: dict[int, tuple[int, int]] = {}

    def walk(node, inherited) -> None:
        if str(node.get("variationType", "")).strip() == "1":
            try:
                inherited = (
                    int(node.get("parentNodeIndex", "")),
                    int(node.get("variationValue", "")),
                )
            except (TypeError, ValueError):
                pass
        if inherited is not None:
            try:
                result[int(node.get("nodeIndex", ""))] = inherited
            except (TypeError, ValueError):
                pass
        for child in node.findall("LevelNode"):
            if inherited is not None:
                try:
                    result.setdefault(
                        int(child.get("parentNodeIndex", "")), inherited
                    )
                except (TypeError, ValueError):
                    pass
            walk(child, inherited)

    for node in root.findall("LevelNode"):
        walk(node, None)
    return MappingProxyType(result)


def selected_variation_tier(tiers: Iterable[int], difficulty: int) -> int | None:
    """The tier of one variation family an instance of ``difficulty`` shows.

    The exact tier, else the highest tier below it (c2303_dun's density
    family has 0,1,2,3,4,6,8: a tier-5 instance keeps tier 4). A family with
    no tier at or below the difficulty is absent: the mercyless-only
    ``difficulty_extra_spawn`` areas sit on tiers 6-9 on all 126 maps that
    carry them and must not appear on a normal instance.
    """

    difficulty = max(0, int(difficulty))
    return max((t for t in tiers if t <= difficulty), default=None)


def _map_level_variations(path: Path) -> dict[int, tuple[int, int]]:
    """Level-based MapVariation node index -> (family node, level)."""

    try:
        root = ET.fromstring(resident_content.read_bytes(path.with_suffix(".mvr")))
    except (OSError, ET.ParseError):
        return {}
    variations: dict[int, tuple[int, int]] = {}
    for node in root.iter("LevelNode"):
        if str(node.get("variationType", "")).strip() != "3":
            continue
        try:
            index = int(node.get("nodeIndex", ""))
            group = int(node.get("parentNodeIndex", ""))
            value = int(node.get("variationValue", ""))
        except (TypeError, ValueError):
            continue
        variations[index] = (group, value)
    return variations


@dataclass(frozen=True)
class TriggerVolumeDefinition:
    row_id: int
    trigger_id: str
    event_set_row: int
    per_player: bool
    category: str
    matrix: tuple[float, ...]
    triggered_event_id: str = ""

    def contains(self, position: tuple[float, float, float]) -> bool:
        """Test a world position against the unit-cube placeholder volume."""

        m = self.matrix
        relative = (
            float(position[0]) - m[12],
            float(position[1]) - m[13],
            float(position[2]) - m[14],
        )
        # Row-vector convention: rows 0..2 are the scaled basis vectors.
        basis = (
            (m[0], m[1], m[2]),
            (m[4], m[5], m[6]),
            (m[8], m[9], m[10]),
        )
        local = _solve_basis(basis, relative)
        if local is None:
            return False
        return all(abs(value) <= 0.5 + 1e-6 for value in local)

    def intersects_segment(
        self,
        start: tuple[float, float, float],
        end: tuple[float, float, float],
    ) -> bool:
        """Test whether a movement segment crosses this trigger box."""

        origin = _solve_basis(
            (
                (self.matrix[0], self.matrix[1], self.matrix[2]),
                (self.matrix[4], self.matrix[5], self.matrix[6]),
                (self.matrix[8], self.matrix[9], self.matrix[10]),
            ),
            tuple(float(start[i]) - self.matrix[12 + i] for i in range(3)),
        )
        destination = _solve_basis(
            (
                (self.matrix[0], self.matrix[1], self.matrix[2]),
                (self.matrix[4], self.matrix[5], self.matrix[6]),
                (self.matrix[8], self.matrix[9], self.matrix[10]),
            ),
            tuple(float(end[i]) - self.matrix[12 + i] for i in range(3)),
        )
        if origin is None or destination is None:
            return False

        lower, upper = 0.0, 1.0
        for start_axis, end_axis in zip(origin, destination, strict=True):
            delta = end_axis - start_axis
            if abs(delta) <= 1e-12:
                if abs(start_axis) > 0.5 + 1e-6:
                    return False
                continue
            first = (-0.5 - start_axis) / delta
            second = (0.5 - start_axis) / delta
            lower = max(lower, min(first, second))
            upper = min(upper, max(first, second))
            if lower > upper:
                return False
        return upper >= 0.0 and lower <= 1.0


def _solve_basis(basis, vector):
    """Express ``vector`` in the (possibly non-orthogonal) row basis."""

    (a, b, c), (d, e, f), (g, h, i) = basis
    # vector = x*row0 + y*row1 + z*row2  ->  solve the transposed system.
    det = a * (e * i - f * h) - d * (b * i - c * h) + g * (b * f - c * e)
    if abs(det) < 1e-9:
        return None
    x, y, z = vector
    # Cramer's rule on the transposed matrix.
    det_x = x * (e * i - f * h) - d * (y * i - z * h) + g * (y * f - z * e)
    det_y = a * (y * i - z * h) - x * (b * i - c * h) + g * (b * z - c * y)
    det_z = a * (e * z - f * y) - d * (b * z - c * y) + x * (b * f - c * e)
    return (det_x / det, det_y / det, det_z / det)


def _read_transform(value: bytes) -> tuple[tuple[float, ...], tuple[float, float, float], float]:
    import struct

    if not isinstance(value, (bytes, bytearray)) or len(value) != 64:
        raise ValueError("map transform must be 64 bytes")
    matrix = struct.unpack("<16f", bytes(value))
    position = (matrix[12], matrix[13], matrix[14])
    rotation = math.atan2(matrix[8], matrix[0])
    return matrix, position, rotation


def deduplicate_pw002_wave_three_warriors(map_id: str, rows):
    """Collapse the stacked PW002 gatekeeper rows into one authored actor.

    The Alliance Fortress map database contains eleven rows for the same
    leader template in EventSet 3438, all at one transform (eight leader rows
    and three sentinel rows whose NpcId also points at the leader). Those
    duplicated scene nodes materialize as a pile of identical dragon
    warriors. Keep one row at that location; preserve every other map spawn.
    """

    if map_key(map_id) != "pw002_alliance_fortress_01_dun":
        return tuple(rows)
    result = []
    seen = set()
    for row in rows:
        if isinstance(row, GatedSpawnRow):
            row_id = row.row_id
            event_set = row.event_set_row
            template = row.template_id
            position = row.position
        else:
            row_id, transform, template, event_set = row[:4]
            try:
                position = _read_transform(transform)[1]
            except (TypeError, ValueError):
                result.append(row)
                continue
        key = (
            int(event_set), str(template).casefold(),
            tuple(round(float(axis), 5) for axis in position),
        )
        if (
            int(event_set) == 3438
            and str(template).casefold() == "pw002_01_leader_warrior_heroic"
        ):
            if key in seen:
                continue
            seen.add(key)
        result.append(row)
    return tuple(result)


def deduplicate_parallel_world_gatekeeper_rows(map_id: str, rows):
    """Collapse exact stacked variation rows in Parallel World maps.

    Several Parallel World maps store the gatekeeper once per difficulty
    variation, with each row pointing at the same event set and transform.
    All variation rows become eligible together in the local event runtime,
    so retain one actor for each exact template/location while preserving
    rows at distinct locations and all non-Parallel-World content.
    """

    if not map_key(map_id).startswith("pw"):
        return tuple(rows)
    result = []
    seen = set()
    for row in rows:
        if isinstance(row, GatedSpawnRow):
            event_set = row.event_set_row
            template = row.template_id
            position = row.position
        else:
            row_id, transform, template, event_set = row[:4]
            try:
                position = _read_transform(transform)[1]
            except (TypeError, ValueError):
                result.append(row)
                continue
        key = (
            int(event_set), str(template).casefold(),
            tuple(round(float(axis), 5) for axis in position),
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(row)
    return tuple(result)


@dataclass(frozen=True)
class MapEventContent:
    map_id: str
    event_sets: Mapping[int, EventSetDefinition]
    referenced_events: frozenset[str]
    spawn_rows: tuple[GatedSpawnRow, ...]
    props: tuple[MapPropDefinition, ...]
    trigger_volumes: tuple[TriggerVolumeDefinition, ...]
    # difficulty tier -> (uncommon, magic, rare, epic) percentages
    chest_tier_chances: Mapping[int, tuple[int, int, int, int]] = field(
        default_factory=dict
    )

    def server_driven_event_sets(self, catalog) -> frozenset[int]:
        """EventSet rows whose flags a server-evaluated map-local event flips.

        The load-time population must leave these alone: the map-event runtime
        spawns their rows when the event starts, so materializing them at map
        load as well produced two bosses in the Balor arena.
        """

        rows = set()
        for row, definition in self.event_sets.items():
            for key in definition.events:
                event = catalog.event(key)
                if event is None or not event.applies_to(self.map_id):
                    continue
                if any(
                    rule is not None and rule.server_evaluated
                    for rule in (catalog.rule(s) for s in event.start_signals)
                ):
                    rows.add(row)
                    break
        return frozenset(rows)

    def event_sets_for(self, event_key: str) -> tuple[EventSetDefinition, ...]:
        return tuple(
            definition for definition in self.event_sets.values()
            if event_key in definition.events
        )

    def enabled_event_set_rows(self, active: frozenset[str]) -> frozenset[int]:
        return frozenset(
            row for row, definition in self.event_sets.items()
            if definition.satisfied(active)
        )

    def spawn_rows_for_sets(self, rows: Iterable[int]) -> tuple[GatedSpawnRow, ...]:
        wanted = {int(value) for value in rows}
        return tuple(row for row in self.spawn_rows if row.event_set_row in wanted)

    def props_for_sets(self, rows: Iterable[int]) -> tuple[MapPropDefinition, ...]:
        wanted = {int(value) for value in rows}
        return tuple(
            prop for prop in self.props
            if prop.event_set_row in wanted and prop.network_prop
        )

    def baseline_props(self) -> tuple[MapPropDefinition, ...]:
        return tuple(
            prop for prop in self.props
            if prop.always_present and prop.network_prop
        )

    def prop(self, prop_id: int) -> MapPropDefinition | None:
        for prop in self.props:
            if prop.prop_id == int(prop_id):
                return prop
        return None


def _load_map_event_content(
    database_path: str, map_id: str, signal_props_key: tuple[str, ...],
) -> MapEventContent:
    signal_props = frozenset(signal_props_key)
    quest_switches = _quest_switch_props()
    path = map_database_path(Path(database_path), map_id)
    level_variations = (
        _map_level_variations(path)
        if map_key(map_id) == "pw017_dragonslair_01_dun" else {}
    )
    with sqlite3.connect(
        f"file:{path.as_posix()}?mode=ro", uri=True
    ) as database:
        client_state_object_guids = active_client_instance_guid_overrides(
            database_path,
            map_id,
            "_Instance_StateObject",
            (
                "_ID", "Id", "Transform", "EventSetRow", "InteractOperation",
                "OperationId", "State",
            ),
        )
        event_sets: dict[int, EventSetDefinition] = {}
        for row_id, flags in database.execute(
            "SELECT Id, EventFlags FROM _Instance_EventSets"
        ):
            flags = effective_event_set_declarations(map_id, int(row_id), flags)
            required = set()
            forbidden = set()
            for declaration in str(flags or "").split(";"):
                name, separator, value = declaration.partition("=")
                if not separator or not name.strip():
                    continue
                target = (
                    required
                    if value.strip().casefold() == "true"
                    else forbidden
                )
                target.add(name.strip().casefold())
            try:
                key = int(row_id)
            except (TypeError, ValueError):
                continue
            event_sets[key] = EventSetDefinition(
                key, frozenset(required), frozenset(forbidden)
            )
        referenced = frozenset(
            event
            for definition in event_sets.values()
            for event in definition.events
        )

        spawn_rows = []
        spawn_variations = _map_variations(path)
        for (
            row_id, transform, template_id, event_set_row, parent_node_index,
        ) in database.execute(
            """
            SELECT rowid, Transform, NpcId, COALESCE(EventSetRow, -1),
                   COALESCE(ParentNodeIndex, 0)
            FROM _Instance_SpawnPoint
            WHERE TRIM(COALESCE(NpcId, ''))<>''
              AND COALESCE(AlliedToPlayer, 0)=0
              AND COALESCE(EventSetRow, -1)>=0
            ORDER BY rowid
            """
        ):
            try:
                _matrix, position, rotation = _read_transform(transform)
            except ValueError:
                continue
            variation_group, variation_difficulty = spawn_variations.get(
                int(parent_node_index or 0), (0, 0)
            )
            spawn_rows.append(GatedSpawnRow(
                int(row_id), int(event_set_row), str(template_id).strip(),
                position, rotation,
                variation_group=variation_group,
                variation_difficulty=variation_difficulty,
            ))
        if map_key(map_id) == "h0004_temple_dun":
            # The Hunt for a Legend sandhill phase stores its Sandsnake
            # leaders as blank-NpcId SpawnPoints. The linked StateObject
            # template names the monster and anchors, but the quest's
            # interaction event intentionally has no drop or Spawn entry;
            # killing these gated actors produces the tablet fragment.
            sandhill_event_sets = tuple(
                row for row, definition in event_sets.items()
                if "h0004_hunt_for_a_legend_05_activate_sandhills"
                in definition.events
            )
            if sandhill_event_sets:
                placeholders = ",".join("?" for _ in sandhill_event_sets)
                for row_id, transform, event_set_row in database.execute(
                    f"""
                    SELECT rowid, Transform, COALESCE(EventSetRow, -1)
                    FROM _Instance_SpawnPoint
                    WHERE lower(COALESCE(_ID, ''))=?
                      AND lower(COALESCE(Rank, ''))='rank_leader'
                      AND TRIM(COALESCE(NpcId, ''))=''
                      AND COALESCE(EventSetRow, -1) IN ({placeholders})
                    ORDER BY rowid
                    """,
                    (
                        "h0004_leader_sandsnake_melee_01_spawnpoint",
                        *sandhill_event_sets,
                    ),
                ):
                    try:
                        _matrix, position, rotation = _read_transform(transform)
                    except ValueError:
                        continue
                    spawn_rows.append(GatedSpawnRow(
                        int(row_id), int(event_set_row),
                        "h0004_leader_sandsnake_melee_01",
                        position, rotation,
                    ))
        spawn_rows = list(deduplicate_pw002_wave_three_warriors(
            map_id, spawn_rows,
        ))

        templates: dict[str, tuple[str, bool, str, str]] = {}
        template_keys: dict[str, tuple[str, int]] = {}
        quest_controlled_templates: set[str] = set()
        chest_tier_chances: dict[int, tuple[int, int, int, int]] = {}
        try:
            static_path = Path(database_path)
            with sqlite3.connect(
                f"file:{static_path.as_posix()}?mode=ro", uri=True
            ) as static_database:
                try:
                    for row in static_database.execute(
                        "SELECT Id, COALESCE(Uncommon, 0), COALESCE(Magic, 0), "
                        "COALESCE(Rare, 0), COALESCE(Epic, 0) "
                        "FROM _Template_MapDifficulty"
                    ):
                        chest_tier_chances[int(row[0])] = tuple(
                            int(value or 0) for value in row[1:5]
                        )
                except (sqlite3.Error, ValueError):
                    chest_tier_chances = {}
                for row in static_database.execute(
                    "SELECT Id, COALESCE(InteractOperation, ''), "
                    "COALESCE(OperationId, ''), COALESCE(OneShot, 0), "
                    "COALESCE(LootListId, ''), COALESCE(ChestId, ''), "
                    "COALESCE(IndividualUsage, 0), COALESCE(KeyId, ''), "
                    "COALESCE(KeyAmount, 0) "
                    "FROM _Template_StateObject"
                ):
                    templates[str(row[0])] = (
                        str(row[1]).strip() or str(row[2]).strip(),
                        bool(int(row[3] or 0)),
                        str(row[4]).strip(),
                        str(row[5]).strip(),
                        bool(int(row[6] or 0)),
                    )
                    template_keys[str(row[0])] = (
                        str(row[7]).strip(), max(0, int(row[8] or 0)),
                    )
                quest_controlled_templates = {
                    str(row[0]).casefold()
                    for row in static_database.execute(
                        "SELECT Id FROM _Template_StateObject "
                        "WHERE COALESCE(QuestControlled, 0)<>0"
                    )
                }
        except sqlite3.Error:
            templates = {}
            template_keys = {}
            quest_controlled_templates = set()

        props = []
        prop_actor_base = local_prop_actor_base(database_path, map_id)
        scene_row_limit = int(
            database.execute(
                "SELECT COALESCE(MAX(rowid), 0) FROM _Instance_StateObject"
            ).fetchone()[0]
        )
        for row in database.execute(
            """
            SELECT rowid, Guid, _ID, Id, Transform, COALESCE(EventSetRow, -1),
                   COALESCE(InteractOperation, ''), COALESCE(OperationId, ''),
                   COALESCE(OneShot, 0), COALESCE(LootListId, ''),
                   COALESCE(ChestId, ''), COALESCE(State, 'active'),
                   COALESCE(ParentNodeIndex, 0), COALESCE(KeyId, ''),
                   COALESCE(KeyAmount, 0)
            FROM _Instance_StateObject
            ORDER BY rowid
            """
        ):
            (
                row_id, guid, instance_id, template_id, transform,
                event_set_row, interact_operation, operation_id, one_shot,
                loot_list_id, chest_id, state, parent_node_index,
                key_id, key_amount,
            ) = row
            try:
                _matrix, position, rotation = _read_transform(transform)
            except ValueError:
                continue
            instance_id = str(instance_id or template_id or "")
            template_id = str(template_id or instance_id or "")
            authored_operation = (
                str(interact_operation).strip() or str(operation_id).strip()
            )
            workbench = bool(
                str(state).casefold() == "active"
                and (
                    authored_operation.startswith("OpenCraftingWindow")
                    or authored_operation.startswith("OpenFilterCraftingWindow:")
                )
            )
            signal_referenced = instance_id.casefold() in signal_props
            level_variation = level_variations.get(int(parent_node_index or 0))
            # QuestControlled scene entities are absent on the client until
            # their quest task is active. PropInfo with the authored GUID is
            # silently discarded when that scene entity is missing; an empty
            # GUID makes the client instantiate the template at this transform.
            quest_seal = (
                map_key(map_id) == "pw017_dragonslair_01_dun"
                and instance_id.casefold() in {"pw017_seal_01", "pw017_seal_02"}
            )
            quest_switch_id = (
                quest_switches.get(instance_id.casefold())
                or quest_switches.get(template_id.casefold(), "")
            )
            quest_controlled = (
                bool(quest_switch_id)
                and template_id.casefold() in quest_controlled_templates
            )
            props.append(MapPropDefinition(
                prop_id=prop_actor_base + int(row_id) - 1,
                row_id=int(row_id),
                kind="scene",
                instance_id=instance_id,
                template_id=template_id,
                instance_guid=(
                    ZERO_GUID if quest_seal or quest_controlled
                    else client_state_object_guids.get(
                        int(row_id), bytes(guid) if guid else ZERO_GUID
                    )
                ),
                position=position,
                rotation=rotation,
                event_set_row=int(event_set_row),
                operation=authored_operation,
                one_shot=bool(int(one_shot or 0)),
                individual_usage=templates.get(template_id, ("", False, "", "", False))[4],
                loot_list_id=str(loot_list_id).strip(),
                chest_id=str(chest_id).strip(),
                signal_referenced=signal_referenced,
                workbench=workbench,
                quest_switch_id=quest_switch_id,
                parent_node_index=int(parent_node_index or 0),
                variation_group=(level_variation[0] if level_variation else 0),
                variation_level=(level_variation[1] if level_variation else 0),
                key_id=str(key_id).strip(),
                key_amount=max(0, int(key_amount or 0)),
            ))
        try:
            spawn_point_rows = database.execute(
                """
                SELECT rowid, _ID, StateObjectId, Transform,
                       COALESCE(EventSetRow, -1)
                FROM _Instance_StateObjectSpawnPoint
                WHERE TRIM(COALESCE(StateObjectId, ''))<>''
                ORDER BY rowid
                """
            ).fetchall()
        except sqlite3.OperationalError:
            spawn_point_rows = []
        for row_id, instance_id, template_id, transform, event_set_row in spawn_point_rows:
            try:
                _matrix, position, rotation = _read_transform(transform)
            except ValueError:
                continue
            template_id = str(template_id).strip()
            instance_id = str(instance_id or template_id)
            operation, one_shot, loot_list_id, chest_id, individual_usage = templates.get(
                template_id, ("", False, "", "", False)
            )
            signal_referenced = (
                instance_id.casefold() in signal_props
                or template_id.casefold() in signal_props
            )
            props.append(MapPropDefinition(
                prop_id=spawn_prop_id(
                    scene_row_limit,
                    row_id,
                    prop_actor_base=prop_actor_base,
                ),
                row_id=int(row_id),
                kind="spawn",
                instance_id=instance_id,
                template_id=template_id,
                instance_guid=ZERO_GUID,
                position=position,
                rotation=rotation,
                event_set_row=int(event_set_row),
                operation=operation,
                one_shot=one_shot,
                individual_usage=individual_usage,
                loot_list_id=loot_list_id,
                chest_id=chest_id,
                signal_referenced=signal_referenced,
                workbench=False,
                # Spawn-point _ID is a placement label, while the quest
                # Switch names the StateObjectId template (StateObjectId).
                # Match either so quest-controlled spawn props are published
                # and revealed when their task is active.
                quest_switch_id=(
                    quest_switches.get(instance_id.casefold())
                    or quest_switches.get(template_id.casefold(), "")
                ),
                key_id=template_keys.get(template_id, ("", 0))[0],
                key_amount=template_keys.get(template_id, ("", 0))[1],
            ))

        volumes = []
        for row in database.execute(
            """
            SELECT rowid, _ID, Id, Transform, COALESCE(EventSetRow, -1),
                   COALESCE(PerPlayer, 0), COALESCE(TriggeredEventCategory, ''),
                   COALESCE(TriggeredEventId, '')
            FROM _Instance_TriggerVolume
            ORDER BY rowid
            """
        ):
            (
                row_id, instance_id, template_id, transform, event_set_row,
                per_player, category, triggered_event_id,
            ) = row
            try:
                matrix, _position, _rotation = _read_transform(transform)
            except ValueError:
                continue
            volumes.append(TriggerVolumeDefinition(
                row_id=int(row_id),
                trigger_id=str(instance_id or template_id or ""),
                event_set_row=int(event_set_row),
                per_player=bool(int(per_player or 0)),
                category=str(category),
                matrix=matrix,
                triggered_event_id=str(triggered_event_id or ""),
            ))
    # Authored reward chests in the parallel-world boss arenas are
    # MapVariation nodes for the difficulty tiers.  They share one transform
    # per arena, so publishing every variant makes all difficulties' chests
    # stack.  Tag each with its variation tier; the PropInfo senders keep only
    # the viewer's tier.
    chest_props = [
        prop for prop in props
        if prop.instance_id.casefold() in PARALLEL_WORLD_REWARD_CHEST_INSTANCE_IDS
    ]
    if chest_props:
        variations = _map_variations(path)
        # ClientPropManager::HandlePropInfoCommand only creates a prop
        # entity when the entry carries a zero GUID; a non-zero GUID goes
        # through LookupPropEntity and the whole entry is dropped when the
        # scene has no entity with that GUID.  The authored chest GUIDs are
        # quest-controlled, so they never exist for a player without the
        # reward quest and both the initial snapshot and the map-entry
        # replay were silently ignored.  Publish the chests under fresh
        # prop ids with a zero GUID so the client instantiates them from
        # the non-quest templates instead.
        next_index = max(prop_index(prop.prop_id) for prop in props) + 1
        if (
            prop_index(prop_actor_base) + next_index + len(chest_props)
            > PROP_INDEX_LIMIT
        ):
            raise ValueError(
                f"map {map_id!r} has no free reward chest prop slots"
            )
        replacements = {}
        for offset, prop in enumerate(chest_props):
            replacements[prop.prop_id] = replace(
                prop,
                prop_id=prop_actor_base + next_index + offset,
                # These visual twins use the same chest graphics and state
                # files as pw_run_lootchest, but are not QuestControlled in
                # the client data.
                template_id=(
                    "t000_state_chest_loot_05"
                    if prop.instance_id.casefold() == "pw_run_lootchest_gold"
                    else "t000_state_chest_loot_04"
                ),
                instance_guid=ZERO_GUID,
                variation_group=variations.get(
                    prop.parent_node_index, (0, 0),
                )[0],
                variation_difficulty=variations.get(
                    prop.parent_node_index, (0, 0),
                )[1],
            )
        props = [
            replacements.get(prop.prop_id, prop) for prop in props
        ]

    return MapEventContent(
        map_id=str(map_id),
        event_sets=MappingProxyType(event_sets),
        referenced_events=referenced,
        spawn_rows=tuple(spawn_rows),
        props=tuple(props),
        trigger_volumes=tuple(volumes),
        chest_tier_chances=MappingProxyType(chest_tier_chances),
    )


_map_content_cache = lru_cache(maxsize=64)(_load_map_event_content)


@lru_cache(maxsize=1)
def _quest_switch_props() -> Mapping[str, str]:
    """prop instance id -> quest switch id, from data/quests.xml when present."""

    try:
        from dso_server.gameplay.quest_content import load_quest_catalog
        from dso_server.paths import DEFAULT_QUESTS_PATH
        return load_quest_catalog(DEFAULT_QUESTS_PATH).switch_props
    except Exception:
        return MappingProxyType({})


def load_map_event_content(
    static_database_path: str | Path, map_id: str,
) -> MapEventContent:
    catalog = load_map_signal_catalog(static_database_path)
    signal_props = tuple(sorted({
        param
        for rule in catalog.rules_by_signal_id.values()
        if rule.event_type == "playerinteractedprop"
        for param in rule.params
    }))
    return _map_content_cache(
        str(resident_content.resolved(static_database_path)), str(map_id), signal_props
    )


@dataclass
class PropRuntimeState:
    definition: MapPropDefinition
    state: int = PROP_STATE_ACTIVE

    def new_prop(self) -> NewProp:
        return self.definition.new_prop(self.state)


@dataclass(frozen=True)
class EventTransition:
    event: MapLocalEventDefinition
    active: bool
    enabled_sets: tuple[int, ...]
    disabled_sets: tuple[int, ...]
    announce: bool


@dataclass
class MapEventRuntime:
    """Mutable scripted-event state of one map instance.

    Every session on the instance shares one runtime, like the combat state.
    The lock protects only small deterministic mutation; callers build
    packets and send them after releasing it.
    """

    content: MapEventContent
    catalog: MapSignalCatalog
    instance_key: tuple
    initial_flags: frozenset[str]
    grimmagstone_gatekeeper_defeated: bool = False
    parallel_world_gatekeepers_defeated: set[str] = field(default_factory=set)
    monster_level: int | None = None
    scaling_difficulty: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)
    # Serialize event transitions through publication without holding the
    # short-lived state lock during packet construction or network delivery.
    transition_lock: threading.RLock = field(default_factory=threading.RLock)
    running: dict[str, float] = field(default_factory=dict)
    pending: dict[str, object] = field(default_factory=dict)
    pending_start_deadlines: dict[str, float] = field(default_factory=dict)
    fractured_planes_limit_deadline: float | None = None
    fractured_planes_limit_timer: object | None = None
    fractured_planes_tower_timer: object | None = None
    fractured_planes_floor_completed: bool = False
    pending_start_tokens: dict[str, object] = field(default_factory=dict)
    wave_countdown_digits: dict[str, int] = field(default_factory=dict)
    instance_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    completed_quests: set[tuple[int, str]] = field(default_factory=set)
    enabled_event_sets: set[int] = field(default_factory=set)
    # Rows the server turned off itself because the content they gate is spent
    # (Fractured Planes trap and debuff auras). ``_transition`` must not bring
    # them back: their authored release signal never fires on these maps.
    suppressed_event_sets: set[int] = field(default_factory=set)
    props: dict[int, PropRuntimeState] = field(default_factory=dict)
    # StateObject IndividualUsage is scoped to a player and this map instance.
    individual_prop_usage: set[tuple[int, int]] = field(default_factory=set)
    # Quest placements used in this map instance: (player, switch id, prop id).
    # Retail refreshes props with the map, so a new run offers them again; a
    # later task's own switch may also reuse the same placement.
    quest_prop_usage: set[tuple[int, str, int]] = field(default_factory=set)
    # Parallel-world arena reward chest props are visible on entry but must
    # reject interaction until the arena boss dies.
    boss_reward_chests_unlocked: set[str] = field(default_factory=set)
    event_set_actor_ids: dict[int, set[int]] = field(default_factory=dict)
    # Monotonic animation anchors for rotating tower beams, shared by every
    # player in this map instance so server hit checks agree with one another.
    tower_beam_started_at: dict[int, float] = field(default_factory=dict)
    fired_once: set[str] = field(default_factory=set)
    published_exit_events: set[tuple[int, str]] = field(default_factory=set)
    f4203_ram_actor_id: int | None = None
    f4203_ram_goal: tuple[float, float, float] | None = None
    f4203_ram_door_event_set_row: int | None = None
    f4203_ram_door_obstacle_id: str = "door_01"
    f4203_ram_path_started: bool = False
    f4203_ram_impact_started: bool = False
    f4203_ram_gate_opened: bool = False
    kill_score: int = 0
    fractured_planes_kill_seen: bool = False
    signal_log: deque = field(default_factory=lambda: deque(maxlen=64))
    generation: int = 0

    def __post_init__(self) -> None:
        self.initial_flags = frozenset(
            str(value).casefold() for value in self.initial_flags
        )
        self.enabled_event_sets = set(
            self.content.enabled_event_set_rows(self.initial_flags)
        )
        for definition in self._select_chest_variants(
            self.content.baseline_props()
        ):
            self.props[definition.prop_id] = PropRuntimeState(definition)
        for definition in self.select_props(
            self.content.props_for_sets(self.enabled_event_sets)
        ):
            self.props.setdefault(definition.prop_id, PropRuntimeState(definition))

    @property
    def map_id(self) -> str:
        return self.content.map_id

    def active_flags(self) -> frozenset[str]:
        return self.initial_flags | frozenset(self.running)

    def running_events(self) -> frozenset[str]:
        return frozenset(self.running)

    def running_definitions(self) -> tuple[MapLocalEventDefinition, ...]:
        return tuple(
            definition
            for key in sorted(self.running)
            for definition in (self.catalog.event(key),)
            if definition is not None
        )

    def published_props(self) -> tuple[PropRuntimeState, ...]:
        return tuple(self.props[key] for key in sorted(self.props))

    def active_trigger_volumes(self) -> tuple[TriggerVolumeDefinition, ...]:
        return tuple(
            volume for volume in self.content.trigger_volumes
            if volume.event_set_row < 0
            or volume.event_set_row in self.enabled_event_sets
        )

    def _transition(
        self, definition: MapLocalEventDefinition, active: bool,
    ) -> EventTransition:
        enabled, disabled = self._update_event_sets((definition.key,))
        return EventTransition(
            event=definition,
            active=active,
            enabled_sets=enabled,
            disabled_sets=disabled,
            # Retail announces every map-local event it starts, including
            # ones no EventSet on the map references (BOSS_death_dtu).
            announce=True,
        )

    def replace_global_flags(
        self, known_flags: Iterable[str], active_flags: Iterable[str],
    ) -> tuple[tuple[int, ...], tuple[int, ...]]:
        """Reconcile scheduled flags without restarting local map events.

        Call under ``lock``. ``known_flags`` includes inactive and removed
        schedule entries, so a flag captured at map entry can be cleared even
        when its deadline has already elapsed. Quest/access flags, running
        events, pending timers and permanently suppressed sets retain ownership.
        """

        before = self.active_flags()
        owned = frozenset(str(flag).casefold() for flag in known_flags)
        active = frozenset(str(flag).casefold() for flag in active_flags)
        self.initial_flags = (self.initial_flags - owned) | active
        changed = before ^ self.active_flags()
        if not changed:
            return (), ()
        self.generation += 1
        return self._update_event_sets(changed)

    def _update_event_sets(
        self, changed_flags: Iterable[str],
    ) -> tuple[tuple[int, ...], tuple[int, ...]]:
        flags = self.active_flags()
        enabled = []
        disabled = []
        affected = {
            event_set.row: event_set
            for flag in changed_flags
            for event_set in self.content.event_sets_for(flag)
        }
        for event_set in affected.values():
            satisfied = event_set.satisfied(flags)
            currently = event_set.row in self.enabled_event_sets
            if satisfied and not currently:
                if event_set.row in self.suppressed_event_sets:
                    continue
                self.enabled_event_sets.add(event_set.row)
                enabled.append(event_set.row)
            elif currently and not satisfied:
                self.enabled_event_sets.discard(event_set.row)
                disabled.append(event_set.row)
        return tuple(sorted(enabled)), tuple(sorted(disabled))

    def begin_event(
        self, definition: MapLocalEventDefinition, *, now: float | None = None,
    ) -> EventTransition | None:
        if definition.key in self.running:
            return None
        self.pending.pop(definition.key, None)
        self.pending_start_deadlines.pop(definition.key, None)
        self.pending_start_tokens.pop(definition.key, None)
        self.wave_countdown_digits.pop(definition.key, None)
        self.running[definition.key] = time.perf_counter() if now is None else now
        self.generation += 1
        return self._transition(definition, True)

    def end_event(
        self, definition: MapLocalEventDefinition,
    ) -> EventTransition | None:
        self.pending.pop(definition.key, None)
        self.pending_start_deadlines.pop(definition.key, None)
        self.pending_start_tokens.pop(definition.key, None)
        self.wave_countdown_digits.pop(definition.key, None)
        if self.running.pop(definition.key, None) is None:
            return None
        self.generation += 1
        return self._transition(definition, False)

    def suppress_event_sets(self, rows: Iterable[int]) -> tuple[int, ...]:
        """Force these EventSet rows off for the rest of the instance.

        Returns the rows that were actually enabled and are now off, so the
        caller can tell whether anything changed. Suppression is permanent by
        design: it is how the server retires Fractured Planes trap auras whose
        authored release signal only ever fires from retail's static selector.
        """

        removed = []
        for row in rows:
            row = int(row)
            self.suppressed_event_sets.add(row)
            if row in self.enabled_event_sets:
                self.enabled_event_sets.discard(row)
                removed.append(row)
        if removed:
            self.generation += 1
        return tuple(removed)

    def gated_spawn_rows(self, rows: Iterable[int]) -> tuple[GatedSpawnRow, ...]:
        """Gated monster rows for these sets, minus tiers this instance lacks."""

        selected = select_variation_spawn_rows(
            (
                row for row in self.content.spawn_rows_for_sets(rows)
                if not bloodshed_only_template(row.template_id)
                or int(self.scaling_difficulty) >= BLOODSHED_DIFFICULTY
            ),
            self.scaling_difficulty,
        )

        if map_key(self.map_id) == "pw001_grimmagstone_01_dun":
            # Extracted assets may omit the MVR selecting Raazghul's three
            # overlapping tier placements. This is one encounter, not three
            # bosses; preserve its six separately authored archer adds.
            seen_boss = False
            result = []
            for row in selected:
                if row.template_id == "pw001_01_leader_undeadmage_heroic":
                    if seen_boss:
                        continue
                    seen_boss = True
                result.append(row)
            return tuple(result)
        return selected

    def select_props(
        self, definitions: Iterable[MapPropDefinition],
    ) -> tuple[MapPropDefinition, ...]:
        """Reduce overlapping spawn-point props to the one this instance shows.

        Scene objects pass through untouched.  Spawn-point props within
        ``CHEST_LOCATION_RADIUS`` of each other in the same EventSet form one
        location; ``normal_*`` templates are kept only on tier 0 and
        ``scaling_*`` ones only above it, then one rarity is rolled from
        ``_Template_MapDifficulty`` (epic, rare, magic, otherwise free), with a
        plain chest preferred over its treasure-goblin twin.  The roll is
        stable per instance and location so every joiner sees the same chest.
        Stacked scene amphorae are reduced by ``_select_chest_variants``.
        """

        selected = []
        locations = []
        for definition in self._select_chest_variants(definitions):
            if definition.kind != "spawn":
                selected.append(definition)
                continue
            for row, anchor, members in locations:
                if row == definition.event_set_row and all(
                    abs(a - b) <= CHEST_LOCATION_RADIUS
                    for a, b in zip(anchor, definition.position)
                ):
                    members.append(definition)
                    break
            else:
                locations.append(
                    (definition.event_set_row, definition.position, [definition])
                )
        tier = max(0, int(self.scaling_difficulty))
        wanted_prefix = "normal_" if tier == 0 else "scaling_"
        other_prefix = "scaling_" if tier == 0 else "normal_"
        _uncommon, magic, rare, epic = self.content.chest_tier_chances.get(
            tier, (0, 0, 0, 0)
        )
        for row, anchor, members in locations:
            if len(members) == 1:
                selected.append(members[0])
                continue
            candidates = [
                m for m in members
                if not m.template_id.casefold().startswith(other_prefix)
            ] or members
            preferred = [
                m for m in candidates
                if m.template_id.casefold().startswith(wanted_prefix)
            ]
            if preferred:
                candidates = preferred
            roll = random.Random(
                f"{self.instance_key}:{row}:{anchor[0]:.1f}:{anchor[2]:.1f}"
            ).uniform(0.0, 100.0)
            if roll < epic:
                marker = "_epic_"
            elif roll < epic + rare:
                marker = "_rare_"
            elif roll < epic + rare + magic:
                marker = "_magic_"
            else:
                marker = ""
            def in_tier(definition: MapPropDefinition) -> bool:
                key = definition.template_id.casefold()
                if marker:
                    return marker in key
                return not any(mk in key for mk in _CHEST_TIER_MARKERS)

            tiered = [m for m in candidates if in_tier(m)] or candidates
            plain = [m for m in tiered if "goblin" not in m.template_id.casefold()]
            selected.append(min(plain or tiered, key=lambda m: m.row_id))
        return tuple(selected)

    def _select_chest_variants(
        self, definitions: Iterable[MapPropDefinition],
    ) -> tuple[MapPropDefinition, ...]:
        """Keep one scene amphora per authored spot.

        Event maps stack a spot's variants on one transform (normal and
        golden; small, normal and golden) and retail showed one of them:
        on e_dra_03_dun_01 it published only the normal amphora of a
        normal/golden pair (2026-10-28 capture). The pick is weighted by key
        cost and stable per instance and spot. Cursed amphorae have no loot
        list, so they are never published and never chosen.
        """

        selected = []
        spots = []
        for definition in definitions:
            if definition.kind != "scene" or not has_prop_loot(
                definition.loot_list_id or definition.chest_id
            ):
                selected.append(definition)
                continue
            for row, anchor, members in spots:
                if row == definition.event_set_row and all(
                    abs(a - b) <= CHEST_LOCATION_RADIUS
                    for a, b in zip(anchor, definition.position)
                ):
                    members.append(definition)
                    break
            else:
                spots.append(
                    (definition.event_set_row, definition.position, [definition])
                )
        for row, anchor, members in spots:
            if len(members) == 1:
                selected.append(members[0])
                continue
            members.sort(key=lambda value: value.row_id)
            weights = [
                _CHEST_VARIANT_WEIGHTS.get(
                    min(int(value.key_amount), 5) if value.key_id else 0,
                    _CHEST_VARIANT_WEIGHTS[5],
                )
                for value in members
            ]
            selected.append(random.Random(
                f"{self.instance_key}:chest:{row}:{anchor[0]:.1f}:{anchor[2]:.1f}"
            ).choices(members, weights=weights)[0])
        return tuple(selected)

    def publish_props(
        self, definitions: Iterable[MapPropDefinition],
    ) -> tuple[PropRuntimeState, ...]:
        published = []
        for definition in definitions:
            if definition.prop_id in self.props:
                continue
            state = PropRuntimeState(definition)
            self.props[definition.prop_id] = state
            published.append(state)
        return tuple(published)

    def unpublish_props(self, prop_ids: Iterable[int]) -> tuple[int, ...]:
        removed = []
        for prop_id in prop_ids:
            if self.props.pop(int(prop_id), None) is not None:
                removed.append(int(prop_id))
        return tuple(removed)

    def interact(self, prop_id: int) -> tuple[PropRuntimeState, bool] | None:
        """Record one interaction; the flag says whether the state flipped."""

        state = self.props.get(int(prop_id))
        if state is None:
            return None
        if state.state != PROP_STATE_ACTIVE:
            return state, False
        if state.definition.one_shot and not state.definition.workbench:
            state.state = PROP_STATE_USED
            return state, True
        return state, False


__all__ = [
    "CLIENT_REPORTED_SIGNAL_TYPES",
    "WIRE_PROP_OPERATION",
    "EventSetDefinition",
    "EventTransition",
    "GatedSpawnRow",
    "MapEventContent",
    "MapEventRuntime",
    "MapLocalEventDefinition",
    "MapPropDefinition",
    "MapSignalCatalog",
    "PropRuntimeState",
    "SERVER_SIGNAL_TYPES",
    "BLOODSHED_DIFFICULTY",
    "CHEST_LOCATION_RADIUS",
    "PROP_INDEX_LIMIT",
    "bloodshed_only_template",
    "prop_index",
    "spawn_prop_id",
    "SignalContext",
    "SignalRule",
    "TriggerVolumeDefinition",
    "load_map_event_content",
    "load_map_signal_catalog",
    "map_key",
]
