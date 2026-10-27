"""Publish owner-hosted minion auras and their timed programs.

``gameplay.minion_auras`` owns the map-scoped rules; this service gathers the
owner's live aura sources, runs one owner pass under the map lock, and then
publishes the result outside every lock:

* hosted effects on minions go through the same ``player_summon_aura_refresh``
  presence event as the constructions' own aura leases, so every viewer that
  sees the turret sees its Lightning conversion, its overheat and its 20-stack
  attack-speed buff, with the owner id remapped per viewer;
* ``Causer`` children are applied to (and ended on) the owner as ordinary
  runtime status effects, which ``active_item_set_evaluation`` already reads
  for modAE gates;
* despawns use ``_expire_player_summon``, location effects use
  ``_publish_player_location_visuals``, strikes use the player-owned
  resolution dispatch, and lifetime changes reschedule the summon's own
  ``player-summon-expire`` deadline.

Only auras that come from item-set programs are hosted here: the set's
``ImmediateManaged`` effects, its accepted event programs, and statuses named
by its ``SkillStatusEffect`` rules (Tesla Turret's
``SetLight2026_dwarf_buff_4_30_despawn_aura_trig``, Defiance's
``skill_defiance_ham_despawn_aura``).  Other systems keep their existing
handling until they register with the same owner pass.
"""

from __future__ import annotations

from dataclasses import replace
import time

from dso_server.gameplay import minion_auras as rules
from dso_server.gameplay.status_runtime_catalog import load_status_runtime_catalog
from dso_server.paths import DEFAULT_STATIC_DB_PATH
from dso_server.services.base import CharacterService


SYNC_TIMER = "minion-aura-sync"
OWNER_GROUP_ORIGINS = "gameplay_minion_group_origins"
MAXIMUM_SYNC_ROUNDS = 4
SERVER_TICKS_PER_SECOND = rules.SERVER_TICKS_PER_SECOND


def _database_path(s):
    return getattr(getattr(s.gameplay_combat_state, "encounter", None),
                   "static_database_path", DEFAULT_STATIC_DB_PATH)


def _named_item_set_resources(evaluation) -> frozenset[str]:
    names = set()
    for rule in getattr(evaluation, "skill_effect_rules", ()) or ():
        arguments = tuple(getattr(rule, "arguments", ()))
        if len(arguments) >= 3:
            names.add(str(arguments[2]).strip().casefold())
    for rule in getattr(evaluation, "actor_effect_rules", ()) or ():
        resource = str(getattr(rule, "actor_effect_id", "") or "").strip()
        if resource:
            names.add(resource.casefold())
    return frozenset(names)


def owner_minion_auras(path, item_set_entries, runtime, tracked, evaluation):
    """The owner's item-set Minion auras as ``OwnerMinionAura`` values."""

    names = load_status_runtime_catalog(path).effect_names
    named = _named_item_set_resources(evaluation)
    candidates = [(entry, None) for entry in item_set_entries]
    for instance_id, (entry, expires_at) in runtime:
        resource = names.get(int(entry.effect_id), "")
        if instance_id in tracked or resource.casefold() in named:
            candidates.append((entry, expires_at))
    result = []
    for entry, expires_at in candidates:
        definition = rules.load_minion_aura_by_id(path, int(entry.effect_id))
        if definition is None or definition.targets != "minion":
            continue
        if definition.resource_id in rules.LEGACY_MINION_AURAS:
            continue
        result.append(rules.OwnerMinionAura(
            key=f"{int(entry.field_04)}:{int(entry.effect_id)}",
            definition=definition,
            expires_at=None if expires_at is None else float(expires_at),
        ))
    return tuple(result)


def owner_group_bonus(path, entries) -> dict[str, tuple[float, float]]:
    """Live ``StatusEffectGroupDuration ... RunTimeUpdate`` on the owner."""

    bonus: dict[str, list[float]] = {}
    for entry in entries:
        program = rules.load_minion_program_by_id(path, int(entry.effect_id))
        if program is None or not program.group_durations:
            continue
        stack = min(program.max_stack_size, max(1, int(getattr(entry, "field_18", 0)) + 1))
        for group, amount, operation, runtime_update in program.group_durations:
            if not runtime_update:
                # Without RunTimeUpdate the modifier only shapes effects
                # applied later; minion lifetimes are fixed at spawn.
                continue
            amounts = bonus.setdefault(group, [0.0, 0.0])
            amounts[0 if operation == "relative" else 1] += amount * stack
    return {group: (values[0], values[1]) for group, values in bonus.items()}


def _adjust_owner_group_durations(s, path, bonus, generation, now) -> None:
    """RunTimeUpdate on the owner's own running group members.

    The Tesla trigger aura (``TurretDespawn``) and the summon visualizers
    (``TurretVisual``) run on the owner; their server lifetime and any
    item-set program deadline follow the live bonus.
    """

    groups = load_status_runtime_catalog(path).groups
    reschedule = []
    with s.flow_lock:
        if s.gameplay_chase_generation != generation:
            return
        origins = getattr(s, OWNER_GROUP_ORIGINS, None)
        if origins is None:
            origins = {}
            setattr(s, OWNER_GROUP_ORIGINS, origins)
        runtime = s.gameplay_runtime_status_effects
        for instance_id in tuple(origins):
            if instance_id not in runtime:
                origins.pop(instance_id, None)
        tracked = getattr(s, "gameplay_item_set_programs", {})
        for instance_id, (entry, expires_at) in tuple(runtime.items()):
            if expires_at is None or int(entry.field_10) <= 0:
                continue
            members = groups.get(int(entry.effect_id), frozenset())
            relative = sum(bonus[group][0] for group in members if group in bonus)
            absolute = sum(bonus[group][1] for group in members if group in bonus)
            if not (relative or absolute) and instance_id not in origins:
                continue
            base = int(entry.field_10) / SERVER_TICKS_PER_SECOND
            origin = origins.setdefault(instance_id, float(expires_at) - base)
            target = origin + base * (1.0 + relative) + absolute
            if not (relative or absolute):
                origins.pop(instance_id, None)
            if abs(target - float(expires_at)) <= 1e-3:
                continue
            runtime[instance_id] = (entry, target)
            if instance_id in tracked:
                reschedule.append((instance_id, target))
        timers = []
        old = []
        if reschedule:
            from dso_server.services.item_set_programs import _expire_item_set_program
            for instance_id, target in reschedule:
                key = ("item-set-program-expire", int(instance_id))
                previous = s.gameplay_projectile_timers.pop(key, None)
                if previous is not None:
                    old.append(previous)
                timer = s.gameplay_deadline_timer(
                    max(0.0, target - now), _expire_item_set_program,
                    args=(s, generation, int(instance_id)),
                )
                s.gameplay_projectile_timers[key] = timer
                timers.append(timer)
    for timer in old:
        timer.cancel()
    for timer in timers:
        timer.start()


def request_minion_aura_sync(s, *, now=None) -> None:
    """Re-evaluate this player's minion auras now; never raises data errors.

    Re-entrant calls (a despawn publishing from inside a pass) fold into one
    more round of the running pass.
    """

    # The flags are claimed under flow_lock (never held across the pass) so a
    # request from another thread is never lost: it either starts the pass or
    # makes the running one go round once more.
    with s.flow_lock:
        if getattr(s, "gameplay_minion_sync_active", False):
            s.gameplay_minion_sync_dirty = True
            return
        s.gameplay_minion_sync_active = True
        s.gameplay_minion_sync_dirty = False
    try:
        for _round in range(MAXIMUM_SYNC_ROUNDS):
            try:
                _sync_once(s, now=now)
            except OSError:
                raise
            except Exception as exc:          # data-driven; never break combat
                print(f"[minion-auras] sync failed: {type(exc).__name__}: {exc}")
                return
            now = None
            with s.flow_lock:
                if not s.gameplay_minion_sync_dirty:
                    s.gameplay_minion_sync_active = False
                    return
                s.gameplay_minion_sync_dirty = False
    finally:
        with s.flow_lock:
            s.gameplay_minion_sync_active = False


def _sync_once(s, *, now=None) -> None:
    with s.flow_lock:
        combat = s.gameplay_combat_state
        character = s.gameplay_character
        if not s.connected or combat is None or character is None:
            return
        generation = s.gameplay_chase_generation
        owner = int(s.gameplay_remote_actor_id
                    or CharacterService._remote_actor_id(character.character_id))
        move = s.gameplay_last_move
        server_tick = int(s.gameplay_chase_server_tick) & rules.TICK_MASK
        item_set_entries = tuple(s.gameplay_item_set_effects.values())
        runtime_entries = tuple(entry for entry, _ in s.gameplay_runtime_status_effects.values())
        persistent_entries = tuple(s.gameplay_persistent_status_effects.values())
    with combat.lock:
        has_summons = any(
            summon.owner_actor_id == owner for summon in combat.player_summons.values()
        )
        has_state = rules.owner_has_minion_state(combat, owner)
    if not has_summons and not has_state:
        _schedule(s, generation, owner, None, now=time.perf_counter())
        return
    path = _database_path(s)
    now = time.perf_counter() if now is None else float(now)
    bonus = owner_group_bonus(path, item_set_entries + runtime_entries + persistent_entries)
    _adjust_owner_group_durations(s, path, bonus, generation, now)
    with s.flow_lock:
        if s.gameplay_chase_generation != generation or s.gameplay_combat_state is not combat:
            return
        runtime = tuple(s.gameplay_runtime_status_effects.items())
        tracked = frozenset(getattr(s, "gameplay_item_set_programs", {}))
        evaluation = s.gameplay_item_set_evaluation
    auras = owner_minion_auras(path, item_set_entries, runtime, tracked, evaluation)
    position = (
        combat.encounter.coordinate_frame.wire_to_world(move.relative_position)
        if move is not None else None
    )
    with combat.lock:
        update = rules.advance_owner_minion_auras(
            combat, owner, auras, owner_position=position, group_bonus=bonus,
            now=now, server_tick=server_tick,
        )
    _publish(s, combat, generation, owner, update, now)
    _schedule(s, generation, owner, update, now=now)


def _publish(s, combat, generation, owner, update, now) -> None:
    output_error = None

    def step(operation, *args, **kwargs):
        # Every authoritative change was committed under the map lock; a
        # failed send must not skip the remaining publications.
        nonlocal output_error
        try:
            operation(*args, **kwargs)
        except OSError as error:
            output_error = output_error or error

    if update.commands:
        callback = s.gameplay_presence_callback
        if callback is not None:
            step(callback, s, ("player_summon_aura_refresh", update.commands))
        else:
            step(CharacterService._send_player_summon_aura_refreshes_to, s, update.commands)
    if update.lifetimes:
        step(_reschedule_lifetimes, s, generation, update.lifetimes, now)
    if update.feedback_remove:
        ended = frozenset(int(value.effect_id) for value in update.feedback_remove)
        step(CharacterService._end_player_runtime_effects, s,
             lambda entry: int(entry.effect_id) in ended,
             label="minion-aura-feedback-end")
    if update.feedback_add:
        step(CharacterService._send_player_skill_status_effects, s, update.feedback_add,
             server_tick=int(s.gameplay_chase_server_tick) & rules.TICK_MASK,
             label="minion-aura-feedback")
    for location in update.locations:
        step(CharacterService._publish_player_location_visuals, s, combat,
             (location.definition,), position=location.position, yaw=location.yaw,
             server_tick=location.server_tick, owner_actor_id=owner,
             cast_id=location.cast_id,
             receiver_status_effects=location.receivers,
             player_damage_range=location.damage_range)
    if update.resolutions:
        step(CharacterService._dispatch_player_owned_resolutions,
             combat, {owner: s}, update.resolutions)
    for actor_id in update.despawn:
        step(CharacterService._expire_player_summon, s, generation,
             ("minion-aura-despawn", int(actor_id)), int(actor_id))
    if output_error is not None:
        raise output_error


def _reschedule_lifetimes(s, generation, lifetimes, now) -> None:
    started, cancelled, expire_now = [], [], []
    with s.flow_lock:
        if s.gameplay_chase_generation != generation:
            return
        for actor_id, expires_at in lifetimes:
            key = ("player-summon-expire", int(actor_id))
            previous = s.gameplay_projectile_timers.pop(key, None)
            if previous is not None:
                cancelled.append(previous)
            if expires_at <= now:
                expire_now.append(int(actor_id))
                continue
            timer = s.gameplay_deadline_timer(
                max(0.0, float(expires_at) - now),
                CharacterService._expire_player_summon,
                args=(s, generation, key, int(actor_id)),
            )
            timer.daemon = True
            s.gameplay_projectile_timers[key] = timer
            started.append(timer)
    for timer in cancelled:
        timer.cancel()
    for timer in started:
        timer.start()
    for actor_id in expire_now:
        # The live bonus ended after the base lifetime had passed.
        CharacterService._expire_player_summon(
            s, generation, ("player-summon-expire", actor_id), actor_id,
        )


def _schedule(s, generation, owner, update, *, now) -> None:
    """Arm the owner's next pass at its earliest deadline or aura tick."""

    due = None if update is None else update.next_due
    if update is not None and update.cadence is not None:
        tick = now + float(update.cadence)
        due = tick if due is None else min(due, tick)
    key = (SYNC_TIMER, int(owner))
    timer = None
    with s.flow_lock:
        previous = s.gameplay_projectile_timers.pop(key, None)
        if due is not None and s.gameplay_chase_generation == generation:
            timer = s.gameplay_deadline_timer(
                max(0.0, float(due) - now), _scheduled_sync, args=(s, generation),
            )
            timer.daemon = True
            s.gameplay_projectile_timers[key] = timer
    if previous is not None and previous is not timer:
        previous.cancel()
    if timer is not None:
        timer.start()


def _scheduled_sync(s, generation) -> None:
    with s.flow_lock:
        if not s.connected or generation != s.gameplay_chase_generation:
            return
    request_minion_aura_sync(s)


__all__ = [
    "owner_group_bonus",
    "owner_minion_auras",
    "request_minion_aura_sync",
]
