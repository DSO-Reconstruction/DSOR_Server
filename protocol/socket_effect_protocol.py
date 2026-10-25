"""Cached jewel/rune modifier programs and runtime event indexes.

Socket items are ordinary equipped-item modifiers for static attributes, but
``ActorStatusEffect`` and ``SkillStatusEffect`` instructions also describe
event driven gameplay.  This module compiles those generated programs once
per database revision and evaluates only the handful of sockets in a player's
currently equipped items.  Combat and pickup handlers therefore perform no
SQLite work and scan only rules indexed for the event being raised.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from functools import cached_property, lru_cache
from pathlib import Path
import math
import sqlite3

from dso_server.protocol.gameplay_protocol import StatusEffect, StatusEffectEntry


SERVER_TICKS_PER_SECOND = 25
PERMANENT_SOCKET_EFFECT_SECONDS = 36_000.0
SOCKET_ITEM_CATEGORIES = frozenset({"equipmentjewel", "equipmentrune"})
IMMEDIATE_TRIGGERS = frozenset({"", "immediate", "immediatemanaged"})
DISABLED_TRIGGERS = frozenset({"disabled", "never"})
SUPPORTED_MODIFIER_KINDS = frozenset({
    "actorstatuseffect",
    "actorvariable",
    "block",
    "cooldown",
    "critical",
    "criticalvalue",
    "damage",
    "dropamount",
    "healthpointsregeneration",
    "itemarmor",
    "itemcritical",
    "itemdamage",
    "itemhealthpoints",
    "itemresistance",
    "largeweaponblock",
    "largeweaponcritical",
    "localize",
    "maxhealthpoints",
    "maxskillresource",
    "offhandblock",
    "offhandcritical",
    "resistance",
    "resourcecost",
    "skillstatuseffect",
    "smallweaponblock",
    "smallweaponcritical",
    "speed",
    "statuseffectgroupduration",
    "talentlevelbonus",
    "weapondamagefactor",
})
SUPPORTED_STATUS_RUNTIME_KINDS = frozenset({
    "actorfeature",
    "actorstatuseffect",
    "cooldown",
    "criticalvalue",
    "currhealthpoints",
    "currhealthpointsdmg",
    "damage",
    "healthpointsregeneration",
    "maxhealthpoints",
    "maxskillresource",
    "minion",
    "resistance",
    "resourcecost",
    "skilldamage",
    "speed",
    "statuseffectimmunitygroup",
    "stopstatuseffect",
})
SUPPORTED_EFFECT_OPERATIONS = frozenset({
    "addae", "addle", "addue", "addve",
    "modbdle", "modble", "modle", "modue", "modve",
})
SUPPORTED_EVENT_TRIGGERS = frozenset({
    "",
    "disabled",
    "hit",
    "hitblocked",
    "hitcritical",
    "hitfrost",
    "hitlowhealth",
    "immediatemanaged",
    "kill",
    "lowhealth",
    "maxstacksizejewelbuffblockedenergy",
    "pickuphealthglobe",
    "revive120",
})
GENERIC_SKILL_EVENT_GROUPS = frozenset({
    "allplayerskills", "player_skills",
})
# Two shipped rows disagree with their localized, player-visible contracts.
# Keep the correction explicit and executable instead of silently preserving
# a known content typo (Ghost Smoke says critical hit; the row says hit).
SOCKET_TRIGGER_OVERRIDES = {
    ("unique_gem_ce2023_offensive", "ce2023_jewel_offensive_effect"):
        "hitcritical",
}

SOCKET_COOLDOWN_OVERRIDES = {
    # The shipped rune status graph omits this documented sixty-second gate.
    ("mythic_gem_ce2025_blessing_stars", "mh_rings_set_aura"): 60.0,
}
def normalize_trigger(value: str) -> str:
    """Normalize the generated event vocabulary to stable server names."""

    compact = "".join(
        character for character in str(value or "").casefold()
        if character.isalnum()
    )
    aliases = {
        "pickupglobe": "pickuphealthglobe",
        "criticalhit": "hitcritical",
        "blockedhit": "hitblocked",
        "lowhealthhit": "hitlowhealth",
    }
    return aliases.get(compact, compact)


@dataclass(frozen=True)
class SocketModifier:
    kind: str
    arguments: tuple[str, ...]
    label: str
    raw: str

    @cached_property
    def metadata(self) -> dict[str, str]:
        result: dict[str, str] = {}
        for argument in self.arguments:
            name, separator, value = argument.partition(":")
            if separator:
                result[name.strip().casefold()] = value.strip()
        return result

    @cached_property
    def parameters(self) -> tuple[float, ...]:
        indexed: dict[int, float] = {}
        for name, raw_value in self.metadata.items():
            if not name.startswith("$"):
                continue
            try:
                index = int(name[1:])
                value = float(raw_value.split("<", 1)[0].strip())
            except ValueError:
                continue
            if index >= 0 and math.isfinite(value):
                indexed[index] = value
        count = max(5, max(indexed, default=-1) + 1)
        return tuple(indexed.get(index, 0.0) for index in range(count))

    @property
    def operation(self) -> str:
        return self.arguments[0].strip() if self.arguments else ""

    @property
    def trigger(self) -> str:
        return normalize_trigger(self.metadata.get("on", ""))

    @property
    def chance(self) -> float:
        raw = self.metadata.get("c", "1").split("<", 1)[0].strip()
        try:
            value = float(raw)
        except ValueError:
            return 0.0
        if "<relative" in self.metadata.get("c", "").casefold():
            value = 1.0 + value
        elif "<absolute" in self.metadata.get("c", "").casefold():
            value = 1.0 + value
        return max(0.0, min(1.0, value))


@dataclass(frozen=True)
class SocketStatusDefinition:
    """Wire-compatible subset of ``VictimStatusEffectDefinition``."""

    resource_id: str
    effect_id: int
    chance: float
    duration_ticks: int
    values: tuple[float, ...]
    trigger: str
    delay_ticks: int = 0
    max_stack_size: int = 1
    consume_on_max_stack: bool = False
    aura_radius: float = 0.0
    aura_height: float = 0.0
    aura_shape: str = ""
    stop_effect_ids: tuple[int, ...] = ()
    modifier_kinds: frozenset[str] = frozenset()


@dataclass(frozen=True)
class SocketEffectRule:
    socket_template_id: str
    category: str
    kind: str
    operation: str
    skill_group: str
    trigger: str
    chance: float
    target: str
    definition: SocketStatusDefinition
    receiver_definitions: tuple[SocketStatusDefinition, ...]
    label: str
    raw: str
    nested_user_definitions: tuple[SocketStatusDefinition, ...] = ()
    event_auras: tuple["SocketEventAura", ...] = ()
    cooldown_seconds: float = 0.0


@dataclass(frozen=True)
class SocketEventAura:
    definition: SocketStatusDefinition
    receiver_definitions: tuple[SocketStatusDefinition, ...]
    target: str


@dataclass(frozen=True)
class ImmediateSocketEffect:
    key: str
    socket_template_id: str
    resource_id: str
    effect_id: int
    duration_ticks: int
    values: tuple[float, ...]


@dataclass(frozen=True)
class SocketPeriodicAura:
    key: str
    interval_seconds: float
    aura_definition: SocketStatusDefinition
    receiver_definitions: tuple[SocketStatusDefinition, ...]
    causer_definitions: tuple[SocketStatusDefinition, ...]


@dataclass(frozen=True)
class SocketItemProgram:
    template_id: str
    category: str
    modifiers: tuple[SocketModifier, ...]
    rules: tuple[SocketEffectRule, ...]


@dataclass(frozen=True)
class SocketEffectEvaluation:
    socket_template_ids: tuple[str, ...]
    modifiers: tuple[SocketModifier, ...]
    event_rules: tuple[tuple[str, tuple[SocketEffectRule, ...]], ...]
    cast_rules: tuple[SocketEffectRule, ...]
    immediate_effects: tuple[ImmediateSocketEffect, ...]
    periodic_auras: tuple[SocketPeriodicAura, ...] = ()
    drop_amounts: tuple[tuple[str, float, float], ...] = ()
    actor_variables: tuple[tuple[str, float], ...] = ()
    boss_bonus_gems: int = 0
    skill_scalars: tuple[
        tuple[str, float, str, tuple[str, ...]], ...
    ] = ()
    status_group_durations: tuple[tuple[str, float], ...] = ()

    def rules_for_event(self, event: str) -> tuple[SocketEffectRule, ...]:
        wanted = normalize_trigger(event)
        return next(
            (rules for trigger, rules in self.event_rules if trigger == wanted),
            (),
        )

    def drop_amount_bonus(self, template_id: str) -> tuple[float, float]:
        wanted = str(template_id or "")
        return next(
            (
                (absolute, relative)
                for item_id, absolute, relative in self.drop_amounts
                if item_id == wanted
            ),
            (0.0, 0.0),
        )

    def skill_scalar(self, kind: str, groups=()) -> float:
        """Return a cached relative/absolute scalar for one skill family."""

        wanted_kind = str(kind or "").casefold()
        wanted_groups = {
            str(value or "").casefold() for value in groups if str(value or "")
        }
        absolute = 0.0
        relative = 0.0
        for entry_kind, amount, operation, scopes in self.skill_scalars:
            if entry_kind != wanted_kind:
                continue
            if scopes and wanted_groups.isdisjoint(scopes):
                continue
            if operation == "relative":
                relative += float(amount)
            else:
                absolute += float(amount)
        return max(0.0, (1.0 + absolute) * (1.0 + relative))

    def status_group_duration_bonus(self, groups) -> float:
        wanted = {
            str(value or "").casefold() for value in groups if str(value or "")
        }
        return sum(
            float(amount)
            for group, amount in self.status_group_durations
            if group in wanted
        )


@dataclass(frozen=True)
class SocketEffectTransition:
    effects: dict[str, StatusEffectEntry]
    next_instance_id: int
    status_effect: StatusEffect | None
    added_keys: tuple[str, ...]
    removed_keys: tuple[str, ...]


@lru_cache(maxsize=4096)
def parse_socket_modifier(raw: str) -> SocketModifier:
    raw = str(raw or "").strip()
    body, separator, label = raw.partition("#")
    kind, colon, argument_text = body.partition(":")
    return SocketModifier(
        kind=kind.strip(),
        arguments=(
            tuple(part.strip() for part in argument_text.split(","))
            if colon else ()
        ),
        label=label.strip() if separator else "",
        raw=raw,
    )


def parse_socket_program(value: str) -> tuple[SocketModifier, ...]:
    return tuple(
        parse_socket_modifier(entry)
        for entry in str(value or "").split(";")
        if entry.strip()
    )


def _split_ids(value: str) -> tuple[str, ...]:
    return tuple(
        item.strip() for item in str(value or "").replace(",", ";").split(";")
        if item.strip()
    )


class SocketEffectDatabase:
    """Read-only compiled view of socketed jewel, rune, and ancient rules."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).resolve()

    def _connect(self) -> sqlite3.Connection:
        if not self.path.is_file():
            raise FileNotFoundError(self.path)
        database = sqlite3.connect(
            f"file:{self.path.as_posix()}?mode=ro", uri=True
        )
        database.row_factory = sqlite3.Row
        return database

    @cached_property
    def _status_rows(self) -> dict[str, sqlite3.Row]:
        with self._connect() as database:
            return {
                str(row["Id"]): row
                for row in database.execute(
                    """
                    SELECT rowid - 1 AS EffectId, Id, StatusEffectDuration,
                           StatusEffectTickRate AS TickRate, MaxStackSize,
                           AuraRadius, AuraHeight,
                           AuraShape, AuraEffectId, AuraTargets, EffectHosts,
                           StartModifiers, TickModifiers, DoneModifiers
                    FROM _Template_StatusEffect
                    """
                )
            }

    @cached_property
    def _talent_stack_programs(self) -> dict[str, tuple[SocketModifier, ...]]:
        """Resolve group-talent programs granted by rune level bonuses."""

        with self._connect() as database:
            rows = database.execute(
                """
                SELECT t.Id, s.StartModifiers
                FROM _Template_Talents AS t
                LEFT JOIN _Template_StatusEffect AS s
                  ON s.Id = t.StackStatusEffectId
                """
            ).fetchall()
        return {
            str(row["Id"]): parse_socket_program(row["StartModifiers"])
            for row in rows
        }

    def _definition(
        self, modifier: SocketModifier, resource_id: str, *, trigger: str
    ) -> SocketStatusDefinition | None:
        row = self._status_rows.get(str(resource_id))
        if row is None:
            return None
        metadata = modifier.metadata
        base_duration = max(0.0, float(row["StatusEffectDuration"] or 0.0))
        raw_duration = metadata.get("d", metadata.get("dp", ""))
        if raw_duration:
            try:
                duration_value = float(raw_duration.split("<", 1)[0].strip())
            except ValueError:
                duration_value = base_duration
            if "<relative" in raw_duration.casefold():
                duration_seconds = base_duration * (1.0 + duration_value)
            elif "<absolute" in raw_duration.casefold():
                duration_seconds = base_duration + duration_value
            else:
                duration_seconds = duration_value
        else:
            duration_seconds = base_duration
        raw_delay = metadata.get("de", "0")
        try:
            delay_seconds = float(raw_delay.split("<", 1)[0].strip())
        except ValueError:
            delay_seconds = 0.0
        stopped_resources = tuple(
            token.strip()
            for raw in str(row["StartModifiers"] or "").split(";")
            for kind, separator, values in (raw.partition(":"),)
            if separator and kind.strip().casefold() == "stopstatuseffect"
            for token in values.split("|")
            if token.strip() and not token.strip().replace(".", "", 1).isdigit()
        )
        return SocketStatusDefinition(
            resource_id=str(resource_id),
            effect_id=int(row["EffectId"]),
            chance=modifier.chance,
            duration_ticks=max(
                0, round(max(0.0, duration_seconds) * SERVER_TICKS_PER_SECOND)
            ),
            values=modifier.parameters,
            trigger=normalize_trigger(trigger),
            delay_ticks=max(
                0, round(max(0.0, delay_seconds) * SERVER_TICKS_PER_SECOND)
            ),
            max_stack_size=max(1, int(row["MaxStackSize"] or 1)),
            consume_on_max_stack=self._consumes_on_max_stack(
                str(resource_id), row
            ),
            aura_radius=max(0.0, float(row["AuraRadius"] or 0.0)),
            aura_height=max(0.0, float(row["AuraHeight"] or 0.0)),
            aura_shape=str(row["AuraShape"] or ""),
            stop_effect_ids=tuple(
                int(self._status_rows[item]["EffectId"])
                for item in stopped_resources
                if item in self._status_rows
            ),
            modifier_kinds=frozenset(
                kind.strip().casefold()
                for encoded in (
                    str(row["StartModifiers"] or ""),
                    str(row["TickModifiers"] or ""),
                )
                for raw in encoded.split(";")
                for kind, separator, _arguments in (raw.partition(":"),)
                if separator and kind.strip()
            ),
        )

    def _consumes_on_max_stack(
        self, resource_id: str, row: sqlite3.Row
    ) -> bool:
        """Resolve a max-stack child that explicitly stops this parent."""

        parent = str(resource_id).strip().casefold()
        for modifier in parse_socket_program(row["StartModifiers"]):
            if modifier.kind.casefold() != "actorstatuseffect":
                continue
            if len(modifier.arguments) < 2 or not any(
                value.casefold().startswith("on:maxstack")
                for value in modifier.arguments[2:]
            ):
                continue
            child = self._status_rows.get(modifier.arguments[1])
            if child is None:
                continue
            for child_modifier in parse_socket_program(
                child["StartModifiers"]
            ):
                if (
                    child_modifier.kind.casefold() != "stopstatuseffect"
                    or not child_modifier.arguments
                ):
                    continue
                stopped = child_modifier.arguments[0].split("|", 1)[0]
                if stopped.strip().casefold() == parent:
                    return True
        return False

    def _rule(
        self, template_id: str, modifier: SocketModifier, *,
        category: str = "EquipmentRune",
    ) -> SocketEffectRule | None:
        kind = modifier.kind.casefold()
        arguments = modifier.arguments
        if kind == "actorstatuseffect":
            if len(arguments) < 2:
                return None
            operation, resource_id = arguments[:2]
            target = (
                "location"
                if operation.casefold().endswith("le")
                else "user"
            )
            skill_group = ""
        elif kind == "skillstatuseffect":
            if len(arguments) < 3:
                return None
            operation, skill_group, resource_id = arguments[:3]
            target = {
                "addve": "victim", "modve": "victim",
                "addue": "user", "modue": "user",
                "addle": "location", "modle": "location",
                "addble": "bullet", "modble": "bullet",
                "addbdle": "bulletdeath", "modbdle": "bulletdeath",
            }.get(operation.casefold(), "")
            if not target:
                return None
        else:
            return None
        if not resource_id or resource_id == "empty_effect":
            return None
        trigger = SOCKET_TRIGGER_OVERRIDES.get(
            (str(template_id), str(resource_id)), modifier.trigger
        )
        definition = self._definition(modifier, resource_id, trigger=trigger)
        if definition is None:
            return None
        nested_user_definitions, event_auras, generated_cooldown = (
            self._nested_runtime_effects(definition)
        )
        cooldown_seconds = 0.0
        if str(category).casefold() != "equipmentjewel":
            cooldown_seconds = max(
                generated_cooldown,
                SOCKET_COOLDOWN_OVERRIDES.get(
                    (str(template_id), str(resource_id)), 0.0
                ),
            )
        return SocketEffectRule(
            socket_template_id=str(template_id),
            category=str(category),
            kind=modifier.kind,
            operation=operation,
            skill_group=skill_group,
            trigger=trigger,
            chance=modifier.chance,
            target=target,
            definition=definition,
            receiver_definitions=self._aura_children(definition),
            label=modifier.label,
            raw=modifier.raw,
            nested_user_definitions=nested_user_definitions,
            event_auras=event_auras,
            cooldown_seconds=cooldown_seconds,
        )

    def _nested_runtime_effects(
        self, parent: SocketStatusDefinition
    ) -> tuple[
        tuple[SocketStatusDefinition, ...],
        tuple[SocketEventAura, ...],
        float,
    ]:
        """Compile the one-level ActorStatusEffect graph used by sockets."""

        row = self._status_rows.get(parent.resource_id)
        if row is None:
            return (), (), 0.0
        definitions = []
        auras = []
        cooldown_seconds = 0.0
        for modifier in parse_socket_program(row["StartModifiers"]):
            if (
                modifier.kind.casefold() != "actorstatuseffect"
                or len(modifier.arguments) < 2
                or not modifier.operation.casefold().startswith("add")
            ):
                continue
            resource_id = modifier.arguments[1]
            child = self._definition(
                modifier, resource_id, trigger="immediate"
            )
            if child is None:
                continue
            if "cooldown" in resource_id.casefold():
                cooldown_seconds = max(
                    cooldown_seconds,
                    child.duration_ticks / SERVER_TICKS_PER_SECOND,
                )
                # The root status creates this client-side cooldown child.
                # Its duration also governs server-side rune procs.
                continue
            definitions.append(child)
            child_row = self._status_rows.get(resource_id)
            receivers = self._aura_children(child)
            if child.aura_radius > 0.0 and receivers:
                auras.append(
                    SocketEventAura(
                        definition=child,
                        receiver_definitions=receivers,
                        target=str(
                            child_row["AuraTargets"] if child_row else ""
                        ).casefold(),
                    )
                )
        return tuple(definitions), tuple(auras), cooldown_seconds

    def _aura_children(
        self, parent: SocketStatusDefinition, *, include_causer: bool = False
    ) -> tuple[SocketStatusDefinition, ...]:
        row = self._status_rows.get(parent.resource_id)
        if row is None:
            return ()
        result = []
        for child_id in _split_ids(row["AuraEffectId"]):
            child = self._status_rows.get(child_id)
            if child is None:
                continue
            if not include_causer and (
                str(row["AuraTargets"] or "").casefold() == "creator"
                or str(child["EffectHosts"] or "").casefold() == "causer"
            ):
                continue
            child_duration = max(
                0,
                round(
                    max(0.0, float(child["StatusEffectDuration"] or 0.0))
                    * SERVER_TICKS_PER_SECOND
                ),
            )
            result.append(
                SocketStatusDefinition(
                    resource_id=child_id,
                    effect_id=int(child["EffectId"]),
                    chance=parent.chance,
                    duration_ticks=(child_duration or parent.duration_ticks),
                    values=parent.values,
                    trigger=parent.trigger,
                    max_stack_size=max(1, int(child["MaxStackSize"] or 1)),
                    consume_on_max_stack=self._consumes_on_max_stack(
                        child_id, child
                    ),
                    aura_radius=max(0.0, float(child["AuraRadius"] or 0.0)),
                    aura_height=max(0.0, float(child["AuraHeight"] or 0.0)),
                    aura_shape=str(child["AuraShape"] or ""),
                    modifier_kinds=frozenset(
                        kind.strip().casefold()
                        for encoded in (
                            str(child["StartModifiers"] or ""),
                            str(child["TickModifiers"] or ""),
                        )
                        for raw in encoded.split(";")
                        for kind, separator, _arguments in (
                            raw.partition(":"),
                        )
                        if separator and kind.strip()
                    ),
                )
            )
        return tuple(result)

    def _periodic_aura(
        self, template_id: str, root: SocketEffectRule, ordinal: int
    ) -> SocketPeriodicAura | None:
        row = self._status_rows.get(root.definition.resource_id)
        if row is None or float(row["TickRate"] or 0.0) <= 0.0:
            return None
        for modifier in parse_socket_program(row["TickModifiers"]):
            if (
                modifier.kind.casefold() != "actorstatuseffect"
                or len(modifier.arguments) < 2
                or not modifier.operation.casefold().startswith("add")
            ):
                continue
            aura = self._definition(
                modifier,
                modifier.arguments[1],
                trigger="periodic",
            )
            if aura is None or aura.aura_radius <= 0.0:
                continue
            all_children = self._aura_children(aura, include_causer=True)
            causer_ids = {
                str(child_id)
                for child_id in _split_ids(
                    self._status_rows[aura.resource_id]["AuraEffectId"]
                )
                if (
                    child_id in self._status_rows
                    and str(
                        self._status_rows[child_id]["EffectHosts"] or ""
                    ).casefold() == "causer"
                )
            }
            return SocketPeriodicAura(
                key=f"{template_id}:{root.definition.resource_id}:{ordinal}",
                interval_seconds=max(0.04, float(row["TickRate"] or 1.0)),
                aura_definition=aura,
                receiver_definitions=tuple(
                    child for child in all_children
                    if child.resource_id not in causer_ids
                ),
                causer_definitions=tuple(
                    child for child in all_children
                    if child.resource_id in causer_ids
                ),
            )
        return None

    @cached_property
    def programs(self) -> dict[str, SocketItemProgram]:
        with self._connect() as database:
            rows = database.execute(
                """
                SELECT Id, ItemCategory, BaseModifier, UniqueModifier
                FROM _Template_Item
                WHERE lower(ItemCategory) IN (
                    'equipmentjewel', 'equipmentrune', 'equipmentancient'
                )
                ORDER BY rowid
                """
            ).fetchall()
        result: dict[str, SocketItemProgram] = {}
        for row in rows:
            modifiers = (
                parse_socket_program(row["BaseModifier"])
                + parse_socket_program(row["UniqueModifier"])
            )
            rules = tuple(
                rule for modifier in modifiers
                for rule in (
                    self._rule(
                        str(row["Id"]),
                        modifier,
                        category=str(row["ItemCategory"]),
                    ),
                )
                if rule is not None
            )
            result[str(row["Id"])] = SocketItemProgram(
                template_id=str(row["Id"]),
                category=str(row["ItemCategory"]),
                modifiers=modifiers,
                rules=rules,
            )
        return result

    def evaluate_inventory(self, inventory) -> SocketEffectEvaluation:
        """Compile the active socket program for equipped containers only."""

        if inventory is None:
            return SocketEffectEvaluation((), (), (), (), (), (), (), (), 0)
        equipped_ids = {
            int(item_id)
            for item_id in getattr(inventory, "equipment_slots", {})
        }
        socket_ids = tuple(
            str(socket_id)
            for item in getattr(inventory, "items", ())
            if int(getattr(item, "item_id", -1)) in equipped_ids
            for socket_id in getattr(item, "socketed_templates", ())
            if str(socket_id) in self.programs
        )
        programs = tuple(self.programs[item_id] for item_id in socket_ids)
        modifiers = tuple(
            modifier for program in programs for modifier in program.modifiers
        )
        event_index: dict[str, list[SocketEffectRule]] = {}
        cast_rules: list[SocketEffectRule] = []
        immediate_effects: list[ImmediateSocketEffect] = []
        periodic_auras: list[SocketPeriodicAura] = []
        drop_amount_index: dict[str, list[float]] = {}
        occurrence: dict[tuple[str, str], int] = {}
        actor_variables: dict[str, float] = {}
        boss_bonus_gems = 0
        skill_scalars = []
        status_group_durations: dict[str, float] = {}
        for program in programs:
            for modifier in program.modifiers:
                normalized_kind = modifier.kind.casefold()
                if (
                    normalized_kind in {"cooldown", "resourcecost"}
                    and len(modifier.arguments) >= 2
                ):
                    try:
                        amount = float(modifier.arguments[0])
                    except ValueError:
                        amount = 0.0
                    skill_scalars.append((
                        normalized_kind,
                        amount,
                        modifier.arguments[1].casefold(),
                        tuple(
                            token.strip().casefold()
                            for token in modifier.arguments[2:]
                            if token.strip()
                        ),
                    ))
                elif (
                    normalized_kind == "statuseffectgroupduration"
                    and len(modifier.arguments) >= 3
                ):
                    try:
                        amount = float(modifier.arguments[0])
                    except ValueError:
                        amount = 0.0
                    group = modifier.arguments[2].strip().casefold()
                    if group:
                        status_group_durations[group] = (
                            status_group_durations.get(group, 0.0) + amount
                        )
                if (
                    normalized_kind == "talentlevelbonus"
                    and len(modifier.arguments) >= 2
                ):
                    try:
                        levels = max(0, int(round(float(modifier.arguments[0]))))
                    except ValueError:
                        levels = 0
                    for nested in self._talent_stack_programs.get(
                        modifier.arguments[1], ()
                    ):
                        if (
                            nested.kind.casefold() != "actorvariable"
                            or len(nested.arguments) < 3
                        ):
                            continue
                        try:
                            amount = float(nested.arguments[0]) * levels
                        except ValueError:
                            continue
                        variable_id = nested.arguments[2].strip()
                        if variable_id:
                            actor_variables[variable_id] = (
                                actor_variables.get(variable_id, 0.0) + amount
                            )
                if (
                    normalized_kind != "dropamount"
                    or len(modifier.arguments) < 3
                ):
                    continue
                try:
                    amount = float(modifier.arguments[0])
                except ValueError:
                    continue
                target_id = modifier.arguments[2].split("#", 1)[0].strip()
                if not target_id:
                    continue
                totals = drop_amount_index.setdefault(target_id, [0.0, 0.0])
                if modifier.arguments[1].casefold() == "relative":
                    totals[1] += amount
                else:
                    totals[0] += amount
            for rule in program.rules:
                if rule.chance <= 0.0 or rule.trigger in DISABLED_TRIGGERS:
                    continue
                if (
                    rule.kind.casefold() == "actorstatuseffect"
                    and rule.trigger in IMMEDIATE_TRIGGERS
                    and rule.operation.casefold().startswith("add")
                ):
                    occurrence_key = (
                        program.template_id, rule.definition.resource_id
                    )
                    ordinal = occurrence.get(occurrence_key, 0)
                    occurrence[occurrence_key] = ordinal + 1
                    immediate_effects.append(
                        ImmediateSocketEffect(
                            key=(
                                f"{program.template_id}:"
                                f"{rule.definition.resource_id}:{ordinal}"
                            ),
                            socket_template_id=program.template_id,
                            resource_id=rule.definition.resource_id,
                            effect_id=rule.definition.effect_id,
                            duration_ticks=(
                                rule.definition.duration_ticks
                                or round(
                                    PERMANENT_SOCKET_EFFECT_SECONDS
                                    * SERVER_TICKS_PER_SECOND
                                )
                            ),
                            values=rule.definition.values,
                        )
                    )
                    boss_bonus_gems += {
                        "jewel_boss_gem_drop_common": 1,
                        "jewel_boss_gem_drop_uncommon": 2,
                        "jewel_boss_gem_drop_magic": 3,
                        "jewel_boss_gem_drop_rare": 4,
                        "jewel_boss_gem_drop_epic": 5,
                    }.get(rule.definition.resource_id, 0)
                    periodic = self._periodic_aura(
                        program.template_id, rule, ordinal
                    )
                    if periodic is not None:
                        periodic_auras.append(periodic)
                elif (
                    rule.kind.casefold() == "skillstatuseffect"
                    and rule.target == "user"
                    and rule.skill_group.casefold()
                    in GENERIC_SKILL_EVENT_GROUPS
                    and rule.trigger not in IMMEDIATE_TRIGGERS
                ):
                    event_index.setdefault(rule.trigger, []).append(rule)
                elif rule.kind.casefold() == "skillstatuseffect":
                    cast_rules.append(rule)
                else:
                    event_index.setdefault(rule.trigger, []).append(rule)
        return SocketEffectEvaluation(
            socket_template_ids=socket_ids,
            modifiers=modifiers,
            event_rules=tuple(
                (trigger, tuple(rules))
                for trigger, rules in sorted(event_index.items())
            ),
            cast_rules=tuple(cast_rules),
            immediate_effects=tuple(immediate_effects),
            periodic_auras=tuple(periodic_auras),
            drop_amounts=tuple(
                (template_id, values[0], values[1])
                for template_id, values in sorted(drop_amount_index.items())
            ),
            actor_variables=tuple(sorted(actor_variables.items())),
            boss_bonus_gems=max(0, int(boss_bonus_gems)),
            skill_scalars=tuple(skill_scalars),
            status_group_durations=tuple(
                sorted(status_group_durations.items())
            ),
        )

    @staticmethod
    def reconcile_effects(
        evaluation: SocketEffectEvaluation,
        current_effects: dict[str, StatusEffectEntry],
        *,
        actor_id: int,
        next_instance_id: int,
        server_tick: int,
    ) -> SocketEffectTransition:
        wanted = {effect.key: effect for effect in evaluation.immediate_effects}
        retained = {
            key: entry for key, entry in current_effects.items() if key in wanted
        }
        removed = tuple(sorted(set(current_effects) - set(wanted)))
        added = tuple(sorted(set(wanted) - set(current_effects)))
        entries: list[StatusEffectEntry] = []
        for key in removed:
            previous = current_effects[key]
            entries.append(
                StatusEffectEntry(
                    effect_id=int(previous.effect_id),
                    field_04=int(previous.field_04),
                    field_0c=int(server_tick) & 0xFFFFFFFF,
                    field_14=0,
                    field_08=int(previous.field_08),
                    field_18=int(previous.field_18),
                    field_10=0,
                    field_1c=100,
                    field_20=int(actor_id),
                    field_25=False,
                    field_26=False,
                    field_39=False,
                    has_details=False,
                    values=(),
                    field_38=False,
                    field_3c=0xFF,
                    shape_values=(),
                )
            )
        for key in added:
            definition = wanted[key]
            duration = max(1, int(definition.duration_ticks))
            entry = StatusEffectEntry(
                effect_id=definition.effect_id,
                field_04=int(next_instance_id),
                field_0c=(int(server_tick) + duration) & 0xFFFFFFFF,
                field_14=SERVER_TICKS_PER_SECOND,
                field_08=int(server_tick) & 0xFFFFFFFF,
                field_18=0,
                field_10=duration,
                field_1c=100,
                field_20=int(actor_id),
                field_25=False,
                field_26=False,
                field_39=False,
                has_details=True,
                values=definition.values,
            )
            retained[key] = entry
            entries.append(entry)
            next_instance_id += 1
        return SocketEffectTransition(
            effects=retained,
            next_instance_id=int(next_instance_id),
            status_effect=(StatusEffect(False, tuple(entries)) if entries else None),
            added_keys=added,
            removed_keys=removed,
        )

    def unresolved_effect_resources(self) -> tuple[tuple[str, str], ...]:
        """Return status references that cannot be resolved in this DB copy."""

        missing = []
        for program in self.programs.values():
            for modifier in program.modifiers:
                kind = modifier.kind.casefold()
                if kind == "actorstatuseffect" and len(modifier.arguments) >= 2:
                    resource_id = modifier.arguments[1]
                elif kind == "skillstatuseffect" and len(modifier.arguments) >= 3:
                    resource_id = modifier.arguments[2]
                else:
                    continue
                if resource_id and resource_id != "empty_effect" and resource_id not in self._status_rows:
                    missing.append((program.template_id, resource_id))
        return tuple(sorted(set(missing)))

    def unsupported_catalogue_entries(
        self,
    ) -> tuple[tuple[str, str, str], ...]:
        """Return every modifier/operation/trigger outside server coverage.

        This is an executable catalogue contract rather than a handwritten
        allow-list in documentation. Tests run it against the bundled DB, so
        a future client database adding a jewel or rune behavior fails loudly
        until a server handler and its coverage declaration are added.
        """

        unsupported = []
        for program in self.programs.values():
            for modifier in program.modifiers:
                kind = modifier.kind.casefold()
                if kind not in SUPPORTED_MODIFIER_KINDS:
                    unsupported.append(
                        (program.template_id, "modifier", modifier.kind)
                    )
                    continue
                if kind in {"actorstatuseffect", "skillstatuseffect"}:
                    operation = modifier.operation.casefold()
                    if operation not in SUPPORTED_EFFECT_OPERATIONS:
                        unsupported.append(
                            (program.template_id, "operation", operation)
                        )
                    trigger = modifier.trigger
                    if (
                        trigger not in SUPPORTED_EVENT_TRIGGERS
                        and not trigger.startswith("maxstacksize")
                    ):
                        unsupported.append(
                            (program.template_id, "trigger", trigger)
                        )
        # Root item programs are only half of the contract. Unique socket
        # effects attach status programs which may attach further statuses;
        # audit that complete graph so new client content cannot silently add
        # a server-side behavior without an implementation.
        pending = [
            rule.definition.resource_id
            for program in self.programs.values()
            for rule in program.rules
        ]
        seen_statuses = set()
        while pending:
            resource_id = pending.pop()
            if resource_id in seen_statuses:
                continue
            seen_statuses.add(resource_id)
            row = self._status_rows.get(resource_id)
            if row is None:
                continue
            for modifier in parse_socket_program(row["StartModifiers"]):
                kind = modifier.kind.casefold()
                if kind not in SUPPORTED_STATUS_RUNTIME_KINDS:
                    unsupported.append(
                        (resource_id, "status-modifier", modifier.kind)
                    )
                if (
                    kind == "actorstatuseffect"
                    and len(modifier.arguments) >= 2
                ):
                    pending.append(modifier.arguments[1])
        return tuple(sorted(set(unsupported)))


@lru_cache(maxsize=4)
def prime_socket_effect_database(path: str | Path) -> SocketEffectDatabase:
    database = SocketEffectDatabase(Path(path).resolve())
    _ = database.programs
    return database


def apply_socket_drop_amounts(evaluation, generation):
    """Apply equipped ``DropAmount`` rules to an immutable loot result."""

    if evaluation is None or generation is None:
        return generation
    updated = []
    changed = False
    actor_variables = dict(getattr(evaluation, "actor_variables", ()))
    for drop in tuple(generation.drops):
        absolute, relative = evaluation.drop_amount_bonus(
            drop.item.template_id
        )
        template_id = str(drop.item.template_id)
        if template_id in {"money_rc", "money_rc_no_auto_use", "money_rc_no_scaling"}:
            relative += actor_variables.get(
                "trait_groupaura_all_essentials_rc", 0.0
            )
        elif template_id in {"money_vc", "money_vc_gold"}:
            relative += actor_variables.get(
                "trait_groupaura_all_essentials_vc", 0.0
            )
        elif template_id == "money_gc":
            relative += actor_variables.get(
                "trait_groupaura_all_progression_wisdom", 0.0
            )
        elif template_id.startswith("soul_shard_"):
            absolute += actor_variables.get(
                "trait_groupaura_all_progression_shards", 0.0
            )
        elif template_id == "e_item_teleport_change_01":
            absolute += actor_variables.get(
                "trait_groupaura_all_essentials_realm_fragments", 0.0
            )
        if absolute == 0.0 and relative == 0.0:
            updated.append(drop)
            continue
        amount = max(
            1,
            int(round(
                (int(drop.item.amount) + absolute)
                * max(0.0, 1.0 + relative)
            )),
        )
        updated.append(replace(drop, item=replace(drop.item, amount=amount)))
        changed = changed or amount != int(drop.item.amount)
    return replace(generation, drops=tuple(updated)) if changed else generation


__all__ = [
    "DISABLED_TRIGGERS",
    "IMMEDIATE_TRIGGERS",
    "SUPPORTED_EFFECT_OPERATIONS",
    "SUPPORTED_EVENT_TRIGGERS",
    "SUPPORTED_MODIFIER_KINDS",
    "SUPPORTED_STATUS_RUNTIME_KINDS",
    "ImmediateSocketEffect",
    "SocketEffectDatabase",
    "SocketEffectEvaluation",
    "SocketEffectRule",
    "SocketEventAura",
    "SocketEffectTransition",
    "SocketItemProgram",
    "SocketModifier",
    "SocketStatusDefinition",
    "SocketPeriodicAura",
    "normalize_trigger",
    "parse_socket_modifier",
    "parse_socket_program",
    "apply_socket_drop_amounts",
    "prime_socket_effect_database",
]
