"""Map-owned, explicitly started quest defense encounters."""

from dataclasses import dataclass, field, replace
import math
import random
import threading
import time

from dso_server.gameplay.holdground import UnsupportedHoldGround, holdground_actor_ids, load_holdground_plan
from dso_server.gameplay.local_map_state import (
    load_local_npc_definition, local_npc_actor_id, local_npc_info, map_database_path,
)
from dso_server.gameplay.local_monster_state import _load_monster, _load_runtime_definition, _spawn_group
from dso_server.gameplay.map_event_state import map_key
from dso_server.gameplay.pathengine_navigation import NavigationError
from dso_server.protocol.gameplay_protocol import NPCBatch, encode_npc_batch, encode_discard_monster_batch
from dso_server.protocol.socket_minion_protocol import load_socket_minion_definition
from dso_server.services.base import CharacterService


@dataclass
class HoldGroundRun:
    owner: object
    generation: int
    quest_id: str
    binding: object
    plan: object
    runtime: object
    combat: object
    character_id: int = field(init=False)
    defender_actor_id: int = 0
    owned_actor_ids: set[int] = field(default_factory=set)
    wave_actor_ids: set[int] = field(default_factory=set)
    wave_killed_actor_ids: set[int] = field(default_factory=set)
    wave_index: int = -1
    wave_deadline: float = 0.0
    wave_clear_deadline: float | None = None
    timer: object | None = None
    poll_token: object | None = None
    outcome: str | None = None
    cleaned: bool = False
    stopped: bool = False
    lock: object = field(default_factory=threading.RLock)

    def __post_init__(self):
        self.character_id = int(self.owner.gameplay_character.character_id)

    @property
    def npc_actor_id(self):
        return self.plan.npc_actor_id

    @property
    def key(self):
        return self.character_id, self.binding.holdground_id


def resolve_holdground(server, session, binding, *, npc_actor_id=None):
    """Read-only validation for defense offers; does not start or credit it."""
    runtime = getattr(session, "gameplay_map_event_runtime", None)
    difficulty = int(getattr(runtime, "scaling_difficulty",
                             getattr(session, "gameplay_scaling_difficulty", 0)))
    config_id = binding.config_for_difficulty(difficulty)
    if not config_id:
        raise UnsupportedHoldGround(f"no defense config for difficulty {difficulty}")
    plan = load_holdground_plan(
        server.static_database_path, session.gameplay_map, config_id,
        binding.actor, npc_actor_id=npc_actor_id,
    )
    # A template may exist but have no usable autonomous attack program.
    for template in {template for wave in plan.waves for spawn in wave.spawns
                     for template in spawn.template_pool or (spawn.template_id,)}:
        if _load_runtime_definition(server.static_database_path, template, 0) is None:
            raise UnsupportedHoldGround(f"no combat definition for defense attacker {template!r}")
    if load_socket_minion_definition(server.static_database_path, plan.defender_template_id,
                                     1, (0.0, 0.0, 0.0, 0.0)) is None:
        raise UnsupportedHoldGround(f"no defender definition for {plan.defender_template_id!r}")
    return plan


def holdground_hosted_here(server, session, task, binding):
    """Whether the session's map hosts a live defense binding.

    The task's ``map`` attribute names where its defense runs. A declared
    map whose exported scene never places the defense actor cannot run it:
    pw208-hide-and-seek-03-n declares pw208_rangervillage_dun, while Sibyl,
    hg_marker01-03, the quest's own pw208expl08 trigger and its enemy spawn
    area are all in pw208_lost_woods_dun_04 (the quest's PlayerEnteredMap
    signals move her there). Only then does the map that resolves the whole
    authored setup host it instead. An absent declared map is missing
    content, never a reason to look elsewhere (hub NPCs share templates).
    """
    current = map_key(getattr(session, "gameplay_map", "") or "")
    if not task.maps or current in {map_key(value) for value in task.maps}:
        return True
    path = server.static_database_path
    if any(not map_database_path(path, value).is_file()
           or holdground_actor_ids(path, value, binding.actor) for value in task.maps):
        return False
    for actor_id in holdground_actor_ids(path, session.gameplay_map, binding.actor):
        try:
            resolve_holdground(server, session, binding, npc_actor_id=actor_id)
        except UnsupportedHoldGround:
            continue
        return True
    return False


def start_holdground(server, session, quest_id, binding, *, npc_actor_id=None):
    """Start only after the caller validates the native Defend interaction.

    False means busy/stale, never unsupported content or quest success.
    UnsupportedHoldGround is reserved for failed authored-data validation.
    """
    plan = resolve_holdground(server, session, binding, npc_actor_id=npc_actor_id)
    with session.gameplay_mutation_lock:
        runtime = session.gameplay_map_event_runtime
        combat = session.gameplay_combat_state
        if (not session.connected or runtime is None or combat is None
                or session.gameplay_exit_transition_started or session.gameplay_death_pending):
            return False
        run = HoldGroundRun(session, session.gameplay_chase_generation, str(quest_id),
                            binding, plan, runtime, combat)
        _snapshot_recipients(server, run)
        with runtime.lock:
            runs = getattr(runtime, "holdgrounds", None)
            if runs is None:
                runs = runtime.holdgrounds = {}
            if run.key in runs or any(not other.stopped and other.npc_actor_id == plan.npc_actor_id
                                      for other in runs.values()):
                return False
            runs[run.key] = run
        session.gameplay_holdground_cancel = lambda reason="cancel": cancel_holdgrounds(
            server, session, reason=reason,
        )
        try:
            with run.lock:
                spawn = _spawn_defender(server, run)
                _publish_npc_enabled(server, run, False)
                _publish(server, run, lambda viewer: CharacterService._send_player_summon_spawn_to(viewer, spawn))
                _spawn_wave(server, run, time.perf_counter())
                _schedule_poll(server, run)
        except Exception:
            with run.lock:
                _finish(server, run, None)
            raise
        return True


def _owner_live(run):
    s = run.owner
    return (s.connected and s.gameplay_chase_generation == run.generation
            and s.gameplay_map_event_runtime is run.runtime
            and s.gameplay_combat_state is run.combat
            and not s.gameplay_exit_transition_started)


def _viewers(server, run):
    return tuple(s for s in server._map_event_sessions(run.runtime)
                 if s.gameplay_combat_state is run.combat
                 and not s.gameplay_exit_transition_started)


def _publish(server, run, action):
    # A failed viewer must not prevent the authoritative timer or other
    # viewers from receiving the encounter's state.
    for viewer in _viewers(server, run):
        try:
            action(viewer)
        except (OSError, RuntimeError) as exc:
            print(f"[holdground] publication to {viewer.addr} failed: {exc}")


def _spawn_defender(server, run):
    s, combat, plan = run.owner, run.combat, run.plan
    definition = load_socket_minion_definition(server.static_database_path,
        plan.defender_template_id, 1, (0.0, 0.0, 0.0, 0.0))
    definition = replace(
        definition, category=f"holdground:{run.key}", lifetime_seconds=0.0,
        owner_despawn_range=float("inf"), stationary=False, movement_locked=True,
        follows_owner=False, move_speed=0.0, taunt_radius=0.0,
        map_companion=True, map_level_scaling=True,
        map_scaling_difficulty=int(run.runtime.scaling_difficulty),
        attack_skill_database_index=None, attack_skill_index=None,
        attack_playback_enabled=False, aura=None,
    )
    level = run.runtime.monster_level or plan.defender_level
    with combat.lock:
        spawn = combat.spawn_player_summon(
            definition, owner_actor_id=s.gameplay_remote_actor_id
            or CharacterService._remote_actor_id(s.gameplay_character.character_id),
            position=plan.position, yaw=plan.yaw, level=level,
            server_tick=int(s.gameplay_chase_server_tick),
            player_damage_range=(0.0, 0.0), now=time.perf_counter(),
        )
        run.defender_actor_id = spawn.actors[0].actor_id
    return spawn


def _route_point_at_distance(points, distance):
    remaining = max(0.0, float(distance))
    for start, end in zip(points, points[1:]):
        segment = math.hypot(end[0] - start[0], end[2] - start[2])
        if segment <= 1e-6:
            continue
        if remaining <= segment:
            ratio = remaining / segment
            return tuple(
                float(start[axis])
                + (float(end[axis]) - float(start[axis])) * ratio
                for axis in range(3)
            )
        remaining -= segment
    return tuple(float(value) for value in points[-1])


def _holdground_spawn_position(run, spec, marker_slot, route_cache, used_positions):
    """Choose a walkable spawn point along the attacker's route to the NPC."""
    navigation = getattr(run.combat.encounter, "navigation", None)
    npc_position = tuple(float(value) for value in run.plan.position)
    source_position = tuple(float(value) for value in spec.position)
    if navigation is None:
        return npc_position

    try:
        route = route_cache.get(source_position)
        if route is None:
            route = navigation.find_route(npc_position, source_position)
            route_cache[source_position] = route
        npc_ground = navigation.project_position(npc_position)
        vertical_offset = npc_position[1] - npc_ground[1]
        route_length = sum(
            math.hypot(end[0] - start[0], end[2] - start[2])
            for start, end in zip(route.points, route.points[1:])
        )
        # Advance successive monsters that share a marker along the same
        # clear approach path instead of stacking them at one coordinate.
        start_distance = min(9.5, 5.5 + 0.65 * int(marker_slot))
        for offset in (0.0, 0.8, 1.6, 2.4):
            distance = min(route_length, start_distance + offset)
            point = _route_point_at_distance(route.points, distance)
            point = (point[0], point[1] + vertical_offset, point[2])
            point = navigation.project_position_relative(
                point, npc_position, max_horizontal_distance=0.75,
            )
            if (math.hypot(point[0] - npc_position[0],
                           point[2] - npc_position[2]) <= 10.0
                    and navigation.is_reachable(npc_position, point)
                    and all(math.dist(point, used) >= 0.9
                            for used in used_positions)):
                return point
    except (NavigationError, ValueError, TypeError, AttributeError):
        pass

    # If the authored approach is not connected, pick a sampled ground point
    # from the NPC's own component. This avoids roofs, houses and sealed yards.
    try:
        component = navigation.component_for_position(npc_position)
        candidates = navigation.component_positions(
            component, around=npc_position, minimum_distance=4.0,
            maximum_distance=10.0, limit=96,
        )
        npc_ground = navigation.project_position(npc_position)
        vertical_offset = npc_position[1] - npc_ground[1]
        candidates = sorted(candidates, key=lambda point: (
            (point[0] - source_position[0]) ** 2
            + (point[2] - source_position[2]) ** 2
        ))
        for candidate in candidates:
            candidate = (candidate[0], candidate[1] + vertical_offset,
                         candidate[2])
            candidate = navigation.project_position_relative(
                candidate, npc_position, max_horizontal_distance=0.75,
            )
            if (navigation.is_reachable(npc_position, candidate)
                    and all(math.dist(candidate, used) >= 0.9
                            for used in used_positions)):
                return candidate
    except (NavigationError, ValueError, TypeError, AttributeError):
        pass
    # The NPC transform itself is a known valid point and is safer than
    # publishing an attacker inside an obstacle where it can never path out.
    return npc_position


def _spawn_wave(server, run, now):
    run.wave_index += 1
    wave = run.plan.waves[run.wave_index]
    run.wave_deadline = float(now) + wave.duration_seconds
    run.wave_clear_deadline = None
    run.wave_actor_ids.clear()
    run.wave_killed_actor_ids.clear()
    if not wave.spawns:
        return  # An authored intermediate empty wave is a timed pause.
    combat = run.combat
    with combat.lock:
        actor_ids = combat.allocate_runtime_actor_ids(len(wave.spawns))
    monsters, definitions = [], []
    marker_slots = {}
    route_cache = {}
    used_spawn_positions = []
    for actor_id, spec in zip(actor_ids, wave.spawns, strict=True):
        # A corrected pool entry draws independently each time its wave
        # starts (module RNG, as event spawn areas use; tests may seed it).
        template = random.choice(spec.template_pool) if spec.template_pool else spec.template_id
        definition = _load_runtime_definition(server.static_database_path, template, actor_id)
        # Holdground markers are often far from the defended NPC. The mob
        # starts on a walkable point along that marker's navmesh route so it
        # stays visible and can path to the target without appearing in a
        # house, behind scenery, or on blocked ground.
        marker_slot = marker_slots.get(spec.marker_id, 0)
        marker_slots[spec.marker_id] = marker_slot + 1
        spawn_position = _holdground_spawn_position(
            run, spec, marker_slot, route_cache, used_spawn_positions,
        )
        used_spawn_positions.append(spawn_position)
        monster = _load_monster(server.static_database_path, template, actor_id,
            spawn_position, monster_level=run.runtime.monster_level,
            scaling_difficulty=run.runtime.scaling_difficulty)
        monsters.append(monster)
        definitions.append(definition)
    group = _spawn_group(server.static_database_path, tuple(monsters))
    ammo = combat.load_ammo_vulnerabilities(server.static_database_path, group.spawn_batch.commands)
    with combat.lock:
        added = combat.add_runtime_monsters(group, definitions, ammo_vulnerabilities=ammo)
        if len(added) != len(wave.spawns):
            raise RuntimeError("defense wave actor allocation was not complete")
        run.owned_actor_ids.update(added)
        run.wave_actor_ids.update(added)
        targets = getattr(combat, "holdground_targets", None)
        if targets is None:
            targets = combat.holdground_targets = {}
        targets.update((actor_id, run.defender_actor_id) for actor_id in added)
        combat.activate_monsters(tuple(added))
        max_npc_distance = max((
            ((combat.monster_positions[actor_id][0] - run.plan.position[0]) ** 2
             + (combat.monster_positions[actor_id][2] - run.plan.position[2]) ** 2) ** 0.5
            for actor_id in added
        ), default=0.0)
    print(
        f"[holdground] {run.binding.holdground_id} wave {wave.number}: "
        f"spawned {len(added)} attackers within {max_npc_distance:.1f}m of NPC"
    )
    _publish(server, run, lambda viewer: server._announce_runtime_group(
        viewer, combat, group, force_actor_ids=set(added),
    ))
    CharacterService._start_monster_chase(run.owner)


def _schedule_poll(server, run):
    if run.stopped:
        return
    scheduler = getattr(run.runtime, "scheduler", None)
    factory = scheduler.timer if scheduler is not None else run.owner.gameplay_deadline_timer
    token = object()
    timer = factory(0.5, _poll, args=(server, run, token))
    run.poll_token = token
    run.timer = timer
    timer.start()


def _poll(server, run, token):
    with run.owner.gameplay_mutation_lock:
        with run.lock:
            if run.stopped or token is not run.poll_token:
                return
            run.poll_token = None
            run.timer = None
            try:
                _advance(server, run)
            finally:
                if not run.stopped:
                    _schedule_poll(server, run)


def _advance(server, run):
    if run.stopped:
        return
    if run.outcome is not None:
        _finish(server, run, run.outcome)
        return
    if not _owner_live(run):
        _finish(server, run, None)
        return
    with run.combat.lock:
        health = run.combat.player_summon_health.get(run.defender_actor_id)
        failed = health is None or health[0] <= 0
    if failed or run.owner.gameplay_death_pending:
        _finish(server, run, "holdgroundfailed")
        return
    now = time.perf_counter()
    while run.wave_index + 1 < len(run.plan.waves):
        if run.wave_actor_ids:
            with run.combat.lock:
                wave_alive = any(
                    run.combat.monster_health.get(actor_id, 0) > 0
                    and actor_id not in run.wave_killed_actor_ids
                    for actor_id in run.wave_actor_ids
                )
            if wave_alive:
                return
            if run.wave_clear_deadline is None:
                run.wave_clear_deadline = now + 2.0
            if now < run.wave_clear_deadline:
                return
        elif now < run.wave_deadline:
            # Authored empty waves are intentional pauses; they have no
            # monsters whose death could trigger the short inter-wave delay.
            return
        _spawn_wave(server, run, now)

    if run.wave_index + 1 == len(run.plan.waves):
        with run.combat.lock:
            wave_alive = any(
                run.combat.monster_health.get(actor_id, 0) > 0
                and actor_id not in run.wave_killed_actor_ids
                for actor_id in run.wave_actor_ids
            )
        if wave_alive:
            return
        if run.wave_actor_ids:
            if run.wave_clear_deadline is None:
                run.wave_clear_deadline = now + 2.0
            if now < run.wave_clear_deadline:
                return
        elif now < run.wave_deadline:
            return
        _finish(server, run, "holdgroundsucceeded")


def advance_holdgrounds(server, session, *, killed_actor_ids=()):
    """Inspect shared encounters after authoritative damage/kill resolution."""
    killed_actor_ids = {int(actor_id) for actor_id in killed_actor_ids if int(actor_id) > 0}
    runtime = getattr(session, "gameplay_map_event_runtime", None)
    if runtime is None:
        return
    with runtime.lock:
        runs = tuple(getattr(runtime, "holdgrounds", {}).values())
    for run in runs:
        # A peer's kill callback may already own its own mutation gate. Never
        # wait for another player's gate: the independent poll will retry.
        if not run.owner.gameplay_mutation_lock.acquire(blocking=False):
            continue
        try:
            with run.lock:
                run.wave_killed_actor_ids.update(
                    killed_actor_ids & run.wave_actor_ids
                )
                _advance(server, run)
        finally:
            run.owner.gameplay_mutation_lock.release()


def cancel_holdgrounds(server, session, quest_id=None, *, reason="cancel"):
    """Abort owned runs on quest cancellation, death, transfer, or departure."""
    runtime = getattr(session, "gameplay_map_event_runtime", None)
    if runtime is None:
        return
    with runtime.lock:
        runs = tuple(run for run in getattr(runtime, "holdgrounds", {}).values()
                     if run.owner is session and (quest_id is None
                     or run.quest_id.casefold() == str(quest_id).casefold()))
    with session.gameplay_mutation_lock:
        for run in runs:
            with run.lock:
                _finish(server, run, "holdgroundfailed" if reason == "death" else None)


@dataclass
class _Recipient:
    session: object
    generation: int
    quest_id: str
    binding: object
    progress: object
    attempted: bool = False
    delivered: bool = False


def _snapshot_recipients(server, run):
    """Freeze participating attempts; joining later cannot earn an old defense."""
    owner_progress = getattr(run.owner, "gameplay_quest_log", {}).get(run.quest_id)
    recipients = [_Recipient(run.owner, run.generation, run.quest_id,
                             run.binding, owner_progress)]
    active = getattr(server, "_quest_active_holdgrounds", None)
    if active is not None:
        for peer in _viewers(server, run):
            if peer is run.owner:
                continue
            for quest, task, binding in active(peer):
                if (task.group and binding.actor.casefold() == run.binding.actor.casefold()
                        and binding.config_for_difficulty(run.runtime.scaling_difficulty)
                        == run.plan.config_id):
                    progress = getattr(peer, "gameplay_quest_log", {}).get(quest.quest_id)
                    if progress is not None:
                        recipients.append(_Recipient(peer, peer.gameplay_chase_generation,
                                                     quest.quest_id, binding, progress))
    run.recipients = tuple(recipients)


def _recipient_current(server, run, recipient):
    peer = recipient.session
    if (not peer.connected or peer.gameplay_chase_generation != recipient.generation
            or peer.gameplay_map_event_runtime is not run.runtime
            or peer.gameplay_combat_state is not run.combat
            or peer.gameplay_exit_transition_started):
        return False
    if recipient.progress is None:
        return peer is run.owner
    if getattr(peer, "gameplay_quest_log", {}).get(recipient.quest_id) is not recipient.progress:
        return False
    # A failed publication may already have applied the quest event. Retain
    # that same attempt for persistence/replay even though its task is closed.
    if recipient.attempted:
        return True
    return any(quest.quest_id == recipient.quest_id
               and binding.holdground_id == recipient.binding.holdground_id
               for quest, _task, binding in server._quest_active_holdgrounds(peer))


def _deliver_outcome(server, run, recipient):
    retry = recipient.attempted
    recipient.attempted = True
    first_error = None
    try:
        server._quest_feed(recipient.session, run.outcome, recipient.binding.holdground_id,
                           quest_id=recipient.quest_id)
    except OSError as error:
        first_error = error
    if recipient.progress is not None:
        # A previous feed can mutate progress before saving or sending raises.
        # Its idempotent retry alone would not save the now-closed task again.
        try:
            CharacterService._request_account_save(recipient.session, "quest defense result", flush=True)
        except OSError as error:
            raise first_error or error
        if first_error is not None:
            raise first_error
        if retry:
            from dso_server.protocol.player_init_protocol import QUEST_LOG_INFO_COMMAND, ServerCommand
            from dso_server.runtime.context import LOCAL_PLAYER_ACTOR_ID
            server._send_quest_commands(recipient.session, (ServerCommand(
                QUEST_LOG_INFO_COMMAND, server._quest_log_info(recipient.session),
                LOCAL_PLAYER_ACTOR_ID,
            ),), label="holdground-quest-replay")
    if first_error is not None:
        raise first_error
    recipient.delivered = True


def _finish(server, run, outcome):
    if run.stopped or getattr(run, "finishing", False):
        return
    run.finishing = True
    try:
        if run.outcome is None:
            run.outcome = outcome
        if not run.cleaned:
            with run.combat.lock:
                run.combat.retire_monsters(tuple(run.owned_actor_ids))
                run.combat.expire_player_summon(run.defender_actor_id)
                for actor_id in run.owned_actor_ids:
                    getattr(run.combat, "holdground_targets", {}).pop(actor_id, None)
            run.cleaned = True
            _publish(server, run, lambda viewer: _discard_owned(viewer, run))
        if run.outcome is not None:
            pending = False
            first_error = None
            for recipient in run.recipients:
                if recipient.delivered:
                    continue
                peer = recipient.session
                # The owner gate is already held. Never wait for a peer gate
                # while retaining it; their independent actions may need ours.
                if not peer.gameplay_mutation_lock.acquire(blocking=False):
                    pending = True
                    continue
                try:
                    if _recipient_current(server, run, recipient):
                        try:
                            _deliver_outcome(server, run, recipient)
                        except Exception as error:
                            first_error = first_error or error
                    else:
                        recipient.delivered = True
                finally:
                    peer.gameplay_mutation_lock.release()
            if first_error is not None:
                raise first_error
            if pending:
                return
        # Keep the existing poll until every recipient is durable. External
        # kill/death callbacks otherwise cancel the only retry on an exception.
        if run.timer is not None:
            run.timer.cancel()
            run.timer = None
        run.stopped = True
        with run.runtime.lock:
            runs = getattr(run.runtime, "holdgrounds", {})
            if runs.get(run.key) is run:
                runs.pop(run.key, None)
        _publish_npc_enabled(server, run, True)
        _publish(server, run, lambda viewer: server._quest_refresh_npc_infos(viewer))
    finally:
        run.finishing = False


def _discard_owned(viewer, run):
    retired = set(run.owned_actor_ids)
    with viewer.flow_lock:
        discard = tuple(sorted(retired & viewer.gameplay_spawned_monster_actor_ids))
        viewer.gameplay_spawned_monster_actor_ids.difference_update(retired)
        viewer.gameplay_announced_monster_actor_ids.difference_update(retired)
        viewer.gameplay_requested_actor_ids.difference_update(retired)
        CharacterService._forget_published_monsters(viewer, retired)
        for actor_id in retired:
            viewer.gameplay_delivered_monster_positions.pop(actor_id, None)
    if discard:
        viewer.send_reliable_ordered(encode_discard_monster_batch(discard), track=True,
                                     label="holdground-wave-discard")
    CharacterService._send_player_summon_despawn_to(viewer, (run.defender_actor_id,))


def _publish_npc_enabled(server, run, enabled):
    definition = load_local_npc_definition(server.static_database_path, run.plan.map_id,
                                           run.plan.npc_actor_id)
    if definition is None:
        return
    value = replace(local_npc_info(definition), enabled=bool(enabled))
    packet, bits = encode_npc_batch(NPCBatch((value,)), include_actor_vicinity=False)
    def send(viewer):
        if enabled and not any(local_npc_actor_id(npc) == run.plan.npc_actor_id
                               for npc in CharacterService._active_npc_definitions(server, viewer)):
            return  # A completed quest or global transition may hide it.
        if run.plan.npc_actor_id in viewer.gameplay_announced_npc_actor_ids:
            CharacterService._send_world_loot_state_batch(viewer, packet, bits,
                label="holdground-npc-restore" if enabled else "holdground-npc-hide")
    _publish(server, run, send)
