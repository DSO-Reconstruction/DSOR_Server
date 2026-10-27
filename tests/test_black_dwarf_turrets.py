"""Black Dwarf 2026 (``set_blackdwarf2026_dwarf``) drives the dwarf's turrets.

Every test uses the authored rows of the live ``static.db4`` and the real
summon, item-set and combat paths.  Client text (gui.statuseffects.xml):

* tier 2 ``set_bd2026_dwarf_2``: "All turrets deal Lightning damage and can
  apply Electrified."
* tier 3 ``set_bd2026_dwarf_3``: "When a turret has been active for 8 seconds,
  it enters Overcharged state ... damage +150%" plus one effect per type.
* tier 4 ``set_bd2026_dwarf_4``: Overload stacks from Quick Shot; 10 stacks
  overcharge every turret, 20 double their duration and add 60% attack speed
  for 8 s, 30 make the Tesla Turret absorb the others and explode for 2000%
  Lightning damage, +250% per absorbed turret.
"""

from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from dso_server.gameplay import minion_auras as rules
from dso_server.gameplay.local_combat_state import LocalCombatState, damage_type_ids
from dso_server.gameplay.local_monster_state import load_local_monster_encounter
from dso_server.paths import DEFAULT_STATIC_DB_PATH
from dso_server.persistence import readonly_db
from dso_server.protocol.inventory_codec import InventoryInfo
from dso_server.protocol.item_set_protocol import ItemSetDatabase
from dso_server.services import item_set_events, minion_auras
from dso_server.services.base import CharacterService
from dso_server.services.item_set_programs import (
    reconcile_item_set_programs, stop_item_set_effects,
)
from dso_server.tests.test_pvp_matchmaking import _session

SET_ID = "set_blackdwarf2026_dwarf"
TURRETS = ("TurretCombat", "TurretTesla", "TurretMachineGun", "TurretCoveringFire")
LIGHTNING = 3


class Clock:
    def __init__(self, value=1000.0):
        self.value = float(value)

    def __call__(self):
        return self.value


def db():
    return readonly_db.connect(DEFAULT_STATIC_DB_PATH)


def skill_index(resource_id):
    return db().execute(
        "SELECT rowid-1 FROM _Template_Skill WHERE Id=?", (resource_id,),
    ).fetchone()[0]


def effect(resource):
    return LocalCombatState._load_victim_status_effects(db(), resource)[0]


@pytest.fixture(scope="module")
def sets():
    return ItemSetDatabase(DEFAULT_STATIC_DB_PATH)


@pytest.fixture
def clock():
    value = Clock()
    with patch("time.perf_counter", value):
        yield value


@pytest.fixture
def populated():
    return False


@pytest.fixture
def owner(clock, populated):
    s = _session(52361, 361, "Black Dwarf")
    s.gameplay_character.class_id = 4
    s.gameplay_combat_state = LocalCombatState(
        load_local_monster_encounter(
            DEFAULT_STATIC_DB_PATH, "b1201_pve", include_spawn_areas=populated,
        ),
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
    with patch.object(CharacterService, "_start_monster_chase"):
        yield s


def equip(s, sets, parts, set_id=SET_ID, class_name="dwarf"):
    definition = next(x for x in sets.definitions if x.set_id == set_id)
    templates = tuple(part.split("|")[0] for part in definition.parts)[:parts]
    evaluation = sets.evaluate(templates, class_name=class_name)
    transition = ItemSetDatabase.reconcile_effects(
        evaluation, dict(s.gameplay_item_set_effects),
        actor_id=1, next_instance_id=s.gameplay_next_status_effect_instance_id,
        server_tick=s.gameplay_chase_server_tick,
    )
    s.gameplay_item_set_effects = transition.effects
    s.gameplay_item_set_evaluation = evaluation
    s.gameplay_next_status_effect_instance_id = transition.next_instance_id
    reconcile_item_set_programs(s)
    return evaluation


def anchor(s, ordinal=0):
    combat = s.gameplay_combat_state
    position = combat.monster_positions[combat.encounter.actor_ids[0]]
    return (position[0] - 6.0 - 3.0 * ordinal, position[1], position[2])


def cast(s, skill, position, cast_id, *, spawn=True):
    """The Shifted cast as player_combat runs it, then the delayed spawn.

    Skillstart user effects (the visualizer, Tesla's set-enabled trigger aura)
    land on the owner, the despawn aura is published, and the construction
    materializes through the real summon service.
    """
    combat = s.gameplay_combat_state
    index = skill_index(skill)
    sources = tuple(CharacterService._active_skill_modifier_sources(s))
    definition = combat.player_summon_definition(index, modifier_sources=sources)
    owner_id = s.gameplay_remote_actor_id
    CharacterService._send_player_skill_status_effects(
        s,
        combat.player_skill_user_effects(
            index, cast_id=cast_id, trigger="skillstart", modifier_effect_ids=sources,
        ) + CharacterService._item_set_user_proc_effects(s, index, triggers=("skillstart",)),
        server_tick=s.gameplay_chase_server_tick, label="test-cast",
    )
    CharacterService._publish_player_location_visuals(
        s, combat, definition.location_visual_effects, position=position, yaw=0.0,
        server_tick=s.gameplay_chase_server_tick, owner_actor_id=owner_id, cast_id=cast_id,
    )
    if not spawn:
        return None
    CharacterService._resolve_player_summon(
        s, s.gameplay_chase_generation, ("player-summon", owner_id, cast_id),
        definition, owner_id, position, 0.0, 100, s.gameplay_chase_server_tick,
        (30.0, 30.0), sources,
    )
    return next(x for x in combat.player_summons.values() if x.cast_id == cast_id)


def hosted(s, summon):
    state = getattr(s.gameplay_combat_state, "minion_aura_state", None)
    return set(state.effects.get(summon.actor_id, {})) if state else set()


def sync(s, clock, at):
    clock.value = at
    minion_auras.request_minion_aura_sync(s)


def attack_geometry(s, summon, now):
    """The geometry of the turret's next real attack at ``now``."""
    combat = s.gameplay_combat_state
    victim = combat.living_actor_ids[0]
    # Two metres in front: inside every turret's reach, Machine Gun's fixed
    # forward strip included (yaw 0 aims along -Z).
    combat.monster_positions[victim] = (
        summon.position[0], summon.position[1], summon.position[2] - 2.0)
    combat.pending_player_summon_attacks.pop(summon.actor_id, None)
    combat.last_player_summon_attack_at.pop(summon.actor_id, None)
    combat.maybe_start_player_summon_attacks(
        {s.gameplay_remote_actor_id: summon.position}, server_tick=900,
        now=max(now, summon.attack_ready_at),
    )
    return combat.pending_player_summon_attacks[summon.actor_id].geometry


def summon_commands(capture):
    return [command for call in capture.call_args_list for command in call.args[1]]


# --------------------------------------------------------------------------
# Authored content
# --------------------------------------------------------------------------

def test_authored_minion_auras_and_programs():
    path = DEFAULT_STATIC_DB_PATH
    tier2 = rules.load_minion_aura(path, "SetLight2026_dwarf_turret_aura_2")
    assert tier2.targets == "minion" and tier2.radius == 60.0
    assert tier2.templates == {
        "minion_coveringfireturret", "minion_machinegunturret", "minion_combatturret",
        "minion_teslaturret", "minion_combatturret_chimera",
    }
    assert [x.resource_id for x in tier2.receivers] == ["SetLight2026_dwarf_turret_aura_2_effect"]
    tesla_trig = rules.load_minion_aura(path, "SetLight2026_dwarf_buff_4_30_despawn_aura_trig")
    assert tesla_trig.templates == {"minion_teslaturret"} and not tesla_trig.receivers
    assert [x.resource_id for x in tesla_trig.causers] == [
        "SetLight2026_dwarf_buff_4_30_despawn_aura_trig_effect"]
    trig = rules.load_minion_program(path, "SetLight2026_dwarf_turret_aura_3_teslaturret_trig")
    (overheat,) = trig.starts
    assert overheat.resource_id == "SetLight2026_teslaturret_overheat"
    assert overheat.delay_ticks == 200 and overheat.duration_ticks == 1500
    double = rules.load_minion_program(path, "SetLight2026_dwarf_turretstime_double")
    assert double.group_durations == (
        ("turretdespawn", 1.0, "relative", True), ("turretvisual", 1.0, "relative", True))
    explosion = rules.load_minion_program(path, "SetLight2026_dwarf_buff_4_30_explo_aura_effect")
    (location,) = explosion.locations
    assert location.definition.delay_ticks == 5 and location.variables == ((0, "tesla2026_explo_dmg"),)
    assert explosion.done_despawn and explosion.stop_despawn
    count = rules.load_minion_program(path, "SetLight2026_tesla2026_explo_add_dmg_count")
    assert count.variables == (("tesla2026_explo_dmg", -2.5, "absolute"),)
    assert count.max_stack_size == 10
    assert rules.load_minion_program(path, "skill_turretcombat_minion_despawn_effect").done_despawn
    assert rules.load_minion_program(path, "SetLight2026_machinegunturret_overheat").end_widths == (
        (5.0, "absolute", ("minionskill_machinegunturret",)),)


# --------------------------------------------------------------------------
# Tier 2: Lightning conversion and Electrified on every turret
# --------------------------------------------------------------------------

def test_tier2_turrets_deal_lightning_and_electrify_until_unequipped(owner, sets, clock):
    equip(owner, sets, 2)
    with patch.object(CharacterService, "_send_player_summon_aura_refreshes_to") as wire:
        turrets = {name: cast(owner, name, anchor(owner), 10 + index)
                   for index, name in enumerate(TURRETS)}
    effect_id = effect("SetLight2026_dwarf_turret_aura_2_effect").effect_id
    added = {command.actor_id for command in summon_commands(wire)
             for entry in command.value.entries
             if entry.effect_id == effect_id and entry.has_details}
    assert added == {summon.actor_id for summon in turrets.values()}
    now = clock.value + 2.0
    combat_geometry = attack_geometry(owner, turrets["TurretCombat"], now)
    assert damage_type_ids(combat_geometry.damage_type) == (LIGHTNING,)
    assert {(x.resource_id, x.chance) for x in combat_geometry.victim_status_effects
            if "charge" in x.resource_id} == {
        ("debuff_cc_charge", 1.0), ("debuff_cc_charge_resis2026", 1.0)}
    # "$0:charge_resis2026" is the owner's variable: the set's -0.02 per stack.
    (resistance,) = [x for x in combat_geometry.victim_status_effects
                     if x.resource_id == "debuff_cc_charge_resis2026"]
    assert resistance.values[0] == pytest.approx(-0.02)
    machine_gun = attack_geometry(owner, turrets["TurretMachineGun"], now)
    assert damage_type_ids(machine_gun.damage_type) == (LIGHTNING,)
    # The Tesla is authored Lightning already: the conversion keeps only it.
    assert damage_type_ids(attack_geometry(owner, turrets["TurretTesla"], now).damage_type) == (
        LIGHTNING,)
    # Covering fire supports the owner; it is not in dwarf_minion_turretSkills.
    assert LIGHTNING not in damage_type_ids(
        attack_geometry(owner, turrets["TurretCoveringFire"], now).damage_type)

    with patch.object(CharacterService, "_send_player_summon_aura_refreshes_to") as wire:
        clock.value = now
        equip(owner, sets, 1)
    removed = {command.actor_id for command in summon_commands(wire)
               for entry in command.value.entries
               if entry.effect_id == effect_id and not entry.has_details}
    assert removed == added
    assert damage_type_ids(
        attack_geometry(owner, turrets["TurretCombat"], now + 1.0).damage_type) == (0,)


# --------------------------------------------------------------------------
# Tier 3: the eight-second overheat per turret type
# --------------------------------------------------------------------------

def test_tier3_overheat_starts_eight_seconds_after_the_turret(owner, sets, clock):
    equip(owner, sets, 3)
    start = clock.value
    turrets = {name: cast(owner, name, anchor(owner), 20 + index)
               for index, name in enumerate(TURRETS)}
    for name, overheat in (("TurretCombat", "SetLight2026_combatturret_overheat"),
                           ("TurretTesla", "SetLight2026_teslaturret_overheat"),
                           ("TurretMachineGun", "SetLight2026_machinegunturret_overheat"),
                           ("TurretCoveringFire", "SetLight2026_coveringfireturret_overheat")):
        assert overheat not in hosted(owner, turrets[name])
    sync(owner, clock, start + 7.9)
    assert "SetLight2026_combatturret_overheat" not in hosted(owner, turrets["TurretCombat"])
    sync(owner, clock, start + 8.0)
    assert "SetLight2026_combatturret_overheat" in hosted(owner, turrets["TurretCombat"])
    assert "SetLight2026_teslaturret_overheat" in hosted(owner, turrets["TurretTesla"])
    assert "SetLight2026_machinegunturret_overheat" in hosted(owner, turrets["TurretMachineGun"])
    assert "SetLight2026_coveringfireturret_overheat" in hosted(owner, turrets["TurretCoveringFire"])
    # The permanent tier-3 aura holds its Deny trigger: no second timer.
    sync(owner, clock, start + 9.0)
    state = owner.gameplay_combat_state.minion_aura_state
    assert not [x for x in state.pending if x.actor_id == turrets["TurretCombat"].actor_id]

    now = start + 8.5
    combat_turret = attack_geometry(owner, turrets["TurretCombat"], now)
    assert combat_turret.impact_count == 3
    assert combat_turret.damage_modifier == pytest.approx(1.2 + 1.5)
    tesla = attack_geometry(owner, turrets["TurretTesla"], now)
    assert tesla.damage_modifier == pytest.approx(1.5 + 1.5)
    assert any(x.resource_id == "tesla_charge_add_SetLight2026" and x.chance == 1.0
               for x in tesla.victim_status_effects)
    machine_gun = attack_geometry(owner, turrets["TurretMachineGun"], now)
    assert machine_gun.end_width == pytest.approx(2.25 + 5.0)
    assert machine_gun.damage_modifier == pytest.approx(0.5 + 1.5)
    covering = attack_geometry(owner, turrets["TurretCoveringFire"], now)
    assert any(x.resource_id == "minionskill_coveringfireturret_buff_SetLight2026"
               for x in covering.victim_status_effects)


@pytest.mark.parametrize("populated", [True])
def test_overcharged_combat_turret_bullet_penetrates_three_enemies(owner, sets, clock):
    equip(owner, sets, 3)
    combat = owner.gameplay_combat_state
    start = clock.value
    turret = cast(owner, "TurretCombat", anchor(owner), 30)
    sync(owner, clock, start + 8.0)
    victims = tuple(combat.living_actor_ids)[:4]
    for distance, actor_id in enumerate(victims, start=2):
        combat.monster_positions[actor_id] = (
            turret.position[0], turret.position[1], turret.position[2] - 1.5 * distance)
    for actor_id in tuple(combat.living_actor_ids):
        if actor_id not in victims:
            combat.monster_health[actor_id] = 0
    health = {actor_id: combat.monster_health[actor_id] for actor_id in victims}
    now = start + 9.0
    combat.maybe_start_player_summon_attacks({}, server_tick=900, now=now)
    pending = combat.pending_player_summon_attacks[turret.actor_id]
    assert pending.target_actor_ids == victims[:3]
    for _ in range(4):
        combat.resolve_due_player_summon_attacks(now=now + 2.0)
    assert [combat.monster_health[x] < health[x] for x in victims] == [True, True, True, False]


def test_overheat_timer_is_bound_to_its_turret_and_set(owner, sets, clock):
    equip(owner, sets, 3)
    start = clock.value
    first = cast(owner, "TurretCombat", anchor(owner), 40)
    clock.value = start + 5.0
    # Recasting replaces the turret (SummonMonsterLimit 1).
    second = cast(owner, "TurretCombat", anchor(owner), 41)
    assert first.actor_id not in owner.gameplay_combat_state.player_summons
    sync(owner, clock, start + 8.0)
    assert "SetLight2026_combatturret_overheat" not in hosted(owner, second)
    sync(owner, clock, start + 13.0)
    assert "SetLight2026_combatturret_overheat" in hosted(owner, second)

    # Unequipping before the deadline stops the trigger and drops its timer.
    clock.value = start + 14.0
    third = cast(owner, "TurretTesla", anchor(owner), 42)
    clock.value = start + 15.0
    equip(owner, sets, 2)
    sync(owner, clock, start + 23.0)
    assert "SetLight2026_teslaturret_overheat" not in hosted(owner, third)
    assert "SetLight2026_dwarf_turret_aura_3_teslaturret_trig" not in hosted(owner, third)


# --------------------------------------------------------------------------
# Tier 4: Overload stacks from Quick Shot
# --------------------------------------------------------------------------

def test_quick_shot_overload_is_carried_once_by_quick_shot_itself(owner, sets):
    """``modVE,SimpleShot,SetLight2026_dwarf_SimpleShot_trig,C:0.4`` enables
    Quick Shot's own C:0.0 entry; it is not a second, independent set proc.
    The Causer-hosted entry lands on the shooter (see
    test_black_dwarf_combat_resolution)."""
    equip(owner, sets, 4)
    combat = owner.gameplay_combat_state
    index = skill_index("SimpleShot")
    sources = tuple(CharacterService._active_skill_modifier_sources(owner))
    geometry = combat._modified_player_skill_geometry(
        combat._load_player_skill_geometry(index), sources)
    (overload,) = [x for x in geometry.victim_status_effects
                   if x.resource_id == "SetLight2026_dwarf_SimpleShot_trig"]
    assert overload.chance == pytest.approx(0.4) and overload.effect_hosts == "Causer"
    with patch("dso_server.gameplay.item_set_procs.random.random", return_value=0.0):
        procs = CharacterService._item_set_procs_for_skill(owner, index)
    assert not [x for group in procs.values() for x in group
                if x.resource_id == "SetLight2026_dwarf_SimpleShot_trig"]


def test_ten_stacks_overcharge_every_existing_turret_at_once(owner, sets, clock):
    equip(owner, sets, 4)
    turrets = {name: cast(owner, name, anchor(owner), 50 + index)
               for index, name in enumerate(TURRETS)}
    clock.value += 1.0
    dispatched = clock.value
    item_set_events.dispatch_item_set_event(owner, "maxstacksizesimpleshot10", server_tick=600)
    assert "SetLight2026_combatturret_overheat" in hosted(owner, turrets["TurretCombat"])
    assert "SetLight2026_teslaturret_overheat" in hosted(owner, turrets["TurretTesla"])
    assert "SetLight2026_machinegunturret_overheat" in hosted(owner, turrets["TurretMachineGun"])
    assert "SetLight2026_coveringfireturret_overheat" in hosted(owner, turrets["TurretCoveringFire"])
    # After the 10 s aura the overheat runs its own 60 s from the aura's end.
    sync(owner, clock, dispatched + 10.5)
    state = owner.gameplay_combat_state.minion_aura_state
    overheat = state.effects[turrets["TurretCombat"].actor_id]["SetLight2026_combatturret_overheat"]
    assert overheat.held_by is None
    assert overheat.expires_at == pytest.approx(dispatched + 10.0 + 60.0)


def test_twenty_stacks_double_turret_lifetime_and_speed_for_eight_seconds(owner, sets, clock):
    equip(owner, sets, 4)
    combat = owner.gameplay_combat_state
    start = clock.value
    turret = cast(owner, "TurretCombat", anchor(owner), 60)
    machine_gun = cast(owner, "TurretMachineGun", anchor(owner, 1), 61)
    assert turret.expires_at == pytest.approx(start + 10.0)
    clock.value = start + 2.0
    item_set_events.dispatch_item_set_event(owner, "maxstacksizesimpleshot20", server_tick=600)
    assert combat.player_summons[turret.actor_id].expires_at == pytest.approx(start + 20.0)
    assert combat.player_summons[machine_gun.actor_id].expires_at == pytest.approx(start + 40.0)
    timer = owner.gameplay_projectile_timers[("player-summon-expire", turret.actor_id)]
    assert timer.delay == pytest.approx(18.0)
    visual = next(x for x in combat.player_location_visuals.values() if x.cast_id == 60)
    assert visual.expires_at == pytest.approx(start + 20.0)
    # The owner's TurretVisual visualizer follows the same live bonus.
    visualizer = effect("dwarf_summon_visualizer_CombatTurret").effect_id
    assert [expires for entry, expires in owner.gameplay_runtime_status_effects.values()
            if entry.effect_id == visualizer] == [pytest.approx(start + 20.0)]
    assert combat._summon_status_scale(turret.actor_id, "Speed", "Skills",
                                       now=start + 3.0) == pytest.approx(1.6)
    sync(owner, clock, start + 10.0)
    assert "SetLight2026_dwarf_buff_4_20_aura_effect" not in hosted(owner, turret)
    assert combat._summon_status_scale(turret.actor_id, "Speed", "Skills",
                                       now=start + 10.0) == pytest.approx(1.0)
    # RunTimeUpdate: when the 20 s bonus ends, running turrets revert.
    clock.value = start + 22.0
    double = effect("SetLight2026_dwarf_turretstime_double")
    with patch.object(CharacterService, "_expire_player_summon") as expire:
        stop_item_set_effects(owner, (double.effect_id,), server_tick=900, reason="done")
    assert {call.args[3] for call in expire.call_args_list} >= {machine_gun.actor_id}


def test_thirty_stacks_tesla_absorbs_turrets_and_explodes(owner, sets, clock):
    equip(owner, sets, 4)
    combat = owner.gameplay_combat_state
    start = clock.value
    tesla_position = anchor(owner)
    feedback = effect("SetLight2026_dwarf_buff_4_30_despawn_aura_trig_effect").effect_id
    trig = effect("SetLight2026_dwarf_buff_4_30_despawn_aura_trig").effect_id

    def running(effect_id):
        return any(entry.effect_id == effect_id
                   for entry, _ in owner.gameplay_runtime_status_effects.values())

    # Casting Tesla puts its set-enabled TurretDespawn aura on the owner; the
    # feedback exists only while that aura admits a living Tesla Turret.
    cast(owner, "TurretTesla", tesla_position, 69, spawn=False)
    assert running(trig) and not running(feedback)
    tesla = cast(owner, "TurretTesla", tesla_position, 70)
    assert running(feedback)
    absorbed = [cast(owner, "TurretCombat", anchor(owner, 1), 71),
                cast(owner, "TurretMachineGun", anchor(owner, 2), 72)]
    victim = combat.encounter.actor_ids[0]
    combat.monster_positions[victim] = (tesla.position[0] + 1.0, *tesla.position[1:])
    health = combat.monster_health[victim]

    clock.value = start + 1.0
    item_set_events.dispatch_item_set_event(owner, "maxstacksizesimpleshot30", server_tick=600)
    for summon in absorbed:
        assert "SetLight2026_dwarf_buff_4_30_despawn_aura_effect" in hosted(owner, summon)
    assert "SetLight2026_dwarf_buff_4_30_despawn_aura_effect" not in hosted(owner, tesla)
    state = combat.minion_aura_state
    assert state.effects[tesla.actor_id]["SetLight2026_tesla2026_explo_add_dmg_count"].stack_count == 2
    assert rules.minion_actor_variable(
        combat, tesla.actor_id, "tesla2026_explo_dmg", now=clock.value) == pytest.approx(-25.0)

    delayed = [timer for key, timer in owner.gameplay_projectile_timers.items()
               if key[0] == "item-set-event-delay"]
    assert sorted(timer.delay for timer in delayed) == [0.48, 0.48]
    clock.value = start + 1.5
    for timer in delayed:
        timer.callback(*timer.args, **timer.kwargs)
    assert "SetLight2026_dwarf_buff_4_30_explo_aura_effect" in hosted(owner, tesla)

    with patch.object(CharacterService, "_publish_player_location_visuals") as published, \
         patch.object(CharacterService, "_dispatch_player_owned_resolutions") as dispatched:
        sync(owner, clock, start + 1.6)       # despawn effects complete
        for summon in absorbed:
            assert summon.actor_id not in combat.player_summons
        assert tesla.actor_id in combat.player_summons
        sync(owner, clock, start + 1.7)       # explosion placed after DE 0.2
        (location,) = [call.args[2][0] for call in published.call_args_list]
        assert location.resource_id == "SetLight2026_dwarf_tesla_explosion_aura"
        assert location.values[0] == pytest.approx(-25.0)
        sync(owner, clock, start + 2.1)       # the Tesla's own Done: Despawn
        assert tesla.actor_id not in combat.player_summons
        assert not any(entry.effect_id == feedback
                       for entry, _ in owner.gameplay_runtime_status_effects.values())
        sync(owner, clock, start + 2.7)       # the 1 s explosion ends
    (resolutions,) = [call.args[2] for call in dispatched.call_args_list]
    (hit,) = [x for x in resolutions if x.target_actor_id == victim]
    # 2000% + 2 x 250% of the owner's 30 damage, as Lightning.
    assert hit.damage == min(health, 750)
    assert hit.hit_tags == (3,)


def test_thirty_stacks_without_a_tesla_absorbs_nothing(owner, sets, clock):
    equip(owner, sets, 4)
    turret = cast(owner, "TurretCombat", anchor(owner), 80)
    clock.value += 1.0
    item_set_events.dispatch_item_set_event(owner, "maxstacksizesimpleshot30", server_tick=600)
    assert "SetLight2026_dwarf_buff_4_30_despawn_aura_effect" not in hosted(owner, turret)
    sync(owner, clock, clock.value + 1.0)
    assert turret.actor_id in owner.gameplay_combat_state.player_summons


# --------------------------------------------------------------------------
# Visibility and cleanup
# --------------------------------------------------------------------------

def test_turret_effects_reach_every_viewer_through_presence(owner, sets, clock):
    events = []
    owner.gameplay_presence_callback = lambda session, event: events.append(event)
    equip(owner, sets, 2)
    turret = cast(owner, "TurretCombat", anchor(owner), 90)
    refreshes = [event for event in events
                 if isinstance(event, tuple) and event[0] == "player_summon_aura_refresh"]
    assert any(command.actor_id == turret.actor_id for event in refreshes for command in event[1])


def test_owner_leaving_the_map_and_stale_timers_leave_no_state(owner, sets, clock):
    equip(owner, sets, 3)
    combat = owner.gameplay_combat_state
    turret = cast(owner, "TurretCombat", anchor(owner), 95)
    key = (minion_auras.SYNC_TIMER, owner.gameplay_remote_actor_id)
    timer = owner.gameplay_projectile_timers[key]
    combat.expire_player_summons_for_owner(owner.gameplay_remote_actor_id)
    state = combat.minion_aura_state
    assert turret.actor_id not in state.effects
    assert owner.gameplay_remote_actor_id not in state.owner_auras
    assert not state.pending
    owner.gameplay_chase_generation += 1
    with patch.object(rules, "advance_owner_minion_auras") as advance:
        timer.callback(*timer.args, **timer.kwargs)
    advance.assert_not_called()


def test_warrior_defiance_hammer_ends_half_a_second_after_its_despawn_aura(owner, sets, clock):
    """The warrior row of the same set: ``skill_defiance_ham_despawn_aura``.

    Defiance's tier-3 hammer minion (template LifeTime 0) lives as long as the
    warrior's 10 s ``AuraTargets:Minion`` despawn aura holds its 0.5 s
    ``Done: Despawn`` child.
    """
    equip(owner, sets, 3, "set_blackdwarf2026_warrior", "warrior")
    combat = owner.gameplay_combat_state
    start = clock.value
    owner.gameplay_spawn_position = anchor(owner)
    index = skill_index("defiance")
    sources = tuple(CharacterService._active_skill_modifier_sources(owner))
    CharacterService._send_player_skill_status_effects(
        owner,
        combat.player_skill_user_effects(
            index, cast_id=7, trigger="skillstart", modifier_effect_ids=sources,
        ) + CharacterService._item_set_user_proc_effects(owner, index, triggers=("skillstart",)),
        server_tick=owner.gameplay_chase_server_tick, label="test-defiance",
    )
    spawns = [timer for key, timer in owner.gameplay_projectile_timers.items()
              if key[0] == "socket-status-minion"]
    assert spawns
    for timer in spawns:
        timer.callback(*timer.args, **timer.kwargs)
    (hammer,) = [x for x in combat.player_summons.values()
                 if x.definition.monster_template_id == "minion_SetLight2026_warrior_ham"]
    assert hammer.expires_at is None
    assert "skill_defiance_ham_despawn_effect" in hosted(owner, hammer)
    with patch.object(CharacterService, "_expire_player_summon") as expire:
        sync(owner, clock, start + 10.4)
        expire.assert_not_called()
        sync(owner, clock, start + 10.6)
    assert [call.args[3] for call in expire.call_args_list] == [hammer.actor_id]
