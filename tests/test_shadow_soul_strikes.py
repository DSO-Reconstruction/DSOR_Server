"""Twilight Wraith's second swing, and what the other Shadow Souls author."""

import sqlite3

import pytest

from dso_server.gameplay.local_combat_state import LocalCombatState
from dso_server.gameplay.local_monster_state import load_local_monster_encounter
from dso_server.protocol.socket_minion_protocol import load_socket_minion_definition
from dso_server.paths import DEFAULT_STATIC_DB_PATH


OWNER = 98305
WRAITH = "mercenary_shadow_stalker"
DAMAGE = 100.0


@pytest.fixture(scope="module")
def encounter():
    return load_local_monster_encounter(DEFAULT_STATIC_DB_PATH, "b1201_pve", include_spawn_areas=True)


@pytest.fixture
def combat(encounter):
    state = LocalCombatState(encounter, player_actor_id=OWNER, player_health=1000)
    state.monster_status_effects.clear()
    return state


def _spawn(combat, template_id, distance=1.5):
    definition = load_socket_minion_definition(
        DEFAULT_STATIC_DB_PATH, template_id, 1, (0.0, 0.0, 0.0, 0.0),
    )
    target = next(iter(combat.living_actor_ids))
    for other in tuple(combat.living_actor_ids):
        if other != target:
            combat.monster_positions[other] = (9000.0, 0.0, 9000.0)
    position = combat.monster_positions[target]
    spawn = combat.spawn_player_summon(
        definition, owner_actor_id=OWNER,
        position=(position[0] - distance, position[1], position[2]),
        yaw=0.0, level=100, server_tick=550,
        player_damage_range=(DAMAGE, DAMAGE), now=100.0,
    )
    actor = spawn.actors[0].actor_id
    return actor, combat.player_summons[actor], target


def _swing(combat, actor, summon):
    started_at = summon.attack_ready_at + 0.01
    attacks = combat.maybe_start_player_summon_attacks({}, server_tick=600, now=started_at)
    return started_at, next(attack for attack in attacks if attack.attacker_actor_id == actor)


def _resolve(combat, now):
    return combat.resolve_due_player_summon_attacks(now=now, server_tick=650)[0]


def test_the_wraiths_second_swing_is_a_delayed_three_percent_aura(combat):
    actor, summon, _target = _spawn(combat, WRAITH)
    geometry = combat._load_player_skill_geometry(summon.definition.attack_skill_database_index)
    ((aura, receivers),) = combat.player_summon_aura_strikes(geometry)
    assert aura.resource_id == "shadow_stalker_attack_dmg2_aura"
    assert aura.delay_ticks == 15
    assert aura.aura_radius == 3.0
    assert [item.resource_id for item in receivers] == ["shadow_stalker_attack_dmg2_aura_effect"]
    assert receivers[0].values[0] == pytest.approx(-0.03)


def test_one_wraith_attack_lands_two_hits(combat):
    actor, summon, target = _spawn(combat, WRAITH)
    started_at, attack = _swing(combat, actor, summon)
    first = _resolve(combat, attack.due_at + 0.01)
    assert [item.target_actor_id for item in first] == [target]
    assert _resolve(combat, started_at + 0.59) == ()
    ((second),) = _resolve(combat, started_at + 0.61)
    assert second.target_actor_id == target
    assert second.attacker_actor_id == actor
    assert second.source_actor_id == OWNER
    assert second.skill_index == attack.skill_index
    assert second.server_tick == 615
    assert first[0].damage == 150
    assert second.damage == 3
    assert _resolve(combat, started_at + 1.0) == ()
    assert not combat.pending_player_summon_strikes


def test_the_second_swing_needs_the_enemy_in_reach(combat):
    actor, summon, target = _spawn(combat, WRAITH)
    started_at, attack = _swing(combat, actor, summon)
    _resolve(combat, attack.due_at + 0.01)
    combat.monster_positions[target] = (9000.0, 0.0, 9000.0)
    assert _resolve(combat, started_at + 0.61) == ()


def test_the_second_swing_dies_with_the_wraith(combat):
    actor, summon, _target = _spawn(combat, WRAITH)
    started_at, attack = _swing(combat, actor, summon)
    _resolve(combat, attack.due_at + 0.01)
    del combat.player_summons[actor]
    assert _resolve(combat, started_at + 0.61) == ()
    assert not combat.pending_player_summon_strikes


def test_only_the_wraith_authors_a_second_hit():
    database = sqlite3.connect(f"file:{DEFAULT_STATIC_DB_PATH}?mode=ro", uri=True)
    skills = database.execute(
        "SELECT Id, COALESCE(UserStatusEffects, '') FROM _Template_Skill "
        "WHERE Groups LIKE '%ShadowSoul_skills%'"
    ).fetchall()
    assert {name for name, effects in skills if effects} == {"mercenaryskill_shadow_stalker_attack"}


@pytest.mark.parametrize("template_id", [
    "mercenary_ancient_watcher", "mercenary_divine_wanderer",
    "mercenary_ember_knight_kain", "mercenary_viridis",
])
def test_the_other_souls_keep_one_hit_per_attack(combat, template_id):
    _actor, summon, _target = _spawn(combat, template_id)
    geometry = combat._load_player_skill_geometry(summon.definition.attack_skill_database_index)
    assert combat.player_summon_aura_strikes(geometry) == ()


def _skill_id(combat, skill_index):
    return combat._load_player_skill_geometry(skill_index).resource_id


def _fight(combat, template_id, swings, distance=1.5):
    actor, summon, target = _spawn(combat, template_id, distance)
    combat.monster_health[target] = 10 ** 7
    now = summon.attack_ready_at + 0.01
    fought = []
    for _swing_number in range(swings):
        attacks = combat.maybe_start_player_summon_attacks({}, server_tick=600, now=now)
        attack = next(item for item in attacks if item.attacker_actor_id == actor)
        ((hit),) = _resolve(combat, attack.due_at + 0.01)
        fought.append((_skill_id(combat, attack.skill_index), hit.damage))
        now += summon.definition.attack_interval_seconds + 0.01
    return actor, summon, fought


def test_ancient_watcher_alternates_its_two_strikes(combat):
    _actor, summon, fought = _fight(combat, "mercenary_ancient_watcher", 4)
    assert summon.definition.attack_interval_seconds == 3.0
    assert fought == [
        ("mercenaryskill_ancient_watcher_attack_01", 150),
        ("mercenaryskill_ancient_watcher_attack_02", 300),
        ("mercenaryskill_ancient_watcher_attack_01", 150),
        ("mercenaryskill_ancient_watcher_attack_02", 300),
    ]


def test_viridis_stabs_an_enemy_inside_her_stab(combat):
    _actor, _summon, fought = _fight(combat, "mercenary_viridis", 3)
    assert [name for name, _damage in fought] == [
        "mercenaryskill_nature_markshot",
        "mercenaryskill_nature_mightystab",
        "mercenaryskill_nature_markshot",
    ]


def test_viridis_only_shoots_past_her_stab(combat):
    _actor, _summon, fought = _fight(combat, "mercenary_viridis", 3, distance=10.0)
    assert {name for name, _damage in fought} == {"mercenaryskill_nature_markshot"}


def test_a_listed_attack_waits_for_its_cooldown(combat):
    actor, summon, _target = _spawn(combat, "mercenary_ancient_watcher")
    first, second = summon.definition.attack_rotation
    combat.player_summon_rotations[actor] = (0, {first: 110.0, second: 105.0})
    rotation = summon.definition.attack_rotation
    assert combat._summon_rotation_turn(actor, rotation, 1.5, 100.0) is None
    assert combat._summon_rotation_turn(actor, rotation, 1.5, 106.0) == 1
    assert combat._summon_rotation_turn(actor, rotation, 1.5, 111.0) == 0


def test_a_souls_rotation_ends_with_it(combat):
    actor, _summon, _fought = _fight(combat, "mercenary_ancient_watcher", 1)
    assert actor in combat.player_summon_rotations
    combat.expire_player_summon(actor)
    assert actor not in combat.player_summon_rotations


@pytest.mark.parametrize("template_id", [
    "mercenary_shadow_stalker", "mercenary_divine_wanderer", "mercenary_ember_knight_kain",
])
def test_a_soul_with_one_open_attack_has_no_rotation(combat, template_id):
    _actor, summon, _target = _spawn(combat, template_id)
    assert summon.definition.attack_rotation == ()
