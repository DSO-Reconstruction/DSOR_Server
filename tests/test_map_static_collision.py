"""Bullets with StaticCollision stop at the walls placed in a map's .col/.map."""

from dataclasses import replace
import math
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

from dso_server.paths import DEFAULT_STATIC_DB_PATH
from dso_server.gameplay.combat_entities import CombatEntity, plan_entity_skill
from dso_server.gameplay.local_combat_state import LocalCombatState
from dso_server.gameplay.local_map_state import load_map_coordinate_frame
from dso_server.gameplay.local_monster_state import load_local_monster_encounter
from dso_server.gameplay.map_collision_terrain import load_map_collision_terrain
from dso_server.gameplay.map_static_collision import (
    StaticCollisionError,
    StaticLineOfSight,
    build_static_line_of_sight,
    read_collision_bundle,
    read_map_collision_instances,
)
from dso_server.protocol.combat_protocol import BULLET_SKILL_COMMAND


MAPS = Path(DEFAULT_STATIC_DB_PATH).parent.parent / "maps"
KINGSCITY = "a0200_kingscity"
ICEBALL = 1857  # mage_icemissile_bullet_new: StaticCollision 1, Straight


def _nvx2(vertices, triangles, groups=None):
    groups = groups or [(0, len(vertices), 0, len(triangles), 0, 1)]
    return (b"2XVN" + struct.pack("<6I", len(groups), len(vertices), 3, len(triangles), 0, 1)
            + b"".join(struct.pack("<6I", *group) for group in groups)
            + b"".join(struct.pack("<3f", *vertex) for vertex in vertices)
            + b"".join(struct.pack("<3H", *triangle) for triangle in triangles))


def _bundle(resources):
    out = b"CPAM" + struct.pack("<I", 1)
    for name, blob in resources.items():
        out += struct.pack("<H", len(name)) + name.encode() + struct.pack("<I", len(blob)) + blob
    return out


def _tag(name):
    return name[::-1]


def _map(strings, templates, instances, event_mappings=("11",)):
    table = b"".join(value.encode() + b"\0" for value in strings)
    out = _tag(b"DSOM") + struct.pack("<I", 5)
    out += _tag(b"MAPI") + struct.pack("<12f3h", *([0.0] * 12), 1, 1, 1)
    out += _tag(b"STRT") + struct.pack("<II", len(strings), len(table)) + table
    out += _tag(b"SETT") + struct.pack("<I", 0)
    out += _tag(b"EVET") + struct.pack("<I", 0)
    out += _tag(b"EMAL") + struct.pack("<I", len(event_mappings))
    out += b"".join(struct.pack("<H", len(value)) + value.encode() for value in event_mappings)
    out += _tag(b"TMPL") + struct.pack("<I", len(templates))
    for collision, group in templates:
        out += struct.pack("<5H8f2H", 0, 0, 0xFFFF, collision, 0, *([0.0] * 8), group, 0)
    out += _tag(b"INST") + struct.pack("<I", len(instances))
    for position, rotation, scale, collide, template, mapping in instances:
        out += struct.pack("<4f4f", *position, 1.0, *rotation)
        out += struct.pack("<B", scale is not None)
        if scale is not None:
            out += struct.pack("<4f", *scale, 0.0)
        out += struct.pack("<BBhhHH", collide, 1, template, -1, 0, mapping)
    return out + _tag(b"GROP") + struct.pack("<I", 0) + _tag(b"NAVB") + struct.pack("<I", 0)


# A 2 x 3 m wall in the local X/Y plane (facing Z) and a floor under it.
WALL = _nvx2(
    [(-1, 0, 0), (1, 0, 0), (1, 3, 0), (-1, 3, 0), (-5, 0, -5), (5, 0, -5), (5, 0, 5), (-5, 0, 5)],
    [(0, 1, 2), (0, 2, 3), (4, 5, 6), (4, 6, 7)],
)
QUARTER_TURN = (0.0, math.sin(math.pi / 4), 0.0, math.cos(math.pi / 4))  # about +Y


class StaticCollisionParserTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.col, self.map = root / "test.col", root / "test.map"
        two_states = _nvx2([(-1, 0, 0), (1, 0, 0), (1, 3, 0), (-1, 3, 0)],
                           [(0, 1, 2), (0, 2, 3), (0, 1, 2)],
                           groups=[(0, 4, 0, 2, 0, 1), (0, 4, 2, 1, 0, 1)])
        self.col.write_bytes(_bundle({
            "meshes:t/wall_c_0.nvx2": WALL,
            "meshes:t/destroy_crate_c_0.nvx2": WALL,
            "meshes:t/state_door_c_0.nvx2": two_states,
        }))
        strings = ["", "meshes:t/wall_c_0.nvx2", "meshes:t/destroy_crate_c_0.nvx2",
                   "meshes:t/state_door_c_0.nvx2", "meshes:t/missing_c_0.nvx2"]
        templates = [(1, 0), (2, 0), (3, 1), (4, 0), (0xFFFF, 0xFFFF)]
        identity = (0.0, 0.0, 0.0, 1.0)
        self.map.write_bytes(_map(strings, templates, [
            ((10.0, 1.0, 0.0), identity, None, True, 0, 0),           # wall across Z at x=10
            ((0.0, 1.0, 20.0), QUARTER_TURN, (2.0, 1.0, 1.0), True, 0, 0),  # turned, 4 m wide
            ((30.0, 1.0, 0.0), identity, None, False, 0, 0),          # collide flag off
            ((40.0, 1.0, 0.0), identity, None, True, 0, 1),           # event-only
            ((50.0, 1.0, 0.0), identity, None, True, 1, 0),           # destructible
            ((60.0, 1.0, 0.0), identity, None, True, 2, 0),           # mesh group 1 only
            ((70.0, 1.0, 0.0), identity, None, True, 3, 0),           # not in the bundle
            ((80.0, 1.0, 0.0), identity, None, True, 4, 0),           # no collision
        ], event_mappings=("11", "10")))

    def tearDown(self):
        self.directory.cleanup()

    def test_bundle_lists_raw_nvx2_resources(self):
        bundle = read_collision_bundle(self.col)
        self.assertEqual(sorted(bundle), ["meshes:t/destroy_crate_c_0.nvx2",
                                          "meshes:t/state_door_c_0.nvx2",
                                          "meshes:t/wall_c_0.nvx2"])
        self.assertEqual(bundle["meshes:t/wall_c_0.nvx2"], WALL)
        self.col.write_bytes(_bundle({"meshes:t/wall_c_0.nvx2": WALL})[:-3])
        with self.assertRaises(StaticCollisionError):
            read_collision_bundle(self.col)
        self.col.write_bytes(b"NOPE" + bytes(8))
        with self.assertRaises(StaticCollisionError):
            read_collision_bundle(self.col)

    def test_instances_keep_colliding_permanent_placements(self):
        instances = read_map_collision_instances(self.map)
        self.assertEqual([(resource.rpartition("/")[2], group, position[0])
                          for resource, group, position, _rotation, _scale in instances], [
            ("wall_c_0.nvx2", 0, 10.0),
            ("wall_c_0.nvx2", 0, 0.0),
            ("destroy_crate_c_0.nvx2", 0, 50.0),
            ("state_door_c_0.nvx2", 1, 60.0),
            ("missing_c_0.nvx2", 0, 70.0),
        ])
        self.assertEqual(instances[1][4], (2.0, 1.0, 1.0))

    def test_walls_block_with_their_transform_and_floors_do_not(self):
        sight = build_static_line_of_sight(self.map, self.col)
        # Two wall triangles each for the plain and turned wall, one for the
        # door's group 1; the floors, the crate and the missing mesh add none.
        self.assertEqual(sight.triangle_count, 5)
        # The wall at x=10 lies along X (x 9..11, z 0, y 1..4).
        self.assertAlmostEqual(sight.clear_distance((10, 2, -5), (10, 2, 5)), 5.0, places=5)
        self.assertFalse(sight.blocked((11.5, 2, -5), (11.5, 2, 5)))  # beside it
        self.assertFalse(sight.blocked((10, 4.5, -5), (10, 4.5, 5)))  # over it
        self.assertFalse(sight.blocked((10, 2, -5), (10, 2, -0.5)))   # stops short
        self.assertTrue(sight.blocked((10, 2, 5), (10, 2, -5)))       # two-sided
        # Turned a quarter about Y and scaled 2 in X: x 0, z 18..22.
        self.assertAlmostEqual(sight.clear_distance((-5, 2, 21.5), (5, 2, 21.5)), 5.0, places=5)
        self.assertFalse(sight.blocked((-5, 2, 23), (5, 2, 23)))
        # The floor under the wall never stops a low or descending shot.
        self.assertFalse(sight.blocked((12, 1.2, -4), (12, 0.5, 4)))
        # Collide flag off, event-only and destructible placements are open.
        for x in (30.0, 40.0, 50.0, 70.0, 80.0):
            self.assertFalse(sight.blocked((x, 2, -5), (x, 2, 5)), x)
        self.assertTrue(sight.blocked((60, 2, -5), (60, 2, 5)))

    def test_grid_returns_the_nearest_wall_across_cells(self):
        far = ((20.0, 0.0, 0.0), (20.0, 0.0, 20.0), (20.0, 5.0, 10.0))
        # A long slanted wall registered in many cells, hit after the near one.
        slanted = ((0.0, 0.0, -1.0), (60.0, 0.0, 30.0), (0.0, 5.0, -1.0))
        sight = StaticLineOfSight((slanted, far))
        hit = sight.clear_distance((1.0, 1.0, 10.0), (40.0, 1.0, 10.0))
        self.assertAlmostEqual(hit, 19.0, places=4)
        self.assertEqual(sight.clear_distance((0, 1, -20), (0, 1, -30)), 10.0)


@unittest.skipUnless((MAPS / f"{KINGSCITY}.col").is_file(), "game data not installed")
class KingsCityLineOfSightTests(unittest.TestCase):
    # Outside and inside the church, east of the market square; the church's
    # large pillar (outer_church_pillar_large_left_01 at 137, 1, -58) is between.
    THROUGH_CHURCH = ((121.0, -58.0), (163.0, -58.0))
    # From the square's arrival point north along the open street.
    OPEN_STREET = ((57.25, -48.36), (57.25, -30.0))

    @classmethod
    def setUpClass(cls):
        cls.sight = build_static_line_of_sight(MAPS / f"{KINGSCITY}.map", MAPS / f"{KINGSCITY}.col")
        cls.terrain = load_map_collision_terrain(MAPS / f"{KINGSCITY}.map", MAPS / f"{KINGSCITY}.col")

    def ground(self, x, z, reference=1.0):
        return self.terrain.project_position((x, reference, z))

    def lifted(self, x, z, height=1.2):
        ground = self.ground(x, z)
        return ground[0], ground[1] + height, ground[2]

    def test_a_shot_through_the_church_stops_at_its_wall(self):
        start, end = (self.lifted(*point) for point in self.THROUGH_CHURCH)
        self.assertTrue(self.sight.blocked(start, end))
        self.assertAlmostEqual(self.sight.clear_distance(start, end), 14.0, delta=1.5)

    def test_a_shot_down_the_open_street_flies_on(self):
        start, end = (self.lifted(*point) for point in self.OPEN_STREET)
        self.assertFalse(self.sight.blocked(start, end))
        self.assertAlmostEqual(self.sight.clear_distance(start, end), math.dist(start, end))

    def test_the_map_loads_quickly_into_a_modest_wall_set(self):
        self.assertGreater(self.sight.triangle_count, 5_000)
        self.assertLess(self.sight.triangle_count, 100_000)


class PlannerStaticReachTests(unittest.TestCase):
    def setUp(self):
        self.geometry = replace(_iceball_geometry(), impact_count=1, bullet_count=1,
                                reproduction_count=0, impact_radius=0.0,
                                location_status_effects=(), location_visual_effects=(),
                                bullet_death_effects=())
        self.near = CombatEntity(101, (0.0, 0.0, 4.0), 0.5)
        self.far = CombatEntity(202, (0.0, 0.0, 12.0), 0.5)
        # A wall 8 m ahead across the aim.
        self.wall = lambda start, end: min(math.dist(start, end), 8.0) if end[2] > 8.0 else math.dist(start, end)

    def plan(self, entities, geometry=None, **kwargs):
        return plan_entity_skill(geometry or self.geometry, entities, origin=(0.0, 0.0, 0.0),
                                 direction=(0.0, 0.0, 1.0, 0.0), projectile=True,
                                 static_reach=self.wall, **kwargs)

    def test_an_aimed_bullet_hits_nothing_beyond_the_wall(self):
        self.assertEqual([i.target.actor_id for i in self.plan((self.far,)).impacts], [])
        self.assertEqual([i.target.actor_id for i in self.plan((self.near, self.far)).impacts], [101])

    def test_an_explicit_target_behind_the_wall_is_dropped(self):
        self.assertEqual(self.plan((self.far,), explicit_targets=(202,)).impacts, ())
        self.assertEqual([i.target.actor_id for i in
                          self.plan((self.near,), explicit_targets=(101,)).impacts], [101])

    def test_bullets_without_static_collision_ignore_walls(self):
        for geometry in (replace(self.geometry, static_collision=False),
                         replace(self.geometry, motion_type="OrbitingActor")):
            self.assertEqual([i.target.actor_id for i in self.plan((self.far,), geometry).impacts], [202])


def _iceball_geometry():
    state = _kingscity_state()
    return state._load_player_skill_geometry(ICEBALL)


def _kingscity_state():
    """b1201's monsters, standing in Kings' City's streets."""
    encounter = load_local_monster_encounter(DEFAULT_STATIC_DB_PATH, "b1201_pve",
                                             include_spawn_areas=False)
    encounter = replace(encounter, map_id=KINGSCITY,
                        coordinate_frame=load_map_coordinate_frame(DEFAULT_STATIC_DB_PATH, KINGSCITY))
    return LocalCombatState(encounter, player_actor_id=1, player_health=1000)


@unittest.skipUnless((MAPS / f"{KINGSCITY}.col").is_file(), "game data not installed")
class CombatStaticCollisionTests(unittest.TestCase):
    def setUp(self):
        self.state = _kingscity_state()
        self.frame = self.state.encounter.coordinate_frame
        self.terrain = load_map_collision_terrain(MAPS / f"{KINGSCITY}.map", MAPS / f"{KINGSCITY}.col")
        self.actor_id = self.state.encounter.actor_ids[0]
        self.state.active_monster_actor_ids = {self.actor_id}

    def shoot(self, start, end, *, explicit=False):
        start = self.terrain.project_position((start[0], 1.0, start[1]))
        end = self.terrain.project_position((end[0], 1.0, end[1]))
        self.state.monster_positions[self.actor_id] = end
        dx, dz = end[0] - start[0], end[2] - start[2]
        length = math.hypot(dx, dz)
        return self.state.plan_entity_skill_hits(
            skill_index=ICEBALL, player_position=self.frame.world_to_wire(start), projectile=True,
            direction=(dx / length, 0.0, dz / length, 0.0),
            explicit_target_actor_ids=(self.actor_id,) if explicit else (),
        )

    def test_the_iceball_reads_static_collision(self):
        self.assertTrue(self.state._load_player_skill_geometry(ICEBALL).static_collision)

    def test_a_monster_behind_the_church_wall_is_not_hit(self):
        start, end = (121.0, -58.0), (138.5, -58.0)
        for explicit in (False, True):
            self.assertEqual(self.shoot(start, end, explicit=explicit).impacts, (), explicit)

    def test_a_monster_down_the_open_street_is_hit(self):
        for explicit in (False, True):
            plan = self.shoot((57.25, -48.36), (57.25, -36.0), explicit=explicit)
            self.assertEqual([i.target.actor_id for i in plan.impacts], [self.actor_id], explicit)

    def test_the_map_collision_is_built_once(self):
        # Built once per map, in the background as its combat state is made,
        # then reused by every shot.
        with patch("dso_server.gameplay.local_combat_state.load_static_line_of_sight",
                   wraps=lambda *paths: build_static_line_of_sight(*paths)) as loader:
            self.state = _kingscity_state()
            self.state.active_monster_actor_ids = {self.actor_id}
            self.shoot((57.25, -48.36), (57.25, -36.0))
            self.shoot((121.0, -58.0), (138.5, -58.0))
        self.assertEqual(loader.call_count, 1)


class MonsterBulletStaticCollisionTests(unittest.TestCase):
    def test_a_monster_bullet_into_a_wall_deals_no_damage(self):
        encounter = load_local_monster_encounter(DEFAULT_STATIC_DB_PATH, "g0004_corrupted_jungle_01_pve")
        for wall, expected in ((0.5, 0), (None, 1)):
            state = LocalCombatState(encounter, player_actor_id=1, player_health=1000,
                                     initially_active=False)
            actor_id = next(actor for actor, monster in state.monsters.items()
                            if monster.attack.command_id == BULLET_SKILL_COMMAND)
            state.activate_monsters((actor_id,))
            state.last_monster_attack_at[actor_id] = 0.0
            state._monster_skill_ready_at[actor_id] = {}
            position = state.monster_positions[actor_id]
            target = encounter.coordinate_frame.world_to_wire((position[0] + 6.0, position[1], position[2]))
            attack, = state.maybe_start_monster_attacks(target, server_tick=500, now=100.0)
            state.pending_monster_attacks[actor_id] = replace(
                attack, definition=replace(attack.definition, static_collision=True))
            sight = None if wall is None else type("Wall", (), {
                "clear_distance": staticmethod(lambda start, end: wall)})()
            with patch.object(state, "_map_static_line_of_sight", return_value=sight):
                results = state.resolve_due_monster_attacks(target, now=attack.due_at)
            self.assertEqual(len(results), expected, wall)


    def test_a_monster_does_not_fire_into_a_wall(self):
        encounter = load_local_monster_encounter(DEFAULT_STATIC_DB_PATH, "g0004_corrupted_jungle_01_pve")
        for wall, expected in ((0.5, 0), (None, 1)):
            state = LocalCombatState(encounter, player_actor_id=1, player_health=1000,
                                     initially_active=False)
            actor_id = next(actor for actor, monster in state.monsters.items()
                            if monster.attack.command_id == BULLET_SKILL_COMMAND)
            monster = state.monsters[actor_id]
            walled = replace(monster.attack, static_collision=True)
            state.monsters[actor_id] = replace(
                monster, attack=walled,
                attacks=tuple(walled if a == monster.attack else a for a in (monster.attacks or ())) or monster.attacks)
            state.activate_monsters((actor_id,))
            state.last_monster_attack_at[actor_id] = 0.0
            state._monster_skill_ready_at[actor_id] = {}
            position = state.monster_positions[actor_id]
            target = encounter.coordinate_frame.world_to_wire((position[0] + 6.0, position[1], position[2]))
            sight = None if wall is None else type("Wall", (), {
                "clear_distance": staticmethod(lambda start, end: wall)})()
            with patch.object(state, "_map_static_line_of_sight", return_value=sight):
                attacks = state.maybe_start_monster_attacks(target, server_tick=500, now=100.0)
            self.assertEqual(len(attacks), expected, wall)

if __name__ == "__main__":
    unittest.main()
