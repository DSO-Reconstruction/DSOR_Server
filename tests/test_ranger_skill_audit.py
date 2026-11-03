"""Ranger casts use live skill identities and exercise damage and target state."""
from dataclasses import replace
from unittest.mock import patch

import pytest

from dso_server.protocol.combat_protocol import BulletSkill
from dso_server.gameplay.combat_entities import CombatEntity, plan_entity_skill
from dso_server.tests.test_ranger_projectile_hits import combat, skill_id
from dso_server.tests.test_item_set_events import wearer
from dso_server.services.base import CharacterService


@pytest.mark.parametrize("resource,coefficient", [
    ("stab", 1.5), ("markshot", 1.0), ("whirlwind", 1.2),
    ("mightyshot", 1.75), ("explosiveshot", 3.0),
    ("birdofprey", 2.5), ("multishot", 1.3),
    ("multishot_Chimera", 1.2), ("mightystab", 5.5),
    ("mightystab_light", 5.5), ("thorntrail", 1.0),
])
def test_damaging_ranger_skills_commit_health_damage(combat, resource, coefficient):
    actor = combat.encounter.actor_ids[0]
    combat.monster_positions[actor] = (0.0, 2.0, -1.0)
    combat.monster_last_published_positions[actor] = (0.0, 2.0, -1.0)
    combat.monster_health[actor] = 10000
    index = skill_id(resource)
    frame = combat.encounter.coordinate_frame
    common = dict(source_actor_id=1, now=100.0,
                  eligible_actor_ids=(actor,), player_damage_range=(100.0, 100.0))
    geometry = combat._load_player_skill_geometry(index)
    if geometry.skill_type in {"Ranged", "RangedTarget"}:
        pending = combat.start_bullet_skill_hits(
            BulletSkill(0, index, 0.0, 2000, 321, (0.0, 0.0, -1.0, 0.0), 0),
            frame.world_to_wire((0.0, 2.0, 0.0)), **common)
    else:
        pending = combat.start_spatial_skill_hits(
            skill_index=index, cast_id=321, server_tick=2000,
            player_position=frame.world_to_wire((0.0, 2.0, 0.0)),
            explicit_target_actor_ids=(actor,) if geometry.targeting_type == "Actor" else (),
            **common)
    assert pending
    assert pending[0].damage_modifier == pytest.approx(coefficient)
    hit = combat.resolve_runtime_player_hit(1, 321, 0, now=pending[0].due_at)
    assert hit is not None and hit.damage > 0
    assert combat.monster_health[actor] == 10000 - hit.damage


@pytest.mark.parametrize("already_marked", (False, True))
def test_marked_hit_talent_uses_target_state_before_the_hit(combat, already_marked):
    actor = combat.encounter.actor_ids[0]
    combat.monster_positions[actor] = (0.0, 2.0, -5.0)
    combat.monster_health[actor] = 10000
    index = skill_id("markshot")
    geometry = combat._load_player_skill_geometry(index)
    mark = next(e for e in geometry.victim_status_effects if e.resource_id == "debuff_cc_mark")
    talent = replace(next(e for e in geometry.victim_status_effects
                          if e.resource_id == "talent_ranger_concentration_resource_on_hit_marked"), chance=1.0)
    if already_marked:
        combat._record_monster_status_effects(actor, (mark,), now=100.0)
    pending, = combat.start_bullet_skill_hits(
        BulletSkill(0, index, 0.0, 2000, 321, (0.0, 0.0, -1.0, 0.0), 0),
        combat.encounter.coordinate_frame.world_to_wire((0.0, 2.0, 0.0)),
        source_actor_id=1, now=100.0, eligible_actor_ids=(actor,),
        player_damage_range=(10.0, 10.0))
    combat.pending_runtime_player_hits[(1, 321, 0)] = replace(
        pending, victim_status_effects=(mark, talent))
    hit = combat.resolve_runtime_player_hit(1, 321, 0, now=pending.due_at)
    assert hit is not None
    assert (talent in hit.victim_status_effects) == already_marked


@pytest.mark.parametrize("resource", ("jump", "adrenalin", "hornetcloud", "trap", "packofwolves", "summontree", "stunshot"))
def test_utility_skills_keep_their_authored_non_damage_role(combat, resource):
    geometry = combat._load_player_skill_geometry(skill_id(resource))
    if resource == "trap":
        assert combat.player_trap_definition(skill_id(resource)) is not None
    elif resource in {"packofwolves", "summontree"}:
        assert combat.player_summon_definition(skill_id(resource)) is not None
    elif resource == "jump":
        assert geometry.skill_type == "Jump"
        assert geometry.damage_modifier == 0

    elif resource == "stunshot":
        effects = {e.resource_id for e in geometry.victim_status_effects if e.chance > 0}
        assert {"debuff_cc_stun", "debuff_cc_mark"} <= effects
        assert geometry.hit_range == pytest.approx(2.75)
    else:
        effects = combat.player_skill_user_effects(skill_id(resource), cast_id=321, trigger="skillstart")
        assert effects
        assert geometry.damage_modifier == 0

def test_hornet_swarm_damages_in_radius_follows_owner_and_does_not_double_tick(combat):
    effects = combat.player_skill_user_effects(skill_id("hornetcloud"), cast_id=1, trigger="skillstart")
    aura, receivers, delay = combat.player_user_effect_location_auras(effects)[0]
    assert aura.resource_id == "skill_hornetcloud_aura"
    assert aura.aura_radius == 3.0 and delay == 0.0
    assert {e.resource_id for e in receivers} == {"skill_hornetcloud_effect"}
    actor, outside = combat.encounter.actor_ids[:2]
    combat.monster_positions[actor] = (2.0, 2.0, 0.0)
    combat.monster_positions[outside] = (8.0, 2.0, 0.0)
    combat.monster_health[actor] = combat.monster_health[outside] = 10000
    combat.start_player_location_visuals(
        (aura,), position=(0.0, 2.0, 0.0), yaw=0.0, server_tick=0,
        owner_actor_id=1, receiver_status_effects=receivers,
        player_damage_range=(100.0, 100.0), follows_owner=True,
        managed_aura=True, now=100.0)
    hits, _ = combat.apply_player_location_auras(now=100.0, player_positions={1: (0.0, 2.0, 0.0)})
    assert [(h.target_actor_id, h.damage) for h in hits] == [(actor, 75)]
    hits, _ = combat.apply_player_location_auras(now=100.5, player_positions={1: (0.0, 2.0, 0.0)})
    assert not hits  # Refreshing membership must not apply a second start hit.
    ticks = combat.resolve_due_monster_status_damage(now=101.0, server_tick=25)
    assert [(h.target_actor_id, h.damage) for h in ticks] == [(actor, 75)]
    hits, _ = combat.apply_player_location_auras(now=101.1, player_positions={1: (8.0, 2.0, 0.0)})
    assert [(h.target_actor_id, h.damage) for h in hits] == [(outside, 75)]
    ticks = combat.resolve_due_monster_status_damage(now=102.1, server_tick=50)
    assert [(h.target_actor_id, h.damage) for h in ticks] == [(outside, 75)]
    assert not combat.apply_player_location_auras(now=110.1, player_positions={1: (8.0, 2.0, 0.0)})[0]
    assert not combat.resolve_due_monster_status_damage(now=110.1, server_tick=275)


def test_adrenaline_tick_delivers_its_authored_attack_speed_buff(wearer):
    combat = wearer.gameplay_combat_state
    parent = next(e for e in combat.player_skill_user_effects(
        skill_id("adrenalin"), cast_id=1, trigger="skillstart")
        if e.resource_id == "skill_adrenalin_buff_movementspeed")
    with patch("dso_server.services.combat_resolution.time.perf_counter", return_value=100.0):
        CharacterService._send_player_skill_status_effects(
            wearer, (parent,), server_tick=200, label="adrenaline-audit")
    timer = next(t for k, t in wearer.gameplay_projectile_timers.items()
                 if k[0] == "player-status-tick")
    with patch("dso_server.services.combat_resolution.time.perf_counter", return_value=101.0), \
            patch.object(CharacterService, "_send_player_skill_status_effects") as send:
        timer.callback(*timer.args)
    assert [e.resource_id for e in send.call_args.args[1]] == ["skill_adrenalin_buff_attackspeed"]


def test_net_hits_only_inside_its_ground_radius_and_applies_stun_and_mark(combat):
    actor, outside = combat.encounter.actor_ids[:2]
    combat.monster_positions[actor] = (2.0, 2.0, -6.0)
    combat.monster_positions[outside] = (4.0, 2.0, -6.0)
    frame = combat.encounter.coordinate_frame
    pending = combat.start_spatial_skill_hits(
        skill_index=skill_id("stunshot"), cast_id=123, server_tick=200,
        player_position=frame.world_to_wire((0.0, 2.0, 0.0)),
        target_position=frame.world_to_wire((0.0, 2.0, -6.0)),
        source_actor_id=1, now=100.0, eligible_actor_ids=(actor, outside),
        player_damage_range=(100.0, 100.0))
    assert [p.target_actor_id for p in pending] == [actor]
    hit = combat.resolve_runtime_player_hit(1, 123, 0, now=pending[0].due_at)
    assert hit is not None and hit.damage == 0
    assert {"debuff_cc_stun", "debuff_cc_mark"} <= {e.resource_id for e in hit.victim_status_effects}


def test_hunting_trap_detonation_deals_damage_and_applies_its_effects(combat):
    index = skill_id("trap")
    trap = combat.player_trap_definition(index)
    actor = combat.encounter.actor_ids[0]
    combat.monster_positions[actor] = (0.0, 2.0, 0.0)
    combat.monster_health[actor] = 10000
    pending = combat.start_spatial_skill_hits(
        skill_index=index, cast_id=123, server_tick=200,
        player_position=combat.encounter.coordinate_frame.world_to_wire((0.0, 2.0, 0.0)),
        source_actor_id=1, now=100.0, eligible_actor_ids=(actor,),
        player_damage_range=(100.0, 100.0), area_radius_override=trap.trip_radius,
        impact_delay_override=0.0, additional_victim_effects=trap.victim_status_effects)
    assert pending
    hit = combat.resolve_runtime_player_hit(1, 123, 0, now=100.0)
    assert hit is not None and hit.damage > 0 and hit.victim_status_effects


def test_deadly_blow_cooldown_reset_requires_a_kill(combat):
    index = skill_id("mightystab")
    assert not combat.player_skill_user_effects(index, cast_id=123, trigger="kill", killed=False)
    effects = combat.player_skill_user_effects(index, cast_id=123, trigger="kill", killed=True)
    assert "skill_mightystab_kill_remove_cooldown" in {e.resource_id for e in effects}


@pytest.mark.parametrize("resource", ("mightystab", "mightystab_light"))
@pytest.mark.parametrize("explicit", (False, True))
def test_deadly_blow_hits_at_normal_ranger_melee_reach(combat, resource, explicit):
    actor = combat.encounter.actor_ids[0]
    index = skill_id(resource)
    geometry = combat._load_player_skill_geometry(index)
    assert geometry.attack_range == pytest.approx(3.2)
    assert geometry.hit_range == pytest.approx(3.2)

    # This distance is outside the DB's 1.75 m reach but inside the 3.2 m melee reach.
    position = (0.0, 2.0, -3.5)
    combat.monster_positions[actor] = position
    combat.monster_last_published_positions[actor] = position
    combat.monster_health[actor] = 10000
    pending = combat.start_spatial_skill_hits(
        skill_index=index, cast_id=124, server_tick=200,
        player_position=combat.encounter.coordinate_frame.world_to_wire(
            (0.0, 2.0, 0.0)),
        source_actor_id=1, explicit_target_actor_ids=(actor,) if explicit else (), now=100.0,
        eligible_actor_ids=(actor,), player_damage_range=(100.0, 100.0),
    )
    assert pending
    hit = combat.resolve_runtime_player_hit(1, 124, 0, now=pending[0].due_at)
    assert hit is not None and hit.damage > 0
    assert combat.monster_health[actor] == 10000 - hit.damage


def test_deadly_blow_does_not_hit_across_a_separate_floor(combat):
    geometry = combat._load_player_skill_geometry(skill_id("mightystab"))
    plan = plan_entity_skill(
        geometry,
        (CombatEntity(100, (0.0, 11.0, 1.0), 0.3, target_radius=0.525),),
        origin=(0.0, 0.0, 0.0), direction=(0.0, 0.0, 1.0, 0.0),
        projectile=False, explicit_targets=(100,), caster_radius=0.525,
    )
    assert not plan.impacts


def test_deadly_blow_does_not_hit_distant_or_behind_targets(combat):
    geometry = combat._load_player_skill_geometry(skill_id("mightystab"))
    # Far target (5.5m away), target behind (-1.0 in Z when facing +Z), target at wide angle (60 deg)
    distant = CombatEntity(101, (0.0, 0.0, 5.5), 0.3, target_radius=0.525)
    behind = CombatEntity(102, (0.0, 0.0, -1.5), 0.3, target_radius=0.525)
    wide_angle = CombatEntity(103, (3.0, 0.0, 1.0), 0.3, target_radius=0.525)
    in_front = CombatEntity(104, (0.0, 0.0, 2.5), 0.3, target_radius=0.525)
    plan = plan_entity_skill(
        geometry,
        (distant, behind, wide_angle, in_front),
        origin=(0.0, 0.0, 0.0), direction=(0.0, 0.0, 1.0, 0.0),
        projectile=False, explicit_targets=(), caster_radius=0.525,
    )
    hit_ids = {impact.target.actor_id for impact in plan.impacts}
    assert hit_ids == {104}


def test_whirlwind_hits_close_monsters_and_rejects_distant_monsters(combat):
    geometry = combat._load_player_skill_geometry(skill_id("whirlwind"))
    assert geometry.hit_range == pytest.approx(2.5)
    close_monster = CombatEntity(201, (1.5, 0.0, 0.0), 0.3, target_radius=0.525)
    distant_monster = CombatEntity(202, (6.0, 0.0, 0.0), 0.3, target_radius=0.525)
    plan = plan_entity_skill(
        geometry,
        (close_monster, distant_monster),
        origin=(0.0, 0.0, 0.0), direction=(0.0, 0.0, 1.0, 0.0),
        projectile=False, explicit_targets=(), caster_radius=0.525,
    )
    hit_ids = {impact.target.actor_id for impact in plan.impacts}
    assert hit_ids == {201}


def test_explosiveshot_point_explosion_does_not_hit_distant_monsters(combat):
    geometry = combat._load_player_skill_geometry(skill_id("explosiveshot"))
    assert geometry.hit_range == pytest.approx(3.5)
    point = (0.0, 0.0, 10.0)
    in_explosion = CombatEntity(301, (0.0, 0.0, 12.0), 0.3, target_radius=0.525)  # 2m from point
    distant_monster = CombatEntity(302, (0.0, 0.0, 18.0), 0.3, target_radius=0.525)  # 8m from point
    plan = plan_entity_skill(
        geometry,
        (in_explosion, distant_monster),
        origin=(0.0, 0.0, 0.0), direction=(0.0, 0.0, 1.0, 0.0),
        projectile=True, point=point, explicit_targets=(), caster_radius=0.525,
    )
    hit_ids = {impact.target.actor_id for impact in plan.impacts}
    assert hit_ids == {301}


@pytest.mark.parametrize("offset,damaged,marked", [
    (2.0, True, True), (6.0, False, True), (13.0, False, False),
])
def test_marking_wave_marks_around_the_explosion_without_widening_it(
        combat, wearer, offset, damaged, marked):
    """Explosion: 3.5 m. Marking Wave: "marks all enemies in a radius of 10
    meters around the explosion". Its 10 m aura once became the arrow's
    splash and carried the explosion as well."""
    import sqlite3
    from dso_server.protocol.gameplay_protocol import StatusEffectEntry
    with sqlite3.connect("data/db/static.db4") as db:
        talent = db.execute("SELECT rowid - 1 FROM _Template_StatusEffect "
                            "WHERE Id='ranger_talent_explosiveshot_mark'").fetchone()[0]
    wearer.gameplay_combat_state = combat
    wearer.gameplay_monster_encounter = combat.encounter
    wearer.gameplay_remote_actor_id = 1
    wearer.gameplay_persistent_status_effects[100] = StatusEffectEntry(
        talent, 100, 0, 0, 0, 0, 0, 0, 0, False, False, True, False)
    victim, other = combat.encounter.actor_ids[:2]
    for actor, position in ((victim, (0.0, 2.0, -10.0)), (other, (offset, 2.0, -10.0))):
        combat.monster_positions[actor] = position
        combat.monster_last_published_positions[actor] = position
        combat.monster_health[actor] = 10000
    pending = combat.start_bullet_skill_hits(
        BulletSkill(0, skill_id("explosiveshot"), 0.0, 2000, 321, (0.0, 0.0, -1.0, 0.0), 0),
        combat.encounter.coordinate_frame.world_to_wire((0.0, 2.0, 0.0)),
        source_actor_id=1, now=100.0, eligible_actor_ids=(victim, other),
        modifier_effect_ids=tuple(CharacterService._active_skill_modifier_sources(wearer)),
        player_damage_range=(100.0, 100.0))
    assert {p.target_actor_id for p in pending} == ({victim, other} if damaged else {victim})
    assert all("debuff_cc_mark" not in {e.resource_id for e in p.victim_status_effects}
               for p in pending)
    with patch("time.perf_counter", return_value=max(p.due_at for p in pending) + 0.01), \
            patch.object(CharacterService, "_start_monster_chase"), \
            patch.object(CharacterService, "_dispatch_location_aura_applications") as sent, \
            patch.object(CharacterService, "_dispatch_player_owned_resolutions") as dealt:
        for value in pending:
            CharacterService._resolve_player_projectile(
                wearer, wearer.gameplay_chase_generation, 321, 1, value.impact_index)
    assert not dealt.called
    assert (combat.monster_health[other] < 10000) == damaged
    assert ("debuff_cc_mark" in combat.monster_status_effects.get(other, {})) == marked
    marks = {application.target_actor_id: {e.resource_id for e in application.status_effects}
             for call in sent.call_args_list for application in call.args[1]}
    assert marks[victim] == {"debuff_cc_mark"}
    assert (marks.get(other) == {"debuff_cc_mark"}) == marked


@pytest.mark.parametrize("offset,inside", [(2.0, True), (5.0, False)])
def test_explosive_arrow_hits_then_explodes_as_its_own_hit(combat, wearer, offset, inside):
    """"Arrow will explode shortly after hitting": the monsters inside the
    3.5 m explosion take the arrow, then (DE:0.1) the explosion, not one
    summed hit at the impact."""
    wearer.gameplay_combat_state = combat
    wearer.gameplay_monster_encounter = combat.encounter
    wearer.gameplay_remote_actor_id = 1
    victim, other = combat.encounter.actor_ids[:2]
    for actor, position in ((victim, (0.0, 2.0, -10.0)), (other, (offset, 2.0, -10.0))):
        combat.monster_positions[actor] = position
        combat.monster_last_published_positions[actor] = position
        combat.monster_health[actor] = 10000
    pending = combat.start_bullet_skill_hits(
        BulletSkill(0, skill_id("explosiveshot"), 0.0, 2000, 321, (0.0, 0.0, -1.0, 0.0), 0),
        combat.encounter.coordinate_frame.world_to_wire((0.0, 2.0, 0.0)),
        source_actor_id=1, now=100.0, eligible_actor_ids=(victim, other),
        player_damage_range=(100.0, 100.0))
    assert {p.target_actor_id for p in pending} == ({victim, other} if inside else {victim})
    assert all("skill_explosiveshot_explosion_effect" not in {
        e.resource_id for e in p.victim_status_effects} for p in pending)
    with patch("time.perf_counter", return_value=max(p.due_at for p in pending) + 0.01), \
            patch.object(CharacterService, "_start_monster_chase"):
        for value in pending:
            CharacterService._resolve_player_projectile(
                wearer, wearer.gameplay_chase_generation, 321, 1, value.impact_index)
    arrow = {actor: 10000 - combat.monster_health[actor] for actor in (victim, other)}
    assert arrow[victim] > 0
    assert (arrow[other] > 0) == inside
    (timer,) = [t for key, t in wearer.gameplay_projectile_timers.items()
                if key[0] == "bullet-explosion"]
    assert timer.delay == pytest.approx(0.08)
    with patch.object(CharacterService, "_dispatch_player_owned_resolutions") as dealt:
        timer.callback(*timer.args, **timer.kwargs)
    (explosion,) = [call.args[2] for call in dealt.call_args_list]
    assert {r.target_actor_id for r in explosion} == ({victim, other} if inside else {victim})
    for resolution in explosion:
        assert resolution.damage > 0
        assert {e.resource_id for e in resolution.victim_status_effects} == {
            "skill_explosiveshot_explosion_effect"}
        assert combat.monster_health[resolution.target_actor_id] == (
            10000 - arrow[resolution.target_actor_id] - resolution.damage)


def test_a_running_mark_is_renewed_rather_than_replaced(combat):
    """AddEffect refuses a stack index at MaxStackSize: the mark's second
    ADD (index 1) was dropped and its cleanup removed the running mark."""
    import sqlite3
    from types import SimpleNamespace
    from dso_server.gameplay.local_combat_state import LocalCombatState
    with sqlite3.connect("data/db/static.db4") as db:
        mark, = LocalCombatState._load_victim_status_effects(db, "debuff_cc_mark,C:1.0,D:8.0")
        burn, = LocalCombatState._load_victim_status_effects(db, "debuff_dot_burn,C:1.0,D:3.0")
    s = SimpleNamespace(gameplay_next_status_effect_instance_id=10)
    cache = {}

    def apply(definition, now):
        return CharacterService._advance_status_effect_instance(
            s, cache, (7, int(definition.effect_id)), definition, actor_id=7,
            source_actor_id=1, server_tick=int(now * 25), applied_at=now)

    first, removed, _ = apply(mark, 100.0)
    assert first.has_details and removed is None
    again, removed, _ = apply(mark, 104.0)
    assert removed is None
    assert (again.instance_id, again.has_details, again.update_timing) == (
        first.instance_id, False, True)
    assert again.end_tick == int(104.0 * 25) + 200
    # Below its cap a stacking effect still adds an occurrence.
    apply(burn, 100.0)
    second, removed, _ = apply(burn, 100.5)
    assert second.has_details and second.stack_index == 1 and removed is not None


def test_mightystab_explosion_talent_causes_area_damage_around_struck_target(combat):
    import sqlite3
    db = sqlite3.connect("data/db/static.db4")
    row_talent = db.execute("SELECT rowid - 1 FROM _Template_StatusEffect WHERE Id='ranger_talent_mightystab_explosion'").fetchone()
    talent_id = int(row_talent[0])

    inside, outside = combat.encounter.actor_ids[:2]
    combat.monster_positions[inside] = (1.5, 2.0, 0.0)
    combat.monster_last_published_positions[inside] = (1.5, 2.0, 0.0)
    combat.monster_health[inside] = 10000

    combat.monster_positions[outside] = (8.0, 2.0, 0.0)
    combat.monster_last_published_positions[outside] = (8.0, 2.0, 0.0)
    combat.monster_health[outside] = 10000

    index = skill_id("mightystab")
    geometry = combat._load_player_skill_geometry(index)
    mod_geom = combat._modified_player_skill_geometry(geometry, (talent_id,))
    aura_def = next(v for v in mod_geom.location_visual_effects if v.resource_id == "ranger_talent_mightystab_explosion_aura")
    assert aura_def.chance == 1.0
    assert aura_def.aura_radius == 3.5

    receivers = combat.player_skill_location_receiver_effects(index, modifier_sources=(talent_id,))
    assert any(e.resource_id == "ranger_talent_mightystab_explosion_effect" for e in receivers)

    # Trigger explosion aura at origin (0, 2, 0)
    combat.start_player_location_visuals(
        (aura_def,),
        position=(0.0, 2.0, 0.0),
        yaw=0.0,
        server_tick=2000,
        owner_actor_id=1,
        cast_id=777,
        receiver_status_effects=receivers,
        source_skill_index=index,
        player_damage_range=(100.0, 100.0),
    )

    resolutions, _ = combat.apply_player_location_auras(now=100.0, server_tick=2000)
    damaged_ids = {r.target_actor_id for r in resolutions}
    assert inside in damaged_ids
    assert outside not in damaged_ids

    # 180% of 100 base damage = 180 damage
    assert [(r.target_actor_id, r.damage) for r in resolutions] == [(inside, 180)]


def test_mightystab_hit_batch_triggers_hit_explosion_aura_on_target(combat, wearer):
    import sqlite3
    from dso_server.gameplay.local_combat_state import DamageResolution
    from dso_server.protocol.gameplay_protocol import StatusEffectEntry
    from dso_server.gameplay.local_combat_state import PendingPlayerHit

    wearer.gameplay_combat_state = combat
    wearer.gameplay_monster_encounter = combat.encounter
    wearer.gameplay_remote_actor_id = 1

    db = sqlite3.connect("data/db/static.db4")
    row_talent = db.execute("SELECT rowid - 1 FROM _Template_StatusEffect WHERE Id='ranger_talent_mightystab_explosion'").fetchone()
    talent_id = int(row_talent[0])

    # Install talent into session
    talent_entry = StatusEffectEntry(talent_id, 100, 0, 0, 0, 0, 0, 0, 0, False, False, True, False)
    wearer.gameplay_persistent_status_effects[100] = talent_entry

    actor1, actor2 = combat.encounter.actor_ids[:2]
    combat.monster_positions[actor1] = (0.0, 2.0, -2.0)
    combat.monster_last_published_positions[actor1] = (0.0, 2.0, -2.0)
    combat.monster_health[actor1] = 10000

    combat.monster_positions[actor2] = (1.5, 2.0, -2.0)
    combat.monster_last_published_positions[actor2] = (1.5, 2.0, -2.0)
    combat.monster_health[actor2] = 10000

    skill_idx = skill_id("mightystab")
    pending = PendingPlayerHit(
        cast_id=888,
        target_actor_id=actor1,
        skill_index=skill_idx,
        due_at=0.0,
        impact_tick=2000,
        distance=2.0,
        travel_distance=0.0,
        emit_delay=0.0,
        victim_status_effects=(),
        source_actor_id=wearer.gameplay_remote_actor_id,
        projectile_index=0,
        impact_index=0,
        target_hit_count=1,
        user_status_effects=(),
        origin=(0.0, 0.0, -2.0),
        emitted_at=100.0,
        maximum_range=3.2,
        motion_type="Area",
        area_radius=3.2,
        damage_modifier=5.5,
        damage_type="Physical",
        player_minimum_damage=100.0,
        player_maximum_damage=100.0,
    )
    timer_key = (wearer.gameplay_remote_actor_id, 888, 0)
    combat.pending_runtime_player_hits[timer_key] = pending

    CharacterService._resolve_player_projectile(
        wearer,
        wearer.gameplay_chase_generation,
        888,
        wearer.gameplay_remote_actor_id,
        0,
    )

    # Nearby actor2 within 3.5m took explosion damage (180 damage)
    assert combat.monster_health[actor2] == 10000 - 180
