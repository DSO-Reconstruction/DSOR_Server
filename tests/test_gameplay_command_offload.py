"""Durable actions share a FIFO with movement without blocking UDP intake."""

import threading
from types import SimpleNamespace

from dso_server.runtime.transport import RakNetServer
from dso_server.services.character_server import CharacterServer


def make_service():
    service = CharacterServer.__new__(CharacterServer)
    RakNetServer.__init__(service, "127.0.0.1", 0, "offload-test", gameplay_ingress_workers=2)
    service.gameplay_command_offload = True
    session = service._session(("127.0.0.1", 50001))
    session.connected = session.gameplay_mode = True
    session.gameplay_map = "test-map"
    return service, session


def test_inventory_disk_wait_does_not_block_other_sessions_or_reorder_movement():
    service, session = make_service()
    entered, release, moved, other_finished = (threading.Event() for _ in range(4))
    order = []

    def inventory(s, payload):
        if s is session:
            order.append("inventory-start")
            entered.set()
            assert release.wait(3)
            order.append("inventory-end")
        else:
            other_finished.set()

    def move(s, payload):
        order.append("move")
        moved.set()

    service._handle_inventory_item_action = inventory
    service._handle_gameplay_move = move
    other = service._session(("127.0.0.1", 50002))
    other.connected = other.gameplay_mode = True
    other.gameplay_map = "test-map"
    try:
        service._on_char_op(session, b"\x8b\x55\x00")
        assert entered.wait(2)
        service._on_char_op(session, b"\x8b\x5f\x00")
        service._on_char_op(other, b"\x8b\x55\x00")
        assert other_finished.wait(2)
        assert not moved.is_set()
        release.set()
        assert moved.wait(2)
        assert order == ["inventory-start", "inventory-end", "move"]
    finally:
        release.set()
        service.gameplay_ingress_scheduler.shutdown(wait=True, timeout=5)
        service.gameplay_deadline_scheduler.shutdown()
        service.gameplay_initialization_scheduler.shutdown()


def test_queued_command_retains_its_exact_bit_length():
    service, session = make_service()
    finished = threading.Event()
    observed = []

    def inventory(s, payload):
        observed.append(s.current_message_bit_length)
        finished.set()

    service._handle_inventory_item_action = inventory
    try:
        session.current_message_bit_length = 27
        service._on_char_op(session, b"\x8b\x55\x00\x00")
        session.current_message_bit_length = 30
        assert finished.wait(2)
        assert observed == [27]
    finally:
        service.gameplay_ingress_scheduler.shutdown(wait=True, timeout=5)
        service.gameplay_deadline_scheduler.shutdown()
        service.gameplay_initialization_scheduler.shutdown()


def test_arena_phase_change_discards_stale_cast_but_preserves_inventory_request():
    service, session = make_service()
    scheduled = []
    service._schedule_gameplay_skill_dispatch = lambda s, token: scheduled.append(token)
    calls = []
    service._handle_inventory_item_action = lambda s, payload: calls.append("inventory")
    service._handle_gameplay_move = lambda s, payload: calls.append("move")
    session.gameplay_match = SimpleNamespace(round_index=0, phase="countdown")
    try:
        service._on_char_op(session, b"\x8b\x5f\x00")
        service._on_char_op(session, b"\x8b\x55\x00")
        session.gameplay_match.phase = "playing"
        service._on_char_op(session, b"\x8b\x5f\x00")
        service._drain_gameplay_skill_dispatch(session, scheduled[0])
        assert calls == ["inventory", "move"]
    finally:
        service.gameplay_ingress_scheduler.shutdown()
        service.gameplay_deadline_scheduler.shutdown()
        service.gameplay_initialization_scheduler.shutdown()


def test_phase_change_without_new_input_preserves_queued_durable_command():
    service, session = make_service()
    scheduled = []
    service._schedule_gameplay_skill_dispatch = lambda s, token: scheduled.append(token)
    calls = []
    service._handle_inventory_item_action = lambda s, payload: calls.append("inventory")
    session.gameplay_match = SimpleNamespace(round_index=0, phase="countdown")
    try:
        service._on_char_op(session, b"\x8b\x55\x00")
        session.gameplay_match.phase = "playing"
        service._drain_gameplay_skill_dispatch(session, scheduled[0])
        assert calls == ["inventory"]
    finally:
        service.gameplay_ingress_scheduler.shutdown()
        service.gameplay_deadline_scheduler.shutdown()
        service.gameplay_initialization_scheduler.shutdown()
