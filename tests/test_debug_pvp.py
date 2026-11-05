"""``!debug_pvp``: bot players in real battleground matches and an open arena."""

import math
import random
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from dso_server.gameplay.pvp_match_state import MatchPhase
from dso_server.gameplay.pvp_objectives import (
    CaptureTheFlagState,
    FortressState,
    load_capture_the_flag_layout,
    load_fortress_layout,
)
from dso_server.paths import DEFAULT_STATIC_DB_PATH
from dso_server.protocol.character_selection import CLASS_IDS
from dso_server.protocol.combat_protocol import (
    decode_bullet_skill,
    decode_target_skill,
)
from dso_server.protocol.gameplay_protocol import (
    ACTOR_REQUEST_COMMAND,
    Move,
    decode_move,
    decode_simple_gameplay_command,
)
from dso_server.protocol.pvp_match_protocol import (
    BATTLEGROUND_MAPS,
    MATCH_TEAM_SIZES,
    CLIENT_QUEUE_SOLO,
    CLIENT_QUEUE_TEAM,
    JoinBattlegroundResponse,
    client_queue_for_mode,
    decode_join_battleground,
    decode_matchmaking_info,
    encode_join_battleground_response,
)
from dso_server.protocol.respawn_protocol import decode_respawn
from dso_server.runtime.transport import Session
from dso_server.services import battleground as battleground_module
from dso_server.services import debug_pvp
from dso_server.services.base import CharacterService
from dso_server.services.character_server import CharacterServer
from dso_server.services.chat import ChatServer
from dso_server.tests.fixtures.characters import character_server

ARENA = debug_pvp.DEFAULT_ARENA_MAP
HUB = "a0200_kingscity"


# ----------------------------------------------------------------------
# wire format
# ----------------------------------------------------------------------

def test_bot_commands_decode_as_client_commands():
    move = Move.from_values((1.5, -2.0, 3.25), 1.0, 0.5, 0.0, 1234, 20)
    assert decode_move(debug_pvp.encode_move_command(move)) == move

    target = decode_target_skill(debug_pvp.encode_target_skill_command(
        1838, 0.25, 777, 0x101, 0x103C5,
    ))
    assert (target.skill_index, target.server_tick, target.cast_id,
            target.target_actor_id) == (1838, 777, 0x101, 0x103C5)
    assert target.aim_angle == pytest.approx(0.25)

    bullet = decode_bullet_skill(debug_pvp.encode_bullet_skill_command(
        1855, -1.0, 778, 0x102, (0.6, 0.0, -0.8, 0.0),
    ))
    assert (bullet.skill_index, bullet.server_tick, bullet.cast_id,
            bullet.variant) == (1855, 778, 0x102, 0)
    assert bullet.direction == pytest.approx((0.6, 0.0, -0.8, 0.0))

    payload, bits = debug_pvp.encode_respawn_command(same_spot=True)
    request = decode_respawn(payload, bits)
    assert (request.nearest_exit, request.same_spot) == (False, True)

    request = decode_simple_gameplay_command(debug_pvp.encode_actor_request(196609))
    assert request.value == 196609
    assert debug_pvp.encode_actor_request(1)[1:3] == ACTOR_REQUEST_COMMAND.to_bytes(2, "little")


def test_the_client_is_told_the_solo_or_team_queue_never_the_server_mode():
    # The client asserts (RankingManager::GetActiveBattlegroundForMatchMode)
    # on anything but 0 and 1, and files 1v1 and last man standing as solo.
    assert {mode: client_queue_for_mode(mode) for mode in BATTLEGROUND_MAPS} == {
        debug_pvp.MATCH_MODES["1v1"]: CLIENT_QUEUE_SOLO,
        debug_pvp.MATCH_MODES["ffa"]: CLIENT_QUEUE_SOLO,
        debug_pvp.MATCH_MODES["tdm"]: CLIENT_QUEUE_TEAM,
        debug_pvp.MATCH_MODES["ctf"]: CLIENT_QUEUE_TEAM,
        debug_pvp.MATCH_MODES["fortress"]: CLIENT_QUEUE_TEAM,
    }
    assert client_queue_for_mode(-1) == -1


# ----------------------------------------------------------------------
# command parsing
# ----------------------------------------------------------------------

def _command_server():
    return SimpleNamespace(
        static_database_path=DEFAULT_STATIC_DB_PATH,
        sessions={},
        _begin_map_transfer=Mock(return_value=True),
    )


def _command_admin(map_id=HUB):
    admin = Session(("127.0.0.1", 52401), Mock())
    admin.gameplay_character = SimpleNamespace(character_id=200_000_001, level=50)
    admin.gameplay_map = map_id
    return admin


@pytest.mark.parametrize("arguments, message", [
    ((), "usage: !debug_pvp"),
    (("chess",), "usage: !debug_pvp"),
    (("ctf", "now"), "usage: !debug_pvp"),
    (("stop", "now"), "usage: !debug_pvp"),
    (("open", "0"), "bots must be from 1 to 8"),
    (("open", "9"), "bots must be from 1 to 8"),
    (("open", "lots"), "bots must be a whole number"),
    (("open", "2", ARENA, "extra"), "usage: !debug_pvp"),
    # Battleground arenas refuse respawn outside a match; a hub never kills.
    (("open", "2", "b1102_dm"), "not an open PvE map with respawn"),
    (("open", "2", HUB), "not an open PvE map with respawn"),
])
def test_bad_arguments_are_refused_before_anything_starts(arguments, message):
    server = _command_server()
    with pytest.raises(ValueError, match=message):
        debug_pvp.handle_admin_command(server, _command_admin(), arguments)
    server._begin_map_transfer.assert_not_called()
    assert debug_pvp.active_arena(server, 200_000_001) is None


def test_match_modes_cover_every_battleground():
    assert set(debug_pvp.MATCH_MODES.values()) == set(BATTLEGROUND_MAPS)
    assert set(debug_pvp.MODE_LABELS) == set(BATTLEGROUND_MAPS)


def test_a_player_already_in_a_battleground_cannot_start_another():
    admin = _command_admin()
    admin.gameplay_match = object()
    for arguments in (("ctf",), ("open",)):
        with pytest.raises(ValueError, match="battleground"):
            debug_pvp.handle_admin_command(_command_server(), admin, arguments)


def test_stop_without_bots_says_so():
    reply = debug_pvp.handle_admin_command(
        _command_server(), _command_admin(), ("stop",)
    )
    assert reply == "No PvP debug bots are running for you."


def test_open_arena_teleports_an_admin_elsewhere_and_waits_for_arrival():
    server = _command_server()
    admin = _command_admin()
    reply = debug_pvp.handle_admin_command(server, admin, ("open",))
    try:
        server._begin_map_transfer.assert_called_once_with(
            admin, ARENA, reason="admin-debug-pvp",
            gameplay_switch=True, scaling_difficulty=0,
        )
        assert reply.startswith(f"Teleporting to {ARENA}; 3 PvP bot(s)")
        arena = debug_pvp.active_arena(server, 200_000_001)
        assert arena is not None and not arena.bots
    finally:
        assert debug_pvp.stop_arena(server, 200_000_001, reason="test")
    assert debug_pvp.active_arena(server, 200_000_001) is None


def test_refused_transfer_is_reported():
    server = _command_server()
    server._begin_map_transfer.return_value = False
    with pytest.raises(ValueError, match="map transfer is already in progress"):
        debug_pvp.handle_admin_command(server, _command_admin(), ("open", "1"))
    assert debug_pvp.active_arena(server, 200_000_001) is None


def test_a_bot_nobody_drives_is_removed_instead_of_transferred():
    server = SimpleNamespace(_retire_session=Mock())
    bot = debug_pvp.DebugBotSession(("192.0.2.1", 1), Mock())
    bot.gameplay_character = SimpleNamespace(character_id=200_000_200, name="Lost")
    assert not debug_pvp.transfer_bot(server, bot, HUB, reason="pvp-leave")
    server._retire_session.assert_called_once()
    assert server._retire_session.call_args.args[0] is bot


def _chat(gameplay_server, gameplay):
    chat = ChatServer.__new__(ChatServer)
    chat.gameplay_servers = (gameplay_server,)
    chat._gameplay_server_and_session_for_user_id = Mock(
        return_value=(gameplay_server, gameplay)
    )
    admin = Session(("127.0.0.1", 52402), Mock())
    admin.chat_is_admin, admin.chat_user_id, admin.chat_user_name = True, 7, "GM"
    sent = []
    chat._send_system_message = lambda _session, text: sent.append(text)
    return chat, admin, sent


def test_chat_command_passes_arguments_and_lists_itself_in_help(monkeypatch):
    gameplay_server, gameplay = object(), object()
    chat, admin, sent = _chat(gameplay_server, gameplay)
    handler = Mock(return_value="ok")
    monkeypatch.setattr(debug_pvp, "handle_admin_command", handler)

    chat._handle_admin_command(admin, "!DEBUG_PVP ctf")
    chat._handle_admin_command(admin, "!help")

    args, kwargs = handler.call_args
    assert args == (gameplay_server, gameplay, ["ctf"])
    assert callable(kwargs["notify"])
    assert sent[0] == "ok"
    assert (
        "!debug_pvp 1v1|tdm|ctf|fortress|ffa | "
        "!debug_pvp open [bots] [mapId] | !debug_pvp stop"
    ) in sent[1]


# ----------------------------------------------------------------------
# objective decisions
# ----------------------------------------------------------------------

def _bot(character_id, team):
    session = SimpleNamespace(gameplay_match_team=team)
    return SimpleNamespace(
        session=session,
        character=SimpleNamespace(character_id=character_id),
    )


def _peer(character_id, actor_id):
    return SimpleNamespace(
        gameplay_character=SimpleNamespace(character_id=character_id),
        gameplay_remote_actor_id=actor_id,
    )


def _ctf_match():
    layout = load_capture_the_flag_layout(DEFAULT_STATIC_DB_PATH, "c2103_ctf")
    if layout is None:
        pytest.skip("c2103_ctf has no capture-the-flag layout")
    return SimpleNamespace(
        lock=threading.RLock(), objective=CaptureTheFlagState(layout, 3),
    )


def test_ctf_runners_go_for_the_enemy_flag_and_carry_it_home():
    match = _ctf_match()
    state = match.objective
    plan = debug_pvp.DebugPvPMatch.__new__(debug_pvp.DebugPvPMatch)
    runner = _bot(200_000_254, 0)
    enemy_close = [(3.0, _peer(200_000_251, 0x10001), (1.0, 0.0, 1.0))]

    # Runners ignore a nearby enemy and head for the enemy flag ...
    assert plan.ctf_plan(runner, match, (0, 0, 0), enemy_close) == (
        "walk", tuple(state.flags[1].position), 0.3,
    )
    # ... and, once it is theirs, take it home.
    state.touch(200_000_254, 0, 1, state.flags[1].position)
    assert plan.ctf_plan(runner, match, (0, 0, 0), enemy_close) == (
        "walk", tuple(state.layout.home(0)), 0.3,
    )


def test_ctf_fighters_hunt_their_own_flag_carrier_first():
    match = _ctf_match()
    state = match.objective
    plan = debug_pvp.DebugPvPMatch.__new__(debug_pvp.DebugPvPMatch)
    fighter = _bot(200_000_253, 0)
    near = _peer(200_000_249, 0x10002)
    carrier = _peer(200_000_251, 0x10003)
    enemies = [(2.0, near, (2.0, 0.0, 0.0)), (40.0, carrier, (40.0, 0.0, 0.0))]

    assert plan.ctf_plan(fighter, match, (0, 0, 0), enemies) == (
        "fight", 0x10002, (2.0, 0.0, 0.0),
    )
    state.touch(200_000_251, 1, 0, state.flags[0].position)
    assert plan.ctf_plan(fighter, match, (0, 0, 0), enemies) == (
        "fight", 0x10003, (40.0, 0.0, 0.0),
    )


def test_fortress_capturer_holds_its_point_until_the_capture_completes():
    layout = load_fortress_layout(DEFAULT_STATIC_DB_PATH, "f4103_atf")
    if layout is None or not layout.control_points:
        pytest.skip("f4103_atf has no fortress control points")
    state = FortressState(layout)
    point = layout.control_points[0]
    team = int(point.owner_team)
    state.control_point_present = lambda _point: True
    match = SimpleNamespace(lock=threading.RLock(), fortress_captures={})
    find = debug_pvp.DebugPvPMatch._open_control_point

    nearest, holding = find(match, state, team, point.position, 7)
    assert not holding and nearest is not None
    match.fortress_captures[nearest.row_id] = {"capturer": 7, "spawned": None}
    assert find(match, state, team, point.position, 7) == (None, True)
    match.fortress_captures[nearest.row_id] = {"capturer": 8, "spawned": None}
    candidate, holding = find(match, state, team, point.position, 7)
    assert not holding and (candidate is None or candidate.row_id != nearest.row_id)


# ----------------------------------------------------------------------
# a real server
# ----------------------------------------------------------------------

class _Cleanup:
    def __init__(self):
        self.calls = []

    def addCleanup(self, function, *args, **kwargs):
        self.calls.append((function, args, kwargs))

    def run(self):
        for function, args, kwargs in reversed(self.calls):
            try:
                function(*args, **kwargs)
            except Exception:
                pass


def _start_schedulers(server, cleanup):
    for scheduler in {
        id(value): value for value in (
            server.gameplay_deadline_scheduler,
            server.gameplay_ingress_scheduler,
        )
    }.values():
        scheduler.start()
        cleanup.addCleanup(scheduler.shutdown, timeout=2.0)


def _player(server, port, map_id, *, labels=None, spawn=None, sent=None):
    """A player whose client never answers, initialized on ``map_id``."""

    s = Session(
        ("127.0.0.1", port), server.sock,
        server.gameplay_deadline_scheduler, server.gameplay_ingress_scheduler,
    )
    s.connected = s.gameplay_mode = True
    s.gameplay_map = map_id
    s.gameplay_character = server.character_selection.active_character
    s.gameplay_route_spawn_position = spawn

    def record(payload, **kwargs):
        if labels is not None:
            labels.append(kwargs.get("label"))
        if sent is not None:
            sent.append((kwargs.get("label"), payload))
        return b""

    s.send_reliable_ordered = record
    s.send_unreliable_sequenced = record
    s.send_reliable_ordered_split = Mock()
    server.sessions[s.addr] = s
    server._send_local_player_initialization(s)
    assert s.gameplay_presence_ready
    combat = s.gameplay_combat_state
    if combat is not None:
        # A standing player, as its client reports every tick.
        s.gameplay_last_move = Move.from_values(
            combat.encounter.coordinate_frame.world_to_wire(
                s.gameplay_spawn_position
            ), 0.0, 0.0, 0.0, 1000, 20,
        )
        s.gameplay_chase_server_tick = 1000
    return s


class SyncArena(debug_pvp.DebugPvPArena):
    """Runs each bot command on the calling thread, so tests are ordered."""

    def _send_client_command(self, s, payload, bits=None):
        if isinstance(payload, tuple):
            payload, bits = payload
        s.current_message_bit_length = len(payload) * 8 if bits is None else bits
        s.current_message_received_at = time.perf_counter()
        self.server._dispatch_char_op_now(s, payload)


@pytest.fixture(scope="module")
def world():
    cleanup = _Cleanup()
    server = character_server(cleanup)
    if server.static_database is None:
        pytest.skip("the static database is not installed")
    server.sock = Mock()
    _start_schedulers(server, cleanup)
    labels = []
    admin = _player(server, 52403, ARENA, labels=labels)
    yield SimpleNamespace(
        server=server, admin=admin, combat=admin.gameplay_combat_state,
        labels=labels, character_id=int(admin.gameplay_character.character_id),
    )
    cleanup.run()


@pytest.fixture
def arena(world):
    health = world.combat.player_health[world.admin.gameplay_remote_actor_id]
    health[0] = health[1]
    world.labels.clear()
    created = []

    def make(count):
        value = SyncArena(
            world.server, world.character_id, ARENA, count,
            rng=random.Random(5),
        )
        value.spawn(world.admin)
        created.append(value)
        return value

    yield make
    for value in created:
        value.stop("test finished")
        value.teardown()


def _bot_sessions(server):
    # Retirement runs on the lifecycle scheduler: poll, never assume.
    return [s for s in server.sessions.values() if debug_pvp.is_debug_bot(s)]


def test_open_arena_bots_are_unsaved_hostile_players_in_the_admins_instance(world, arena):
    admin, combat, server = world.admin, world.combat, world.server
    was_flagged = admin.gameplay_pvp_enabled
    bots = arena(4)

    sessions = [bot.session for bot in bots.bots]
    assert [s.gameplay_character.class_id for s in sessions] == [
        CLASS_IDS[name] for name in ("warrior", "mage", "ranger", "dwarf")
    ]
    assert [s.gameplay_character.name for s in sessions] == [
        "Bot 1 Warrior", "Bot 2 Mage", "Bot 3 Ranger", "Bot 4 Dwarf",
    ]
    assert len({s.gameplay_character.character_id for s in sessions}) == 4
    assert admin.gameplay_pvp_enabled
    for s in sessions:
        actor_id = s.gameplay_remote_actor_id
        assert s.gameplay_combat_state is combat
        assert CharacterService._gameplay_instance_key(s) == (
            CharacterService._gameplay_instance_key(admin)
        )
        assert s.gameplay_presence_ready and s.gameplay_pvp_enabled
        assert s.gameplay_character.level == admin.gameplay_character.level
        assert combat.player_health[actor_id][0] > 0
        assert CharacterService._pvp_target_is_hostile(admin, s)
        assert CharacterService._pvp_target_is_hostile(s, admin)
        assert f"remote-player-enter-{actor_id}" in world.labels
        assert f"remote-player-pvp-{actor_id}" in world.labels
        # Nothing reaches the socket or the reliable queue, and no save.
        assert not s.pending_reliable
        assert not CharacterService._persist_gameplay_session(
            server, s, reason="map initialization", flush=True,
        )
        assert server.account_store.get(s.gameplay_character.character_id) is None

    bots.stop("test")
    bots.teardown()
    assert _wait(lambda: not _bot_sessions(server), 5.0)
    for s in sessions:
        assert f"remote-player-left-{s.gameplay_remote_actor_id}" in world.labels
    assert admin.gameplay_pvp_enabled == was_flagged


def test_every_class_lands_its_basic_attack_on_the_admin(world, arena):
    admin, combat = world.admin, world.combat
    admin_actor = admin.gameplay_remote_actor_id
    implementation = CharacterService._implementation
    assert "_resolve_pvp_skill_hit" not in implementation.__dict__
    original = implementation._resolve_pvp_skill_hit
    landed = set()

    def spy(s, generation, key, *args, **kwargs):
        pending = s.gameplay_pending_pvp_hits.get(key)
        if pending is not None and pending.target_actor_id == admin_actor:
            landed.add(s.gameplay_character.class_id)
        return original(s, generation, key, *args, **kwargs)

    implementation._resolve_pvp_skill_hit = staticmethod(spy)
    try:
        bots = arena(4)
        before = combat.player_health[admin_actor][0]
        start = time.perf_counter()
        deadline = start + 15.0
        while len(landed) < 4 and time.perf_counter() < deadline:
            # Bot time runs fast so each tick may attack; the server's own
            # cast cadence still gates how often a cast is accepted.
            bots.tick(time.perf_counter() + 2.0 + 30 * (time.perf_counter() - start))
            time.sleep(0.05)
    finally:
        del implementation._resolve_pvp_skill_hit
    assert landed == {CLASS_IDS[name] for name in ("warrior", "mage", "ranger", "dwarf")}
    assert combat.player_health[admin_actor][0] < before


def test_dead_open_arena_bot_respawns_on_the_spot_after_the_delay(world, arena):
    combat = world.combat
    bots = arena(1)
    bot = bots.bots[0]
    s = bot.session
    actor_id = s.gameplay_remote_actor_id
    with combat.lock:
        combat.player_health[actor_id][0] = 0
    CharacterService._notify_player_death(s)
    now = time.perf_counter()

    bots.tick(now)
    assert combat.player_health[actor_id][0] == 0
    bots.tick(now + debug_pvp.BOT_RESPAWN_DELAY_SECONDS - 0.5)
    assert combat.player_health[actor_id][0] == 0
    bots.tick(now + debug_pvp.BOT_RESPAWN_DELAY_SECONDS + 0.1)

    health = combat.player_health[actor_id]
    assert health[0] == health[1] > 0
    assert not s.gameplay_death_pending


def test_open_command_in_the_arena_spawns_bots_and_stop_removes_them(world):
    server, admin = world.server, world.admin
    notes = []
    reply = debug_pvp.handle_admin_command(
        server, admin, ("open", "1"), notify=notes.append,
    )
    assert reply == "Spawning 1 PvP bot(s) around you. !debug_pvp stop removes them."
    try:
        deadline = time.perf_counter() + 20.0
        while not notes and time.perf_counter() < deadline:
            time.sleep(0.05)
        assert notes and notes[0].startswith(f"PvP debug: 1 bot(s) joined on {ARENA}")
        arena = debug_pvp.active_arena(server, world.character_id)
        assert arena is not None and len(arena.bots) == 1
        bot = arena.bots[0].session
        # The 5 Hz loop drives the bot through the real ingress path.
        deadline = time.perf_counter() + 5.0
        while bot.gameplay_last_move is None and time.perf_counter() < deadline:
            time.sleep(0.05)
        assert bot.gameplay_last_move is not None
    finally:
        reply = debug_pvp.handle_admin_command(server, admin, ("stop",))
    assert reply == "PvP debug bots removed."
    assert debug_pvp.active_arena(server, world.character_id) is None
    assert _wait(lambda: not _bot_sessions(server), 5.0)


def test_open_arena_bots_leave_when_the_admin_does(world):
    server, admin = world.server, world.admin
    arena = debug_pvp.DebugPvPArena(server, world.character_id, ARENA, 1)
    arena.admin = admin
    assert arena._admin_present()
    admin.gameplay_map = HUB
    try:
        assert not arena._admin_present()
    finally:
        admin.gameplay_map = ARENA


def test_spawn_points_stay_on_the_admins_ground(world):
    combat = world.combat
    navigation = combat.encounter.navigation
    center = world.admin.gameplay_spawn_position
    home = navigation.component_for_position(center)
    for index in range(8):
        angle = math.tau * index / 8
        point = debug_pvp.DebugPvPArena._ground_position(
            combat,
            (center[0] + 8 * math.cos(angle), center[1], center[2] + 8 * math.sin(angle)),
            center,
        )
        assert navigation.component_for_position(point) == home


# ----------------------------------------------------------------------
# battleground matches
# ----------------------------------------------------------------------

@pytest.fixture(scope="module")
def match_world():
    """A server whose admin client is played by the test.

    The admin's own map transfers are recorded instead of sent (there is no
    client to reconnect); the test then brings the admin in on the arena,
    which is what the client does. Bot transfers run for real.
    """

    cleanup = _Cleanup()
    server = character_server(cleanup)
    if server.static_database is None:
        pytest.skip("the static database is not installed")
    server.sock = Mock()
    _start_schedulers(server, cleanup)
    transfers = []
    original = CharacterServer._begin_map_transfer

    def transfer(self, s, destination, **kwargs):
        if debug_pvp.is_debug_bot(s):
            return original(self, s, destination, **kwargs)
        transfers.append((s, destination, kwargs))
        return True

    CharacterServer._begin_map_transfer = transfer
    cleanup.addCleanup(setattr, CharacterServer, "_begin_map_transfer", original)
    ports = iter(range(52500, 52600))
    yield SimpleNamespace(server=server, transfers=transfers, ports=ports)
    cleanup.run()


def _wait(condition, timeout, step=0.05):
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        value = condition()
        if value:
            return value
        time.sleep(step)
    return condition()


class _AdminClient:
    """Plays the admin's side: accept the invitation, load the arena."""

    def __init__(self, world):
        self.world = world
        self.server = world.server
        world.transfers.clear()
        self.sent = []
        self.home = _player(self.server, next(world.ports), HUB, sent=self.sent)
        self.character_id = int(self.home.gameplay_character.character_id)
        self.arena = None
        self.notes = []

    @property
    def registry(self):
        return CharacterService._matchmaking_registry(self.server)

    def match(self):
        return self.registry.match_for_character(self.character_id)

    def accept(self, match):
        payload, bits = encode_join_battleground_response(JoinBattlegroundResponse(
            server_tick=0, battleground_id=match.match_id, accepted=True,
            field_2c=0, match_mode=match.match_mode,
        ))
        self.home.current_message_bit_length = bits
        self.server._dispatch_char_op_now(self.home, payload)

    def arrive(self):
        (source, destination, kwargs), = self.world.transfers
        assert source is self.home
        self.arena = _player(
            self.server, next(self.world.ports), destination,
            spawn=kwargs.get("spawn_position"),
        )
        return self.arena

    def keep_moving(self):
        """The client's MoveCommand every tick: its clock moves on."""

        s = self.arena
        s.gameplay_chase_server_tick += 5
        move = s.gameplay_last_move
        if move is not None:
            s.gameplay_last_move = Move.from_values(
                move.relative_position, 0.0, 0.0, 0.0,
                s.gameplay_chase_server_tick, 20,
            )

    def close(self):
        debug_pvp.stop_arena(self.server, self.character_id, reason="test")
        for session in (self.arena, self.home):
            if session is None:
                continue
            if getattr(session, "gameplay_match", None) is not None:
                CharacterService._leave_battleground(
                    self.server, session, reason="test finished",
                )
            self.registry.dequeue(self.character_id)
            self.server.sessions.pop(session.addr, None)


@pytest.mark.parametrize("mode", ["1v1", "tdm", "ctf", "fortress", "ffa"])
def test_every_mode_forms_a_full_match_and_seats_every_bot(match_world, mode):
    client = _AdminClient(match_world)
    server = match_world.server
    try:
        reply = debug_pvp.handle_admin_command(
            server, client.home, (mode,), notify=client.notes.append,
        )
        match_mode = debug_pvp.MATCH_MODES[mode]
        sizes = MATCH_TEAM_SIZES[match_mode]
        assert reply.startswith(f"Queueing you and {sum(sizes) - 1} bot(s)")

        match = _wait(client.match, 10.0)
        assert match is not None and match.match_mode == match_mode
        assert sorted(len(match.team_members(team)) for team in range(len(sizes))) == sorted(sizes)
        assert _wait(lambda: client.notes, 5.0)
        assert "formed on " + BATTLEGROUND_MAPS[match_mode] in client.notes[0]
        # The bots accept like clients; then the admin does.
        assert _wait(lambda: all(
            entry.accepted for entry in match.participants
            if entry.character_id != client.character_id
        ), 5.0)
        client.accept(match)
        assert match.phase == MatchPhase.LOADING
        expected = client_queue_for_mode(match_mode)
        infos = [decode_matchmaking_info(*payload) for label, payload in client.sent
                 if label == "pvp-matchmaking-info"]
        invitations = [decode_join_battleground(*payload) for label, payload in client.sent
                       if label == "pvp-join-battleground"]
        assert infos and {info.match_mode for info in infos} == {expected}
        assert [invitation.match_mode for invitation in invitations] == [expected]
        assert invitations[0].map_id == match.map_id
        client.arrive()

        assert _wait(lambda: match.phase in (
            MatchPhase.COUNTDOWN, MatchPhase.RUNNING,
        ), 60.0), match.phase
        combat = client.arena.gameplay_combat_state
        for entry in match.participants:
            assert entry.arrived
            if entry.character_id == client.character_id:
                continue
            bot = entry.session
            assert debug_pvp.is_debug_bot(bot)
            assert bot.gameplay_map == match.map_id
            assert bot.gameplay_match is match
            assert bot.gameplay_match_team == entry.team
            assert bot.gameplay_combat_state is combat
        # One session per bot: the lobby sessions were retired on arrival.
        assert _wait(lambda: len(_bot_sessions(server)) == sum(sizes) - 1, 5.0)
    finally:
        client.close()
    assert debug_pvp.active_arena(server, client.character_id) is None
    assert _wait(lambda: not _bot_sessions(server), 5.0)
    assert not any(
        entry.character_id == client.character_id
        for entry in client.registry.queued_for(debug_pvp.MATCH_MODES[mode])
    )


def test_a_1v1_runs_to_the_result_screen_and_the_bot_leaves(match_world, monkeypatch):
    monkeypatch.setattr(battleground_module, "MATCH_COUNTDOWN_SECONDS", 1.0)
    monkeypatch.setattr(battleground_module, "ROUND_INTERMISSION_SECONDS", 0.5)
    monkeypatch.setattr(battleground_module, "MATCH_OVER_DELAY_SECONDS", 0.5)
    monkeypatch.setattr(debug_pvp, "BOT_LEAVE_DELAY_SECONDS", 0.5)
    client = _AdminClient(match_world)
    server = match_world.server
    try:
        debug_pvp.handle_admin_command(server, client.home, ("1v1",))
        match = _wait(client.match, 10.0)
        assert _wait(lambda: all(e.accepted for e in match.participants
                                 if e.character_id != client.character_id), 5.0)
        client.accept(match)
        arena = client.arrive()
        # A fragile admin, so the bot can win both rounds quickly.
        pool = arena.gameplay_combat_state.player_health[arena.gameplay_remote_actor_id]
        pool[0] = pool[1] = 1000

        def finished():
            client.keep_moving()
            return match.phase == MatchPhase.OVER

        assert _wait(finished, 90.0, step=0.2), (match.phase, match.team_rounds_won)
        admin_entry = match.participant(client.character_id)
        (bot_entry,) = [e for e in match.participants if e is not admin_entry]
        assert match.winning_team == bot_entry.team
        assert match.team_rounds_won[bot_entry.team] == 2
        assert (bot_entry.kills, admin_entry.deaths) == (2, 2)
        # The bot presses Leave on the result screen and is gone.
        assert _wait(lambda: debug_pvp.active_arena(server, client.character_id) is None, 20.0)
        assert bot_entry.left
        assert _wait(lambda: not _bot_sessions(server), 5.0)
    finally:
        client.close()
