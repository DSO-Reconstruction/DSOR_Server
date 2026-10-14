"""Regressions drawn from the ExamplePlayer live lag report."""
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from dso_server.runtime.gameplay_scheduler import GameplayDeadlineScheduler

from dso_server.services.character_server import CharacterServer
from dso_server.runtime.transport import Session


class MonsterClockTests(unittest.TestCase):
    def setUp(self):
        self.session = Session(("127.0.0.1", 41980), object())
        self.session.connected = True
        self.combat = object()
        self.session.gameplay_combat_state = self.combat
        self.session.gameplay_chase_last_at = 100.0
        self.session.gameplay_chase_server_tick = 1000

    def tick(self, now):
        snapshot = CharacterServer._snapshot_monster_tick_owner(
            self.session, 0, self.combat, now=now,
        )
        self.assertTrue(CharacterServer._commit_monster_tick_owner(
            self.session, 0, self.combat, snapshot, now=now,
        ))
        return snapshot

    def test_jitter_does_not_make_the_clock_run_faster_than_wall_time(self):
        for cycle in range(1000):
            self.tick(100.0 + cycle * 0.08 + 0.061)
            self.tick(100.0 + (cycle + 1) * 0.08)
        self.assertEqual(self.session.gameplay_chase_server_tick, 3000)

    def test_fast_followup_does_not_invent_another_tick(self):
        self.tick(100.04)
        self.tick(100.041)
        self.assertEqual(self.session.gameplay_chase_server_tick, 1001)

    def test_clock_uses_full_elapsed_time_while_physics_step_is_capped(self):
        snapshot = self.tick(102.0)
        self.assertEqual(snapshot.server_tick, 1050)
        self.assertEqual(snapshot.elapsed, 0.5)

    def test_unsigned_tick_wraparound(self):
        self.session.gameplay_chase_server_tick = 0xFFFFFFFE
        self.tick(100.16)
        self.assertEqual(self.session.gameplay_chase_server_tick, 2)

    def test_client_advance_during_simulation_is_not_overwritten(self):
        snapshot = CharacterServer._snapshot_monster_tick_owner(
            self.session, 0, self.combat, now=100.04,
        )
        self.session.gameplay_chase_server_tick = 1010
        CharacterServer._commit_monster_tick_owner(
            self.session, 0, self.combat, snapshot, now=100.04,
        )
        self.assertEqual(self.session.gameplay_chase_server_tick, 1010)
        self.tick(100.08)
        self.tick(100.12)
        self.assertLessEqual(self.session.gameplay_chase_server_tick, 1012)

    def test_new_map_generation_does_not_inherit_old_clock_anchor(self):
        self.tick(101.0)
        self.session.gameplay_chase_generation = 1
        self.session.gameplay_chase_server_tick = 200
        self.session.gameplay_chase_last_at = 500.0
        snapshot = CharacterServer._snapshot_monster_tick_owner(
            self.session, 1, self.combat, now=500.04,
        )
        self.assertEqual(snapshot.server_tick, 201)


class ImpactDeadlineTests(unittest.TestCase):
    def setUp(self):
        self.scheduler = GameplayDeadlineScheduler()
        self.session = Session(
            ("127.0.0.1", 41981), object(),
            gameplay_deadline_scheduler=self.scheduler,
        )
        self.addCleanup(self.scheduler.shutdown)

    def registered_timer(self):
        hit = SimpleNamespace(source_actor_id=1, cast_id=2, impact_index=0, due_at=100.2)
        with self.session.flow_lock:
            timers, _old = CharacterServer._register_runtime_projectile_deadlines(
                self.session, (hit,), now=100.0, generation=0,
            )
        return timers[0]

    def test_cast_preparation_does_not_add_to_authored_impact_delay(self):
        timer = self.registered_timer()
        with patch.object(self.scheduler, "_ensure_started"), \
                patch("dso_server.runtime.gameplay_scheduler.time.perf_counter", return_value=100.15):
            timer.start()
        self.assertAlmostEqual(timer.deadline, 100.2)

    def test_already_due_impact_does_not_wait_its_flight_time_again(self):
        timer = self.registered_timer()
        with patch.object(self.scheduler, "_ensure_started"), \
                patch("dso_server.runtime.gameplay_scheduler.time.perf_counter", return_value=101.0):
            timer.start()
        self.assertLessEqual(timer.deadline, 101.0)

    def test_ordinary_relative_timers_still_count_from_start(self):
        timer = self.scheduler.timer(0.2, lambda: None)
        with patch.object(self.scheduler, "_ensure_started"), \
                patch("dso_server.runtime.gameplay_scheduler.time.perf_counter", return_value=101.0):
            timer.start()
        self.assertAlmostEqual(timer.deadline, 101.2)


class ProjectileVisualTimingTests(unittest.TestCase):
    def test_missed_ranger_arrows_keep_their_draw_phase_before_visual_flight(self):
        from dso_server.gameplay.local_combat_state import LocalCombatState
        from dso_server.gameplay.local_monster_state import load_local_player_only_encounter
        from dso_server.paths import DEFAULT_STATIC_DB_PATH
        from dso_server.protocol.combat_protocol import BulletSkill

        encounter = load_local_player_only_encounter(DEFAULT_STATIC_DB_PATH, "a0002_start_hub")
        combat = LocalCombatState(encounter, player_actor_id=1, player_health=100)
        session = Session(("127.0.0.1", 41982), object())
        for skill_index, draw_seconds in ((1867, 0.20), (1870, 0.36)):
            for speed in (1.0, 2.0):
                with self.subTest(skill=skill_index, speed=speed):
                    geometry = combat._load_player_skill_geometry(skill_index)
                    skill = BulletSkill(0, skill_index, 0.0, 2000, 3000,
                                        (1.0, 0.0, 0.0, 0.0), 0)
                    impact_tick, shifted_delay = CharacterServer._remote_skill_impact_tick(
                        session, skill, geometry, (0.0, 0.0, 0.0),
                        round(geometry.loop_start_frame / speed), speed, None,
                    )
                    self.assertEqual(impact_tick, 2000 + round(
                        (draw_seconds / speed + geometry.projectile_lifetime) * 25,
                    ))
                    self.assertEqual(shifted_delay, 0)


class CastPreparationTests(unittest.TestCase):
    def setUp(self):
        from dso_server.tests.test_session_lock_hot_paths import AccountSnapshotLockTests
        from dso_server.gameplay.local_combat_state import LocalCombatState
        from dso_server.gameplay.local_monster_state import load_local_monster_encounter
        from dso_server.paths import (
            DEFAULT_STATIC_DB_PATH,
        )
        AccountSnapshotLockTests.setUp(self)
        encounter = load_local_monster_encounter(
            DEFAULT_STATIC_DB_PATH, "b1201_pve", include_spawn_areas=False,
        )
        self.combat = LocalCombatState(encounter, player_actor_id=1, player_health=1000)
        self.session.gameplay_combat_state = self.combat
        self.session.gameplay_monster_encounter = encounter
        self.session.gameplay_skill_book_ids = frozenset((1857,))

    def run_preparation(self, *, spatial=False, expired=False):
        from dso_server.protocol.combat_protocol import BulletSkill, SkillCast
        from dso_server.runtime.concurrency import assert_no_state_locks_held
        from dso_server.services import player_combat
        from dso_server.services.base import CharacterService

        class Prepared(Exception):
            pass

        def checked_stats(*args, **kwargs):
            assert_no_state_locks_held("cast-stat-calculation")
            raise Prepared

        skill = (SkillCast(0, 1857, 0.0, 200, 2) if spatial else
                 BulletSkill(0, 1857, 0.0, 200, 2, (0.0, 0.0, 1.0, 0.0), 0))
        if expired:
            # Feature checks are stubbed: this models an effect expiring
            # between those checks and the subsequent cast snapshot.
            self.session.gameplay_runtime_status_effects[999] = (
                SimpleNamespace(effect_id=4717), 0.0,
            )
        with (
            patch.object(CharacterService, "_player_victim_feature_locked", return_value=False),
            patch.object(player_combat, "decode_skill" if spatial else "decode_bullet_skill", return_value=skill),
            patch.object(player_combat, "calculate_player_combat_stats", side_effect=checked_stats),
        ):
            with self.assertRaises(Prepared):
                if spatial:
                    CharacterServer._handle_spatial_skill(self.session, b"\x8b\x46\x00")
                else:
                    CharacterServer._handle_bullet_skill(self.session, b"\x8b\x48\x00")
        if expired:
            self.assertNotIn(999, self.session.gameplay_runtime_status_effects)

    def test_bullet_stats_are_calculated_outside_session_lock(self):
        self.run_preparation()

    def test_spatial_stats_are_calculated_outside_session_lock(self):
        self.run_preparation(spatial=True)

    def test_expiry_during_bullet_preparation_does_not_nest_map_lock(self):
        self.run_preparation(expired=True)

    def test_expiry_during_spatial_preparation_does_not_nest_map_lock(self):
        self.run_preparation(spatial=True, expired=True)

    def test_effect_expiry_preserves_equipment_cache(self):
        from dso_server.gameplay.player_combat_stats import player_combat_stats_cache_metrics
        CharacterServer._refresh_player_combat_vitals(self.session)
        before = player_combat_stats_cache_metrics(self.session)
        self.session.gameplay_runtime_status_effects[999] = (SimpleNamespace(effect_id=4717), 0.0)
        CharacterServer._expire_runtime_status_effects(self.session)
        after = player_combat_stats_cache_metrics(self.session)
        self.assertEqual(after["inventory_observations"], before["inventory_observations"])
        self.assertEqual(after["invalidations"], before["invalidations"])


class SocketMinionWorkerTests(unittest.TestCase):
    def test_socket_spawn_does_not_break_the_map_tick(self):
        import time
        from dso_server.tests.test_populated_map_simulation import PopulatedMapSimulationTests
        from dso_server.protocol.socket_minion_protocol import load_socket_minion_definition
        fixture = PopulatedMapSimulationTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        combat, owner = fixture.combat, fixture.owner
        definition = load_socket_minion_definition(
            combat.encounter.static_database_path,
            "minion_jewel_buff_snow_wolf", 1, (0.0, 0.0, 0.0, 0.0),
        )
        self.assertIsNotNone(definition)
        actor_id = CharacterServer._remote_actor_id(owner.gameplay_character.character_id)
        with combat.lock:
            spawned = combat.spawn_player_summon(
                definition, owner_actor_id=actor_id,
                position=combat.encounter.trigger_center, yaw=0.0, level=100,
                server_tick=1900, now=time.perf_counter(), player_damage_range=(100.0, 100.0),
            )
        self.assertTrue(spawned.actors)
        for _ in range(3):
            self.assertTrue(CharacterServer._monster_chase_tick(owner, owner.gameplay_chase_generation))
        actor = spawned.actors[0]
        self.assertGreaterEqual(actor.attack_ready_at, actor.spawn_ready_at)
