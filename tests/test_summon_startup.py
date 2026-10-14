"""Deployment blocks summon actions independently of their attack cooldown."""

from dataclasses import replace
from unittest.mock import patch

import pytest

from dso_server.tests import test_local_combat_state as fixtures


@pytest.fixture
def combat():
    fixture = fixtures.LocalCombatStateTests()
    fixture.setUp()
    return fixture.state


def spawn(combat, skill_index):
    target = combat.monster_positions[combat.encounter.actor_ids[0]]
    return combat.spawn_player_summon(
        combat.player_summon_definition(skill_index), owner_actor_id=1,
        position=target, yaw=0.0, level=100, server_tick=2500,
        player_damage_range=(10.0, 10.0), now=100.0,
    ).actors[0]


@pytest.mark.parametrize("skill_index,delay", [
    (1856, 0.1), (1857, 0.1), (1877, 0.1),
    (1890, 1.04), (1891, 1.5), (1892, 1.04), (1895, 1.04),
])
@pytest.mark.parametrize("speed_scale", [0.5, 10.0])
def test_first_attack_waits_for_deployment(combat, skill_index, delay, speed_scale):
    actor = spawn(combat, skill_index)
    assert actor.attack_ready_at == pytest.approx(100.0 + delay)
    positions = {1: actor.position}
    # The wolf pack fans out on spawn. Put a victim in the first wolf's
    # melee range so target selection cannot mask readiness at the boundary.
    combat.monster_positions[combat.encounter.actor_ids[0]] = (
        actor.position[0], actor.position[1], actor.position[2] - 0.5)
    health = dict(combat.monster_health)
    with patch.object(combat, "_summon_status_scale", return_value=speed_scale):
        for now in (100.0, actor.attack_ready_at - 0.001):
            assert combat.maybe_start_player_summon_attacks(
                positions, server_tick=2500, now=now) == ()
            assert not combat.pending_player_summon_attacks
            assert combat.resolve_due_player_summon_attacks(
                now=now, player_positions=positions) == ((), ())
            assert combat.monster_health == health
        combat.maybe_start_player_summon_attacks(
            positions, server_tick=2600, now=actor.attack_ready_at)
        assert actor.actor_id in combat.pending_player_summon_attacks
        pending = combat.pending_player_summon_attacks[actor.actor_id]
        assert pending.attack.due_at >= actor.attack_ready_at


def test_attack_cooldown_starts_with_first_attack(combat):
    actor = spawn(combat, 1890)
    ready = actor.attack_ready_at
    interval = actor.definition.attack_interval_seconds
    combat.maybe_start_player_summon_attacks({}, server_tick=2600, now=ready)
    pending = combat.pending_player_summon_attacks[actor.actor_id]
    combat.resolve_due_player_summon_attacks(now=pending.attack.due_at)
    assert not combat.pending_player_summon_attacks
    assert combat.maybe_start_player_summon_attacks(
        {}, server_tick=2700, now=ready + interval - 0.001) == ()
    assert combat.maybe_start_player_summon_attacks(
        {}, server_tick=2701, now=ready + interval + 0.001)


def test_movement_waits_for_spawn_animation(combat):
    actor = spawn(combat, 1877)
    for index, actor_id in enumerate(combat.living_actor_ids):
        combat.monster_positions[actor_id] = (
            actor.position[0] + 5 + index, actor.position[1], actor.position[2])
    assert combat.advance_player_summons(
        elapsed_seconds=0.2, server_tick=2500, now=100.0) == ()
    assert combat.player_summons[actor.actor_id].position == actor.position
    assert combat.advance_player_summons(
        elapsed_seconds=0.2, server_tick=2525, now=actor.spawn_ready_at)


def test_expired_summon_never_attacks_after_deployment(combat):
    actor = spawn(combat, 1892)
    combat.expire_player_summon(actor.actor_id)
    assert combat.maybe_start_player_summon_attacks(
        {}, server_tick=2600, now=actor.attack_ready_at) == ()
    assert not combat.pending_player_summon_attacks


def test_sustained_attack_preserves_longer_authored_delay(combat):
    actor = spawn(combat, 1892)
    actor = replace(actor, definition=replace(
        actor.definition, attack_initial_delay_seconds=2.0))
    combat.player_summons[actor.actor_id] = actor
    assert combat.maybe_start_player_summon_attacks(
        {}, server_tick=2526, now=actor.spawn_ready_at) == ()
    assert combat.maybe_start_player_summon_attacks(
        {}, server_tick=2550, now=102.0)


def test_tree_support_waits_for_deployment(combat):
    actor = spawn(combat, 1881)
    combat.player_health[1][0] = 500
    assert actor.spawn_ready_at == 101.0
    for now in (100.0, 100.999):
        assert combat.resolve_due_player_summon_attacks(
            now=now, player_positions={1: actor.position}) == ((), ())
        assert combat.player_health[1][0] == 500
    damage, support = combat.resolve_due_player_summon_attacks(
        now=101.0, player_positions={1: actor.position})
    assert not damage
    assert support
    assert combat.player_health[1][0] == 550
