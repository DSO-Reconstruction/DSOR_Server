"""``!stress <amount> [templateId]`` spawns a load-test wave around a GM."""

import math
from unittest.mock import Mock

import pytest

from dso_server.gameplay.local_combat_state import MONSTER_VISIBILITY_ENTER_RADIUS
from dso_server.runtime.transport import Session
from dso_server.services.chat import ChatServer
from dso_server.services.monster_combat import (
    STRESS_SPAWN_INNER_RADIUS,
    STRESS_SPAWN_LIMIT,
    STRESS_SPAWN_SPACING,
    stress_spawn_positions,
)
from dso_server.tests import test_map_events as fixtures

SKELETON = "a0203_normal_add_skeleton_warrior"


@pytest.fixture
def world():
    case = fixtures.MapEventServiceTests(methodName="runTest")
    case.setUp()
    try:
        yield case
    finally:
        case.doCleanups()


def test_rings_fit_the_announcement_radius_without_overlap():
    center = (10.0, 2.0, -5.0)
    positions = stress_spawn_positions(center, STRESS_SPAWN_LIMIT)

    assert len(positions) == STRESS_SPAWN_LIMIT
    distances = [
        math.dist((x, z), (center[0], center[2])) for x, _y, z in positions
    ]
    assert min(distances) >= STRESS_SPAWN_INNER_RADIUS - 1e-6
    # A metre inside the announcement radius, so ground snapping cannot push
    # the outer ring across it.
    assert max(distances) <= MONSTER_VISIBILITY_ENTER_RADIUS - 1.0
    assert min(
        math.dist(first, second)
        for index, first in enumerate(positions)
        for second in positions[index + 1:]
    ) >= STRESS_SPAWN_SPACING * 0.9


def test_spawns_the_wave_around_the_gm_and_announces_it(world):
    s = world.session
    combat = s.gameplay_combat_state
    before = set(combat.monsters)

    spawned, template = world.server._admin_spawn_stress_monsters(
        s, 25, SKELETON
    )

    new = set(combat.monsters) - before
    assert (spawned, template, len(new)) == (25, SKELETON, 25)
    center = s.gameplay_monster_encounter.coordinate_frame.wire_to_world(
        s.gameplay_last_move.relative_position
    )
    for actor_id in new:
        monster = combat.monsters[actor_id]
        assert monster.template_id == SKELETON
        assert combat.monster_health[actor_id] > 0
        assert math.dist(
            (monster.position[0], monster.position[2]), (center[0], center[2])
        ) <= MONSTER_VISIBILITY_ENTER_RADIUS
    assert new <= s.gameplay_announced_monster_actor_ids


def test_without_a_template_it_copies_the_maps_commonest_monster(world):
    s = world.session
    world.server._admin_spawn_stress_monsters(s, 3, SKELETON)

    assert world.server._admin_spawn_stress_monsters(s, 4) == (4, SKELETON)


@pytest.mark.parametrize("amount", [0, STRESS_SPAWN_LIMIT + 1])
def test_amount_outside_the_limit_is_refused(world, amount):
    with pytest.raises(ValueError, match="amount must be"):
        world.server._admin_spawn_stress_monsters(world.session, amount, SKELETON)


def test_a_map_without_monsters_needs_a_template(world):
    with pytest.raises(ValueError, match="no monster to copy"):
        world.server._admin_spawn_stress_monsters(world.session, 5)


def test_unknown_template_is_refused(world):
    with pytest.raises(ValueError, match="no runtime definition"):
        world.server._admin_spawn_stress_monsters(world.session, 5, "no_such_monster")


def _chat(world):
    chat = ChatServer.__new__(ChatServer)
    chat.gameplay_servers = (world.server,)
    admin = Session(("127.0.0.1", 52010), Mock())
    admin.chat_is_admin, admin.chat_user_id, admin.chat_user_name = True, 1, "P1"
    sent = []
    chat._send_system_message = lambda _session, text: sent.append(text)
    return chat, admin, sent


def test_chat_command_spawns_and_reports(world):
    world.server._admin_spawn_stress_monsters = Mock(return_value=(30, SKELETON))
    chat, admin, sent = _chat(world)

    chat._handle_admin_command(admin, "!stress 30")

    world.server._admin_spawn_stress_monsters.assert_called_once_with(
        world.session, 30, None
    )
    assert sent == [f"Spawned 30x {SKELETON} around you. !killmobs clears them."]


@pytest.mark.parametrize("text", ["!stress", "!stress lots", "!stress 1 a b"])
def test_chat_command_usage(world, text):
    chat, admin, _sent = _chat(world)
    with pytest.raises(ValueError, match="usage: !stress"):
        chat._handle_admin_command(admin, text)
