"""Owner-hosted ``AuraTargets:Minion`` auras and minion-hosted effect programs.

Evidence: ``static.db4`` of release261_1024 (the client's ``db\\static.db4`` is
byte-identical) and ``language\\en\\gui.statuseffects.xml``
(``set_bd2026_dwarf_2`` .. ``_4``).

``set_blackdwarf2026_dwarf`` ("Lightning Power") is built from status effects
whose ``AuraTargets`` is ``Minion``.  Such an aura is hosted by the player and
gives its ``AuraEffectId`` children to that player's own minions whose monster
template is listed in ``AuraEffectAffectedTemplateId`` (an empty list admits
every minion) and which stand inside ``AuraRadius`` of the owner.  A child
whose ``EffectHosts`` is ``Causer`` is hosted by the aura's owner instead: it
is feedback that exists while at least one admitted minion is inside
(``SetLight2026_dwarf_buff_4_30_despawn_aura_trig_effect`` gates the 30-stack
rules on a living Tesla Turret).

Lifecycle rules, all read from the authored rows:

* A child is *held* while its aura admits the minion.  When the aura ends on
  its own (or the minion leaves it) a child with its own
  ``StatusEffectDuration`` runs that duration out and then completes
  (``DoneModifiers``); a child without one ends at once (``StopModifiers``).
  ``SetLight2026_dwarf_buff_4_20_aura_effect`` (no duration, 8 s aura) is the
  tooltip's "Attack Speed is increased by 60% for 8 seconds";
  ``skill_turret*_minion_despawn_effect`` (0.5 s, ``Done: Despawn``) is how a
  turret ends half a second after its despawn aura.
* An aura that disappears before its own end (unequip, death cleanup, a
  replaced instance without successor) stops every child it held: their Stop
  programs run and their pending delayed starts are dropped.
* ``ExclusivityType:Deny`` children are never restarted while running
  (``SetLight2026_dwarf_turret_aura_3_*_trig``: one 8 s overheat timer per
  turret however often the permanent aura is re-evaluated).
* ``ActorStatusEffect:addAE,<x>,...,ON:Immediate`` in a hosted effect's
  ``StartModifiers`` starts ``<x>`` on the same minion, ``DE`` seconds later
  when authored (``SetLight2026_*turret_overheat``, ``DE:8.0``).  A delayed
  start authored exactly at its parent's end still fires: only an early Stop
  cancels it.  When ``<x>`` is an aura (``SetLight2026_tesla2026_explo_add_dmg
  _aura``: ``AuraTargets:Allies`` limited to ``minion_teslaturret``) its
  children go to the owner's admitted minions around the host.
* ``ActorStatusEffect:addLE,<aura>,...,$N:<ActorVariable>`` places a location
  effect at the host after ``DE``.  ``$N`` names the *host's* actor variable
  (``tesla2026_explo_dmg``: initial -20, ``-2.5`` per
  ``SetLight2026_tesla2026_explo_add_dmg_count`` stack, limits -200..0).  The
  location's receivers carry ``CurrHealthPointsDmg`` in Stop/Done only, so the
  strike lands once, on the enemies inside when the location ends.
* ``Despawn`` in a Done/Stop program unregisters the minion.
* ``StatusEffectGroupDuration:<x>,relative|absolute,<group>,RunTimeUpdate`` on
  the owner scales a minion's lifetime-defining effects (``TurretDespawn``
  location aura, ``TurretVisual`` summon visualizer) and re-evaluates running
  minions whenever the bonus changes
  (``SetLight2026_dwarf_turretstime_double``: "All turrets' duration is
  doubled").

Everything here runs under ``LocalCombatState.lock``.  Publication, session
timers and owner feedback belong to ``services.minion_auras``.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from functools import lru_cache
import math
from pathlib import Path
import random
import time

from dso_server.gameplay.status_modifier_runtime import (
    parse_modifier_program, resolve_modifier_value,
)
from dso_server.persistence import readonly_db
from dso_server.protocol.gameplay_protocol import (
    StatusEffect, StatusEffectEntry, StatusEffectServerCommand,
)


SERVER_TICKS_PER_SECOND = 25
TICK_MASK = 0xFFFFFFFF
# Local choice: a deadline timer may fire a little late.  An aura that is gone
# within two server ticks of its recorded end ended on its own (its children
# linger); one that is gone earlier was removed (its children stop).
NATURAL_END_TOLERANCE_SECONDS = 2.0 / SERVER_TICKS_PER_SECOND
# Deadlines this close are due now; avoids a zero-length re-arm loop.
DUE_EPSILON_SECONDS = 0.005
# Bounded recursion for nested addAE programs (authored chains are <= 3 deep).
MAXIMUM_PROGRAM_DEPTH = 8
# The Ember Knight heat aura is already modelled by
# ``LocalCombatState._apply_summon_causer_effects`` (its child only acts on
# ``ON:maxstacksizeheat``).  Leave it to that implementation.
LEGACY_MINION_AURAS = frozenset({"mercenary_ember_knight_heatmax_aura"})
IMMEDIATE_TRIGGERS = frozenset({"", "immediate", "immediatemanaged"})
HOSTED_AURA_TARGETS = frozenset({"minion", "allies"})


@dataclass(frozen=True)
class MinionAuraDefinition:
    """One aura row whose children are given to admitted minions."""

    resource_id: str
    effect_id: int
    targets: str
    radius: float
    templates: frozenset[str]
    receivers: tuple
    causers: tuple
    tick_seconds: float = 1.0
    duration_ticks: int = 0

    def admits_template(self, template_id: str) -> bool:
        return not self.templates or str(template_id).casefold() in self.templates


@dataclass(frozen=True)
class LocationStart:
    """``addLE`` from a hosted effect; ``variables`` maps ``$N`` to a name."""

    definition: object
    variables: tuple[tuple[int, str], ...] = ()


@dataclass(frozen=True)
class MinionEffectProgram:
    """The parts of a hosted effect's programs this runtime executes."""

    effect_id: int
    resource_id: str
    max_stack_size: int
    starts: tuple
    locations: tuple[LocationStart, ...]
    variables: tuple[tuple[str, float, str], ...]
    end_widths: tuple[tuple[float, str, tuple[str, ...]], ...]
    group_durations: tuple[tuple[str, float, str, bool], ...]
    stop_despawn: bool
    done_despawn: bool
    stop_strikes: tuple[tuple[str, bool, str], ...]
    done_strikes: tuple[tuple[str, bool, str], ...]


@dataclass(frozen=True)
class OwnerMinionAura:
    """An aura currently hosted by a player; ``expires_at`` None is held."""

    key: str
    definition: MinionAuraDefinition
    expires_at: float | None = None


@dataclass
class HostedMinionEffect:
    definition: object
    entry: StatusEffectEntry
    started_at: float
    expires_at: float | None
    held_by: str | None
    stack_count: int = 1


@dataclass(frozen=True)
class _PendingStart:
    due_at: float
    owner_actor_id: int
    actor_id: int
    created_at: float
    parent: tuple[int, int]
    definition: object
    server_tick: int


@dataclass(frozen=True)
class MinionLocation:
    """A location effect started by a hosted program, for publication."""

    due_at: float
    owner_actor_id: int
    source_actor_id: int
    position: tuple[float, float, float]
    yaw: float
    definition: object
    receivers: tuple
    damage_range: tuple[float, float]
    server_tick: int
    cast_id: int


@dataclass(frozen=True)
class _PendingStrike:
    due_at: float
    owner_actor_id: int
    position: tuple[float, float, float]
    radius: float
    programs: tuple[tuple[float, str, bool], ...]
    damage_range: tuple[float, float]
    server_tick: int


@dataclass(frozen=True)
class MinionAuraUpdate:
    commands: tuple = ()
    feedback_add: tuple = ()
    feedback_remove: tuple = ()
    despawn: tuple[int, ...] = ()
    locations: tuple[MinionLocation, ...] = ()
    resolutions: tuple = ()
    lifetimes: tuple[tuple[int, float], ...] = ()
    next_due: float | None = None
    cadence: float | None = None


class MinionAuraState:
    """Map-scoped minion aura runtime, attached to one ``LocalCombatState``."""

    def __init__(self) -> None:
        self.owner_auras: dict[int, dict[str, OwnerMinionAura]] = {}
        self.effects: dict[int, dict[str, HostedMinionEffect]] = {}
        self.pending: list[_PendingStart] = []
        self.locations: list[MinionLocation] = []
        self.strikes: list[_PendingStrike] = []
        # owner -> effect id -> (definition, aura key, aura expiry)
        self.feedback: dict[int, dict[int, tuple]] = {}
        # Summons whose lifetime this runtime changed; their base is restored
        # when the bonus ends.
        self.adjusted_summons: set[int] = set()
        # location visual instance -> (duration origin time, origin tick)
        self.visual_origins: dict[int, tuple[float, int]] = {}
        self.next_cast_id = 0x5A000000


def minion_state(combat) -> MinionAuraState:
    state = getattr(combat, "minion_aura_state", None)
    if state is None:
        state = MinionAuraState()
        combat.minion_aura_state = state
    return state


# --------------------------------------------------------------------------
# Static content
# --------------------------------------------------------------------------

_REVISIONS: dict[str, tuple[float, tuple[str, int, int]]] = {}
# Content caches are keyed by the database revision.  The summon attack loop
# asks for it per attack, so a path's stat is reused for a few seconds rather
# than resolving and stat-ing the file on the battle thread every time.
REVISION_RECHECK_SECONDS = 5.0


def _revision(path) -> tuple[str, int, int]:
    key = str(path)
    now = time.monotonic()
    cached = _REVISIONS.get(key)
    if cached is not None and cached[0] > now:
        return cached[1]
    resolved = Path(path).resolve()
    stat = resolved.stat()
    revision = (str(resolved), stat.st_mtime_ns, stat.st_size)
    _REVISIONS[key] = (now + REVISION_RECHECK_SECONDS, revision)
    return revision


def _clean(value: str) -> str:
    return str(value or "").split("#", 1)[0].split("<", 1)[0].strip()


def _metadata(tokens) -> dict[str, str]:
    result = {}
    for token in tokens:
        name, separator, value = str(token).partition(":")
        if separator:
            result[name.strip().casefold()] = value.strip()
    return result


def _effect_name(path, effect_id: int) -> str:
    return _effect_names(*_revision(path)).get(int(effect_id), "")


@lru_cache(maxsize=4)
def _effect_names(path, _modified, _size):
    from dso_server.gameplay.status_runtime_catalog import load_status_runtime_catalog

    return load_status_runtime_catalog(path).effect_names


def load_minion_aura(path, resource_id: str) -> MinionAuraDefinition | None:
    """The aura row ``resource_id`` when it gives children to minions."""

    return _minion_aura(*_revision(path), str(resource_id))


def load_minion_aura_by_id(path, effect_id: int) -> MinionAuraDefinition | None:
    name = _effect_name(path, effect_id)
    return load_minion_aura(path, name) if name else None


@lru_cache(maxsize=2048)
def _minion_aura(path, _modified, _size, resource_id):
    from dso_server.gameplay.local_combat_state import LocalCombatState

    database = readonly_db.connect(path)
    row = database.execute(
        "SELECT rowid - 1, COALESCE(AuraTargets, ''), COALESCE(AuraRadius, 0), "
        "COALESCE(AuraEffectAffectedTemplateId, ''), COALESCE(AuraEffectId, ''), "
        "COALESCE(StatusEffectTickRate, 1), COALESCE(StatusEffectDuration, 0) "
        "FROM _Template_StatusEffect WHERE Id=? LIMIT 1",
        (resource_id,),
    ).fetchone()
    if row is None:
        return None
    targets = str(row[1]).strip().casefold()
    if targets not in HOSTED_AURA_TARGETS or not str(row[4]).strip():
        return None
    receivers, causers = [], []
    for child_id in str(row[4]).split(";"):
        child_id = child_id.strip()
        if not child_id:
            continue
        for child in LocalCombatState._load_victim_status_effects(
            database, child_id, default_trigger="immediate",
        ):
            if child.effect_hosts.casefold() == "causer":
                causers.append(child)
            else:
                receivers.append(child)
    return MinionAuraDefinition(
        resource_id=resource_id,
        effect_id=int(row[0]),
        targets=targets,
        radius=max(0.0, float(row[2] or 0.0)),
        templates=frozenset(
            value.strip().casefold()
            for value in str(row[3]).split(";") if value.strip()
        ),
        receivers=tuple(receivers),
        causers=tuple(causers),
        tick_seconds=max(0.04, float(row[5] or 1.0)),
        duration_ticks=max(0, round(float(row[6] or 0.0) * SERVER_TICKS_PER_SECOND)),
    )


def load_minion_program(path, resource_id: str) -> MinionEffectProgram | None:
    return _minion_program(*_revision(path), str(resource_id))


def load_minion_program_by_id(path, effect_id: int) -> MinionEffectProgram | None:
    name = _effect_name(path, effect_id)
    return load_minion_program(path, name) if name else None


def _has_despawn(program: str) -> bool:
    return any(item.kind == "despawn" for item in parse_modifier_program(program))


def _strikes(program: str) -> tuple[tuple[str, bool, str], ...]:
    from dso_server.gameplay.local_combat_state import DAMAGE_TYPE_IDS

    result = []
    for item in parse_modifier_program(program):
        if item.kind != "currhealthpointsdmg" or not item.arguments:
            continue
        options = tuple(_clean(value) for value in item.arguments[1:])
        damage_type = next(
            (value for value in options if value.casefold() in DAMAGE_TYPE_IDS),
            "Physical",
        )
        for value in options:
            suffix = value[len("DamageDone"):] if value.casefold().startswith("damagedone") else ""
            if suffix.casefold() in DAMAGE_TYPE_IDS:
                damage_type = suffix
        result.append((
            item.arguments[0],
            any(value.casefold() == "relative" for value in options),
            damage_type,
        ))
    return tuple(result)


def _variable_parameters(database, tokens) -> tuple[tuple[int, str], ...]:
    """``$N:<ActorVariable>`` pairs; literal parameters are not variables."""

    result = []
    for token in tokens:
        name, separator, raw_value = str(token).partition(":")
        if not separator or not name.startswith("$") or not name[1:].isdigit():
            continue
        reference = _clean(raw_value).lstrip("$")
        try:
            float(reference)
            continue
        except ValueError:
            pass
        row = database.execute(
            "SELECT Id FROM _Template_ActorVariables WHERE lower(Id)=? LIMIT 1",
            (reference.casefold(),),
        ).fetchone()
        if row is not None:
            result.append((int(name[1:]), str(row[0]).casefold()))
    return tuple(result)


@lru_cache(maxsize=4096)
def _minion_program(path, _modified, _size, resource_id):
    from dso_server.gameplay.local_combat_state import LocalCombatState

    database = readonly_db.connect(path)
    row = database.execute(
        "SELECT rowid - 1, COALESCE(StartModifiers, ''), "
        "COALESCE(StopModifiers, ''), COALESCE(DoneModifiers, ''), "
        "COALESCE(MaxStackSize, 1) "
        "FROM _Template_StatusEffect WHERE Id=? LIMIT 1",
        (resource_id,),
    ).fetchone()
    if row is None:
        return None
    starts, locations, variables, widths, groups = [], [], [], [], []
    for item in parse_modifier_program(row[1]):
        arguments = item.arguments
        if item.kind == "actorstatuseffect" and len(arguments) >= 2:
            trigger = _metadata(arguments[2:]).get("on", "immediate").casefold()
            if trigger not in IMMEDIATE_TRIGGERS:
                continue
            operation = arguments[0].casefold()
            encoded = ",".join(arguments[1:])
            loaded = LocalCombatState._load_victim_status_effects(
                database, encoded, default_trigger="immediate",
            )
            if operation == "addae":
                starts.extend(loaded)
            elif operation == "addle":
                parameters = _variable_parameters(database, arguments[2:])
                locations.extend(LocationStart(value, parameters) for value in loaded)
        elif item.kind == "actorvariable" and len(arguments) >= 3:
            try:
                amount = float(_clean(arguments[0]))
            except ValueError:
                continue
            variables.append((_clean(arguments[2]).casefold(), amount, arguments[1].casefold()))
        elif item.kind == "skillendwidth" and len(arguments) >= 3:
            try:
                amount = float(_clean(arguments[0]))
            except ValueError:
                continue
            widths.append((amount, arguments[1].casefold(), tuple(arguments[2:])))
        elif item.kind == "statuseffectgroupduration" and len(arguments) >= 3:
            try:
                amount = float(_clean(arguments[0]))
            except ValueError:
                continue
            groups.append((
                _clean(arguments[2]).casefold(), amount, arguments[1].casefold(),
                any(value.casefold() == "runtimeupdate" for value in arguments[3:]),
            ))
    return MinionEffectProgram(
        effect_id=int(row[0]),
        resource_id=resource_id,
        max_stack_size=max(1, int(row[4] or 1)),
        starts=tuple(starts),
        locations=tuple(locations),
        variables=tuple(variables),
        end_widths=tuple(widths),
        group_durations=tuple(groups),
        stop_despawn=_has_despawn(row[2]),
        done_despawn=_has_despawn(row[3]),
        stop_strikes=_strikes(row[2]),
        done_strikes=_strikes(row[3]),
    )


@lru_cache(maxsize=256)
def _variable_limits(path, _modified, _size, name):
    row = readonly_db.connect(path).execute(
        "SELECT COALESCE(InitialValue, 0), LowerLimit, UpperLimit "
        "FROM _Template_ActorVariables WHERE lower(Id)=? LIMIT 1",
        (str(name).casefold(),),
    ).fetchone()
    if row is None:
        return 0.0, None, None
    return (
        float(row[0] or 0.0),
        None if row[1] is None else float(row[1]),
        None if row[2] is None else float(row[2]),
    )


@lru_cache(maxsize=512)
def _location_receivers(path, _modified, _size, resource_id):
    from dso_server.gameplay.local_combat_state import LocalCombatState

    return LocalCombatState._load_location_aura_effects(
        readonly_db.connect(path), resource_id,
    )


# --------------------------------------------------------------------------
# Hot-path helpers used by the summon attack loop
# --------------------------------------------------------------------------

def minion_modifier_sources(combat, actor_id: int, *, now: float) -> tuple:
    """Wire entries of the effects a minion currently hosts.

    The summon attack loop adds them to the owner's modifier sources, so a
    hosted ``SkillDamageType``/``SkillDamage``/``BulletImpactCount``/
    ``SkillStatusEffect`` program changes that minion's own skill.
    """

    state = getattr(combat, "minion_aura_state", None)
    if state is None:
        return ()
    hosted = state.effects.get(int(actor_id))
    if not hosted:
        return ()
    return tuple(
        effect.entry for effect in hosted.values()
        if effect.expires_at is None or effect.expires_at > now
    )


@lru_cache(maxsize=512)
def _skill_variable_parameters(path, _modified, _size, skill_resource_id):
    """``$N:<ActorVariable>`` parameters of one skill's VictimStatusEffects."""

    database = readonly_db.connect(path)
    row = database.execute(
        "SELECT COALESCE(VictimStatusEffects, '') FROM _Template_Skill WHERE Id=? LIMIT 1",
        (skill_resource_id,),
    ).fetchone()
    result = {}
    for raw in str(row[0] if row else "").split(";"):
        tokens = [value.strip() for value in raw.split(",") if value.strip()]
        if not tokens:
            continue
        parameters = _variable_parameters(database, tokens[1:])
        if parameters:
            result[tokens[0]] = parameters
    return tuple(result.items())


def owner_actor_variable(combat, sources, name: str) -> float:
    """The owner's ActorVariable from its set modifiers and status programs.

    ``set_blackdwarf2026_*`` tier 2 authors ``ActorVariable:-0.02,absolute,
    charge_resis2026``; ``debuff_cc_charge_resis2026,...,$0:charge_resis2026``
    then carries ``Resistance:-0.02,relative,Lightning`` per Electrified stack
    ("Each stack of Electrified reduces enemy Lightning resistance by 2%").
    """

    path = combat.encounter.static_database_path
    key = str(name).casefold()
    value, lower, upper = _variable_limits(*_revision(path), key)
    for source in sources:
        if hasattr(source, "kind") and hasattr(source, "arguments"):
            arguments = tuple(str(item) for item in source.arguments)
            if str(source.kind).casefold() != "actorvariable" or len(arguments) < 3:
                continue
            if _clean(arguments[2]).casefold() != key:
                continue
            try:
                amount = float(_clean(arguments[0]))
            except ValueError:
                continue
            value = value * (1.0 + amount) if arguments[1].casefold() == "relative" else value + amount
            continue
        if not hasattr(source, "effect_id"):
            continue
        program = load_minion_program_by_id(path, int(source.effect_id))
        if program is None:
            continue
        stack = min(program.max_stack_size, max(1, int(getattr(source, "field_18", 0)) + 1))
        for variable, amount, operation in program.variables:
            if variable == key:
                scaled = amount * stack
                value = value * (1.0 + scaled) if operation == "relative" else value + scaled
    if lower is not None:
        value = max(lower, value)
    if upper is not None:
        value = min(upper, value)
    return value


def minion_geometry_overrides(combat, geometry, sources, owner_sources=()):
    """Apply hosted geometry kinds the shared resolver does not know.

    ``SkillEndWidth`` (``SetLight2026_machinegunturret_overheat``: +5 on the
    2.25 m strip, "Attack area becomes a cone") widens the stationary strip's
    far end that ``_stationary_directional_targets`` already honours.

    A victim effect whose authored parameter names an ActorVariable
    (``$0:charge_resis2026``) takes the owner's live value rather than the
    template's initial value the static parser stored.
    """

    if not sources:
        return geometry
    path = combat.encounter.static_database_path
    variables = dict(_skill_variable_parameters(*_revision(path), str(geometry.resource_id)))
    if variables and any(effect.resource_id in variables for effect in geometry.victim_status_effects):
        resolved = []
        for effect in geometry.victim_status_effects:
            parameters = variables.get(effect.resource_id)
            if parameters:
                values = list(effect.values)
                for index, name in parameters:
                    while len(values) <= index:
                        values.append(0.0)
                    values[index] = owner_actor_variable(
                        combat, tuple(owner_sources) + tuple(sources), name,
                    )
                effect = replace(effect, values=tuple(values))
            resolved.append(effect)
        geometry = replace(geometry, victim_status_effects=tuple(resolved))
    end_width = float(geometry.end_width)
    changed = False
    for source in sources:
        program = load_minion_program_by_id(path, int(source.effect_id))
        if program is None or not program.end_widths:
            continue
        stack = min(program.max_stack_size, max(1, int(getattr(source, "field_18", 0)) + 1))
        for amount, operation, scopes in program.end_widths:
            if not combat._skill_scope_matches(geometry, scopes):
                continue
            end_width = combat._apply_numeric_modifier(end_width, amount, operation, stack)
            changed = True
    if not changed:
        return geometry
    return replace(geometry, end_width=max(0.0, end_width))


def minion_penetrating_targets(combat, summon, direction, geometry, target_actor_ids):
    """Extra actors a straight bullet with ``impact_count > 1`` passes through.

    ``SetLight2026_combatturret_overheat`` adds ``BulletImpactCount:2`` to the
    Combat Turret's one-impact bullet ("Attacks penetrate through 3
    enemies").  The bullet keeps its aimed line to its authored range
    (``Velocity`` x ``LifeTime``); actors are struck in travel order.
    """

    count = int(getattr(geometry, "impact_count", 1))
    if count <= 1 or not target_actor_ids:
        return tuple(target_actor_ids)
    reach = max(
        float(geometry.projectile_range), float(geometry.attack_range), 1.0,
    )
    width = 2.0 * max(0.05, float(geometry.projectile_radius))
    ray = replace(
        geometry, targeting_type="", angle_degrees=0.0,
        attack_range=reach, hit_range=reach,
        player_width=width, end_width=width,
    )
    ordered = [
        monster.actor_id
        for monster in combat._stationary_directional_targets(
            summon.position, direction, ray,
        )
    ]
    primary = int(target_actor_ids[0])
    if primary not in ordered:
        ordered.insert(0, primary)
    return tuple(ordered[:count])


# --------------------------------------------------------------------------
# Cleanup hooks called by LocalCombatState
# --------------------------------------------------------------------------

def forget_minion(combat, actor_id: int) -> None:
    """A minion left the map: drop what it hosted and its pending starts.

    Location effects and strikes it already started are independent of it
    (the Tesla explosion outlives the exploding turret).
    """

    state = getattr(combat, "minion_aura_state", None)
    if state is None:
        return
    actor_id = int(actor_id)
    state.effects.pop(actor_id, None)
    state.adjusted_summons.discard(actor_id)
    if state.pending:
        state.pending = [item for item in state.pending if item.actor_id != actor_id]


def forget_minion_owner(combat, owner_actor_id: int) -> None:
    """The owner left the map: its auras and queued programs go with it."""

    state = getattr(combat, "minion_aura_state", None)
    if state is None:
        return
    owner = int(owner_actor_id)
    state.owner_auras.pop(owner, None)
    state.feedback.pop(owner, None)
    state.pending = [item for item in state.pending if item.owner_actor_id != owner]
    state.locations = [item for item in state.locations if item.owner_actor_id != owner]
    state.strikes = [item for item in state.strikes if item.owner_actor_id != owner]


def owner_has_minion_state(combat, owner_actor_id: int) -> bool:
    state = getattr(combat, "minion_aura_state", None)
    if state is None:
        return False
    owner = int(owner_actor_id)
    return bool(
        state.owner_auras.get(owner)
        or state.feedback.get(owner)
        or any(item.owner_actor_id == owner for item in state.pending)
        or any(item.owner_actor_id == owner for item in state.locations)
        or any(item.owner_actor_id == owner for item in state.strikes)
        or any(
            summon.owner_actor_id == owner and actor_id in state.effects
            for actor_id, summon in combat.player_summons.items()
        )
    )


def minion_actor_variable(combat, actor_id: int, name: str, *, now: float) -> float:
    """A minion's ActorVariable: initial value plus its hosted programs."""

    path = combat.encounter.static_database_path
    key = str(name).casefold()
    value, lower, upper = _variable_limits(*_revision(path), key)
    state = getattr(combat, "minion_aura_state", None)
    for hosted in (state.effects.get(int(actor_id), {}) if state else {}).values():
        if hosted.expires_at is not None and hosted.expires_at <= now:
            continue
        program = load_minion_program(path, hosted.definition.resource_id)
        if program is None:
            continue
        for variable, amount, operation in program.variables:
            if variable != key:
                continue
            scaled = amount * max(1, int(hosted.stack_count))
            value = value * (1.0 + scaled) if operation == "relative" else value + scaled
    if lower is not None:
        value = max(lower, value)
    if upper is not None:
        value = min(upper, value)
    return value


# --------------------------------------------------------------------------
# The owner pass
# --------------------------------------------------------------------------

def advance_owner_minion_auras(
        combat, owner_actor_id: int, auras, *, owner_position=None,
        group_bonus=None, now: float, server_tick: int) -> MinionAuraUpdate:
    """Reconcile one owner's minion auras, programs and lifetimes at ``now``.

    The caller holds ``combat.lock`` and publishes the returned update.
    """

    return _OwnerPass(
        combat, int(owner_actor_id), tuple(auras),
        owner_position=owner_position, group_bonus=dict(group_bonus or {}),
        now=float(now), server_tick=int(server_tick) & TICK_MASK,
    ).run()


class _OwnerPass:
    def __init__(self, combat, owner, auras, *, owner_position, group_bonus,
                 now, server_tick):
        self.combat = combat
        self.state = minion_state(combat)
        self.owner = owner
        self.auras = auras
        self.owner_position = owner_position
        self.group_bonus = group_bonus
        self.now = now
        self.tick = server_tick
        self.path = combat.encounter.static_database_path
        self.adds: dict[int, list] = {}
        self.removals: dict[int, list] = {}
        self.feedback_add = []
        self.feedback_remove = []
        self.despawn: list[int] = []
        self.locations: list[MinionLocation] = []
        self.resolutions = []
        self.lifetimes: list[tuple[int, float]] = []
        self.current: dict[str, OwnerMinionAura] = {}
        self.depth = 0

    # -- helpers -----------------------------------------------------------

    def summons(self):
        health = getattr(self.combat, "player_summon_health", {})
        return tuple(
            summon for actor_id, summon in sorted(self.combat.player_summons.items())
            if summon.owner_actor_id == self.owner
            and health.get(actor_id, [1])[0] > 0
        )

    def alive(self, actor_id: int):
        summon = self.combat.player_summons.get(int(actor_id))
        if summon is None or summon.owner_actor_id != self.owner:
            return None
        if getattr(self.combat, "player_summon_health", {}).get(int(actor_id), [1])[0] <= 0:
            return None
        return summon

    def admitted(self, aura: MinionAuraDefinition, summon) -> bool:
        if not aura.admits_template(summon.definition.monster_template_id):
            return False
        if self.owner_position is None or aura.radius <= 0.0:
            return True
        return math.hypot(
            float(summon.position[0]) - float(self.owner_position[0]),
            float(summon.position[2]) - float(self.owner_position[2]),
        ) <= aura.radius

    def record(self, actor_id: int, hosted: HostedMinionEffect) -> None:
        active = self.combat.monster_status_effects.setdefault(int(actor_id), {})
        active[hosted.definition.resource_id] = (hosted.definition, hosted.expires_at)

    def unrecord(self, actor_id: int, resource_id: str) -> None:
        active = self.combat.monster_status_effects.get(int(actor_id))
        if active is not None:
            active.pop(resource_id, None)
            if not active:
                self.combat.monster_status_effects.pop(int(actor_id), None)

    @staticmethod
    def removal(entry: StatusEffectEntry, tick: int) -> StatusEffectEntry:
        return replace(
            entry, field_0c=tick, field_14=SERVER_TICKS_PER_SECOND,
            field_08=tick, field_10=0, field_25=False, field_26=False,
            field_39=False, has_details=False, values=(), field_38=False,
            field_3c=0xFF, shape_values=(),
        )

    # -- application ---------------------------------------------------------

    def apply(self, actor_id, definition, *, held_by=None):
        """Start or advance ``definition`` on one admitted minion."""

        summon = self.alive(actor_id)
        if summon is None or self.depth >= MAXIMUM_PROGRAM_DEPTH:
            return
        if definition.chance < 1.0 and random.random() >= definition.chance:
            return
        hosted_map = self.state.effects.setdefault(summon.actor_id, {})
        existing = hosted_map.get(definition.resource_id)
        if (existing is not None and existing.expires_at is not None
                and existing.expires_at <= self.now):
            self.complete(summon.actor_id, existing)
            existing = None
        if existing is not None and definition.exclusivity_type.casefold() == "deny":
            if held_by is not None and existing.held_by is None:
                existing.held_by = held_by
                existing.expires_at = None
                self.record(summon.actor_id, existing)
            return
        if held_by is None and existing is not None:
            # An explicit start (a delayed overheat) on an effect an aura is
            # holding refreshes it; the aura keeps holding it.
            held_by = existing.held_by
        program = load_minion_program(self.path, definition.resource_id)
        maximum = program.max_stack_size if program is not None else max(1, definition.max_stack_size)
        stack = min(maximum, existing.stack_count + 1) if existing is not None else 1
        duration = max(0, int(definition.duration_ticks))
        end_tick = (self.tick + duration) & TICK_MASK if duration else 0
        if existing is not None and stack - 1 == int(existing.entry.field_18):
            # At its cap (or MaxStackSize 1) a re-application refreshes the
            # running instance: the native no-details field_25 branch.
            refreshed = replace(
                existing.entry, field_08=self.tick, field_0c=end_tick,
                field_10=duration, field_25=True, has_details=False, values=(),
            )
            self.adds.setdefault(summon.actor_id, []).append(refreshed)
            entry = replace(
                refreshed, field_25=False, has_details=True,
                values=tuple(definition.values),
            )
        else:
            entry = StatusEffectEntry(
                effect_id=int(definition.effect_id),
                field_04=self.combat._allocate_player_summon_aura_instance_id(),
                field_0c=end_tick,
                field_14=SERVER_TICKS_PER_SECOND if duration else 0,
                field_08=self.tick,
                field_18=stack - 1,
                field_10=duration,
                field_1c=100,
                field_20=self.owner,
                field_25=False,
                field_26=stack > 1,
                # End tick zero with field_39 is native permanence: a child
                # without its own duration lives exactly as long as its hold.
                field_39=not duration,
                has_details=True,
                values=tuple(definition.values),
            )
            self.adds.setdefault(summon.actor_id, []).append(entry)
            if existing is not None:
                self.removals.setdefault(summon.actor_id, []).append(
                    self.removal(existing.entry, self.tick)
                )
        hosted = HostedMinionEffect(
            definition=replace(definition, stack_count=stack),
            entry=entry,
            started_at=self.now,
            expires_at=(
                None if held_by is not None or not duration
                else self.now + duration / SERVER_TICKS_PER_SECOND
            ),
            held_by=held_by,
            stack_count=stack,
        )
        hosted_map[definition.resource_id] = hosted
        self.record(summon.actor_id, hosted)
        if program is not None:
            self.depth += 1
            try:
                self.start_program(summon, hosted, program)
            finally:
                self.depth -= 1

    def start_program(self, summon, hosted, program):
        parent = (summon.actor_id, int(hosted.entry.field_04))
        for start in program.starts:
            if start.delay_ticks > 0:
                self.state.pending.append(_PendingStart(
                    due_at=self.now + start.delay_ticks / SERVER_TICKS_PER_SECOND,
                    owner_actor_id=self.owner,
                    actor_id=summon.actor_id,
                    created_at=float(summon.created_at),
                    parent=parent,
                    definition=replace(start, delay_ticks=0),
                    server_tick=(self.tick + start.delay_ticks) & TICK_MASK,
                ))
                continue
            self.start_nested(summon, start)
        for location in program.locations:
            self.start_location(summon, location)

    def start_nested(self, summon, definition):
        aura = load_minion_aura(self.path, definition.resource_id)
        if aura is None or aura.radius <= 0.0 or not aura.receivers:
            self.apply(summon.actor_id, definition)
            return
        # An aura started on a minion (``AuraTargets:Allies``/``Minion``
        # limited by template) gives its children to the owner's admitted
        # minions around that host.
        for other in self.summons():
            if other.actor_id == summon.actor_id or not aura.admits_template(
                    other.definition.monster_template_id):
                continue
            if math.hypot(
                float(other.position[0]) - float(summon.position[0]),
                float(other.position[2]) - float(summon.position[2]),
            ) > aura.radius:
                continue
            for child in aura.receivers:
                self.apply(other.actor_id, replace(
                    child, duration_ticks=child.duration_ticks or definition.duration_ticks,
                ))

    def start_location(self, summon, location: LocationStart):
        definition = location.definition
        if definition.chance < 1.0 and random.random() >= definition.chance:
            return
        values = list(definition.values)
        for index, variable in location.variables:
            while len(values) <= index:
                values.append(0.0)
            values[index] = minion_actor_variable(
                self.combat, summon.actor_id, variable, now=self.now,
            )
        definition = replace(definition, values=tuple(values))
        receivers = tuple(
            replace(child, values=definition.values)
            for child in _location_receivers(*_revision(self.path), definition.resource_id)
        )
        self.state.next_cast_id = (self.state.next_cast_id + 1) & TICK_MASK
        self.state.locations.append(MinionLocation(
            due_at=self.now + definition.delay_ticks / SERVER_TICKS_PER_SECOND,
            owner_actor_id=self.owner,
            source_actor_id=summon.actor_id,
            position=tuple(float(value) for value in summon.position[:3]),
            yaw=float(summon.yaw),
            definition=replace(definition, delay_ticks=0),
            receivers=receivers,
            damage_range=tuple(summon.player_damage_range),
            server_tick=(self.tick + definition.delay_ticks) & TICK_MASK,
            cast_id=self.state.next_cast_id,
        ))

    def stop(self, actor_id, hosted):
        """End a hosted effect early: Stop program, wire removal, no DE."""

        actor_id = int(actor_id)
        hosted_map = self.state.effects.get(actor_id, {})
        if hosted_map.get(hosted.definition.resource_id) is hosted:
            hosted_map.pop(hosted.definition.resource_id, None)
        self.unrecord(actor_id, hosted.definition.resource_id)
        self.removals.setdefault(actor_id, []).append(
            self.removal(hosted.entry, self.tick)
        )
        parent = (actor_id, int(hosted.entry.field_04))
        self.state.pending = [item for item in self.state.pending if item.parent != parent]
        program = load_minion_program(self.path, hosted.definition.resource_id)
        if program is not None and program.stop_despawn and actor_id not in self.despawn:
            self.despawn.append(actor_id)

    def complete(self, actor_id, hosted):
        """A hosted effect ran its duration out: Done program."""

        actor_id = int(actor_id)
        hosted_map = self.state.effects.get(actor_id, {})
        if hosted_map.get(hosted.definition.resource_id) is hosted:
            hosted_map.pop(hosted.definition.resource_id, None)
        self.unrecord(actor_id, hosted.definition.resource_id)
        program = load_minion_program(self.path, hosted.definition.resource_id)
        if program is not None and program.done_despawn and actor_id not in self.despawn:
            self.despawn.append(actor_id)

    # -- passes --------------------------------------------------------------

    def run(self) -> MinionAuraUpdate:
        state = self.state
        previous = state.owner_auras.get(self.owner, {})
        self.current = {
            aura.key: aura for aura in self.auras
            if aura.definition.resource_id not in LEGACY_MINION_AURAS
            and (aura.expires_at is None or aura.expires_at > self.now)
        }
        # key -> (ended on its own, when it ended).  A child left behind by a
        # natural end lingers from that instant, even when the pass that
        # notices it runs a little later.
        ended = {}
        for key, aura in previous.items():
            if key in self.current:
                continue
            natural = (
                aura.expires_at is not None
                and self.now + NATURAL_END_TOLERANCE_SECONDS >= aura.expires_at
            )
            ended[key] = (natural, min(self.now, aura.expires_at) if natural else self.now)
        if self.current:
            state.owner_auras[self.owner] = dict(self.current)
        else:
            state.owner_auras.pop(self.owner, None)
        summons = self.summons()
        owned = {summon.actor_id for summon in summons}

        self.run_pending()

        # (minion, child resource) -> (child, the aura that holds it)
        desired: dict[tuple[int, str], tuple] = {}
        feedback: dict[int, tuple] = {}
        cadence = None
        for aura in sorted(self.current.values(), key=lambda value: value.key):
            admitted = [summon for summon in summons if self.admitted(aura.definition, summon)]
            if not admitted:
                continue
            cadence = min(cadence or aura.definition.tick_seconds, aura.definition.tick_seconds)
            for summon in admitted:
                for child in aura.definition.receivers:
                    desired[(summon.actor_id, child.resource_id)] = (child, aura)
            for child in aura.definition.causers:
                feedback[int(child.effect_id)] = (child, aura)
        for (actor_id, _resource), (child, aura) in desired.items():
            self.hold(actor_id, child, aura)
        self.release(desired, ended, owned)
        for actor_id in sorted(owned):
            for hosted in tuple(state.effects.get(actor_id, {}).values()):
                if (hosted.expires_at is not None
                        and hosted.expires_at <= self.now + DUE_EPSILON_SECONDS):
                    self.complete(actor_id, hosted)
        self.reconcile_feedback(feedback)
        self.reconcile_lifetimes(summons)
        self.run_locations()
        self.run_strikes()
        return self.update(owned, cadence)

    def hold(self, actor_id, child, aura):
        hosted = self.state.effects.get(actor_id, {}).get(child.resource_id)
        active = hosted is not None and (
            hosted.expires_at is None or hosted.expires_at > self.now
        )
        if active and hosted.held_by is not None:
            if hosted.held_by != aura.key and hosted.held_by not in self.current:
                # A successor instance of the same aura keeps holding it.
                hosted.held_by = aura.key
            return
        if active and child.exclusivity_type.casefold() == "deny":
            hosted.held_by = aura.key
            hosted.expires_at = None
            self.record(actor_id, hosted)
            return
        self.apply(actor_id, child, held_by=aura.key)

    def release(self, desired, ended, owned):
        for actor_id in sorted(owned):
            for hosted in tuple(self.state.effects.get(actor_id, {}).values()):
                if hosted.held_by is None:
                    continue
                if (actor_id, hosted.definition.resource_id) in desired:
                    continue
                key = hosted.held_by
                natural, released_at = (
                    (True, self.now) if key in self.current
                    else ended.get(key, (True, self.now))
                )
                duration = int(hosted.definition.duration_ticks)
                if natural and duration > 0:
                    hosted.held_by = None
                    hosted.expires_at = released_at + duration / SERVER_TICKS_PER_SECOND
                    self.record(actor_id, hosted)
                else:
                    self.stop(actor_id, hosted)

    def reconcile_feedback(self, desired):
        previous = self.state.feedback.get(self.owner, {})
        result = {}
        for effect_id, (child, aura) in desired.items():
            if child.duration_ticks > 0:
                duration = int(child.duration_ticks)
            elif aura.expires_at is not None:
                duration = max(1, round((aura.expires_at - self.now) * SERVER_TICKS_PER_SECOND))
            else:
                # Local choice: feedback of a permanent aura has no finite
                # lease to send; no authored row needs it.
                continue
            old = previous.get(effect_id)
            if (
                old is None or old[1] != aura.key
                or (aura.expires_at is not None and old[2] is not None
                    and abs(float(aura.expires_at) - float(old[2])) > 0.05)
            ):
                self.feedback_add.append(replace(child, duration_ticks=duration))
            result[effect_id] = (child, aura.key, aura.expires_at)
        for effect_id, (child, _key, expires_at) in previous.items():
            if effect_id in result:
                continue
            if expires_at is None or float(expires_at) > self.now + NATURAL_END_TOLERANCE_SECONDS:
                self.feedback_remove.append(child)
        if result:
            self.state.feedback[self.owner] = result
        else:
            self.state.feedback.pop(self.owner, None)

    def run_pending(self):
        due = sorted(
            (item for item in self.state.pending
             if item.owner_actor_id == self.owner
             and item.due_at <= self.now + DUE_EPSILON_SECONDS),
            key=lambda item: (item.due_at, item.actor_id),
        )
        if not due:
            return
        self.state.pending = [item for item in self.state.pending if item not in due]
        for item in due:
            summon = self.alive(item.actor_id)
            # The summon identity guards against a recycled actor id.
            if summon is None or abs(float(summon.created_at) - item.created_at) > 1e-6:
                continue
            tick = self.tick
            self.tick = item.server_tick
            try:
                self.start_nested(summon, item.definition)
            finally:
                self.tick = tick

    def run_locations(self):
        due = [item for item in self.state.locations
               if item.owner_actor_id == self.owner
               and item.due_at <= self.now + DUE_EPSILON_SECONDS]
        if not due:
            return
        self.state.locations = [item for item in self.state.locations if item not in due]
        for item in due:
            self.locations.append(item)
            programs = []
            for receiver in item.receivers:
                program = load_minion_program(self.path, receiver.resource_id)
                if program is None:
                    continue
                # Stop and Done carry the same strike; whichever ends the
                # receiver deals it once.
                for raw, relative, damage_type in (program.done_strikes or program.stop_strikes):
                    amount = resolve_modifier_value(raw, receiver.values)
                    if amount:
                        programs.append((abs(float(amount)), damage_type, bool(relative)))
            radius = float(item.definition.aura_radius)
            if programs and radius > 0.0:
                self.state.strikes.append(_PendingStrike(
                    due_at=self.now + item.definition.duration_ticks / SERVER_TICKS_PER_SECOND,
                    owner_actor_id=self.owner,
                    position=item.position,
                    radius=radius,
                    programs=tuple(programs),
                    damage_range=item.damage_range,
                    server_tick=(item.server_tick + item.definition.duration_ticks) & TICK_MASK,
                ))

    def run_strikes(self):
        due = [item for item in self.state.strikes
               if item.owner_actor_id == self.owner
               and item.due_at <= self.now + DUE_EPSILON_SECONDS]
        if not due:
            return
        from dso_server.gameplay.local_combat_state import (
            DamageResolution, _monster_health_after_damage, damage_type_ids,
        )

        self.state.strikes = [item for item in self.state.strikes if item not in due]
        combat = self.combat
        for strike in due:
            average = max(0.0, sum(strike.damage_range) * 0.5)
            for actor_id in tuple(combat.living_actor_ids):
                monster = combat.monsters.get(actor_id)
                position = combat.monster_positions.get(actor_id)
                if monster is None or position is None:
                    continue
                if actor_id in getattr(combat, "returning_home_actor_ids", ()):
                    continue
                if combat._distance(strike.position, position) > (
                        strike.radius + max(0.0, monster.collision_radius)):
                    continue
                previous = combat.monster_health.get(actor_id, 0)
                if previous <= 0:
                    continue
                total = 0.0
                tags = []
                for coefficient, damage_type, relative in strike.programs:
                    raw = average * coefficient if relative else coefficient
                    total += raw * combat.monster_incoming_damage_multiplier(
                        actor_id, damage_type, now=self.now,
                    )
                    tags.extend(value for value in damage_type_ids(damage_type) if value not in tags)
                damage = max(0, int(math.ceil(total)))
                if damage <= 0:
                    continue
                current, killed = _monster_health_after_damage(monster, previous, damage)
                combat.monster_health[actor_id] = current
                self.resolutions.append(DamageResolution(
                    target_actor_id=int(actor_id),
                    attacker_actor_id=self.owner,
                    current_health=int(current),
                    maximum_health=int(monster.maximum_health),
                    damage=int(previous - current),
                    killed=bool(killed),
                    position=position,
                    server_tick=int(strike.server_tick),
                    hit_tags=tuple(tags),
                    skill_index=0,
                    source_actor_id=self.owner,
                ))

    def reconcile_lifetimes(self, summons):
        """Apply live ``StatusEffectGroupDuration`` to minion lifetimes."""

        bonus = self.group_bonus
        state = self.state
        combat = self.combat
        for summon in summons:
            definition = summon.definition
            if summon.expires_at is None:
                continue
            effects = list(definition.location_visual_effects)
            geometry = None
            if getattr(definition, "skill_index", None) is not None:
                try:
                    geometry = combat._load_player_skill_geometry(int(definition.skill_index))
                except Exception:
                    geometry = None
            if geometry is not None:
                effects.extend(
                    effect for effect in geometry.user_status_effects
                    if "summon_visualizer" in effect.resource_id.casefold()
                )
            scaled = []
            matched = False
            for effect in effects:
                relative, absolute = self.bonus_for(effect.effect_id)
                if relative or absolute:
                    matched = True
                scaled.append(
                    effect.duration_ticks / SERVER_TICKS_PER_SECOND * (1.0 + relative) + absolute
                )
            if not matched and summon.actor_id not in state.adjusted_summons:
                continue
            lifetime = max(scaled) if matched and scaled else float(definition.lifetime_seconds)
            expires_at = float(summon.created_at) + max(0.0, lifetime)
            if matched:
                state.adjusted_summons.add(summon.actor_id)
            else:
                state.adjusted_summons.discard(summon.actor_id)
            if abs(expires_at - float(summon.expires_at)) > 1e-3:
                combat.player_summons[summon.actor_id] = replace(summon, expires_at=expires_at)
                self.lifetimes.append((summon.actor_id, expires_at))
            self.reconcile_visuals(summon, definition)
        live = set(combat.player_location_visuals)
        for instance_id in tuple(state.visual_origins):
            if instance_id not in live:
                state.visual_origins.pop(instance_id, None)

    def bonus_for(self, effect_id) -> tuple[float, float]:
        if not self.group_bonus:
            return 0.0, 0.0
        relative = absolute = 0.0
        for group in self.combat._status_groups_for_effect(int(effect_id)):
            amounts = self.group_bonus.get(group)
            if amounts:
                relative += amounts[0]
                absolute += amounts[1]
        return relative, absolute

    def reconcile_visuals(self, summon, definition):
        """Keep the summon's despawn-aura visual alive for its new lifetime.

        Discarding that location effect is what runs the turret's teardown on
        the client, so the server-side expiry must follow the lifetime.
        """

        resources = {effect.resource_id for effect in definition.location_visual_effects}
        if not resources:
            return
        combat = self.combat
        state = self.state
        for instance_id, visual in tuple(combat.player_location_visuals.items()):
            if (
                int(visual.owner_actor_id) != self.owner
                or int(getattr(visual, "cast_id", 0)) != int(summon.cast_id)
                or visual.definition.resource_id not in resources
            ):
                continue
            base_ticks = int(visual.definition.duration_ticks)
            origin = state.visual_origins.get(instance_id)
            if origin is None:
                origin = (
                    float(visual.expires_at) - base_ticks / SERVER_TICKS_PER_SECOND,
                    int(visual.starts_tick),
                )
                state.visual_origins[instance_id] = origin
            relative, absolute = self.bonus_for(visual.definition.effect_id)
            ticks = max(0, round(base_ticks * (1.0 + relative) + absolute * SERVER_TICKS_PER_SECOND))
            expires_at = origin[0] + ticks / SERVER_TICKS_PER_SECOND
            if abs(expires_at - float(visual.expires_at)) <= 1e-3:
                continue
            combat.player_location_visuals[instance_id] = replace(
                visual, expires_at=expires_at,
                expires_tick=(origin[1] + ticks) & TICK_MASK,
            )

    def update(self, owned, cadence) -> MinionAuraUpdate:
        commands = []
        for actor_id in sorted(set(self.adds) | set(self.removals)):
            entries = tuple(self.adds.get(actor_id, ()))
            if entries:
                commands.append(StatusEffectServerCommand(StatusEffect(False, entries), actor_id))
            removed = tuple(self.removals.get(actor_id, ()))
            if removed:
                commands.append(StatusEffectServerCommand(StatusEffect(False, removed), actor_id))
        deadlines = []
        state = self.state
        deadlines.extend(item.due_at for item in state.pending if item.owner_actor_id == self.owner)
        deadlines.extend(item.due_at for item in state.locations if item.owner_actor_id == self.owner)
        deadlines.extend(item.due_at for item in state.strikes if item.owner_actor_id == self.owner)
        for actor_id in owned:
            deadlines.extend(
                hosted.expires_at for hosted in state.effects.get(actor_id, {}).values()
                if hosted.expires_at is not None
            )
        deadlines.extend(
            aura.expires_at for aura in self.current.values() if aura.expires_at is not None
        )
        for _child, _key, expires_at in state.feedback.get(self.owner, {}).values():
            if expires_at is not None:
                deadlines.append(float(expires_at))
        return MinionAuraUpdate(
            commands=tuple(commands),
            feedback_add=tuple(self.feedback_add),
            feedback_remove=tuple(self.feedback_remove),
            despawn=tuple(self.despawn),
            locations=tuple(self.locations),
            resolutions=tuple(self.resolutions),
            lifetimes=tuple(self.lifetimes),
            next_due=min(deadlines) if deadlines else None,
            cadence=cadence,
        )


__all__ = [
    "LEGACY_MINION_AURAS",
    "MinionAuraDefinition",
    "MinionAuraState",
    "MinionAuraUpdate",
    "MinionEffectProgram",
    "MinionLocation",
    "OwnerMinionAura",
    "advance_owner_minion_auras",
    "forget_minion",
    "forget_minion_owner",
    "load_minion_aura",
    "load_minion_aura_by_id",
    "load_minion_program",
    "load_minion_program_by_id",
    "minion_actor_variable",
    "minion_geometry_overrides",
    "minion_modifier_sources",
    "minion_penetrating_targets",
    "minion_state",
    "owner_has_minion_state",
]
