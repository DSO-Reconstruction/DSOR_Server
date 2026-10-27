"""Ranger effects must wait for their cast and projectile phases."""
from dataclasses import replace
from functools import lru_cache
from types import SimpleNamespace
from unittest.mock import patch
import math
import sqlite3
import unittest

import pytest

from dso_server.paths import DEFAULT_STATIC_DB_PATH
from dso_server.protocol.combat_protocol import BulletSkill, SkillCast, ShiftedSkill
from dso_server.runtime.transport import Session
from dso_server.services.character_server import CharacterServer
from dso_server.services import player_combat
from dso_server.tests import test_local_combat_state as combat_fixtures
from dso_server.tests import test_combat_entities as service_fixtures


@lru_cache(maxsize=None)
def skill_id(resource):
    # Static content updates may insert skills and renumber their wire IDs.
    with sqlite3.connect(f"{DEFAULT_STATIC_DB_PATH.as_uri()}?mode=ro", uri=True) as database:
        return database.execute(
            "SELECT rowid-1 FROM _Template_Skill WHERE Id=?", (resource,),
        ).fetchone()[0]


@pytest.fixture
def combat():
    fixture = combat_fixtures.LocalCombatStateTests()
    fixture.setUp()
    # These tests place actors in synthetic space, away from map obstacles.
    with patch.object(fixture.state, "_bullet_static_reach", return_value=None):
        yield fixture.state


@pytest.mark.parametrize("resource,delay", [("multishot", 0.36), ("stunshot", 1.0)])
@pytest.mark.parametrize("speed", [1.0, 2.0])
def test_scatter_and_net_wait_for_the_complete_cast(combat, resource, delay, speed):
    actor = combat.encounter.actor_ids[0]
    origin, target = (0.0, 2.0, 0.0), (0.0, 2.0, -2.0)
    combat.monster_positions[actor] = target
    combat.monster_health[actor] = 1
    frame = combat.encounter.coordinate_frame
    (pending,) = combat.start_spatial_skill_hits(
        skill_index=skill_id(resource), cast_id=321, server_tick=2000,
        player_position=frame.world_to_wire(origin), source_actor_id=1,
        target_position=frame.world_to_wire(target) if resource == "stunshot" else None,
        eligible_actor_ids=(actor,), now=100.0, attack_speed_multiplier=speed,
        player_damage_range=(1000.0, 1000.0),
    )
    expected = delay / speed if resource == "multishot" else delay
    assert pending.due_at == pytest.approx(100.0 + expected)
    assert combat.resolve_runtime_player_hit(1, 321, 0, now=100.0 + expected - 0.001) is None
    assert combat.monster_health[actor] == 1
    assert combat.resolve_runtime_player_hit(1, 321, 0, now=pending.due_at) is not None


@pytest.mark.parametrize("speed", [0.5, 1.0, 2.0, 10.0])
def test_net_damage_and_remote_animation_have_the_same_deadline(combat, speed):
    index = skill_id("stunshot")
    geometry = combat._load_player_skill_geometry(index)
    session = Session(("127.0.0.1", 41985), object())
    skill = ShiftedSkill(0, index, 0.0, 2000, 321, 0, (0.0, 0.0, -2.0))
    visual_tick, impact_phase = CharacterServer._remote_skill_impact_tick(
        session, skill, geometry, (0.0, 0.0, 0.0), 10, 1.0, None,
    )
    assert impact_phase == 15
    assert visual_tick == 2025
    assert combat.player_skill_impact_delay_seconds(index, attack_speed_multiplier=speed) == (
        visual_tick - 2000) / 25.0


@pytest.mark.parametrize("speed", [1.0, 2.0])
@pytest.mark.parametrize("timing", [1.0, 0.5])
def test_thicket_segments_follow_the_projectile_instead_of_appearing_before_it(combat, speed, timing):
    session = Session(("127.0.0.1", 41986), object())
    origin = (0.0, 2.0, 0.0)
    index = skill_id("thorntrail")
    skill = BulletSkill(0, index, 0.0, 2000, 321, (0.0, 0.0, -1.0, 0.0), 0)
    specs = combat.player_skill_bullet_location_trail(index)
    geometry = replace(combat._load_player_skill_geometry(index), skill_timing_multiplier=timing)
    with patch.object(CharacterServer, "_schedule_player_location_visuals") as schedule, \
            patch.object(combat.encounter.navigation, "project_position", side_effect=lambda p: p), \
            patch.object(combat, "_load_player_skill_geometry", return_value=geometry):
        CharacterServer._schedule_player_bullet_trail(
            session, combat, combat.encounter, skill, skill,
            combat.encounter.coordinate_frame.world_to_wire(origin), 1, specs, (10.0, 10.0),
            attack_speed_multiplier=speed, cast_started_at=100.0,
        )
    assert schedule.call_count > 1
    for call in schedule.call_args_list:
        at = call.kwargs["delay_seconds"]
        position = call.kwargs["position"]
        # Thorntrail draws for 15 frames, travels at 15 units/s, and has
        # a 0.1-unit muzzle offset. Every segment must coincide with flight.
        distance = math.dist(origin, position)
        release = 0.60 * timing / speed
        assert at >= release
        assert distance == pytest.approx(0.1 + (at - release) * 15.0 / timing, abs=1e-5)
        assert distance <= specs[2] + 1e-5
        assert call.kwargs["cast_started_at"] == 100.0


@pytest.mark.parametrize("resource,release,velocity,scales", [
    ("mightyshot", 0.36, 21.0, True), ("explosiveshot", 0.36, 21.0, True),
    ("birdofprey", 0.60, 5.5, False), ("thorntrail", 0.60, 15.0, True),
])
@pytest.mark.parametrize("speed", [1.0, 2.0])
def test_remaining_ranger_projectiles_wait_for_release_and_flight(
        combat, resource, release, velocity, scales, speed):
    origin = (0.0, 2.0, 0.0)
    actor = combat.encounter.actor_ids[0]
    combat.monster_positions[actor] = (0.0, 2.0, -8.0)
    combat.monster_health[actor] = 1
    skill = BulletSkill(0, skill_id(resource), 0.0, 2000, 321, (0.0, 0.0, -1.0, 0.0), 0)
    (pending,) = combat.start_bullet_skill_hits(
        skill, combat.encounter.coordinate_frame.world_to_wire(origin), source_actor_id=1,
        explicit_target_actor_ids=(actor,), eligible_actor_ids=(actor,), now=100.0,
        attack_speed_multiplier=speed, player_damage_range=(1000.0, 1000.0),
    )
    draw = release / speed if scales else release
    arrival = 100.0 + draw + pending.travel_distance / velocity
    assert pending.emit_delay == pytest.approx(draw)
    assert pending.due_at == pytest.approx(arrival)
    assert combat.resolve_runtime_player_hit(1, 321, 0, now=arrival - 0.001) is None
    assert combat.monster_health[actor] == 1
    assert combat.resolve_runtime_player_hit(1, 321, 0, now=pending.due_at) is not None


@pytest.mark.parametrize("resource,seconds,scaled", [
    ("stab", 0.20, True), ("whirlwind", 0.12, False), ("jump", 0.48, False),
    ("adrenalin", 0.24, False), ("mightystab", 0.20, True), ("hornetcloud", 0.32, False),
])
@pytest.mark.parametrize("speed", [0.5, 2.0])
def test_other_ranger_contact_frames_keep_their_authored_speed_policy(combat, resource, seconds, scaled, speed):
    assert combat.player_skill_impact_delay_seconds(skill_id(resource), attack_speed_multiplier=speed) == (
        pytest.approx(seconds / speed if scaled else seconds))


@pytest.mark.parametrize("resource,release,impact", [("lightningstrike", 9, 35), ("meteor", 5, 60)])
@pytest.mark.parametrize("speed", [0.5, 2.0, 10.0])
def test_shared_shifted_timing_keeps_long_impact_phase_and_visual_deadline_aligned(
        combat, resource, release, impact, speed):
    skill_index = skill_id(resource)
    geometry = combat._load_player_skill_geometry(skill_index)
    session = Session(("127.0.0.1", 41987), object())
    skill = ShiftedSkill(0, skill_index, 0.0, 2000, 321, 0, (0.0, 0.0, -2.0))
    release_frames = max(1, round(release / speed))
    tick, impact_frames = CharacterServer._remote_skill_impact_tick(
        session, skill, geometry, (0.0, 0.0, 0.0), release_frames, speed, None,
    )
    assert impact_frames == impact
    assert tick == 2000 + release_frames + impact
    assert combat.player_skill_impact_delay_seconds(skill_index, attack_speed_multiplier=speed) == (tick - 2000) / 25.0


def test_ranger_miss_lifetime_scales_with_the_projectile_timing_modifier(combat):
    index = skill_id("mightyshot")
    geometry = replace(combat._load_player_skill_geometry(index), skill_timing_multiplier=0.5)
    session = Session(("127.0.0.1", 41988), object())
    skill = BulletSkill(0, index, 0.0, 2000, 321, (0.0, 0.0, -1.0, 0.0), 0)
    tick, _ = CharacterServer._remote_skill_impact_tick(
        session, skill, geometry, (0.0, 0.0, 0.0), round(9 * 0.5), 1.0, None,
    )
    assert tick == 2000 + round((0.36 + geometry.projectile_lifetime) * 0.5 * 25)


class TrapPlacementTests(unittest.TestCase):
    def setUp(self):
        fixture = service_fixtures.MixedEntityCastTests()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        self.fixture = fixture
        self.session, self.combat = fixture.source, fixture.combat
        self.session.gameplay_skill_book_ids = {skill_id("trap")}
        self.session.gameplay_character.class_id = 2
        self.session.gameplay_presence_callback = None

    def cast(self):
        stats = SimpleNamespace(minimum_damage=10.0, maximum_damage=10.0,
                                critical_hit_chance=0.0, critical_damage_multiplier=2.0, attack_speed=1.0,
                                skill_modifier_sources=())
        with (
            patch.object(player_combat, "decode_skill", return_value=SkillCast(0, skill_id("trap"), 0.0, 200, 321)),
            patch.object(player_combat, "calculate_player_combat_stats", return_value=stats),
            patch.object(CharacterServer, "_apply_player_skill_resource", return_value=True),
            patch.object(CharacterServer, "_consume_active_essence"),
            patch.object(CharacterServer, "_start_monster_chase"),
            patch.object(CharacterServer, "_record_monster_targeting_result"),
            patch("dso_server.services.player_combat.time.perf_counter", return_value=100.0),
        ):
            CharacterServer._handle_spatial_skill(self.session, b"\x8b\x46\x00")

    def test_hunting_trap_is_created_after_placement_frame(self):
        self.cast()
        self.assertFalse(self.combat.player_traps)
        timer = next(t for t in self.fixture.timers if t.delay == 0.16)
        self.assertAlmostEqual(timer.absolute_deadline, 100.16)
        with patch("dso_server.services.player_traps.time.perf_counter", return_value=100.16):
            timer.function(*timer.args, **timer.kwargs)
        self.assertEqual(len(self.combat.player_traps), 1)
        trap = next(iter(self.combat.player_traps.values()))
        self.assertEqual(trap.server_tick, 204)

    def test_leaving_the_map_cancels_pending_trap_creation(self):
        self.cast()
        self.assertFalse(self.combat.player_traps)
        timer = next(t for t in self.fixture.timers if t.delay == 0.16)
        self.session.gameplay_chase_generation += 1
        timer.function(*timer.args, **timer.kwargs)
        self.assertFalse(self.combat.player_traps)
