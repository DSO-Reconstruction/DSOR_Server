"""The dwarf's Bomb lands, then explodes where it lies after its fuse.

``Grenade`` (RangedTargetPoint, DamageModifier 0) deals nothing itself: its
bullet comes down (``dwarf_grenade_impact``, the fuse) and
``skill_grenade_damage_aura`` (D:0, DE:1.2, 2.5 m) goes off there, the
client drawing ``dwarf_grenade_explode`` when that aura ends.
"""
from functools import lru_cache
import sqlite3
from unittest.mock import patch

import pytest

from dso_server.paths import DEFAULT_STATIC_DB_PATH
from dso_server.protocol.combat_protocol import (
    TARGET_POINT_BULLET_SKILL_COMMAND, BulletSkill, TargetPointBulletSkill,
)
from dso_server.protocol.gameplay_protocol import decode_new_location_effect_batch
from dso_server.services.base import CharacterService
from dso_server.services import player_combat
from dso_server.tests import test_combat_entities as cast_fixtures
from dso_server.tests.test_ranger_projectile_hits import combat, skill_id
from dso_server.tests.test_item_set_events import wearer

ORIGIN = (0.0, 2.0, 0.0)
POINT = (0.0, 2.0, -10.0)
CAST = 321


@lru_cache(maxsize=None)
def effect_id(resource):
    with sqlite3.connect(f"{DEFAULT_STATIC_DB_PATH.as_uri()}?mode=ro", uri=True) as database:
        return database.execute(
            "SELECT rowid-1 FROM _Template_StatusEffect WHERE Id=?", (resource,),
        ).fetchone()[0]


@pytest.fixture
def thrower(combat, wearer):
    wearer.gameplay_combat_state = combat
    wearer.gameplay_monster_encounter = combat.encounter
    wearer.gameplay_remote_actor_id = 1
    return wearer


def place(combat, actor, position):
    combat.monster_positions[actor] = position
    combat.monster_last_published_positions[actor] = position
    combat.monster_health[actor] = 10000


def throw(combat, wearer, *, point=POINT, modifiers=(), eligible=()):
    frame = combat.encounter.coordinate_frame
    index = skill_id("Grenade")
    skill = BulletSkill(0, index, 0.0, 2000, CAST, (0.0, 0.0, -1.0, 0.0), 0)
    common = dict(modifier_sources=modifiers, eligible_actor_ids=eligible)
    plan = combat.plan_entity_skill_hits(
        skill_index=index, player_position=frame.world_to_wire(ORIGIN),
        projectile=True, direction=skill.direction,
        target_position=frame.world_to_wire(point), **common)
    pending = combat.start_bullet_skill_hits(
        skill, frame.world_to_wire(ORIGIN), hit_plan=plan, source_actor_id=1,
        now=100.0, target_position=frame.world_to_wire(point),
        modifier_effect_ids=modifiers, eligible_actor_ids=eligible,
        player_damage_range=(100.0, 100.0))
    CharacterService._schedule_point_bullet_landing(
        wearer, combat, plan, skill,
        player_position=frame.world_to_wire(ORIGIN),
        target_position=frame.world_to_wire(point), owner_actor_id=1,
        player_damage_range=(100.0, 100.0), cast_started_at=100.0)
    return plan, pending


def timers(wearer, kind):
    return {key: timer for key, timer in wearer.gameplay_projectile_timers.items()
            if key[0] == kind}


def fire(timer):
    timer.callback(*timer.args, **timer.kwargs)


def published(wearer):
    return [entry for call in wearer.send_reliable_ordered.call_args_list
            if call.kwargs.get("label") == "player-location-effect-new"
            for entry in decode_new_location_effect_batch(*call.args[0]).entries]


SMALL = 0.5249761899362675  # the gnomes' ActorShape; their capsule is 0.3


@pytest.mark.parametrize("walked_to,caught", [
    (1.5, True), (6.0, False),
    # The client's radius query adds the actor shape, not the capsule.
    (2.5 + SMALL - 0.05, True), (2.5 + SMALL + 0.05, False),
])
def test_the_bomb_explodes_where_it_landed_after_its_fuse(
        combat, thrower, walked_to, caught):
    inside, walker = combat.encounter.actor_ids[:2]
    place(combat, inside, (1.0, 2.0, -10.0))
    place(combat, walker, (9.0, 2.0, -10.0))
    plan, pending = throw(combat, thrower, eligible=(inside, walker))

    # Landing deals nothing: the explosion is not part of any impact.
    assert {p.target_actor_id for p in pending} == {inside}
    assert all("skill_grenade_damage_effect" not in {
        e.resource_id for e in p.victim_status_effects} for p in pending)
    with patch("time.perf_counter", return_value=max(p.due_at for p in pending) + 0.01), \
            patch.object(CharacterService, "_start_monster_chase"):
        for value in pending:
            CharacterService._resolve_player_projectile(
                thrower, thrower.gameplay_chase_generation, CAST, 1, value.impact_index)
    assert combat.monster_health[inside] == 10000

    (landing,) = timers(thrower, "point-bullet-landing").values()
    assert landing.delay == pytest.approx(plan.impacts[0].delay)
    assert landing.absolute_deadline == pytest.approx(100.0 + plan.impacts[0].delay)
    fire(landing)

    # The aura goes out for its Done sequence, a tick long, after its fuse.
    (aura,) = published(thrower)
    landing_tick = landing.kwargs["server_tick"]
    assert aura.effect_resource_id == "skill_grenade_damage_aura"
    assert aura.position == pytest.approx(POINT)
    assert (aura.starts_tick, aura.expires_tick) == (landing_tick + 30, landing_tick + 31)
    visual, = combat.active_player_location_visuals()
    assert visual.receiver_status_effects == ()

    (fuse,) = timers(thrower, "bullet-explosion").values()
    assert fuse.delay == pytest.approx(1.2)
    place(combat, walker, (walked_to, 2.0, -10.0))  # during the fuse
    with patch.object(CharacterService, "_dispatch_player_owned_resolutions") as dealt:
        fire(fuse)
    (explosion,) = [call.args[2] for call in dealt.call_args_list]
    assert {r.target_actor_id for r in explosion} == (
        {inside, walker} if caught else {inside})
    for resolution in explosion:
        assert resolution.damage > 0
        assert resolution.server_tick == landing_tick + 30
        assert {e.resource_id for e in resolution.victim_status_effects} == {
            "skill_grenade_damage_effect"}
        assert combat.monster_health[resolution.target_actor_id] == 10000 - resolution.damage
    assert (combat.monster_health[walker] < 10000) == caught


@pytest.fixture
def caster():
    value = cast_fixtures.MixedEntityCastTests()
    value.setUp()
    yield value
    value.doCleanups()


@pytest.mark.parametrize("ahead,armed", [(6.0, True), (25.0, False)])
def test_a_cast_bomb_is_armed_for_where_it_lands(caster, ahead, armed):
    """The cast handler arms the landing, monster or player there or not."""
    from types import SimpleNamespace
    index = skill_id("Grenade")
    caster.source.gameplay_skill_book_ids = {index}
    point = (25.0, 0.0, 50.0 + ahead)
    skill = TargetPointBulletSkill(0, index, 0.0, 200, 88, (0.0, 0.0, 1.0, 0.0), 0, point)
    stats = SimpleNamespace(minimum_damage=10.0, maximum_damage=10.0,
                            critical_hit_chance=0.0, critical_damage_multiplier=2.0,
                            attack_speed=1.0, skill_modifier_sources=(),
                            rift_force_echo_chance=0.0, rift_force_echo_multiplier=0.0)
    with patch.object(player_combat, "decode_target_point_bullet_skill", return_value=skill), \
            patch.object(player_combat, "calculate_player_combat_stats", return_value=stats), \
            patch.object(CharacterService, "_apply_player_skill_resource", return_value=True), \
            patch.object(CharacterService, "_start_monster_chase"), \
            patch.object(CharacterService, "_record_monster_targeting_result"), \
            patch.object(CharacterService, "_send_remote_player_skill"):
        CharacterService._handle_bullet_skill(
            caster.source, b"\x8b" + TARGET_POINT_BULLET_SKILL_COMMAND.to_bytes(2, "little"))
    landings = [timer for timer in caster.timers
                if timer.function == CharacterService._land_point_bullet]
    assert len(landings) == int(armed)
    if armed:
        (landing,) = landings
        assert landing.kwargs["position"] == pytest.approx(point)
        assert landing.kwargs["cast_id"] == 88


@pytest.mark.parametrize("health", ["full", 150])
def test_a_damage_doll_shows_the_whole_explosion(combat, thrower, health):
    """A training dummy resets to full health on a lethal hit. The explosion
    reported ``previous - current`` = 0 there and sent no Hit: a0201_basicarena,
    874739 base damage against the dolls' 1,000,000 health, always full."""
    from dataclasses import replace
    doll = combat.encounter.actor_ids[0]
    place(combat, doll, (1.0, 2.0, -10.0))
    combat.monsters[doll] = replace(
        combat.monsters[doll], template_id="damage_doll", maximum_health=150)
    combat.monster_health[doll] = 150 if health == "full" else health
    if health != "full":
        combat.monsters[doll] = replace(combat.monsters[doll], maximum_health=1000000)
    throw(combat, thrower, eligible=(doll,))
    fire(*timers(thrower, "point-bullet-landing").values())
    with patch.object(CharacterService, "_dispatch_player_owned_resolutions") as dealt:
        fire(*timers(thrower, "bullet-explosion").values())
    ((hit,),) = [call.args[2] for call in dealt.call_args_list]
    assert (hit.damage, hit.killed) == (200, False)
    assert combat.monster_health[doll] == combat.monsters[doll].maximum_health


def test_a_hit_reports_what_it_took_except_from_a_doll(combat):
    from dataclasses import replace
    from dso_server.gameplay.local_combat_state import _reported_monster_damage
    monster = combat.monsters[combat.encounter.actor_ids[0]]
    doll = replace(monster, template_id="damage_doll_lvl_50")
    assert _reported_monster_damage(monster, 500, 300, 200) == 200
    assert _reported_monster_damage(monster, 150, 0, 200) == 150  # a kill: what was left
    assert _reported_monster_damage(doll, 150, 1000000, 200) == 200  # reset from 150
    assert _reported_monster_damage(doll, 1000000, 1000000, 1750000) == 1750000  # at full


def test_a_bomb_thrown_at_empty_ground_still_explodes(combat, thrower):
    plan, pending = throw(combat, thrower)
    assert not pending
    fire(*timers(thrower, "point-bullet-landing").values())
    assert [entry.effect_resource_id for entry in published(thrower)] == [
        "skill_grenade_damage_aura"]
    assert len(timers(thrower, "bullet-explosion")) == 1


def test_a_point_beyond_the_bombs_range_throws_nothing(combat, thrower):
    throw(combat, thrower, point=(0.0, 2.0, -30.0))
    assert not timers(thrower, "point-bullet-landing")


@pytest.mark.parametrize("talent,fuses", [
    (None, {"skill_grenade_damage_aura": 1.2}),
    # Explosive Dwarf: DE:-0.5<relative, with the fast impact sequence.
    ("dwarf_talent_explosive_dwarf", {"skill_grenade_damage_aura": 0.6}),
    ("dwarf_talent_frozen_master", {
        "skill_grenade_damage_aura": 1.2,
        "skill_grenade_frost_explosion_aura": 1.2,
        "skill_all_frost_explosion_vfx": 1.2,
    }),
])
def test_dwarf_talents_shape_the_explosion(combat, thrower, talent, fuses):
    modifiers = (effect_id(talent),) if talent else ()
    throw(combat, thrower, modifiers=modifiers)
    fire(*timers(thrower, "point-bullet-landing").values())
    assert {entry.effect_resource_id for entry in published(thrower)} == set(fuses)
    by_effect = {key[3]: timer for key, timer in timers(thrower, "bullet-explosion").items()}
    assert {resource: by_effect[effect_id(resource)].delay for resource in fuses} == (
        pytest.approx(fuses))


def test_the_frost_explosion_goes_off_once_with_the_bomb(combat, thrower):
    """Frozen Master raises the frost aura's chance above its authored 0,
    which also made each landing impact set it off on its monster."""
    from dso_server.protocol.gameplay_protocol import StatusEffectEntry
    victim = combat.encounter.actor_ids[0]
    place(combat, victim, (1.0, 2.0, -10.0))
    thrower.gameplay_persistent_status_effects[100] = StatusEffectEntry(
        effect_id("dwarf_talent_frozen_master"), 100, 0, 0, 0, 0, 0, 0, 0,
        False, False, True, False)
    _plan, pending = throw(
        combat, thrower, eligible=(victim,),
        modifiers=tuple(CharacterService._active_skill_modifier_sources(thrower)))
    assert pending
    with patch("time.perf_counter", return_value=max(p.due_at for p in pending) + 0.01), \
            patch.object(CharacterService, "_start_monster_chase"), \
            patch.object(CharacterService, "_schedule_player_location_visuals") as at_hit:
        for value in pending:
            CharacterService._resolve_player_projectile(
                thrower, thrower.gameplay_chase_generation, CAST, 1, value.impact_index)
    assert not at_hit.called
    assert combat.monster_health[victim] == 10000
    assert not timers(thrower, "bullet-explosion")
    fire(*timers(thrower, "point-bullet-landing").values())
    received = {}
    with patch.object(CharacterService, "_dispatch_player_owned_resolutions") as dealt:
        for key, fuse in timers(thrower, "bullet-explosion").items():
            fire(fuse)
    for call in dealt.call_args_list:
        for resolution in call.args[2]:
            received.setdefault(resolution.target_actor_id, set()).update(
                e.resource_id for e in resolution.victim_status_effects)
    assert received == {victim: {
        "skill_grenade_damage_effect", "skill_grenade_frost_explosion_effect"}}


def test_other_bullets_keep_their_own_explosions(combat):
    grenade = combat._load_player_skill_geometry(skill_id("Grenade"))
    arrow = combat._load_player_skill_geometry(skill_id("explosiveshot"))
    assert combat.delayed_bullet_explosions(grenade) == ()
    assert combat.point_bullet_explosions(arrow) == ()
    assert [e.resource_id for e in combat.delayed_bullet_explosions(arrow)] == [
        "skill_explosiveshot_explosion_aura"]


def test_a_visual_only_aura_gives_nothing_to_whoever_stands_in_it(combat):
    victim = combat.encounter.actor_ids[0]
    place(combat, victim, (0.5, 2.0, -10.0))
    grenade = combat._load_player_skill_geometry(skill_id("Grenade"))
    aura, = combat.point_bullet_explosions(grenade)
    combat.start_player_location_visuals(
        (aura,), position=POINT, yaw=0.0, server_tick=2000, owner_actor_id=1,
        visual_only=True, now=100.0)
    hits, applications = combat.apply_player_location_auras(now=100.0, server_tick=2000)
    assert not hits and not applications
    assert combat.monster_health[victim] == 10000


def test_a_delay_modifier_moves_an_effects_start():
    """``skill_warshout_lvl``'s ``modLE,earthquake,skill_earthquake_end,
    DE:2.0<absolute`` goes with the 2 s it adds to the quake itself."""
    from dso_server.gameplay.local_combat_state import LocalCombatState
    with sqlite3.connect(DEFAULT_STATIC_DB_PATH) as database:
        end, = LocalCombatState._load_victim_status_effects(
            database, "skill_earthquake_end,C:1.0,D:2.0,DP:2.0,DE:7.9")
        merged = LocalCombatState._merge_status_effect_modifier(
            object.__new__(LocalCombatState), database, end, end,
            "skill_earthquake_end,DE:2.0<absolute", 1)
    assert (end.delay_ticks, merged.delay_ticks) == (198, 248)
    assert merged.duration_ticks == end.duration_ticks
