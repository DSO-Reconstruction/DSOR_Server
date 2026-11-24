"""``TriggerSkillDamage:<skill>`` on a hit's victim effect strikes with that skill.

Data (static.db4): the warrior Dragan 2025 set (3 parts) authors
``SkillStatusEffect:addVE,mightybash,jewel_dra_warrior_mightybash_trig_effect,
C:0.0,D:0.2,DP:0.2``; the second Jewel of Black Knights
(``jewel_dragon_03_sp_warrior_02``) raises it to ``C:1.0``. That victim
effect's whole program is ``TriggerSkillDamage:mightybash_jewel_dra_03_trig``,
a hidden 120% Physical melee skill (HitRange 3.5, Angle 120,
TargetingType AngleMinHpTarget). Before this, the server listed the kind as
unsupported (``unique_gem``/socket audit) and Smash never struck again.
"""

from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from dso_server.gameplay.local_combat_state import DamageResolution, LocalCombatState
from dso_server.gameplay.local_monster_state import load_local_monster_encounter
from dso_server.paths import DEFAULT_STATIC_DB_PATH
from dso_server.persistence import readonly_db
from dso_server.protocol.character_selection import CLASS_IDS
from dso_server.protocol.inventory_codec import InventoryInfo
from dso_server.protocol.item_set_protocol import ItemSetDatabase
from dso_server.protocol.socket_effect_protocol import SocketEffectDatabase
from dso_server.services.base import CharacterService
from dso_server.services.item_set_programs import reconcile_item_set_programs
from dso_server.tests.test_pvp_matchmaking import _session

TRIGGER_EFFECT = "jewel_dra_warrior_mightybash_trig_effect"
TRIGGERED_SKILL = "mightybash_jewel_dra_03_trig"
DRAGAN = ("warrior_mythic_helmet_dragan_sp", "warrior_mythic_shoulders_dragan_sp",
          "warrior_mythic_backitem_dragan_sp")


def skill(resource_id):
    return readonly_db.connect(DEFAULT_STATIC_DB_PATH).execute(
        "SELECT rowid-1 FROM _Template_Skill WHERE Id=?", (resource_id,),
    ).fetchone()[0]


@pytest.fixture(scope="module")
def sets():
    return ItemSetDatabase(DEFAULT_STATIC_DB_PATH)


@pytest.fixture(scope="module")
def sockets():
    return SocketEffectDatabase(DEFAULT_STATIC_DB_PATH)


@pytest.fixture
def quiet():
    with patch.object(CharacterService, "_start_monster_chase"), \
            patch.object(CharacterService, "_activate_player_user_effect_children"):
        yield


def warrior(sets, sockets, *, jewel):
    s = _session(52371, 371, "BlackKnight")
    s.gameplay_character.class_id = CLASS_IDS["warrior"]
    s.gameplay_combat_state = LocalCombatState(
        load_local_monster_encounter(DEFAULT_STATIC_DB_PATH, "b1201_pve", include_spawn_areas=True),
        player_actor_id=s.gameplay_remote_actor_id, player_health=1000,
    )
    s.gameplay_chase_server_tick = 500
    s.gameplay_presence_callback = None
    s.gameplay_last_move = None
    s.gameplay_spawn_position = (0.0, 0.0, 0.0)
    s.send_reliable_ordered = Mock()
    s.gameplay_deadline_timer = lambda delay, callback, args=(), kwargs=None: SimpleNamespace(
        start=Mock(), cancel=Mock(), daemon=False, delay=delay,
        callback=callback, args=args, kwargs=kwargs or {},
    )
    s.gameplay_inventory = InventoryInfo(
        health_points=1000, secondary_resource=100,
        items=tuple(
            SimpleNamespace(item_id=index + 1, template_id=template, socketed_templates=(
                ("jewel_dragon_03_sp_warrior_02",) if jewel and index == 2 else ()))
            for index, template in enumerate(DRAGAN)
        ),
        equipment_slots={1: (0,), 2: (1,), 3: (7,)},
    )
    evaluation = sets.evaluate(DRAGAN, class_name="warrior")
    transition = ItemSetDatabase.reconcile_effects(
        evaluation, {}, actor_id=1,
        next_instance_id=s.gameplay_next_status_effect_instance_id, server_tick=500,
    )
    s.gameplay_item_set_effects = transition.effects
    s.gameplay_item_set_evaluation = evaluation
    s.gameplay_next_status_effect_instance_id = transition.next_instance_id
    s.gameplay_socket_effect_evaluation = sockets.evaluate_inventory(s.gameplay_inventory)
    reconcile_item_set_programs(s)
    return s


def smash_hit(s, victim, effects):
    combat = s.gameplay_combat_state
    return DamageResolution(
        target_actor_id=victim, attacker_actor_id=s.gameplay_remote_actor_id,
        current_health=100, maximum_health=200, damage=10, killed=False,
        position=combat.monster_positions[victim], server_tick=600, hit_tags=(5,),
        skill_index=skill("mightybash"), victim_status_effects=tuple(effects),
        source_actor_id=s.gameplay_remote_actor_id, cast_id=77,
    )


def smash_trigger_effects(s):
    combat = s.gameplay_combat_state
    geometry = combat._modified_player_skill_geometry(
        combat._load_player_skill_geometry(skill("mightybash")),
        tuple(CharacterService._active_skill_modifier_sources(s)),
    )
    return [d for d in geometry.victim_status_effects if d.resource_id == TRIGGER_EFFECT]


def place(combat):
    victim, beside, far, *rest = combat.living_actor_ids
    combat.monster_positions[victim] = (0.0, 2.0, -8.0)
    combat.monster_positions[beside] = (2.0, 2.0, -8.0)
    combat.monster_positions[far] = (12.0, 2.0, -8.0)
    for actor in rest:
        combat.monster_positions[actor] = (500.0, 2.0, 500.0)
    return victim, beside, far


def test_jewel_turns_on_the_smash_trigger(sets, sockets, quiet):
    (with_jewel,) = smash_trigger_effects(warrior(sets, sockets, jewel=True))
    assert with_jewel.chance == 1.0
    (without,) = smash_trigger_effects(warrior(sets, sockets, jewel=False))
    assert without.chance == 0.0


def test_trigger_strikes_the_victim_and_its_neighbours_with_the_hidden_skill(sets, sockets, quiet):
    s = warrior(sets, sockets, jewel=True)
    combat = s.gameplay_combat_state
    victim, beside, far = place(combat)
    effects = smash_trigger_effects(s)
    CharacterService._apply_triggered_skill_damage(s, (smash_hit(s, victim, effects),))
    pending = [p for p in combat.pending_runtime_player_hits.values()
               if p.skill_index == skill(TRIGGERED_SKILL)]
    assert {p.target_actor_id for p in pending} == {victim, beside}
    assert all(p.source_actor_id == s.gameplay_remote_actor_id for p in pending)
    timers = [key for key in s.gameplay_projectile_timers
              if isinstance(key, tuple) and len(key) == 3
              and key[1] == pending[0].cast_id]
    assert len(timers) == len(pending)


def test_triggered_strike_deals_damage_when_it_lands(sets, sockets, quiet):
    s = warrior(sets, sockets, jewel=True)
    combat = s.gameplay_combat_state
    victim, beside, far = place(combat)
    far_health = combat.monster_health[far]
    CharacterService._apply_triggered_skill_damage(
        s, (smash_hit(s, victim, smash_trigger_effects(s)),))
    landed = []
    for key, pending in list(combat.pending_runtime_player_hits.items()):
        if pending.skill_index != skill(TRIGGERED_SKILL):
            continue
        resolution = combat.resolve_runtime_player_hit(
            key[0], key[1], key[2], now=pending.due_at + 0.01)
        if resolution is not None:
            landed.append(resolution)
    assert {r.target_actor_id for r in landed} == {victim, beside}
    assert all(r.damage > 0 for r in landed)
    assert combat.monster_health[far] == far_health


def test_one_trigger_per_cast_even_when_smash_hits_several_monsters(sets, sockets, quiet):
    s = warrior(sets, sockets, jewel=True)
    combat = s.gameplay_combat_state
    victim, beside, _far = place(combat)
    effects = smash_trigger_effects(s)
    CharacterService._apply_triggered_skill_damage(
        s, (smash_hit(s, victim, effects), smash_hit(s, beside, effects)))
    casts = {p.cast_id for p in combat.pending_runtime_player_hits.values()
             if p.skill_index == skill(TRIGGERED_SKILL)}
    assert len(casts) == 1


def test_no_trigger_without_the_jewel(sets, sockets, quiet):
    s = warrior(sets, sockets, jewel=False)
    combat = s.gameplay_combat_state
    victim, _beside, _far = place(combat)
    CharacterService._apply_triggered_skill_damage(
        s, (smash_hit(s, victim, smash_trigger_effects(s)),))
    assert not [p for p in combat.pending_runtime_player_hits.values()
                if p.skill_index == skill(TRIGGERED_SKILL)]
