"""Quest definitions and the token engine that drives task progress.

``data/quests.xml`` is the client's ``quests.pbxml`` decoded losslessly
(docs/QUESTS.md).  A quest is a chain of tasks; every task is a small token
machine: ``<Event>`` elements turn world happenings (a kill, a pickup, a
prop interaction, an NPC dialog, a trigger volume) into ``<Return>`` tokens
when their ``<Guard>`` conditions hold, and the task is done once its own
``has`` guard sees enough of its completion token.  The wire side of quests
is in docs/QUEST_PROTOCOL.md; this module knows nothing about packets.

Simplifications, all deliberate and logged where they apply:

* Ground ``<Drop>`` items are returned to the service for materialization;
  only a successful pickup feeds ``collect``. Direct-inventory drops retain
  immediate collection credit (their inventory grant is not modeled here).
* Hold-ground bindings retain difficulty configurations and guards. Their
  authoritative encounters feed success/failure through the same token engine.
  ``battle`` and ``victory`` use the battleground's id when a match finishes.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from functools import lru_cache
from pathlib import Path
import random
from types import MappingProxyType
from typing import Iterable, Mapping
import xml.etree.ElementTree as ET
from dso_server.gameplay.status_modifier_runtime import ModifierAmounts
from dso_server.runtime.file_revisions import observed_revision


# QuestState values carried by QuestUpdate/QuestLogInfo (docs/QUEST_PROTOCOL.md §3).
QUEST_STATE_ACCEPTED = 0
QUEST_STATE_FINISHED = 1
QUEST_STATE_CLOSED = 2
# ClientQuestManager state 3 removes the quest and clears its guidance.
QUEST_STATE_CANCELLED = 3
# Native QuestFailed; acceptance resets its tokens and counters (client 0x1409e0604).
QUEST_STATE_FAILED = 4

# Task counters per quest in QuestLogInfo (Game::QuestLog MaxTasksCount).
MAX_TASKS = 16

NO_TIMESTAMP = "0000-00-00 00:00:00"

# Game::QuestInfo::Type, recovered from LoadQuestsFromXML (0x1409a7a04).
# These values drive NPC and prop indications, not the quest's progress state.
QUEST_TYPE_IDS = MappingProxyType({
    "repeatable": 0, "heroicrepeatable": 1, "eventrepeatable": 2,
    "pvprepeatable": 3, "rowrepeatable": 4, "local": 5, "world": 6,
    "heroic": 7, "event": 8, "pvp": 9, "crafting": 10,
    "craftingrepeatable": 11, "wantedrepeatable": 12, "treasurediggingrepeatable": 13,
})

UNSUPPORTED_EVENT_TYPES = frozenset()

# A few decoded client quest tasks omit their ExploreTask trigger Event. Keep
# these runtime compatibility bindings here instead of patching quest data.
_EXPLORE_TASK_TRIGGER_OVERRIDES = MappingProxyType({
    (
        "h0001-hunt-for-the-lantern-09-n",
        "h0001-hunt-for-the-lantern-09-explore-01",
    ): "h0007explore01",
})

# Telepolos' main story asks the player to collect four Catapult Stones from
# Wandering Stone Golems, but the extracted quest XML sets this story drop to
# only 10%. Keep this required progression item deterministic in the server
# runtime so the catapult objective cannot stall on repeated failed rolls.
_QUEST_DROP_CHANCE_OVERRIDES = MappingProxyType({
    (
        "pw205-a-helios-war-07-n",
        "pw205_a_helios_war_07_stone",
    ): 1.0,
})


def explore_trigger_for_task(
    quest: "QuestDefinition", task: "TaskDefinition",
) -> str:
    """Return a runtime trigger for authored ExploreTasks missing an Event."""

    if str(task.task_type or "").casefold() != "exploretask":
        return ""
    return _EXPLORE_TASK_TRIGGER_OVERRIDES.get((
        str(quest.quest_id or "").casefold(),
        str(task.title or "").casefold(),
    ), "")


def _tokens(value: str | None) -> tuple[str, ...]:
    return tuple(
        token.strip() for token in str(value or "").split(";") if token.strip()
    )


def _bool(value: str | None) -> bool:
    return str(value or "").strip().casefold() in ("true", "1", "yes")


@dataclass(frozen=True)
class Guard:
    kind: str          # "has" or "hasnot"
    token: str
    amount: int = 1

    def holds(self, tokens: Mapping[str, int]) -> bool:
        count = int(tokens.get(self.token, 0))
        if self.kind == "has":
            return count >= self.amount
        return count < max(1, self.amount)


@dataclass(frozen=True)
class Drop:
    item_id: str
    amount: int
    chance: float
    to_inventory: bool


@dataclass(frozen=True)
class QuestSpawn:
    template_id: str
    difficulty: str
    level: int
    location: str


@dataclass(frozen=True)
class TaskEvent:
    kind: str                       # kill, collect, interact, talk, trigger, ...
    value: str
    guards: tuple[Guard, ...]
    returns: tuple[str, ...]
    drops: tuple[Drop, ...]
    return_amounts: Mapping[str, int] = field(default_factory=lambda: MappingProxyType({}))
    spawns: tuple[QuestSpawn, ...] = ()
    failure: bool = False


@dataclass(frozen=True)
class SwitchDefinition:
    """A ``<Switch>``: the prop a task turns into an interact target.

    The prop is live for a player only while the owning task is open and
    ``guards`` hold on the player's tokens; ``switch_id`` is the operation
    string the prop carries on the wire and the client echoes back.
    """

    switch_id: str
    prop: str                       # prop instance id (_Instance_StateObject._ID)
    item: str
    amount: int
    consume: bool
    guards: tuple[Guard, ...]

    def live(self, tokens: Mapping[str, int]) -> bool:
        return all(guard.holds(tokens) for guard in self.guards)


@dataclass(frozen=True)
class BarterDefinition:
    barter_id: str
    actor: str
    items: tuple[str, ...]  # Semicolon-separated alternatives count together.
    amount: int
    reply: bool
    only_show: bool
    guards: tuple[Guard, ...]
    event_kind: str = "deliver"

    def live(self, tokens: Mapping[str, int]) -> bool:
        return all(guard.holds(tokens) for guard in self.guards)


@dataclass(frozen=True)
class HoldGroundDefinition:
    """An authored defense interaction and its difficulty-specific setup."""

    holdground_id: str
    actor: str
    configurations: tuple[tuple[int | None, str], ...]
    guards: tuple[Guard, ...] = ()

    def live(self, tokens: Mapping[str, int]) -> bool:
        return all(guard.holds(tokens) for guard in self.guards)

    def config_for_difficulty(self, difficulty: int) -> str:
        exact = next((value for tier, value in self.configurations
                      if tier == int(difficulty)), "")
        return exact or next((value for tier, value in self.configurations if tier is None), "")


@dataclass(frozen=True)
class TaskDefinition:
    index: int
    title: str
    task_type: str
    maps: frozenset[str]
    returns: tuple[str, ...]        # tokens granted when the task completes
    done_guards: tuple[Guard, ...]  # the task is done once these hold
    events: tuple[TaskEvent, ...]
    switches: Mapping[str, str]     # switch id -> prop instance id
    dialogs: Mapping[str, str]      # dialog id -> NPC template id
    clear_regions: Mapping[str, str]  # clear-region id -> monster template
    group: bool
    show_guidance: bool
    switch_definitions: tuple[SwitchDefinition, ...] = ()
    pre_tasks: tuple[str, ...] = ()  # titles that must be finished first
    barters: tuple[BarterDefinition, ...] = ()
    return_amounts: Mapping[str, int] = field(default_factory=lambda: MappingProxyType({}))
    holdgrounds: tuple[HoldGroundDefinition, ...] = ()

    @property
    def counter_guard(self) -> Guard | None:
        """Track this task's own event token, rather than a prerequisite token."""
        produced = {token for event in self.events for token in event.returns}
        guards = tuple(g for g in self.done_guards if g.kind == "has")
        return next((g for g in guards if g.token in produced), guards[0] if guards else None)

    @property
    def target(self) -> int:
        guard = self.counter_guard
        return max(1, guard.amount) if guard else 1

    @property
    def counter_token(self) -> str:
        guard = self.counter_guard
        return guard.token if guard else ""

    def is_done(self, tokens: Mapping[str, int]) -> bool:
        return bool(self.done_guards) and all(
            guard.holds(tokens) for guard in self.done_guards
        )

    def counter(self, tokens: Mapping[str, int]) -> int:
        token = self.counter_token
        if not token:
            return 0
        return min(self.target, int(tokens.get(token, 0)))

    @property
    def event_kinds(self) -> frozenset[str]:
        return frozenset(event.kind for event in self.events)


@dataclass(frozen=True)
class QuestReward:
    kind: str       # xp, vc (gold), rc (andermant), loot, statuseffect, dynamicXP
    value: str
    amount: int = 1
    class_name: str = ""


@dataclass(frozen=True)
class QuestDefinition:
    quest_id: str
    giver: str
    goal: str
    start_signal: str
    stop_signal: str
    quest_type: str
    level: int
    min_level: int
    max_level: int
    class_name: str                 # "", "ranger", "notdwarf", ...
    required: tuple[str, ...]
    required_operator: str          # ANY / ALL
    followups: tuple[str, ...]
    chapter_id: str
    maps: frozenset[str]
    staged_tasks: bool
    deletable: bool
    event_id: str
    tasks: tuple[TaskDefinition, ...]
    rewards: tuple[QuestReward, ...]
    bound_to_map: bool = False
    accept_inventory_drops: tuple[Drop, ...] = ()

    @property
    def key(self) -> str:
        return self.quest_id.casefold()

    @property
    def quest_type_id(self) -> int:
        return QUEST_TYPE_IDS[self.quest_type.casefold()]

    @property
    def repeatable(self) -> bool:
        return "repeatable" in self.quest_type.casefold()

    @property
    def auto_start(self) -> bool:
        """Quests without a giver start themselves when their signal fires."""

        return not self.giver and bool(self.start_signal)

    def accepts_class(self, class_name: str) -> bool:
        wanted = self.class_name.casefold()
        actual = str(class_name or "").casefold()
        if not wanted:
            return True
        if wanted.startswith("not"):
            return actual != wanted[3:]
        return actual == wanted

    def accepts_level(self, level: int) -> bool:
        level = int(level)
        if self.min_level and level < self.min_level:
            return False
        if self.max_level and level > self.max_level:
            return False
        return True

    def requirements_met(self, closed_quests: Iterable[str]) -> bool:
        if not self.required:
            return True
        closed = {str(value).casefold() for value in closed_quests}
        met = [quest.casefold() in closed for quest in self.required]
        if self.required_operator.casefold() == "all":
            return all(met)
        return any(met)

    def event_requirement_met(self, active_events: Iterable[str]) -> bool:
        """A semicolon-separated event list allows any one active event."""

        required = {value.casefold() for value in _tokens(self.event_id)}
        return not required or not required.isdisjoint(
            str(value).strip().casefold() for value in active_events
        )

    def rewards_for_class(self, class_name: str) -> tuple[QuestReward, ...]:
        actual = str(class_name or "").casefold()
        return tuple(
            reward for reward in self.rewards
            if not reward.class_name or reward.class_name.casefold() == actual
        )


@dataclass
class QuestProgress:
    """Mutable per-character state of one quest, persisted as a document."""

    quest_id: str
    state: int = QUEST_STATE_ACCEPTED
    tokens: dict[str, int] = field(default_factory=dict)
    finished_mask: int = 0
    completed_at: str = NO_TIMESTAMP
    map_instance: str = ""
    continue_on_map: str = ""
    # Authored prop placements already consumed by each switch in this quest
    # attempt. Network actor ids and map runtime instances are not persistent.
    used_props: dict[str, set[str]] = field(default_factory=dict)

    def to_document(self) -> dict:
        document = {
            "state": int(self.state),
            "tokens": {str(k): int(v) for k, v in self.tokens.items()},
            "finished_mask": int(self.finished_mask),
            "completed_at": str(self.completed_at),
            "map_instance": self.map_instance,
            "continue_on_map": self.continue_on_map,
        }
        if self.used_props:
            document["used_props"] = {
                key: sorted(values) for key, values in self.used_props.items()
            }
        return document

    @classmethod
    def from_document(cls, quest_id: str, document: Mapping) -> "QuestProgress":
        tokens = document.get("tokens", {})
        used_props = document.get("used_props", {})
        return cls(
            quest_id=str(quest_id),
            state=int(document.get("state", QUEST_STATE_ACCEPTED)),
            tokens={
                str(k): int(v) for k, v in tokens.items()
            } if isinstance(tokens, Mapping) else {},
            finished_mask=int(document.get("finished_mask", 0)),
            completed_at=str(document.get("completed_at", NO_TIMESTAMP)),
            map_instance=str(document.get("map_instance", "")),
            continue_on_map=str(document.get("continue_on_map", "")),
            used_props={
                str(key): {str(value) for value in values}
                for key, values in used_props.items()
                if isinstance(values, (list, tuple, set))
            } if isinstance(used_props, Mapping) else {},
        )

    def task_finished(self, index: int) -> bool:
        return bool(self.finished_mask & (1 << int(index)))


@dataclass(frozen=True)
class TaskChange:
    """One counter change or completion the engine produced."""

    task: TaskDefinition
    progress: int
    target: int
    finished: bool


@dataclass(frozen=True)
class QuestEventResult:
    changes: tuple[TaskChange, ...]
    quest_finished: bool
    unsupported: tuple[str, ...] = ()
    drops: tuple[Drop, ...] = ()
    spawns: tuple[QuestSpawn, ...] = ()
    inventory_drops: tuple[Drop, ...] = ()
    quest_failed: bool = False


def _parse_guards(element: ET.Element) -> tuple[Guard, ...]:
    return tuple(
        Guard(
            kind=str(child.get("type", "has")).casefold(),
            token=str(child.get("value", "")),
            amount=int(child.get("amount", 1) or 1),
        )
        for child in element.findall("Guard")
        if child.get("value")
    )


def _parse_task(
    index: int, element: ET.Element, *, quest_id: str = "",
) -> TaskDefinition:
    events = []
    for child in element.findall("Event"):
        events.append(TaskEvent(
            failure=child.find("Failure") is not None,
            kind=str(child.get("type", "")).casefold(),
            value=str(child.get("value", "")),
            guards=_parse_guards(child),
            returns=tuple(
                str(r.get("value", "")) for r in child.findall("Return")
                if r.get("value")
            ),
            return_amounts=MappingProxyType({
                str(r.get("value")): int(r.get("amount", 1))
                for r in child.findall("Return") if r.get("value")
            }),
            drops=tuple(
                Drop(
                    item_id=str(d.get("value", "")),
                    amount=int(d.get("amount", 1) or 1),
                    chance=max(
                        float(d.get("chance", 1) or 1),
                        _QUEST_DROP_CHANCE_OVERRIDES.get((
                            str(quest_id).casefold(),
                            str(d.get("value", "")).casefold(),
                        ),
                        0.0,
                    ),
                    ),
                    to_inventory=_bool(d.get("inventory")),
                )
                for d in child.findall("Drop")
                if d.get("value")
            ),
            spawns=tuple(
                QuestSpawn(
                    template_id=str(spawn.get("value", "")),
                    difficulty=str(spawn.get("difficulty", "")),
                    level=int(spawn.get("level", 1) or 1),
                    location=str(spawn.get("location", "")),
                )
                for spawn in child.findall("Spawn")
                if spawn.get("value") and spawn.get("location")
            ),
        ))
    switch_definitions = tuple(
        SwitchDefinition(
            switch_id=str(s.get("id")),
            prop=str(s.get("prop", "")),
            item=str(s.get("item", "")),
            amount=int(s.get("amount", 1) or 1),
            consume=_bool(s.get("consume")),
            guards=_parse_guards(s),
        )
        for s in element.findall("Switch") if s.get("id")
    )
    switches = {switch.switch_id: switch.prop for switch in switch_definitions}
    dialogs = {
        str(d.get("id")): str(d.get("actor", ""))
        for d in element.findall("Dialog") if d.get("id")
    }
    clear_regions = {
        str(c.get("id")): str(c.get("monster", ""))
        for c in element.findall("ClearRegion") if c.get("id")
    }
    return TaskDefinition(
        index=index,
        title=str(element.get("title", "")),
        task_type=str(element.get("tasktype", "")),
        maps=frozenset(v.casefold() for v in _tokens(element.get("map"))),
        returns=tuple(
            str(r.get("value", "")) for r in element.findall("Return")
            if r.get("value")
        ),
        return_amounts=MappingProxyType({
            str(r.get("value")): int(r.get("amount", 1))
            for r in element.findall("Return") if r.get("value")
        }),
        done_guards=_parse_guards(element),
        events=tuple(events),
        switches=MappingProxyType(switches),
        dialogs=MappingProxyType(dialogs),
        clear_regions=MappingProxyType(clear_regions),
        group=_bool(element.get("group")),
        show_guidance=_bool(element.get("showGuidance")),
        switch_definitions=switch_definitions,
        barters=tuple(
            BarterDefinition(
                barter_id=str(b.get("id")), actor=str(b.get("actor", "")),
                items=_tokens(
                    b.get("item") if b.tag == "Barter" else b.get("currency")
                ),
                amount=int(b.get("amount", 1)),
                reply=_bool(b.get("reply", "true")), only_show=_bool(b.get("onlyShow")),
                guards=_parse_guards(b),
                event_kind="pay" if b.tag == "Pay" else "deliver",
            )
            for b in element
            if b.tag in {"Barter", "Pay"} and b.get("id")
        ),
        pre_tasks=tuple(_tokens(element.get("preTasks"))),
        holdgrounds=tuple(
            HoldGroundDefinition(
                holdground_id=str(binding.get("id")),
                actor=str(binding.get("actor", "")),
                configurations=tuple(
                    (int(config.get("difficulty")) if config.get("difficulty") else None,
                     str(config.get("value")))
                    for config in binding.findall("Config") if config.get("value")
                ),
                guards=_parse_guards(binding),
            )
            for binding in element.findall("HoldGround") if binding.get("id")
        ),
    )


def _parse_quest(element: ET.Element) -> QuestDefinition:
    accept_inventory_drops = tuple(
        Drop(
            item_id=str(drop.get("value", "")),
            amount=int(drop.get("amount", 1) or 1),
            chance=float(drop.get("chance", 1) or 1),
            to_inventory=_bool(drop.get("inventory")),
        )
        for drop in element.findall("Drop")
        if drop.get("value") and _bool(drop.get("inventory"))
    )
    rewards = tuple(
        QuestReward(
            kind=str(r.get("type", "")).casefold(),
            value=str(r.get("value", "")),
            amount=int(r.get("amount", 1) or 1),
            class_name=str(r.get("class", "")),
        )
        for r in element.findall("Reward")
    )
    quest_id = str(element.get("id", ""))
    tasks = tuple(
        _parse_task(index, task, quest_id=quest_id)
        for index, task in enumerate(element.findall("Task"))
    )
    return QuestDefinition(
        quest_id=quest_id,
        giver=str(element.get("giver", "")),
        goal=str(element.get("goal", "")),
        start_signal=str(element.get("startSignal", "")),
        stop_signal=str(element.get("stopSignal", "")),
        quest_type=str(element.get("type", "")),
        level=int(element.get("level", 0) or 0),
        min_level=int(element.get("minLevel", 0) or 0),
        max_level=int(element.get("maxLevel", 0) or 0),
        class_name=str(element.get("class", "")),
        required=_tokens(element.get("required")),
        required_operator=str(element.get("requiredOperator", "ANY")),
        followups=_tokens(element.get("followup")),
        chapter_id=str(element.get("chapterId", "")),
        maps=frozenset(v.casefold() for v in _tokens(element.get("map"))),
        staged_tasks=_bool(element.get("stagedTasks")),
        deletable=_bool(element.get("deletable")),
        event_id=str(element.get("event", "")),
        tasks=tasks,
        rewards=rewards,
        bound_to_map=_bool(element.get("boundToMap")),
        accept_inventory_drops=accept_inventory_drops,
    )


@dataclass(frozen=True)
class PropSwitchBinding:
    """One quest task's claim on a prop instance."""

    quest_id: str
    task_index: int
    switch: SwitchDefinition


@dataclass(frozen=True)
class QuestCatalog:
    quests: Mapping[str, QuestDefinition]          # key -> definition
    by_giver: Mapping[str, tuple[QuestDefinition, ...]]
    by_goal: Mapping[str, tuple[QuestDefinition, ...]]
    by_start_signal: Mapping[str, tuple[QuestDefinition, ...]]
    switch_props: Mapping[str, str]                 # prop instance id -> switch id
    by_chapter: Mapping[str, tuple[QuestDefinition, ...]]
    # prop instance id -> every quest task that switches it
    prop_switches: Mapping[str, tuple[PropSwitchBinding, ...]] = MappingProxyType({})

    def quest(self, quest_id: str) -> QuestDefinition | None:
        return self.quests.get(str(quest_id).casefold())

    def switches_for_prop(self, instance_id: str) -> tuple[PropSwitchBinding, ...]:
        return self.prop_switches.get(str(instance_id).casefold(), ())

    def offered_by(self, npc_template_id: str) -> tuple[QuestDefinition, ...]:
        return self.by_giver.get(str(npc_template_id).casefold(), ())

    def completed_at(self, npc_template_id: str) -> tuple[QuestDefinition, ...]:
        return self.by_goal.get(str(npc_template_id).casefold(), ())

    def started_by(self, signal_id: str) -> tuple[QuestDefinition, ...]:
        return self.by_start_signal.get(str(signal_id).casefold(), ())

    def chapter_quests(self, chapter_id: str, class_name: str) -> tuple[QuestDefinition, ...]:
        """The chapter's quests for one class; retail's ChapterProgress total.

        The ``china-`` prefixed rows are the Chinese-region variants of the
        tutorial and are not part of the western chapter count (retail sent
        7 for ``act_00_chapter_10``).
        """

        return tuple(
            quest for quest in self.by_chapter.get(str(chapter_id).casefold(), ())
            if quest.accepts_class(class_name)
            and not quest.key.startswith("china-")
        )

    def available(
        self,
        quest: QuestDefinition,
        *,
        class_name: str,
        level: int,
        closed_quests: Iterable[str],
        known_quests: Iterable[str],
        active_events: Iterable[str] = (),
    ) -> bool:
        """Whether a giver may offer ``quest`` to this character now."""

        known = {str(value).casefold() for value in known_quests}
        if quest.key in known and not quest.repeatable:
            return False
        return (
            quest.accepts_class(class_name)
            and quest.accepts_level(level)
            and quest.requirements_met(closed_quests)
            and quest.event_requirement_met(active_events)
        )


def _build_catalog(path: str, mtime_ns: int, size: int) -> QuestCatalog:
    del mtime_ns, size
    root = ET.parse(path).getroot()
    quests: dict[str, QuestDefinition] = {}
    by_giver: dict[str, list] = {}
    by_goal: dict[str, list] = {}
    by_signal: dict[str, list] = {}
    by_chapter: dict[str, list] = {}
    switch_props: dict[str, str] = {}
    prop_switches: dict[str, list] = {}
    for element in root.findall("Quest"):
        quest = _parse_quest(element)
        if not quest.quest_id:
            continue
        quests[quest.key] = quest
        if quest.giver:
            by_giver.setdefault(quest.giver.casefold(), []).append(quest)
        if quest.goal:
            by_goal.setdefault(quest.goal.casefold(), []).append(quest)
        if quest.start_signal:
            by_signal.setdefault(quest.start_signal.casefold(), []).append(quest)
        if quest.chapter_id:
            by_chapter.setdefault(quest.chapter_id.casefold(), []).append(quest)
        for task in quest.tasks:
            for switch in task.switch_definitions:
                if not switch.prop:
                    continue
                switch_props.setdefault(switch.prop.casefold(), switch.switch_id)
                prop_switches.setdefault(switch.prop.casefold(), []).append(
                    PropSwitchBinding(quest.quest_id, task.index, switch)
                )
    freeze = lambda table: MappingProxyType({k: tuple(v) for k, v in table.items()})
    return QuestCatalog(
        quests=MappingProxyType(quests),
        by_giver=freeze(by_giver),
        by_goal=freeze(by_goal),
        by_start_signal=freeze(by_signal),
        switch_props=MappingProxyType(switch_props),
        by_chapter=freeze(by_chapter),
        prop_switches=freeze(prop_switches),
    )


_build_catalog_cached = lru_cache(maxsize=4)(_build_catalog)


def load_quest_catalog(path: str | Path) -> QuestCatalog:
    path = Path(path)
    # Hundreds of lookups per map entry: never stat quests.xml on each one
    # (runtime/file_revisions.py).
    revision = observed_revision(path)
    if revision is None:
        return QuestCatalog(
            quests=MappingProxyType({}),
            by_giver=MappingProxyType({}),
            by_goal=MappingProxyType({}),
            by_start_signal=MappingProxyType({}),
            switch_props=MappingProxyType({}),
            by_chapter=MappingProxyType({}),
            prop_switches=MappingProxyType({}),
        )
    return _build_catalog_cached(str(path), *revision)


# ---------------------------------------------------------------------------
# Progress engine
# ---------------------------------------------------------------------------

def reconcile_quest_progress(quest: QuestDefinition, progress: QuestProgress) -> None:
    """Restore progress after a content update removes trailing objectives.

    Release 261_1024 removes the final encounter task from three summer quests.
    Their remaining task indices and token names are unchanged. Retain those
    counters and earned completion bits, discard bits outside the current task
    layout, and make an already delivered quest ready for its normal turn-in.
    Closed quests stay closed: changing rewards must never pay them again.

    This does not infer task completion from guards (an eventless CompleteTask
    can still require an NPC encounter), replay events, or grant rewards.
    Reordering/removing interior tasks needs an explicit identity migration.
    """

    current_mask = sum(1 << task.index for task in quest.tasks)
    progress.finished_mask &= current_mask
    if (
        progress.state == QUEST_STATE_ACCEPTED
        and current_mask
        and progress.finished_mask == current_mask
    ):
        progress.state = QUEST_STATE_FINISHED


def active_tasks(
    quest: QuestDefinition, progress: QuestProgress,
) -> tuple[TaskDefinition, ...]:
    """Tasks that currently accept events.

    A task is open once every task its ``preTasks`` names is finished; tasks
    without ``preTasks`` run in parallel.  ``stagedTasks`` is a display
    hint only: retail progressed the three collect tasks of the staged
    garden quest interleaved (tutorial capture, 16:06–16:07) and sent all
    of their props with the map, so it does not serialize tasks.
    """

    if progress.state != QUEST_STATE_ACCEPTED:
        return ()
    finished_titles = {
        task.title.casefold() for task in quest.tasks
        if progress.task_finished(task.index)
    }
    return tuple(
        task for task in quest.tasks
        if not progress.task_finished(task.index)
        and all(title.casefold() in finished_titles for title in task.pre_tasks)
    )


def announced_chapter(chapter_id: str) -> bool:
    """Whether opening ``chapter_id`` is announced with ``ChapterUnlocked``.

    The tutorial chapter (``act_00_chapter_10``) is not: retail sent its
    only ``ChapterUnlocked`` for ``act_10_chapter_10`` when the Grimford
    quest was accepted, and the client has no
    ``notificationchapter_act_00*`` artwork, so announcing act 0 only
    produces texture-load warnings.  Every other chapter of ``quests.xml``
    has its title texture.
    """

    return not str(chapter_id or "").casefold().startswith("act_00")


def active_prop_switches(
    quest: QuestDefinition, progress: QuestProgress,
) -> tuple[tuple[TaskDefinition, SwitchDefinition], ...]:
    """The switches whose props are live for ``progress`` right now."""

    return tuple(
        (task, switch)
        for task in active_tasks(quest, progress)
        for switch in task.switch_definitions
        if switch.prop and switch.live(progress.tokens)
    )


def apply_quest_event(
    quest: QuestDefinition,
    progress: QuestProgress,
    kind: str,
    value: str,
    *,
    count: int = 1,
    rng: random.Random | None = None,
    drop_amount_bonus: ModifierAmounts = ModifierAmounts(),
) -> QuestEventResult:
    """Feed one world event into a quest and return what changed.

    ``count`` repeats the event (``count`` kills of one template).  Drops
    produced by the event are rolled with ``rng``. Ground drops are returned
    without collection credit; inventory drops collect immediately.
    The quest becomes ``QUEST_STATE_FINISHED`` when
    its last task completes.
    """

    kind = str(kind).casefold()
    value_key = str(value).casefold()
    changes: list[TaskChange] = []
    unsupported: list[str] = []
    drops: list[Drop] = []
    spawns = []
    inventory_drops: list[Drop] = []
    rng = rng or random.Random()
    for _ in range(max(1, int(count))):
        for task in active_tasks(quest, progress):
            before = task.counter(progress.tokens)
            implicit_explore_match = (
                kind == "trigger"
                and explore_trigger_for_task(quest, task).casefold() == value_key
            )
            # CompleteTask objectives have no event row in client quest data.
            # Encountering their goal NPC completes them once their guards
            # (usually the previous task's completion token) are satisfied.
            matched = (
                implicit_explore_match
                or (
                    kind == "encounter"
                    and task.task_type.casefold() == "completetask"
                    and quest.goal.casefold() == value_key
                    and task.is_done(progress.tokens)
                )
            )
            for event in task.events:
                if event.kind in UNSUPPORTED_EVENT_TYPES:
                    unsupported.append(event.kind)
                if event.kind != kind or event.value.casefold() != value_key:
                    continue
                if not all(guard.holds(progress.tokens) for guard in event.guards):
                    continue
                if event.failure:
                    progress.state = QUEST_STATE_FAILED
                    return QuestEventResult((), False, quest_failed=True)
                matched = True
                spawns.extend(event.spawns)
                for token in event.returns:
                    progress.tokens[token] = progress.tokens.get(token, 0) + event.return_amounts.get(token, 1)
                for drop in event.drops:
                    if drop.chance >= 1.0 or rng.random() < drop.chance:
                        if drop_amount_bonus != ModifierAmounts():
                            drop = replace(drop, amount=drop_amount_bonus.apply(drop.amount, minimum=1))
                        if not drop.to_inventory:
                            drops.append(drop)
                            continue
                        inventory_drops.append(drop)
                        inner = apply_quest_event(
                            quest, progress, "collect", drop.item_id,
                            count=drop.amount, rng=rng, drop_amount_bonus=drop_amount_bonus,
                        )
                        changes.extend(inner.changes)
                        drops.extend(inner.drops)
                        inventory_drops.extend(inner.inventory_drops)
            if not matched:
                continue
            after = task.counter(progress.tokens)
            finished = task.is_done(progress.tokens) or implicit_explore_match
            if finished and not progress.task_finished(task.index):
                progress.finished_mask |= 1 << task.index
                for token in task.returns:
                    progress.tokens[token] = progress.tokens.get(token, 0) + task.return_amounts.get(token, 1)
                changes.append(TaskChange(task, max(after, task.target), task.target, True))
            elif after != before:
                changes.append(TaskChange(task, after, task.target, False))
    quest_finished = False
    if progress.state == QUEST_STATE_ACCEPTED and quest.tasks and all(
        progress.task_finished(task.index) for task in quest.tasks
    ):
        progress.state = QUEST_STATE_FINISHED
        quest_finished = True
    return QuestEventResult(
        _collapse_changes(changes), quest_finished, tuple(dict.fromkeys(unsupported)),
        tuple(drops), tuple(spawns), tuple(inventory_drops),
    )


def _collapse_changes(changes: Iterable[TaskChange]) -> tuple[TaskChange, ...]:
    """One record per task: the final counter, finished if it finished."""

    merged: dict[int, TaskChange] = {}
    for change in changes:
        previous = merged.get(change.task.index)
        if previous is None:
            merged[change.task.index] = change
            continue
        merged[change.task.index] = TaskChange(
            change.task,
            max(previous.progress, change.progress),
            change.target,
            previous.finished or change.finished,
        )
    return tuple(merged.values())


def task_counters(quest: QuestDefinition, progress: QuestProgress) -> tuple[int, ...]:
    """The 16 per-slot counters QuestLogInfo carries."""

    counters = [0] * MAX_TASKS
    for task in quest.tasks[:MAX_TASKS]:
        counters[task.index] = task.counter(progress.tokens)
    return tuple(counters)


__all__ = [
    "MAX_TASKS",
    "NO_TIMESTAMP",
    "QUEST_STATE_ACCEPTED",
    "QUEST_STATE_CLOSED",
    "QUEST_STATE_FINISHED",
    "QUEST_STATE_FAILED",
    "UNSUPPORTED_EVENT_TYPES",
    "Drop",
    "Guard",
    "HoldGroundDefinition",
    "QuestCatalog",
    "QuestDefinition",
    "QuestEventResult",
    "PropSwitchBinding",
    "QuestProgress",
    "QuestReward",
    "QuestSpawn",
    "SwitchDefinition",
    "TaskChange",
    "TaskDefinition",
    "TaskEvent",
    "active_prop_switches",
    "active_tasks",
    "announced_chapter",
    "apply_quest_event",
    "explore_trigger_for_task",
    "load_quest_catalog",
    "reconcile_quest_progress",
    "task_counters",
]
