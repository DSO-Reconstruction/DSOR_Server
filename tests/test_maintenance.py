"""Native maintenance packets, admin authorization, and application deadlines."""

from contextlib import ExitStack
import threading
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from dso_server.app import ServerApplication
from dso_server.protocol.chat_protocol import decode_chat_text, encode_chat_text
from dso_server.protocol.gameplay_protocol import (
    MESSAGE_COMMAND, decode_message_batch, encode_maintenance_countdown,
)
from dso_server.runtime.maintenance import MaintenanceShutdown
from dso_server.services.character_server import CharacterServer
from dso_server.services.chat import ChatServer
from dso_server.tests.test_pvp_matchmaking import _session


class ManualTimer:
    def __init__(self, interval, callback, args=()):
        self.interval = interval
        self.callback = callback
        self.args = args
        self.started = False
        self.cancelled = False

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True

    def fire(self):
        # A callback already dequeued by a timer can run after cancel().
        self.callback(*self.args)


class Endpoint:
    def __init__(self, *sessions):
        self.sessions = {s.addr: s for s in sessions}

    def session_snapshot(self):
        return tuple(self.sessions.values())


def ready_session(port, character_id):
    session = _session(port, character_id, f"Player{character_id}")
    session.gameplay_initialization_sent = True
    session.gameplay_presence_ready = True
    return session


def seconds_received(session):
    values = []
    for data, bits, label in session.recorder.messages:
        if label == "maintenance-countdown":
            message, actor_id = decode_message_batch(data, bits)
            assert message.message_type == 0 and actor_id == 0
            assert len(message.arguments) == 1 and message.arguments[0].kind == 1
            values.append(message.arguments[0].uint32_value)
    return values


@pytest.fixture
def maintenance():
    clock = SimpleNamespace(now=100.0)
    home = ready_session(52101, 1)
    away = ready_session(52102, 2)
    servers = (Endpoint(home), Endpoint(away))
    timers = []
    stopped = Mock()

    def timer_factory(*args, **kwargs):
        timer = ManualTimer(*args, **kwargs)
        timers.append(timer)
        return timer

    controller = MaintenanceShutdown(
        servers, stopped, clock=lambda: clock.now, timer_factory=timer_factory,
    )
    yield SimpleNamespace(
        controller=controller, clock=clock, home=home, away=away,
        servers=servers, timers=timers, stopped=stopped,
    )
    controller.close()


def test_native_five_minute_packet():
    packet, bits = encode_maintenance_countdown(300)
    # MessageCommand, subtype 0, one kind-1 integer (seconds), global actor.
    expected = bytes.fromhex("8500000001000000012c01000000000000ff")
    expected = expected[:1] + MESSAGE_COMMAND.to_bytes(2, "little") + expected[3:]
    assert packet == expected
    assert bits == 144


@pytest.mark.parametrize("seconds", [-1, 0x80000000, 1.5, True])
def test_countdown_rejects_values_the_native_signed_integer_cannot_use(seconds):
    with pytest.raises(ValueError):
        encode_maintenance_countdown(seconds)


def test_broadcasts_to_both_gameplay_endpoints_and_stops_at_deadline(maintenance):
    m = maintenance
    m.controller.schedule(5)
    assert seconds_received(m.home) == seconds_received(m.away) == [300]
    timer, = m.timers
    assert timer.started and timer.interval == 300
    m.stopped.assert_not_called()
    m.clock.now += 300
    timer.fire()
    timer.fire()
    m.stopped.assert_called_once_with()


@pytest.mark.parametrize("attribute,value", [
    ("connected", False), ("gameplay_mode", False),
    ("gameplay_initialization_sent", False), ("gameplay_presence_ready", False),
    ("gameplay_presence_departed", True), ("gameplay_character", None),
])
def test_only_initialized_current_gameplay_sessions_receive_timer(maintenance, attribute, value):
    m = maintenance
    setattr(m.away, attribute, value)
    m.controller.schedule(1)
    assert seconds_received(m.home) == [60]
    assert seconds_received(m.away) == []


def test_late_login_and_map_handoff_receive_remaining_seconds(maintenance):
    m = maintenance
    m.controller.schedule(5)
    m.home.gameplay_presence_departed = True
    arriving = ready_session(52103, 1)
    m.clock.now += 72.25
    m.controller.notify_session(arriving)
    assert seconds_received(arriving) == [228]
    assert len(m.timers) == 1 and m.timers[0].interval == 300


def test_a_lost_client_does_not_block_other_notifications_or_shutdown(maintenance):
    m = maintenance
    m.home.send_reliable_ordered = Mock(side_effect=OSError("closed socket"))
    m.controller.schedule(1)
    assert seconds_received(m.away) == [60]
    m.timers[0].fire()
    m.stopped.assert_called_once_with()


def test_rescheduling_invalidates_an_already_queued_old_timer(maintenance):
    m = maintenance
    m.controller.schedule(1)
    old = m.timers[0]
    m.clock.now += 10
    m.controller.schedule(5)
    assert old.cancelled
    old.fire()
    m.stopped.assert_not_called()
    assert seconds_received(m.home) == [60, 300]
    m.timers[1].fire()
    m.stopped.assert_called_once_with()


def test_manual_stop_cancels_pending_shutdown_and_late_notices(maintenance):
    m = maintenance
    m.controller.schedule(5)
    m.controller.close()
    assert m.timers[0].cancelled
    m.timers[0].fire()
    m.controller.notify_session(m.home)
    assert seconds_received(m.home) == [300]
    m.stopped.assert_not_called()
    with pytest.raises(ValueError, match="in progress"):
        m.controller.schedule(1)


def test_zero_minutes_announces_zero_and_requests_immediate_shutdown(maintenance):
    m = maintenance
    m.controller.schedule(0)
    assert seconds_received(m.home) == [0]
    assert m.timers[0].interval == 0
    m.timers[0].fire()
    m.stopped.assert_called_once_with()


def test_ready_hook_notifies_only_after_initialization_followups(maintenance):
    m = maintenance
    m.home.gameplay_presence_ready = False
    m.controller.schedule(5)
    m.clock.now += 30
    server = CharacterServer.__new__(CharacterServer)
    server.maintenance = m.controller
    state = SimpleNamespace(
        persisted_state=None, gameplay_map="a0200_kingscity",
        map_access=SimpleNamespace(
            request_map_id="a0200_kingscity", access_group_id="",
            map_difficulty=0, scaling_difficulty=0,
        ),
    )
    completed = []
    methods = (
        "_select_dungeon_instance", "_resolve_initialization_inventory", "_encode_initialization_new_player",
        "_resolve_initialization_talents", "_resolve_initialization_persistent_effects",
        "_resolve_initialization_vitals", "_apply_initialization_state",
        "_initialize_respawn_state", "_announce_battleground", "_load_initialization_world",
        "_build_initialization_commands", "_send_initialization_commands",
        "_restore_consumable_timers", "_log_initialization_summary",
        "_persist_gameplay_session", "_announce_remote_presence", "_bind_battleground_session",
    )
    with ExitStack() as patches:
        for name in methods:
            patches.enter_context(patch.object(CharacterServer, name))
        patches.enter_context(patch.object(CharacterServer, "_begin_local_player_initialization", return_value=True))
        patches.enter_context(patch.object(CharacterServer, "_resolve_initialization_character", return_value=state))
        patches.enter_context(patch.object(
            CharacterServer, "_send_initialization_followups",
            side_effect=lambda *args: completed.append(seconds_received(m.home)),
        ))
        server._send_local_player_initialization(m.home)
    assert completed == [[]]
    assert m.home.gameplay_presence_ready
    assert seconds_received(m.home) == [270]


@pytest.fixture
def admin_chat(maintenance):
    m = maintenance
    server = ChatServer.__new__(ChatServer)
    server.gameplay_servers = m.servers
    server.shutdown_callback = m.controller.schedule
    server._audit = Mock()
    server._channel_recipients = Mock(return_value=())
    admin = ready_session(52201, 1)
    admin.chat_user_id = 1
    admin.chat_user_name = "Admin"
    admin.chat_is_admin = True
    admin.chat_joined_channels = {"map"}
    return server, admin


def chat_command(server, session, text):
    # A forged 'hb' administrator display tag cannot grant privileges.
    packet = encode_chat_text(f"sm#map#en#hb#{text}", from_client=True)
    server._on_char_op(session, packet)
    return decode_chat_text(session.recorder.messages[-1][0], from_client=False)


def test_admin_command_schedules_native_countdown_without_public_chat(admin_chat, maintenance):
    server, admin = admin_chat
    reply = chat_command(server, admin, "!shutdown 3")
    assert "3 minute(s)" in reply
    assert seconds_received(maintenance.home) == [180]
    server._channel_recipients.assert_not_called()


def test_shutdown_defaults_to_five_minutes(admin_chat, maintenance):
    server, admin = admin_chat
    chat_command(server, admin, "!shutdown")
    assert seconds_received(maintenance.home) == [300]


def test_non_admin_cannot_schedule_even_with_forged_chat_tag(admin_chat, maintenance):
    server, admin = admin_chat
    admin.chat_is_admin = False
    assert "administrator privileges" in chat_command(server, admin, "!shutdown 0")
    assert not maintenance.timers
    assert not seconds_received(maintenance.home)


@pytest.mark.parametrize("text", ["!shutdown -1", "!shutdown 1441", "!shutdown nan", "!shutdown 1.5", "!shutdown 1 extra"])
def test_bad_arguments_do_not_change_a_pending_deadline(admin_chat, maintenance, text):
    server, admin = admin_chat
    maintenance.controller.schedule(10)
    assert "Command failed" in chat_command(server, admin, text)
    assert len(maintenance.timers) == 1 and not maintenance.timers[0].cancelled
    assert seconds_received(maintenance.home) == [600]


def test_help_advertises_shutdown(admin_chat):
    server, admin = admin_chat
    assert "!shutdown [minutes]" in chat_command(server, admin, "!help")


def test_application_run_loop_uses_normal_stop_and_database_drain():
    app = ServerApplication.__new__(ServerApplication)
    app._stop_event = threading.Event()
    app._started = True
    app.start = Mock()
    calls = []
    app.maintenance = MaintenanceShutdown((), app._stop_event.set, timer_factory=ManualTimer)
    app.stall_watchdog = SimpleNamespace(stop=lambda: calls.append("watchdog"), statistics=lambda: {"stall_count": 0})
    app.autosaver = SimpleNamespace(stop=lambda: calls.append("autosaver"))
    app.accounts = SimpleNamespace(stop=lambda: calls.append("http"))
    app.services = tuple(SimpleNamespace(stop=lambda n=n: calls.append(n)) for n in ("gateway", "login", "map", "chat"))
    app.account_store = SimpleNamespace(close=lambda: calls.append("drain-players"))
    app.account_registry = SimpleNamespace(close=lambda: calls.append("close-accounts"))
    app.maintenance.schedule(1)
    app.maintenance._timer.fire()
    app.serve_forever()
    assert calls == ["watchdog", "autosaver", "http", "chat", "map", "login", "gateway", "drain-players", "close-accounts"]
    assert not app._started
    with pytest.raises(ValueError):
        app.maintenance.schedule(1)


def test_real_timer_requests_stop_without_running_application_cleanup_on_timer_thread():
    stop_requested = threading.Event()
    controller = MaintenanceShutdown((), stop_requested.set)
    try:
        controller.schedule(0)
        assert stop_requested.wait(1.0)
        with pytest.raises(ValueError, match="in progress"):
            controller.schedule(1)
    finally:
        controller.close()


def test_social_writer_failure_does_not_skip_player_database_drain():
    app = ServerApplication.__new__(ServerApplication)
    app._started = True
    app.maintenance = SimpleNamespace(close=lambda: None)
    app.stall_watchdog = SimpleNamespace(stop=lambda: None, statistics=lambda: {"stall_count": 0})
    app.autosaver = SimpleNamespace(stop=lambda: None)
    app.accounts = None
    app.services = ()
    drained = []

    def fail():
        raise TimeoutError("social disk stalled")

    app.social_store = SimpleNamespace(close=fail)
    app.account_store = SimpleNamespace(close=lambda: drained.append("players"))
    app.account_registry = SimpleNamespace(close=lambda: drained.append("accounts"))
    with pytest.raises(TimeoutError, match="social disk stalled"):
        app.stop()
    assert drained == ["players", "accounts"]
