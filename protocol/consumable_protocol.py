"""Database-owned consumable item definitions.

Consumables do not encode their gameplay behavior in the client command.  The
item instance only identifies a ``_Template_Item`` row; that row supplies the
parameter values and points at a parameterized ``_Template_StatusEffect``.
Keeping this resolver outside the network service makes buffs and potions use
the same standalone static database as the client while remaining cheap after
the first lookup.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path
import sqlite3

from dso_server.runtime import resident_content
from dso_server.gameplay.local_combat_state import (
    SERVER_TICKS_PER_SECOND,
    VictimStatusEffectDefinition,
)


@dataclass(frozen=True)
class HealthModifier:
    """One ``CurrHealthPoints`` instruction owned by a status effect."""

    value_index: int | None
    constant: float
    relative: bool

    def resolve(self, values: tuple[float, ...]) -> float:
        if self.value_index is None:
            return float(self.constant)
        if not 0 <= self.value_index < len(values):
            return 0.0
        return float(values[self.value_index])


@dataclass(frozen=True)
class LinkedStatusEffectDefinition:
    '''One effect added by an authored ActorStatusEffect:AddAE instruction.

    Edge metadata belongs to the instruction that adds the effect. Timing,
    stacking and persistence belong to the referenced status-effect row.
    '''

    effect_resource_id: str
    effect_id: int
    duration_ticks: int
    tick_interval_seconds: float
    values: tuple[float, ...]
    max_stack_size: int
    exclusive_group: str
    exclusivity_type: str
    persistent: bool
    real_time: bool
    reset_run_time: bool
    keep_on_death: bool
    keep_on_way_home: bool
    on_resume_start_modifiers: bool
    activation_phase: str
    trigger: str
    chance: float
    delay_ticks: int
    immediate_health: tuple[HealthModifier, ...]
    periodic_health: tuple[HealthModifier, ...]
    start_modifiers: str
    tick_modifiers: str
    stop_modifiers: str
    done_modifiers: str
    aura_effect_ids: tuple[str, ...] = ()
    aura_targets: str = ''
    effect_hosts: str = ''
    child_effects: tuple['LinkedStatusEffectDefinition', ...] = ()

    @property
    def modifier_kinds(self) -> frozenset[str]:
        return _modifier_kinds(
            self.start_modifiers,
            self.tick_modifiers,
            self.stop_modifiers,
            self.done_modifiers,
        )

    def runtime_definition(self) -> VictimStatusEffectDefinition:
        return VictimStatusEffectDefinition(
            resource_id=self.effect_resource_id,
            effect_id=self.effect_id,
            chance=self.chance,
            duration_ticks=self.duration_ticks,
            values=self.values,
            trigger=self.trigger,
            delay_ticks=self.delay_ticks,
            max_stack_size=self.max_stack_size,
            modifier_kinds=self.modifier_kinds,
        )


@dataclass(frozen=True)
class ConsumableDefinition:
    item_template_id: str
    effect_resource_id: str
    effect_id: int
    duration_ticks: int
    tick_interval_seconds: float
    values: tuple[float, ...]
    max_stack_size: int
    exclusive_group: str
    exclusivity_type: str
    persistent: bool
    real_time: bool
    immediate_health: tuple[HealthModifier, ...]
    periodic_health: tuple[HealthModifier, ...]
    item_category: str = 'Consumable'
    reset_run_time: bool = False
    keep_on_death: bool = False
    keep_on_way_home: bool = False
    on_resume_start_modifiers: bool = False
    start_modifiers: str = ''
    tick_modifiers: str = ''
    stop_modifiers: str = ''
    done_modifiers: str = ''
    aura_effect_ids: tuple[str, ...] = ()
    aura_targets: str = ''
    effect_hosts: str = ''
    child_effects: tuple[LinkedStatusEffectDefinition, ...] = ()

    @property
    def is_instant(self) -> bool:
        return self.item_category.casefold() == 'instantconsumable'

    @property
    def modifier_kinds(self) -> frozenset[str]:
        return _modifier_kinds(
            self.start_modifiers,
            self.tick_modifiers,
            self.stop_modifiers,
            self.done_modifiers,
        )

    def runtime_definition(self) -> VictimStatusEffectDefinition:
        return VictimStatusEffectDefinition(
            resource_id=self.effect_resource_id,
            effect_id=self.effect_id,
            chance=1.0,
            duration_ticks=self.duration_ticks,
            values=self.values,
            trigger="immediate",
            max_stack_size=self.max_stack_size,
            modifier_kinds=self.modifier_kinds,
        )


StatusEffectDefinition = (
    ConsumableDefinition | LinkedStatusEffectDefinition
)


@dataclass(frozen=True)
class ConsumableCatalog:
    definitions: dict[str, ConsumableDefinition]
    exclusive_groups_by_effect_id: dict[int, str]
    definitions_by_effect_id: dict[
        int, tuple[StatusEffectDefinition, ...]
    ]

    def definition(self, item_template_id: str) -> ConsumableDefinition | None:
        return self.definitions.get(str(item_template_id))

    def consumable_definition(
        self, item_template_id: str
    ) -> ConsumableDefinition | None:
        definition = self.definition(item_template_id)
        if definition is None or definition.is_instant:
            return None
        return definition

    def instant_definition(
        self, item_template_id: str
    ) -> ConsumableDefinition | None:
        definition = self.definition(item_template_id)
        if definition is None or not definition.is_instant:
            return None
        return definition

    def exclusive_group(self, effect_id: int) -> str:
        return self.exclusive_groups_by_effect_id.get(int(effect_id), "")

    def definition_for_effect(
        self, effect_id: int, values: tuple[float, ...] = ()
    ) -> StatusEffectDefinition | None:
        candidates = self.definitions_by_effect_id.get(int(effect_id), ())
        if not candidates:
            return None
        if not values:
            return candidates[0]
        return min(
            candidates,
            key=lambda definition: sum(
                abs(float(left) - float(right))
                for left, right in zip(definition.values, values)
            ),
        )


def _modifier_kinds(*programs: str) -> frozenset[str]:
    return frozenset(
        kind.strip().casefold()
        for program in programs
        for encoded in str(program or '').split(';')
        for kind, separator, _arguments in (encoded.partition(':'),)
        if separator and kind.strip()
    )


def _health_modifiers(program: str) -> tuple[HealthModifier, ...]:
    result = []
    for encoded in str(program or "").split(";"):
        kind, separator, raw_arguments = encoded.strip().partition(":")
        if not separator or kind.strip().casefold() != "currhealthpoints":
            continue
        arguments = tuple(
            value.strip()
            for value in raw_arguments.split(",")
            if value.strip()
        )
        if not arguments:
            continue
        raw_value = arguments[0].split("<", 1)[0].strip()
        value_index = None
        constant = 0.0
        if raw_value.startswith("$") and raw_value[1:].isdigit():
            value_index = int(raw_value[1:])
        else:
            try:
                constant = float(raw_value)
            except ValueError:
                continue
        result.append(HealthModifier(
            value_index=value_index,
            constant=constant,
            relative=any(
                value.casefold() == "relative" for value in arguments[1:]
            ),
        ))
    return tuple(result)


def _numeric_metadata(
    raw_value: object,
    *,
    base: float,
    default: float,
) -> float:
    '''Resolve plain, relative and absolute generated modifier metadata.'''

    text = str(raw_value or '').split('#', 1)[0].strip()
    if not text:
        return float(default)
    numeric = text.split('<', 1)[0].strip()
    try:
        value = float(numeric)
    except ValueError:
        return float(default)
    lowered = text.casefold()
    if '<relative' in lowered:
        return float(base) * (1.0 + value)
    if '<absolute' in lowered:
        return float(base) + value
    return value


def _actor_status_effect_instructions(
    program: str,
) -> tuple[tuple[str, dict[str, str]], ...]:
    '''Parse ActorStatusEffect:AddAE instructions from one modifier program.'''

    result = []
    for encoded in str(program or '').split(';'):
        kind, separator, raw_arguments = encoded.strip().partition(':')
        if not separator or kind.strip().casefold() != 'actorstatuseffect':
            continue
        arguments = tuple(
            value.strip() for value in raw_arguments.split(',')
            if value.strip()
        )
        if len(arguments) < 2 or arguments[0].casefold() != 'addae':
            continue
        resource_id = arguments[1].split('#', 1)[0].strip()
        if not resource_id:
            continue
        metadata = {}
        for token in arguments[2:]:
            name, token_separator, value = token.partition(':')
            if token_separator:
                metadata[name.strip().upper()] = value.strip()
        result.append((resource_id, metadata))
    return tuple(result)


def _linked_status_effects(
    status: sqlite3.Row,
    status_by_resource: dict[str, sqlite3.Row],
    actor_variables: dict[str, float],
    *,
    trail: frozenset[str],
    inherited_duration_seconds: float | None = None,
    inherited_values: tuple[float, ...] = (),
) -> tuple[LinkedStatusEffectDefinition, ...]:
    '''Resolve every authored AddAE edge, retaining its lifecycle phase.'''

    result = []
    programs = (
        ('start', status['StartModifiers']),
        ('tick', status['TickModifiers']),
        ('stop', status['StopModifiers']),
        ('done', status['DoneModifiers']),
    )
    for phase, program in programs:
        for resource_id, metadata in _actor_status_effect_instructions(
            program
        ):
            child = status_by_resource.get(resource_id)
            if child is None:
                continue
            base_duration = max(
                0.0, float(child['StatusEffectDuration'] or 0.0)
            )
            raw_duration = metadata.get('D', metadata.get('DP', ''))
            duration_seconds = (
                _numeric_metadata(
                    raw_duration, base=base_duration,
                    default=base_duration,
                )
                if raw_duration else base_duration
            )
            base_tick_rate = max(
                0.0, float(child['StatusEffectTickRate'] or 0.0)
            )
            raw_tick_rate = metadata.get('TR', '')
            tick_interval = (
                _numeric_metadata(
                    raw_tick_rate, base=base_tick_rate,
                    default=base_tick_rate,
                )
                if raw_tick_rate else base_tick_rate
            )
            parameters = {}
            for name, raw_value in metadata.items():
                if not name.startswith('$') or not name[1:].isdigit():
                    continue
                index = int(name[1:])
                value_text = str(raw_value).split('#', 1)[0]
                numeric = value_text.split('<', 1)[0].strip()
                try:
                    value = float(numeric)
                except ValueError:
                    reference = numeric.lstrip('$').casefold()
                    if reference not in actor_variables:
                        continue
                    value = actor_variables[reference]
                parameters[index] = value
            value_count = max(5, max(parameters, default=-1) + 1)
            child_key = resource_id.casefold()
            descendants = ()
            if child_key not in trail:
                descendants = _linked_status_effects(
                    child,
                    status_by_resource,
                    actor_variables,
                    trail=trail | frozenset((child_key,)),
                    inherited_duration_seconds=duration_seconds,
                    inherited_values=tuple(
                        float(parameters.get(index, 0.0))
                        for index in range(value_count)
                    ),
                )
            result.append(LinkedStatusEffectDefinition(
                effect_resource_id=resource_id,
                effect_id=int(child['EffectId']),
                duration_ticks=max(
                    1,
                    round(
                        max(0.0, duration_seconds)
                        * SERVER_TICKS_PER_SECOND
                    ),
                ),
                tick_interval_seconds=max(
                    1.0 / SERVER_TICKS_PER_SECOND,
                    tick_interval,
                ),
                values=tuple(
                    float(parameters.get(index, 0.0))
                    for index in range(value_count)
                ),
                max_stack_size=max(1, int(child['MaxStackSize'] or 1)),
                exclusive_group=str(
                    child['ExclusiveGroup'] or ''
                ).strip(),
                exclusivity_type=str(
                    child['ExclusivityType'] or 'Replace'
                ).strip(),
                persistent=bool(child['Persistent']),
                real_time=bool(child['StatusEffectRealTime']),
                reset_run_time=bool(child['ResetRunTime']),
                keep_on_death=bool(child['KeepOnDeath']),
                keep_on_way_home=bool(child['KeepOnWayHome']),
                on_resume_start_modifiers=bool(
                    child['OnResumeStartModifiers']
                ),
                activation_phase=phase,
                trigger=str(
                    metadata.get('ON', 'immediate')
                ).split('#', 1)[0].strip().casefold(),
                chance=max(0.0, min(1.0, _numeric_metadata(
                    metadata.get('C', '1'), base=1.0, default=1.0,
                ))),
                delay_ticks=max(0, round(
                    max(0.0, _numeric_metadata(
                        metadata.get('DE', '0'), base=0.0, default=0.0,
                    )) * SERVER_TICKS_PER_SECOND
                )),
                immediate_health=_health_modifiers(
                    child['StartModifiers']
                ),
                periodic_health=_health_modifiers(
                    child['TickModifiers']
                ),
                start_modifiers=str(child['StartModifiers'] or ''),
                tick_modifiers=str(child['TickModifiers'] or ''),
                stop_modifiers=str(child['StopModifiers'] or ''),
                done_modifiers=str(child['DoneModifiers'] or ''),
                aura_effect_ids=tuple(
                    value.strip()
                    for value in str(
                        child['AuraEffectId'] or ''
                    ).split(';')
                    if value.strip()
                ),
                aura_targets=str(child['AuraTargets'] or '').strip(),
                effect_hosts=str(child['EffectHosts'] or '').strip(),
                child_effects=descendants,
            ))
    parent_duration = inherited_duration_seconds
    if parent_duration is None:
        parent_duration = max(
            0.0, float(status['StatusEffectDuration'] or 0.0)
        )
    result.extend(_aura_receiver_effects(
        status,
        status_by_resource,
        actor_variables,
        trail=trail,
        inherited_duration_seconds=parent_duration,
        inherited_values=inherited_values,
    ))
    return tuple(result)


def _aura_receiver_effects(
    status: sqlite3.Row,
    status_by_resource: dict[str, sqlite3.Row],
    actor_variables: dict[str, float],
    *,
    trail: frozenset[str],
    inherited_duration_seconds: float,
    inherited_values: tuple[float, ...],
) -> tuple[LinkedStatusEffectDefinition, ...]:
    '''Resolve receiver-hosted AuraEffectId children of an item effect.'''

    result = []
    aura_targets = str(status['AuraTargets'] or '').strip()
    values = tuple(float(value) for value in inherited_values)
    if len(values) < 5:
        values += (0.0,) * (5 - len(values))
    for resource_id in tuple(
        value.strip()
        for value in str(status['AuraEffectId'] or '').split(';')
        if value.strip()
    ):
        child = status_by_resource.get(resource_id)
        if child is None:
            continue
        effect_hosts = str(child['EffectHosts'] or '').strip()
        host_tokens = frozenset(
            value.strip().casefold()
            for value in effect_hosts.replace(',', ';').split(';')
            if value.strip()
        )
        if host_tokens and 'receiver' not in host_tokens:
            continue
        authored_duration = max(
            0.0, float(child['StatusEffectDuration'] or 0.0)
        )
        duration_seconds = authored_duration or max(
            0.0, inherited_duration_seconds
        )
        child_key = resource_id.casefold()
        descendants = ()
        if child_key not in trail:
            descendants = _linked_status_effects(
                child,
                status_by_resource,
                actor_variables,
                trail=trail | frozenset((child_key,)),
                inherited_duration_seconds=duration_seconds,
                inherited_values=values,
            )
        result.append(LinkedStatusEffectDefinition(
            effect_resource_id=resource_id,
            effect_id=int(child['EffectId']),
            duration_ticks=max(
                1,
                round(duration_seconds * SERVER_TICKS_PER_SECOND),
            ),
            tick_interval_seconds=max(
                1.0 / SERVER_TICKS_PER_SECOND,
                float(child['StatusEffectTickRate'] or 0.0),
            ),
            values=values,
            max_stack_size=max(1, int(child['MaxStackSize'] or 1)),
            exclusive_group=str(child['ExclusiveGroup'] or '').strip(),
            exclusivity_type=str(
                child['ExclusivityType'] or 'Replace'
            ).strip(),
            persistent=bool(child['Persistent']),
            real_time=bool(child['StatusEffectRealTime']),
            reset_run_time=bool(child['ResetRunTime']),
            keep_on_death=bool(child['KeepOnDeath']),
            keep_on_way_home=bool(child['KeepOnWayHome']),
            on_resume_start_modifiers=bool(
                child['OnResumeStartModifiers']
            ),
            activation_phase='start',
            trigger='immediate',
            chance=1.0,
            delay_ticks=0,
            immediate_health=_health_modifiers(child['StartModifiers']),
            periodic_health=_health_modifiers(child['TickModifiers']),
            start_modifiers=str(child['StartModifiers'] or ''),
            tick_modifiers=str(child['TickModifiers'] or ''),
            stop_modifiers=str(child['StopModifiers'] or ''),
            done_modifiers=str(child['DoneModifiers'] or ''),
            aura_effect_ids=tuple(
                value.strip()
                for value in str(child['AuraEffectId'] or '').split(';')
                if value.strip()
            ),
            aura_targets=aura_targets,
            effect_hosts=effect_hosts,
            child_effects=descendants,
        ))
    return tuple(result)


def _database_signature(path: str | Path) -> tuple[str, int, int]:
    resolved = resident_content.resolved(path)
    stat = resident_content.stat(resolved)
    return str(resolved), int(stat.st_mtime_ns), int(stat.st_size)


@lru_cache(maxsize=4)
def _load_consumable_catalog_cached(
    database_path: str,
    _mtime_ns: int,
    _size: int,
) -> ConsumableCatalog:
    uri = f"file:{Path(database_path).as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as database:
        database.row_factory = sqlite3.Row
        status_rows = tuple(database.execute('''
            SELECT rowid - 1 AS EffectId, Id, StatusEffectDuration,
                   StatusEffectTickRate, MaxStackSize, ExclusiveGroup,
                   ExclusivityType, Persistent, StatusEffectRealTime,
                   ResetRunTime, KeepOnDeath, KeepOnWayHome,
                   OnResumeStartModifiers, AuraEffectId, AuraTargets,
                   EffectHosts, StartModifiers, TickModifiers,
                   StopModifiers, DoneModifiers, Groups
            FROM _Template_StatusEffect
        '''))
        actor_variables = {
            str(row[0]).casefold(): float(row[1] or 0.0)
            for row in database.execute('''
                SELECT Id, InitialValue FROM _Template_ActorVariables
            ''')
        }
        status_by_resource = {
            str(row["Id"]): row for row in status_rows
        }
        groups = {
            int(row["EffectId"]): str(row["ExclusiveGroup"] or "").strip()
            for row in status_rows
        }
        item_rows = tuple(database.execute('''
            SELECT Id, ItemCategory, StatusEffectId, StatusEffectDuration,
                   StatusEffectTickRate, StatusEffectValue0,
                   StatusEffectValue1, StatusEffectValue2,
                   StatusEffectValue3, StatusEffectValue4
            FROM _Template_Item
            WHERE ItemCategory IN ('Consumable', 'InstantConsumable')
              AND COALESCE(StatusEffectId, '') <> ''
        '''))

    definitions = {}
    for item in item_rows:
        effect_resource_id = str(item["StatusEffectId"] or "").strip()
        status = status_by_resource.get(effect_resource_id)
        if status is None:
            continue
        item_duration = max(0.0, float(item["StatusEffectDuration"] or 0.0))
        status_duration = max(
            0.0, float(status["StatusEffectDuration"] or 0.0)
        )
        duration_seconds = item_duration or status_duration
        values = tuple(
            float(item[f"StatusEffectValue{index}"] or 0.0)
            for index in range(5)
        )
        definition = ConsumableDefinition(
            item_template_id=str(item["Id"]),
            effect_resource_id=effect_resource_id,
            effect_id=int(status["EffectId"]),
            duration_ticks=max(
                1,
                round(duration_seconds * SERVER_TICKS_PER_SECOND),
            ),
            tick_interval_seconds=max(
                1.0 / SERVER_TICKS_PER_SECOND,
                float(status["StatusEffectTickRate"] or 1.0),
            ),
            values=values,
            max_stack_size=max(1, int(status["MaxStackSize"] or 1)),
            exclusive_group=str(status["ExclusiveGroup"] or "").strip(),
            exclusivity_type=str(
                status["ExclusivityType"] or "Replace"
            ).strip(),
            persistent=bool(status["Persistent"]),
            real_time=bool(status["StatusEffectRealTime"]),
            immediate_health=_health_modifiers(status["StartModifiers"]),
            periodic_health=_health_modifiers(status["TickModifiers"]),
        )
        item_tick_interval = max(
            0.0, float(item['StatusEffectTickRate'] or 0.0)
        )
        definition = replace(
            definition,
            tick_interval_seconds=max(
                1.0 / SERVER_TICKS_PER_SECOND,
                item_tick_interval or definition.tick_interval_seconds,
            ),
            item_category=str(item['ItemCategory'] or 'Consumable'),
            reset_run_time=bool(status['ResetRunTime']),
            keep_on_death=bool(status['KeepOnDeath']),
            keep_on_way_home=bool(status['KeepOnWayHome']),
            on_resume_start_modifiers=bool(
                status['OnResumeStartModifiers']
            ),
            start_modifiers=str(status['StartModifiers'] or ''),
            tick_modifiers=str(status['TickModifiers'] or ''),
            stop_modifiers=str(status['StopModifiers'] or ''),
            done_modifiers=str(status['DoneModifiers'] or ''),
            aura_effect_ids=tuple(
                value.strip()
                for value in str(status['AuraEffectId'] or '').split(';')
                if value.strip()
            ),
            aura_targets=str(status['AuraTargets'] or '').strip(),
            effect_hosts=str(status['EffectHosts'] or '').strip(),
            child_effects=_linked_status_effects(
                status,
                status_by_resource,
                actor_variables,
                trail=frozenset((effect_resource_id.casefold(),)),
                inherited_duration_seconds=duration_seconds,
                inherited_values=values,
            ),
        )
        definitions[definition.item_template_id] = definition
    reward_effects = []
    with sqlite3.connect(uri, uri=True) as database:
        references = database.execute(
            "SELECT DISTINCT RewardStatusEffect FROM _Template_DailyChallenges "
            "WHERE RewardStatusEffect<>''").fetchall()
    # Quest-only rewards have no consumable item row. Register their native
    # definitions too so expiry, offline clocks, and reconnect use the same
    # lifecycle metadata as item and daily-challenge effects.
    references = tuple(references) + tuple(
        (str(row['Id']),) for row in status_rows
        if 'questreward' in {token.strip().casefold()
                             for token in str(row['Groups'] or '').split(';')}
    )
    for (reference,) in references:
        root = dict(next(iter(status_by_resource.values())))
        root.update(StartModifiers='ActorStatusEffect:AddAE,' + reference + ';',
                    TickModifiers='', StopModifiers='', DoneModifiers='')
        reward_effects.extend(_linked_status_effects(
            root, status_by_resource, actor_variables, trail=frozenset()))
    by_effect = {}
    for effect in reward_effects:
        by_effect.setdefault(effect.effect_id, []).append(effect)
    for definition in definitions.values():
        pending: list[StatusEffectDefinition] = [definition]
        while pending:
            effect = pending.pop()
            by_effect.setdefault(effect.effect_id, []).append(effect)
            pending.extend(effect.child_effects)
    return ConsumableCatalog(
        definitions,
        groups,
        {
            effect_id: tuple(values)
            for effect_id, values in by_effect.items()
        },
    )


def load_consumable_catalog(path: str | Path) -> ConsumableCatalog:
    """Load and cache all usable consumables from one static database."""

    return _load_consumable_catalog_cached(*_database_signature(path))


def load_consumable_definition(
    path: str | Path,
    item_template_id: str,
) -> ConsumableDefinition | None:
    return load_consumable_catalog(path).consumable_definition(
        item_template_id
    )


def load_instant_consumable_definition(
    path: str | Path,
    item_template_id: str,
) -> ConsumableDefinition | None:
    '''Resolve an auto-picked InstantConsumable and its complete effect tree.'''

    return load_consumable_catalog(path).instant_definition(item_template_id)


__all__ = [
    'LinkedStatusEffectDefinition',
    'StatusEffectDefinition',
    'load_instant_consumable_definition',
    "ConsumableCatalog",
    "ConsumableDefinition",
    "HealthModifier",
    "load_consumable_catalog",
    "load_consumable_definition",
]
