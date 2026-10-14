'''Shared evaluation for database-authored player status modifiers.

The client stores persistent gameplay bonuses as small programs on
``_Template_StatusEffect``.  Runtime status entries provide parameter values
and a zero-based stack index.  This module compiles the modifier subset used
by loot and player experience so every item/status source follows the same
arithmetic instead of growing item-id special cases in service handlers.
'''

from __future__ import annotations

from dataclasses import dataclass, replace
from functools import lru_cache
import math
from pathlib import Path
import sqlite3
import time
from types import MappingProxyType
from typing import Iterable, Mapping


VIRTUAL_CURRENCY_DROP_TEMPLATES = frozenset((
    'money_vc',
    'money_vc_gold',
))


@dataclass(frozen=True)
class ModifierInstruction:
    '''One semicolon-delimited status modifier instruction.'''

    kind: str
    arguments: tuple[str, ...]
    label: str = ''
    raw: str = ''


@dataclass(frozen=True)
class StatusModifierDefinition:
    effect_id: int
    max_stack_size: int
    start_modifiers: tuple[ModifierInstruction, ...]
    tick_modifiers: tuple[ModifierInstruction, ...]


@dataclass(frozen=True)
class StatusModifierCatalog:
    definitions: Mapping[int, StatusModifierDefinition]
    actor_variables: Mapping[str, float]


@dataclass(frozen=True)
class ModifierAmounts:
    '''Additive absolute and relative contributions for one property.'''

    absolute: float = 0.0
    relative: float = 0.0

    def plus(self, value: float, operation: str) -> 'ModifierAmounts':
        if str(operation).casefold() == 'relative':
            return replace(self, relative=self.relative + float(value))
        return replace(self, absolute=self.absolute + float(value))

    def combine(self, other: 'ModifierAmounts') -> 'ModifierAmounts':
        return ModifierAmounts(
            self.absolute + other.absolute,
            self.relative + other.relative,
        )

    def apply(self, base: int, *, minimum: int = 0) -> int:
        '''Apply native additive buckets with deterministic half-up rounding.'''

        result = (
            float(base) * max(0.0, 1.0 + self.relative)
            + self.absolute
        )
        return max(int(minimum), int(math.floor(result + 0.5)))


@dataclass(frozen=True)
class ActiveStatusModifiers:
    xp_gain: ModifierAmounts = ModifierAmounts()
    virtual_currency_drop: ModifierAmounts = ModifierAmounts()
    drop_amounts: tuple[tuple[str, ModifierAmounts], ...] = ()
    quest_experience: ModifierAmounts = ModifierAmounts()

    def drop_amount_bonus(
        self,
        template_id: str,
        *,
        virtual_currency_templates: Iterable[str] = (
            VIRTUAL_CURRENCY_DROP_TEMPLATES
        ),
    ) -> ModifierAmounts:
        wanted = str(template_id or '').casefold()
        result = ModifierAmounts()
        if wanted in {
            str(value or '').casefold()
            for value in virtual_currency_templates
        }:
            result = result.combine(self.virtual_currency_drop)
        for target, amounts in self.drop_amounts:
            if target.casefold() == wanted:
                result = result.combine(amounts)
        return result

    @property
    def xp_gain_multiplier(self) -> float:
        return max(0.0, 1.0 + self.xp_gain.relative)

    def apply_xp_gain(self, base: int) -> int:
        return self.xp_gain.apply(base, minimum=0)


@lru_cache(maxsize=8192)
def parse_modifier(raw: str) -> ModifierInstruction:
    text = str(raw or '').strip()
    body, separator, label = text.partition('#')
    kind, colon, argument_text = body.partition(':')
    return ModifierInstruction(
        kind=kind.strip().casefold(),
        arguments=(
            tuple(value.strip() for value in argument_text.split(','))
            if colon else ()
        ),
        label=label.strip() if separator else '',
        raw=text,
    )


def parse_modifier_program(value: str) -> tuple[ModifierInstruction, ...]:
    return tuple(
        parse_modifier(instruction)
        for instruction in str(value or '').split(';')
        if instruction.strip()
    )


def resolve_modifier_value(
    raw_value: str,
    values: Iterable[float] = (),
    actor_variables: Mapping[str, float] | None = None,
) -> float | None:
    '''Resolve a literal, ``$N`` parameter or ``$ActorVariable`` operand.'''

    token = str(raw_value or '').split('#', 1)[0].split('<', 1)[0].strip()
    if not token:
        return None
    if token.startswith('$'):
        reference = token[1:].strip()
        if reference.isdigit():
            parameters = tuple(values)
            index = int(reference)
            if not 0 <= index < len(parameters):
                return None
            return float(parameters[index])
        variables = actor_variables or {}
        found = variables.get(reference.casefold())
        return float(found) if found is not None else None
    try:
        return float(token)
    except ValueError:
        return None


def _database_signature(path: str | Path) -> tuple[str, int, int]:
    resolved = Path(path).resolve()
    stat = resolved.stat()
    return str(resolved), int(stat.st_mtime_ns), int(stat.st_size)


@lru_cache(maxsize=4)
def _load_status_modifier_catalog_cached(
    database_path: str,
    _mtime_ns: int,
    _size: int,
) -> StatusModifierCatalog:
    path = Path(database_path)
    with sqlite3.connect(
        f'file:{path.as_posix()}?mode=ro', uri=True
    ) as database:
        rows = tuple(database.execute('''
            SELECT rowid - 1, COALESCE(MaxStackSize, 1),
                   COALESCE(StartModifiers, ''),
                   COALESCE(TickModifiers, '')
            FROM _Template_StatusEffect
        '''))
        variables = tuple(database.execute('''
            SELECT Id, COALESCE(InitialValue, 0)
            FROM _Template_ActorVariables
        '''))
    definitions = {
        int(effect_id): StatusModifierDefinition(
            effect_id=int(effect_id),
            max_stack_size=max(1, int(max_stack_size or 1)),
            start_modifiers=parse_modifier_program(start_modifiers),
            tick_modifiers=parse_modifier_program(tick_modifiers),
        )
        for effect_id, max_stack_size, start_modifiers, tick_modifiers in rows
    }
    return StatusModifierCatalog(
        definitions=MappingProxyType(definitions),
        actor_variables=MappingProxyType({
            str(resource_id).casefold(): float(value or 0.0)
            for resource_id, value in variables
        }),
    )


def load_status_modifier_catalog(
    database_path: str | Path,
) -> StatusModifierCatalog:
    return _load_status_modifier_catalog_cached(
        *_database_signature(database_path)
    )


def _status_entries(status_effects: Iterable[object]) -> tuple[object, ...]:
    '''Normalize direct entries and runtime ``(entry, expires_at)`` values.'''

    result = []
    for value in status_effects:
        entry = value
        if not hasattr(entry, 'effect_id'):
            if not isinstance(value, tuple) or not value:
                continue
            entry = value[0]
        if hasattr(entry, 'effect_id'):
            result.append(entry)
    return tuple(result)


def snapshot_active_status_entries(
    persistent_status_effects: Mapping[int, object],
    timed_status_effect_expiries: Mapping[int, float],
    runtime_status_effects: Mapping[int, tuple[object, float | None]],
    *additional_status_effects: Mapping[object, object] | Iterable[object],
    now_epoch: float | None = None,
    now_monotonic: float | None = None,
) -> tuple[object, ...]:
    '''Snapshot only live entries across the session's status stores.

    Persistent consumables use wall-clock deadlines so they survive relog;
    map-local runtime effects use the monotonic clock. Timer callbacks may be
    delayed under load, therefore consumers must enforce those deadlines as
    well instead of treating mere dictionary membership as active state.
    '''

    epoch = time.time() if now_epoch is None else float(now_epoch)
    monotonic = (
        time.perf_counter()
        if now_monotonic is None else float(now_monotonic)
    )
    entries = [
        entry
        for instance_id, entry in persistent_status_effects.items()
        if (
            (expires_at := timed_status_effect_expiries.get(instance_id))
            is None
            or float(expires_at) > epoch
        )
    ]
    entries.extend(
        entry
        for entry, expires_at in runtime_status_effects.values()
        if expires_at is None or float(expires_at) > monotonic
    )
    for source in additional_status_effects:
        values = source.values() if isinstance(source, Mapping) else source
        entries.extend(_status_entries(values))
    return tuple(entries)


def evaluate_active_status_modifiers(
    catalog: StatusModifierCatalog,
    status_effects: Iterable[object],
) -> ActiveStatusModifiers:
    '''Evaluate ongoing StartModifiers for all active runtime entries.'''

    xp_gain = ModifierAmounts()
    quest_experience = ModifierAmounts()
    virtual_currency = ModifierAmounts()
    drops: dict[str, ModifierAmounts] = {}
    entries = _status_entries(status_effects)
    # Talent ranks modify actor variables before their companion effect reads
    # them. Reading InitialValue alone reduced +106% Wisdom gold to +1.7%.
    actor_variables = dict(catalog.actor_variables)
    for entry in entries:
        definition = catalog.definitions.get(int(entry.effect_id))
        if definition is None:
            continue
        stack_count = min(
            max(1, int(getattr(entry, 'field_18', 0)) + 1),
            definition.max_stack_size,
        )
        values = tuple(getattr(entry, 'values', ()))
        for modifier in definition.start_modifiers:
            if modifier.kind != 'actorvariable' or len(modifier.arguments) < 3:
                continue
            value = resolve_modifier_value(modifier.arguments[0], values, actor_variables)
            if value is None:
                continue
            value *= stack_count
            variable_id = modifier.arguments[2].strip().casefold()
            current = actor_variables.get(variable_id, 0.0)
            actor_variables[variable_id] = (
                current * (1.0 + value)
                if modifier.arguments[1].casefold() == 'relative'
                else current + value
            )
    for entry in entries:
        definition = catalog.definitions.get(int(entry.effect_id))
        if definition is None:
            continue
        stack_count = min(
            max(1, int(getattr(entry, 'field_18', 0)) + 1),
            definition.max_stack_size,
        )
        values = tuple(getattr(entry, 'values', ()))
        for modifier in definition.start_modifiers:
            kind = modifier.kind
            if kind not in {
                'xpgain', 'questexperience', 'virtualcurrencydropamount', 'dropamount'
            }:
                continue
            if len(modifier.arguments) < 2:
                continue
            value = resolve_modifier_value(
                modifier.arguments[0], values, actor_variables
            )
            if value is None:
                continue
            value *= stack_count
            operation = modifier.arguments[1]
            if kind == 'xpgain':
                xp_gain = xp_gain.plus(value, operation)
            elif kind == 'questexperience':
                quest_experience = quest_experience.plus(value, operation)
            elif kind == 'virtualcurrencydropamount':
                virtual_currency = virtual_currency.plus(value, operation)
            elif len(modifier.arguments) >= 3:
                target = modifier.arguments[2].strip().casefold()
                if target:
                    drops[target] = drops.get(
                        target, ModifierAmounts()
                    ).plus(value, operation)
    return ActiveStatusModifiers(
        xp_gain=xp_gain,
        quest_experience=quest_experience,
        virtual_currency_drop=virtual_currency,
        drop_amounts=tuple(sorted(drops.items())),
    )


def active_xp_gain_multiplier(
    database_path: str | Path,
    status_effects: Iterable[object],
) -> float:
    '''Return the multiplier future normal-XP awards should use.'''

    return evaluate_active_status_modifiers(
        load_status_modifier_catalog(database_path), status_effects
    ).xp_gain_multiplier


def instant_current_xp_gain(
    definition: object,
    *,
    current_xp: int = 0,
    stack_count: int = 1,
    actor_variables: Mapping[str, float] | None = None,
) -> int:
    '''Resolve ``CurrXP`` once when an instant item is consumed/picked up.'''

    amounts = ModifierAmounts()
    programs = (
        getattr(definition, 'start_modifiers', ''),
        getattr(definition, 'tick_modifiers', ''),
    )
    values = tuple(getattr(definition, 'values', ()))
    count = min(
        max(1, int(stack_count)),
        max(1, int(getattr(definition, 'max_stack_size', 1))),
    )
    for program in programs:
        for modifier in parse_modifier_program(program):
            if modifier.kind != 'currxp' or len(modifier.arguments) < 2:
                continue
            value = resolve_modifier_value(
                modifier.arguments[0], values, actor_variables
            )
            if value is not None:
                amounts = amounts.plus(value * count, modifier.arguments[1])
    updated = amounts.apply(max(0, int(current_xp)), minimum=0)
    return max(0, updated - max(0, int(current_xp)))


def apply_status_drop_amounts(
    database_path: str | Path,
    status_effects: Iterable[object],
    generation,
    *,
    excluded_template_ids: Iterable[str] = (),
):
    '''Apply active generic drop modifiers to an immutable loot generation.'''

    if generation is None:
        return generation
    evaluation = evaluate_active_status_modifiers(
        load_status_modifier_catalog(database_path), status_effects
    )
    excluded = {
        str(template_id or '').casefold()
        for template_id in excluded_template_ids
    }
    updated = []
    changed = False
    for drop in tuple(generation.drops):
        template_id = str(drop.item.template_id)
        if template_id.casefold() in excluded:
            updated.append(drop)
            continue
        bonus = evaluation.drop_amount_bonus(template_id)
        if bonus == ModifierAmounts():
            updated.append(drop)
            continue
        amount = bonus.apply(int(drop.item.amount), minimum=1)
        updated.append(replace(
            drop, item=replace(drop.item, amount=amount)
        ))
        changed = changed or amount != int(drop.item.amount)
    return replace(generation, drops=tuple(updated)) if changed else generation


__all__ = [
    'ActiveStatusModifiers',
    'ModifierAmounts',
    'ModifierInstruction',
    'StatusModifierCatalog',
    'StatusModifierDefinition',
    'VIRTUAL_CURRENCY_DROP_TEMPLATES',
    'active_xp_gain_multiplier',
    'apply_status_drop_amounts',
    'evaluate_active_status_modifiers',
    'instant_current_xp_gain',
    'load_status_modifier_catalog',
    'parse_modifier',
    'parse_modifier_program',
    'resolve_modifier_value',
    'snapshot_active_status_entries',
]
