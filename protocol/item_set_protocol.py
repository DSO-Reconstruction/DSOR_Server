"""Database-driven item-set evaluation and immediate status-effect support.

``_Template_ItemSet.Modifiers`` is a compact modifier program.  A leading
``N)`` activates the following ``;``-separated modifiers when at least N
distinct set parts are equipped, and ``|`` starts another tier.  The client
uses the same table to calculate presentation and item-derived attributes;
the server keeps the parsed rules as authoritative gameplay state and emits
the actor effects whose trigger explicitly says they are immediate.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from functools import cached_property
from pathlib import Path
import re
import sqlite3

from dso_server.persistence import readonly_db

from dso_server.protocol.gameplay_protocol import StatusEffect, StatusEffectEntry
from dso_server.protocol.inventory_codec import active_equipment_slots


SERVER_TICKS_PER_SECOND = 25
PERMANENT_SET_EFFECT_SECONDS = 36_000.0
_TIER_PREFIX = re.compile(r"^\s*(\d+)\)(.*)$", re.DOTALL)


@dataclass(frozen=True)
class ItemSetModifier:
    kind: str
    arguments: tuple[str, ...]
    label: str
    raw: str
    source_set_id: str = ""

    @cached_property
    def metadata(self) -> dict[str, str]:
        result: dict[str, str] = {}
        for argument in self.arguments:
            name, separator, value = argument.partition(":")
            if separator:
                result[name.casefold()] = value
        return result

    @cached_property
    def parameters(self) -> tuple[float, ...]:
        indexed: dict[int, float] = {}
        for name, value in self.metadata.items():
            if not name.startswith("$"):
                continue
            try:
                index = int(name[1:])
                number = float(value)
            except ValueError:
                continue
            if index >= 0:
                indexed[index] = number
        count = max(5, max(indexed, default=-1) + 1)
        return tuple(indexed.get(index, 0.0) for index in range(count))

    @property
    def numeric_value(self) -> float | None:
        if not self.arguments:
            return None
        try:
            return float(self.arguments[0])
        except ValueError:
            return None

    @property
    def actor_operation(self) -> str:
        if self.kind.casefold() != "actorstatuseffect" or not self.arguments:
            return ""
        return self.arguments[0]

    @property
    def actor_effect_id(self) -> str:
        if (
            self.kind.casefold() != "actorstatuseffect"
            or len(self.arguments) < 2
        ):
            return ""
        return self.arguments[1]

    @property
    def trigger(self) -> str:
        return self.metadata.get("on", "").strip().casefold()

    @property
    def chance(self) -> float:
        try:
            return max(0.0, min(1.0, float(self.metadata.get("c", "1"))))
        except ValueError:
            return 0.0


@dataclass(frozen=True)
class ItemSetTier:
    required_parts: int
    modifiers: tuple[ItemSetModifier, ...]


@dataclass(frozen=True)
class ItemSetDefinition:
    set_id: str
    parts: tuple[str, ...]
    tiers: tuple[ItemSetTier, ...]
    raw_modifiers: str


@dataclass(frozen=True)
class ActiveItemSetTier:
    set_id: str
    equipped_parts: int
    total_parts: int
    required_parts: int
    modifiers: tuple[ItemSetModifier, ...]


@dataclass(frozen=True)
class ImmediateItemSetEffect:
    key: str
    set_id: str
    required_parts: int
    resource_id: str
    effect_id: int
    duration_ticks: int
    values: tuple[float, ...]


@dataclass(frozen=True)
class ItemSetEvaluation:
    equipped_template_ids: frozenset[str]
    active_tiers: tuple[ActiveItemSetTier, ...]
    modifiers: tuple[ItemSetModifier, ...]
    attribute_modifiers: tuple[ItemSetModifier, ...]
    actor_effect_rules: tuple[ItemSetModifier, ...]
    skill_effect_rules: tuple[ItemSetModifier, ...]
    immediate_effects: tuple[ImmediateItemSetEffect, ...]

    @property
    def active_set_ids(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(tier.set_id for tier in self.active_tiers))


@dataclass(frozen=True)
class ItemSetEffectTransition:
    effects: dict[str, StatusEffectEntry]
    next_instance_id: int
    status_effect: StatusEffect | None
    added_keys: tuple[str, ...]
    removed_keys: tuple[str, ...]


def _parse_modifier(raw: str) -> ItemSetModifier:
    raw = raw.strip()
    body, separator, label = raw.partition("#")
    kind, colon, argument_text = body.partition(":")
    arguments = (
        tuple(part.strip() for part in argument_text.split(","))
        if colon
        else ()
    )
    return ItemSetModifier(
        kind=kind.strip(),
        arguments=arguments,
        label=label.strip() if separator else "",
        raw=raw,
    )


def parse_item_set_modifiers(value: str) -> tuple[ItemSetTier, ...]:
    """Parse the complete generated ``Modifiers`` column without dropping rules."""

    tiers: list[ItemSetTier] = []
    for raw_group in str(value or "").split("|"):
        raw_group = raw_group.strip()
        if not raw_group:
            continue
        match = _TIER_PREFIX.match(raw_group)
        if match is None:
            raise ValueError(f"item-set tier is missing N) prefix: {raw_group!r}")
        required_parts = int(match.group(1))
        if required_parts <= 0:
            raise ValueError("item-set tier requires a positive part count")
        modifiers = tuple(
            _parse_modifier(raw_modifier)
            for raw_modifier in match.group(2).split(";")
            if raw_modifier.strip()
        )
        if modifiers:
            tiers.append(ItemSetTier(required_parts, modifiers))
    return tuple(tiers)


class ItemSetDatabase:
    """Load, evaluate, and resolve the client's item-set definitions."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def _connect(self) -> sqlite3.Connection:
        # Called once per effect resolution. Reopening a 68 MB database costs
        # ~56us each time for a file that never changes, so connections are
        # cached per thread. `with connection` is sqlite3's *transaction*
        # context manager -- it commits, it does not close -- so existing
        # `with self._connect() as db` callers keep working unchanged.
        if not self.path.is_file():
            raise FileNotFoundError(self.path)
        return readonly_db.connect(self.path)

    @cached_property
    def definitions(self) -> tuple[ItemSetDefinition, ...]:
        with self._connect() as database:
            rows = database.execute(
                "SELECT [Id], [SetParts], [Modifiers] "
                "FROM [_Template_ItemSet] ORDER BY rowid"
            ).fetchall()
        definitions = []
        for set_id, raw_parts, raw_modifiers in rows:
            parts = tuple(
                dict.fromkeys(
                    part.strip()
                    for part in str(raw_parts or "").split(";")
                    if part.strip()
                )
            )
            if not parts:
                continue
            tiers = parse_item_set_modifiers(str(raw_modifiers or ""))
            if not tiers:
                continue
            definitions.append(
                ItemSetDefinition(
                    str(set_id),
                    parts,
                    tiers,
                    str(raw_modifiers or ""),
                )
            )
        return tuple(definitions)

    def _effect_definition(
        self, resource_id: str
    ) -> tuple[int, int] | None:
        with self._connect() as database:
            row = database.execute(
                "SELECT rowid - 1, [StatusEffectDuration] "
                "FROM [_Template_StatusEffect] WHERE [Id] = ? LIMIT 1",
                (str(resource_id),),
            ).fetchone()
        if row is None:
            return None
        duration_ticks = max(
            1,
            round(
                (
                    max(0.0, float(row[1] or 0.0))
                    or PERMANENT_SET_EFFECT_SECONDS
                )
                * SERVER_TICKS_PER_SECOND
            ),
        )
        return int(row[0]), duration_ticks

    @staticmethod
    def _class_score(definition: ItemSetDefinition, class_name: str) -> int:
        class_name = str(class_name or "").casefold()
        set_id = definition.set_id.casefold()
        if not class_name:
            return 0
        return int(
            set_id.startswith(f"set_{class_name}_")
            or set_id.endswith(f"_{class_name}")
            or f"_{class_name}_" in set_id
        )

    def evaluate(
        self,
        equipped_template_ids,
        *,
        class_name: str = "",
        enabled_rulesets=(),
    ) -> ItemSetEvaluation:
        equipped = frozenset(
            str(template_id)
            for template_id in equipped_template_ids
            if template_id
        )
        enabled = {str(value).casefold() for value in enabled_rulesets}
        # Four class-named rows share identical parts/modifiers for the common
        # Dragonsilver jewelry set.  Prefer the current class row, then
        # collapse exact aliases so one physical loadout is counted once.
        ordered = sorted(
            self.definitions,
            key=lambda definition: self._class_score(
                definition, class_name
            ),
            reverse=True,
        )
        seen_signatures = set()
        active_tiers: list[ActiveItemSetTier] = []
        immediate_effects: list[ImmediateItemSetEffect] = []
        for definition in ordered:
            signature = (
                definition.parts,
                definition.raw_modifiers,
            )
            if signature in seen_signatures:
                continue
            seen_signatures.add(signature)
            equipped_count = sum(
                any(alternative.strip() in equipped
                    for alternative in part.split("|"))
                for part in definition.parts
            )
            if equipped_count <= 0:
                continue
            for tier_ordinal, tier in enumerate(definition.tiers):
                if equipped_count < tier.required_parts:
                    continue
                active = ActiveItemSetTier(
                    definition.set_id,
                    equipped_count,
                    len(definition.parts),
                    tier.required_parts,
                    tuple(
                        replace(modifier, source_set_id=definition.set_id)
                        for modifier in tier.modifiers
                    ),
                )
                active_tiers.append(active)
                for modifier_ordinal, modifier in enumerate(tier.modifiers):
                    if modifier.kind.casefold() != "actorstatuseffect":
                        continue
                    if not modifier.actor_operation.casefold().startswith("add"):
                        continue
                    if modifier.trigger not in ("immediate", "immediatemanaged"):
                        continue
                    required_ruleset = modifier.metadata.get(
                        "enableonruleset", ""
                    ).casefold()
                    if required_ruleset and required_ruleset not in enabled:
                        continue
                    resource_id = modifier.actor_effect_id
                    if (
                        not resource_id
                        or resource_id == "empty_effect"
                        or modifier.chance <= 0.0
                    ):
                        continue
                    resolved = self._effect_definition(resource_id)
                    if resolved is None:
                        continue
                    effect_id, duration_ticks = resolved
                    immediate_effects.append(
                        ImmediateItemSetEffect(
                            key=(
                                f"{definition.set_id}:{tier.required_parts}:"
                                f"{tier_ordinal}:{modifier_ordinal}:"
                                f"{resource_id}"
                            ),
                            set_id=definition.set_id,
                            required_parts=tier.required_parts,
                            resource_id=resource_id,
                            effect_id=effect_id,
                            duration_ticks=duration_ticks,
                            values=modifier.parameters,
                        )
                    )
        modifiers = tuple(
            modifier
            for tier in active_tiers
            for modifier in tier.modifiers
        )
        return ItemSetEvaluation(
            equipped_template_ids=equipped,
            active_tiers=tuple(active_tiers),
            modifiers=modifiers,
            attribute_modifiers=tuple(
                modifier
                for modifier in modifiers
                if modifier.kind.casefold()
                not in ("actorstatuseffect", "skillstatuseffect", "localize")
            ),
            actor_effect_rules=tuple(
                modifier
                for modifier in modifiers
                if modifier.kind.casefold() == "actorstatuseffect"
            ),
            skill_effect_rules=tuple(
                modifier
                for modifier in modifiers
                if modifier.kind.casefold() == "skillstatuseffect"
            ),
            immediate_effects=tuple(immediate_effects),
        )

    @staticmethod
    def equipped_templates(inventory) -> tuple[str, ...]:
        items = {
            int(item.item_id): str(item.template_id)
            for item in inventory.items
        }
        # A parked secondary essence is equipped but inert.
        return tuple(
            items[item_id]
            for item_id in active_equipment_slots(inventory)
            if int(item_id) in items
        )

    def evaluate_inventory(
        self,
        inventory,
        *,
        class_name: str = "",
        enabled_rulesets=(),
    ) -> ItemSetEvaluation:
        return self.evaluate(
            self.equipped_templates(inventory),
            class_name=class_name,
            enabled_rulesets=enabled_rulesets,
        )

    @staticmethod
    def reconcile_effects(
        evaluation: ItemSetEvaluation,
        current_effects: dict[str, StatusEffectEntry],
        *,
        actor_id: int,
        next_instance_id: int,
        server_tick: int,
    ) -> ItemSetEffectTransition:
        desired = {
            definition.key: definition
            for definition in evaluation.immediate_effects
        }
        current = dict(current_effects)
        removed_keys = tuple(key for key in current if key not in desired)
        added_keys = tuple(key for key in desired if key not in current)
        changed_entries: list[StatusEffectEntry] = []
        for key in removed_keys:
            previous = current.pop(key)
            changed_entries.append(
                replace(
                    previous,
                    field_0c=0,
                    field_14=0,
                    field_18=0,
                    field_10=0,
                    field_39=False,
                    has_details=False,
                    values=(),
                    field_38=False,
                    field_3c=0xFF,
                    shape_values=(),
                )
            )
        next_instance_id = max(1, int(next_instance_id))
        for key in added_keys:
            definition = desired[key]
            duration = int(definition.duration_ticks)
            entry = StatusEffectEntry(
                effect_id=definition.effect_id,
                field_04=next_instance_id,
                field_0c=(
                    (int(server_tick) + duration) & 0xFFFFFFFF
                    if duration
                    else 0
                ),
                field_14=25,
                field_08=int(server_tick) & 0xFFFFFFFF,
                field_18=0,
                field_10=duration,
                field_1c=100,
                field_20=int(actor_id),
                field_25=False,
                field_26=False,
                field_39=True,
                has_details=True,
                values=definition.values,
            )
            next_instance_id += 1
            current[key] = entry
            changed_entries.append(entry)
        return ItemSetEffectTransition(
            effects=current,
            next_instance_id=next_instance_id,
            status_effect=(
                StatusEffect(False, tuple(changed_entries))
                if changed_entries
                else None
            ),
            added_keys=added_keys,
            removed_keys=removed_keys,
        )
