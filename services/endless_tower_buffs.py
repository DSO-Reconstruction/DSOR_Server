"""Execute the selected tower buffs' authored status-effect graphs."""

from dataclasses import dataclass, replace
from functools import lru_cache
import math
import time

from dso_server.protocol.socket_effect_protocol import (
    SocketEffectEvaluation, parse_socket_program, prime_socket_effect_database,
)


@dataclass(frozen=True)
class TowerEdge:
    key: str
    parent: str
    phase: str
    trigger: str
    delay: float
    rule: object


@lru_cache(maxsize=64)
def tower_program(path, roots):
    database = prime_socket_effect_database(path)
    nodes, edges = {}, {}

    def immediate_receivers(definitions, seen=()):
        result = []
        for definition in definitions:
            if definition.resource_id in seen:
                continue
            result.append(definition)
            row = database._status_rows.get(definition.resource_id)
            if row is None:
                continue
            children = []
            for modifier in parse_socket_program(row['StartModifiers']):
                if (modifier.kind.casefold() != 'actorstatuseffect'
                        or modifier.trigger not in ('', 'immediate')
                        or not modifier.operation.casefold().startswith('add')
                        or len(modifier.arguments) < 2):
                    continue
                child = database._definition(
                    modifier, modifier.arguments[1], trigger='immediate',
                )
                if child is not None:
                    children.append(child)
            result.extend(immediate_receivers(
                children, (*seen, definition.resource_id),
            ))
        return tuple(result)

    def visit(resource):
        if resource in nodes:
            return
        row = database._status_rows.get(resource)
        if row is None:
            return
        nodes[resource] = (
            max(0., float(row['StatusEffectDuration'] or 0.)),
            max(.04, float(row['TickRate'] or 1.)),
        )
        for phase, column in (('start', 'StartModifiers'),
                              ('tick', 'TickModifiers'), ('done', 'DoneModifiers')):
            for ordinal, modifier in enumerate(parse_socket_program(row[column])):
                if modifier.kind.casefold() != 'actorstatuseffect':
                    continue
                rule = database._rule('fracturedplanes:' + resource, modifier)
                if rule is None or not rule.operation.casefold().startswith('add'):
                    continue
                key = f'fp{rule.definition.effect_id}{len(edges)}'
                child_row = database._status_rows[rule.definition.resource_id]
                targets = str(child_row['AuraTargets'] or '').casefold().split(';')
                receivers = database._aura_children(rule.definition, include_causer=True)
                rule = replace(
                    rule, trigger=key,
                    definition=replace(rule.definition, delay_ticks=0),
                    # Execute nested Start/Tick/Done edges explicitly once.
                    nested_user_definitions=(receivers if any(
                        target in targets for target in ('creator', 'causer')
                    ) else ()),
                    receiver_definitions=(immediate_receivers(receivers)
                                          if 'enemies' in targets else ()),
                    event_auras=(),
                )
                edges[key] = TowerEdge(
                    key, resource, phase, modifier.trigger,
                    max(0., float(modifier.metadata.get('de', '0'))), rule,
                )
                visit(rule.definition.resource_id)
                for receiver in rule.nested_user_definitions:
                    visit(receiver.resource_id)

    for root in roots:
        visit(root)
    evaluation = SocketEffectEvaluation(
        (), (), tuple((key, (edge.rule,)) for key, edge in edges.items()), (), (),
    )
    return nodes, edges, evaluation


def _schedule(server, session, generation, root_instance, edge, delay,
              *, interval=0., expires=None):
    key = ('fractured-status', root_instance, edge.key)
    with session.flow_lock:
        if not _live(session, generation, root_instance):
            return
        previous = session.gameplay_projectile_timers.pop(key, None)
        timer = session.gameplay_deadline_timer(
            max(0., delay), _fire,
            args=(server, session, generation, root_instance, edge, key, interval, expires),
        )
        session.gameplay_projectile_timers[key] = timer
    if previous is not None:
        previous.cancel()
    timer.start()


def _live(session, generation, root_instance):
    return (
        session.connected
        and session.gameplay_chase_generation == generation
        and session.gameplay_combat_state is not None
        and root_instance in session.gameplay_persistent_status_effects
        and session.gameplay_fractured_planes_run
    )


def _activate(server, session, generation, root_instance, resource):
    nodes, edges, _evaluation = session.gameplay_fractured_planes_buff_program
    duration, interval = nodes.get(resource, (0., 1.))
    expires = time.perf_counter() + duration if duration > 0 else None
    for edge in edges.values():
        if edge.parent != resource or edge.trigger not in ('', 'immediate'):
            continue
        if edge.phase == 'tick':
            _schedule(server, session, generation, root_instance, edge,
                      interval + edge.delay, interval=interval, expires=expires)
        elif edge.phase == 'start':
            _schedule(server, session, generation, root_instance, edge, edge.delay)
        elif edge.phase == 'done' and duration > 0:
            _schedule(server, session, generation, root_instance, edge, duration + edge.delay)


def _fire(server, session, generation, root_instance, edge, key, interval, expires):
    with session.flow_lock:
        session.gameplay_projectile_timers.pop(key, None)
        if not _live(session, generation, root_instance):
            return
        tick = int(session.gameplay_chase_server_tick) & 0xffffffff
        combat = session.gameplay_combat_state
        owner = session.gameplay_remote_actor_id or combat.player_actor_id
        _nodes, _edges, evaluation = session.gameplay_fractured_planes_buff_program
    now = time.perf_counter()
    if expires is not None and now > expires + .05:
        return
    with combat.lock:
        alive = combat.player_health.get(owner, (0,))[0] > 0
    if alive:
        # Native zero-duration effects are map-long. The shared player
        # runtime requires a positive server lease for stack bookkeeping;
        # bind those children to this run's remaining lifetime.
        runtime = getattr(session, 'gameplay_map_event_runtime', None)
        deadline = getattr(runtime, 'fractured_planes_limit_deadline', None)
        lease = max(1, math.ceil((max(1., deadline - now) if deadline else 480.) * 25.))
        def leased(definition):
            return (replace(definition, duration_ticks=lease)
                    if definition.duration_ticks <= 0 else definition)
        rule = replace(
            edge.rule, definition=leased(edge.rule.definition),
            nested_user_definitions=tuple(leased(d) for d in edge.rule.nested_user_definitions),
            receiver_definitions=tuple(leased(d) for d in edge.rule.receiver_definitions),
        )
        evaluation = replace(evaluation, event_rules=((edge.key, (rule,)),))
        passed = server._dispatch_socket_event_batch(
            session, ((edge.key, tick, tick ^ root_instance),), _evaluation=evaluation,
        )
        if passed and passed[0]:
            _activate(server, session, generation, root_instance,
                      edge.rule.definition.resource_id)
            for receiver in edge.rule.nested_user_definitions:
                _activate(server, session, generation, root_instance, receiver.resource_id)
            _aura_damage(server, session, combat, owner, edge.rule, tick)
            server._start_monster_chase(session)
    if interval > 0 and (expires is None or now + interval <= expires):
        _schedule(server, session, generation, root_instance, edge,
                  interval, interval=interval, expires=expires)


def tower_damage_range(database_path, session, modifier_sources):
    from dso_server.gameplay.player_combat_stats import calculate_player_combat_stats
    from dso_server.protocol.character_selection import CLASS_NAMES
    with session.flow_lock:
        character = session.gameplay_character
        inventory = session.gameplay_inventory
    stats = calculate_player_combat_stats(
        database_path, cache_owner=session,
        class_name=CLASS_NAMES.get(int(character.class_id), 'mage'),
        level=max(1, int(character.level)), inventory=inventory,
        modifier_sources=tuple(modifier_sources),
    )
    return stats.minimum_damage, stats.maximum_damage


def _aura_damage(server, session, combat, owner, rule, tick):
    if not rule.receiver_definitions or not any(
        'currhealthpointsdmg' in receiver.modifier_kinds
        for receiver in rule.receiver_definitions
    ) or rule.target == 'location':
        return
    from dso_server.protocol.socket_effect_protocol import SocketPeriodicAura
    damage_range = tower_damage_range(
        server.static_database_path, session, server._active_skill_modifier_sources(session),
    )
    with session.flow_lock:
        move = session.gameplay_last_move
        position = (combat.encounter.coordinate_frame.wire_to_world(move.relative_position)
                    if move is not None else tuple(session.gameplay_spawn_position))
    aura = SocketPeriodicAura(rule.trigger, 1., rule.definition, rule.receiver_definitions, ())
    with combat.lock:
        resolutions, _owner = combat.resolve_socket_periodic_auras(
            owner, position, (aura,), player_damage_range=damage_range, server_tick=tick,
        )
    server._dispatch_player_owned_resolutions(combat, {owner: session}, resolutions)


def start_tower_buffs(server, session):
    with session.flow_lock:
        if not getattr(session, 'gameplay_fractured_planes_run', False):
            return
        generation = session.gameplay_chase_generation
        roots = tuple(
            (instance, session.gameplay_persistent_status_effects[instance].effect_id)
            for instance in session.gameplay_fractured_planes_status_effect_instance_ids
            if instance in session.gameplay_persistent_status_effects
        )
    database = prime_socket_effect_database(server.static_database_path)
    names = {int(row['EffectId']): name for name, row in database._status_rows.items()}
    resources = tuple(names[effect] for _instance, effect in roots if effect in names)
    session.gameplay_fractured_planes_buff_program = tower_program(
        str(server.static_database_path), resources,
    )
    session.gameplay_fractured_planes_buff_roots = {
        names[effect]: instance for instance, effect in roots if effect in names
    }
    for resource, instance in session.gameplay_fractured_planes_buff_roots.items():
        _activate(server, session, generation, instance, resource)


def tower_kills(server, session, monsters):
    program = getattr(session, 'gameplay_fractured_planes_buff_program', None)
    if program is None:
        return
    _nodes, edges, _evaluation = program
    for edge in edges.values():
        if edge.phase != 'start' or edge.trigger != 'kill':
            continue
        instance = session.gameplay_fractured_planes_buff_roots.get(edge.parent)
        if instance is None:
            continue
        for _monster in monsters:
            _fire(server, session, session.gameplay_chase_generation, instance,
                  edge, ('fractured-kill', instance), 0., None)
