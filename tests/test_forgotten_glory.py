"""Forgotten Glory (``set_*_bgh_heredur``) and its Jewel of Glory, as authored.

Every test uses the live ``static.db4`` rows and the real cast, program and
combat paths. Client text (gui.statuseffects.xml, db.itemset.xml):

* mage: Ice Missile fires 3 missiles; jewel: "Using Frost Wind instantly
  grants maximum Stirring Frost stacks".
* warrior: Banner of War +200% damage for 10 s; jewel: "immediately gain the
  maximum number of Battle Frenzy stacks".
* ranger: jewel: "Explosive Arrow triggers an additional explosion with a 4.5
  meter radius, dealing 100% Physical Damage ... and applying 3 stacks of
  Burning".
* dwarf: "60% chance that hits with Quick Shot cause your currently active
  Mechanical Turrets to fire 3 additional ice bullets ... When a Mechanical
  Turret is used, you get 100% Damage and 15% Critical Value"; jewel: "an
  additional 100% increased Damage".
"""

from collections import Counter
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
from dso_server.services.item_set_programs import (
    cleanse_unequipped_items, reconcile_item_set_programs,
)
from dso_server.tests import test_black_dwarf_turrets as turrets
from dso_server.tests.test_pvp_matchmaking import _session


def db():
    return readonly_db.connect(DEFAULT_STATIC_DB_PATH)


def skill(resource_id):
    return db().execute(
        "SELECT rowid-1 FROM _Template_Skill WHERE Id=?", (resource_id,),
    ).fetchone()[0]


def effect_name(effect_id):
    return db().execute(
        "SELECT Id FROM _Template_StatusEffect WHERE rowid=?", (int(effect_id) + 1,),
    ).fetchone()[0]


@pytest.fixture(scope="module")
def sets():
    return ItemSetDatabase(DEFAULT_STATIC_DB_PATH)


@pytest.fixture(scope="module")
def sockets():
    return SocketEffectDatabase(DEFAULT_STATIC_DB_PATH)


def parts(class_name):
    return (f"{class_name}_unique_ring_bgh", f"{class_name}_unique_backitem_bgh_heredur")


def wearer(sets, sockets, class_name, *, jewel=False):
    s = _session(52361, 361, "Glory")
    s.gameplay_character.class_id = CLASS_IDS[class_name]
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
    ring, cloak = parts(class_name)
    s.gameplay_inventory = InventoryInfo(
        health_points=1000, secondary_resource=100,
        items=(
            SimpleNamespace(item_id=1, template_id=ring, socketed_templates=()),
            SimpleNamespace(item_id=2, template_id=cloak, socketed_templates=(
                (f"jewel_bgh_{class_name}",) if jewel else ())),
        ),
        equipment_slots={1: (10,), 2: (11,)},
    )
    evaluation = sets.evaluate((ring, cloak), class_name=class_name)
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


@pytest.fixture
def quiet():
    with patch.object(CharacterService, "_start_monster_chase"), \
            patch.object(CharacterService, "_activate_player_user_effect_children"):
        yield


def cast(s, resource_id, trigger="skillstart"):
    index = skill(resource_id)
    sources = tuple(CharacterService._active_skill_modifier_sources(s))
    effects = s.gameplay_combat_state.player_skill_user_effects(
        index, cast_id=11, trigger=trigger, modifier_effect_ids=sources,
    ) + CharacterService._item_set_user_proc_effects(s, index, triggers=(trigger,))
    CharacterService._send_player_skill_status_effects(
        s, effects, server_tick=600, label="test-cast")
    return effects


def stacks(s):
    result = Counter()
    for entry, _expires in s.gameplay_runtime_status_effects.values():
        name = effect_name(entry.effect_id)
        result[name] = max(result[name], int(entry.field_18) + 1)
    return result


def expire_programs(s, resource_id):
    for key, timer in list(s.gameplay_projectile_timers.items()):
        if key[0] != "item-set-program-expire":
            continue
        definition = s.gameplay_item_set_programs.get(key[1])
        if definition is not None and definition.resource_id == resource_id:
            timer.callback(*timer.args)


def test_mage_jewel_fills_both_stirring_frost_stacks(sets, sockets, quiet):
    s = wearer(sets, sockets, "mage", jewel=True)
    cast(s, "frostwind")
    running = stacks(s)
    assert running["skill_frostwind_buff_iceball_damage"] == 20
    assert running["skill_frostwind_buff_frostnova_damage"] == 20


def test_mage_without_the_jewel_gets_no_stacks(sets, sockets, quiet):
    s = wearer(sets, sockets, "mage")
    cast(s, "frostwind")
    assert "skill_frostwind_buff_iceball_damage" not in stacks(s)


def test_warrior_jewel_trigger_grants_maximum_battle_frenzy(sets, sockets, quiet):
    """The zero-duration trigger runs its program; its 0.3 s carriers grant
    the stacks when they are done."""
    s = wearer(sets, sockets, "warrior", jewel=True)
    cast(s, "defiance")
    for carrier in ("skill_defiance_buff_skill_might360_damage_jewel_bgh2026_effect",
                    "skill_defiance_buff_skill_origin_damage_jewel_bgh2026_effect"):
        assert stacks(s)[carrier] == 1
        expire_programs(s, carrier)
    running = stacks(s)
    assert running["skill_defiance_buff_skill_damage_mighty360"] == 10
    assert running["skill_defiance_buff_skill_damage"] == 10
    assert running["bgh_warrior_bonus_heredur_02"] == 1


def test_dwarf_turret_damage_bonus_lasts_the_turret_shell(sets, sockets, quiet):
    s = wearer(sets, sockets, "dwarf")
    effects = {d.resource_id: d for d in cast(s, "TurretCombat")}
    assert effects["bgh_dwarf_bonus_heredur_dmg_aura"].duration_ticks == 250
    assert effects["bgh_dwarf_bonus_heredur_dmg"].duration_ticks == 250
    assert "bgh_dwarf_bonus_heredur_dmg_jewel2026" not in effects


def test_dwarf_jewel_adds_its_own_damage_payload(sets, sockets, quiet):
    s = wearer(sets, sockets, "dwarf", jewel=True)
    effects = {d.resource_id: d for d in cast(s, "TurretCombat")}
    assert effects["bgh_dwarf_bonus_heredur_dmg_jewel2026"].duration_ticks == 250


def test_ranger_jewel_explodes_around_the_victim_with_three_burns(sets, sockets, quiet):
    s = wearer(sets, sockets, "ranger", jewel=True)
    combat = s.gameplay_combat_state
    victim, beside, far, *rest = combat.living_actor_ids
    combat.monster_positions[victim] = (0.0, 2.0, -8.0)
    combat.monster_positions[beside] = (3.0, 2.0, -8.0)
    combat.monster_positions[far] = (9.0, 2.0, -8.0)
    for actor in rest:
        combat.monster_positions[actor] = (500.0, 2.0, 500.0)
    geometry = combat._modified_player_skill_geometry(
        combat._load_player_skill_geometry(skill("explosiveshot")),
        tuple(CharacterService._active_skill_modifier_sources(s)),
    )
    (explosion,) = [d for d in geometry.victim_status_effects
                    if d.resource_id == "skill_explosiveshot_explosion_aura_bgh_jewel2026"]
    assert explosion.chance == 1.0
    hit = DamageResolution(
        target_actor_id=victim, attacker_actor_id=s.gameplay_remote_actor_id,
        current_health=100, maximum_health=200, damage=10, killed=False,
        position=combat.monster_positions[victim], server_tick=600, hit_tags=(5,),
        skill_index=skill("explosiveshot"), victim_status_effects=(explosion,),
        source_actor_id=s.gameplay_remote_actor_id,
    )
    far_health = combat.monster_health[far]
    struck = []
    with patch.object(CharacterService, "_dispatch_player_owned_resolutions",
                      staticmethod(lambda _combat, _owners, values: struck.extend(values))):
        CharacterService._apply_receiver_hosted_hit_auras(s, (hit,))
        (timer,) = [t for key, t in s.gameplay_projectile_timers.items()
                    if key[0] == "receiver-hosted-aura"]
        assert timer.delay == pytest.approx(0.12)
        timer.callback(*timer.args)
    assert {r.target_actor_id for r in struck} == {victim, beside}
    assert all(r.damage > 0 for r in struck)
    assert combat.monster_health[far] == far_health
    transactions = CharacterService._victim_effect_command_transactions(s, struck[:1])
    burns = [entry.field_18 + 1 for commands in transactions for command in commands
             for entry in command.value.entries
             if entry.has_details and effect_name(entry.effect_id) == "debuff_dot_burn"]
    assert burns == [1, 2, 3]


def test_quick_shot_proc_unlocks_only_the_owners_turret_ice_shot(quiet):
    clock = turrets.Clock()
    with patch("time.perf_counter", clock):
        s = _session(52361, 361, "Dwarf")
        s.gameplay_character.class_id = 4
        s.gameplay_combat_state = LocalCombatState(
            load_local_monster_encounter(DEFAULT_STATIC_DB_PATH, "b1201_pve", include_spawn_areas=False),
            player_actor_id=s.gameplay_remote_actor_id, player_health=1000,
        )
        s.gameplay_inventory = InventoryInfo(health_points=1000, secondary_resource=100)
        s.gameplay_chase_server_tick = 500
        s.gameplay_presence_callback = None
        s.gameplay_last_move = None
        s.gameplay_spawn_position = (0.0, 0.0, 0.0)
        s.send_reliable_ordered = Mock()
        s.gameplay_deadline_timer = lambda delay, callback, args=(), kwargs=None: SimpleNamespace(
            start=Mock(), cancel=Mock(), daemon=False, delay=delay,
            callback=callback, args=args, kwargs=kwargs or {},
        )
        combat = s.gameplay_combat_state
        turret = turrets.cast(s, "TurretCombat", turrets.anchor(s), 7001)
        encoded = "item_pw_sigrismarr_dwarf_turret_bonus_aura_t2,C:1.0,D:1.0,DP:1.0,ON:Hit"
        aura = LocalCombatState._load_victim_status_effects(db(), encoded)
        receivers = LocalCombatState._load_location_aura_effects(db(), encoded)
        victim = combat.encounter.actor_ids[0]
        combat.monster_positions[victim] = (
            turret.position[0], turret.position[1], turret.position[2] - 2.0)
        combat.start_player_location_visuals(
            aura, position=combat.monster_positions[victim], yaw=0.0, server_tick=500,
            owner_actor_id=s.gameplay_remote_actor_id, receiver_status_effects=receivers,
            now=clock.value,
        )
        _resolutions, applications = combat.apply_player_location_auras(
            now=clock.value, server_tick=500,
            player_positions={s.gameplay_remote_actor_id: turret.position},
        )
        assert [(a.target_actor_id, [d.resource_id for d in a.status_effects])
                for a in applications] == [
            (turret.actor_id, ["item_pw_sigrismarr_dwarf_turret_bonus_effect_t2"])]
        assert not combat.monster_status_effects.get(victim)
        picks = []
        for step in range(6):
            clock.value += 0.6
            combat.pending_player_summon_attacks.pop(turret.actor_id, None)
            combat.maybe_start_player_summon_attacks(
                {s.gameplay_remote_actor_id: turret.position},
                server_tick=900 + step * 15, now=max(clock.value, turret.attack_ready_at),
            )
            pending = combat.pending_player_summon_attacks.get(turret.actor_id)
            picks.append(None if pending is None else pending.geometry.resource_id)
        ice = [index for index, pick in enumerate(picks) if pick == "dwarf_CombatTowerTurret_Ice"]
        # Ready at once, then held off by its own 1.5 s CoolDown.
        assert ice[0] == 0 and len(ice) >= 2
        assert all((later - earlier) * 0.6 >= 1.5 - 1e-6 for earlier, later in zip(ice, ice[1:]))
        assert "minionskill_combatturret" in picks


def test_unequipping_a_piece_cleanses_bgh_bonus_effects(sets, sockets, quiet):
    s = wearer(sets, sockets, "ranger")
    cast(s, "thorntrail", trigger="hit")
    assert stacks(s)["bgh_ranger_bonus_heredur"] == 1
    previous = s.gameplay_inventory
    s.gameplay_inventory = InventoryInfo(
        health_points=1000, secondary_resource=100,
        items=previous.items, equipment_slots={1: (10,)},
    )
    cleanse_unequipped_items(s, previous, s.gameplay_inventory)
    assert "bgh_ranger_bonus_heredur" not in stacks(s)
