"""Wave banners follow the shared instance's actual spawn deadline."""
from dataclasses import replace
from unittest.mock import patch

import pytest

from dso_server.protocol.character_selection import BitReader
from dso_server.protocol.gameplay_protocol import (
    SERVER_TO_CLIENT_MSG_COMMAND,
    decode_message_batch,
    encode_screen_message,
)
from dso_server.services.character_server import CharacterServer
from dso_server.tests.test_event_waves_and_vendors import enter, world


def digits(session):
    return [
        decode_message_batch(data, bits)[0].arguments[0].uint32_value
        for data, bits, label in session.recorder.messages
        if label == "map-wave-countdown"
    ]


def banners(session):
    result = []
    for data, bits, label in session.recorder.messages:
        if label != "map-wave-start":
            continue
        reader = BitReader(data, bits)
        assert reader.read_uint8() == 0x85
        assert int.from_bytes(reader.read_bytes(2), "little") == SERVER_TO_CLIENT_MSG_COMMAND
        assert reader.read_uint32_le() == 0  # Nonmodal MessageWidget queue.
        result.append(reader.read_string())
        assert reader.read_string() == ""
        assert reader.read_uint32_le() == 0  # Actor context.
        assert reader.read_uint8() == 0xFF
        assert reader.remaining == 0
    return result


@pytest.fixture
def scheduled_world(world):
    session, runtime = enter(world, "e_anniversary_arena_01")
    wave = runtime.catalog.event("e_anniversary_arena_random_wave_01")
    with (
        patch("dso_server.services.map_events.time.perf_counter", return_value=100.0) as clock,
        patch.object(CharacterServer, "_apply_map_event_transition") as apply,
    ):
        yield world, session, runtime, wave, clock, apply


def test_screen_message_matches_native_decoder_layout():
    packet, bits = encode_screen_message("Wave 1")
    assert packet == bytes.fromhex("857001000000000600576176652031000000000000ff")
    assert bits == len(packet) * 8


@pytest.mark.parametrize("delay", [0, 2, 5, 6, 20])
def test_countdown_then_wave_start_once_at_the_spawn_deadline(scheduled_world, delay):
    case, session, runtime, wave, clock, apply = scheduled_world
    wave = replace(wave, start_delay=delay)
    case.server._schedule_map_local_event_start(runtime, wave)
    deadline = 100 + max(5, delay)
    assert runtime.pending_start_deadlines[wave.key] == deadline
    assert wave.key not in runtime.running
    assert digits(session) == ([5] if delay <= 5 else [])
    timers = tuple(runtime.scheduler.timers)
    case.server._schedule_map_local_event_start(runtime, wave)
    assert tuple(runtime.scheduler.timers) == timers
    for timer in sorted(timers, key=lambda handle: handle.absolute_deadline):
        clock.return_value = timer.absolute_deadline
        timer.run()
        if clock.return_value < deadline:
            assert wave.key not in runtime.running
            assert banners(session) == []
    assert digits(session) == [5, 4, 3, 2, 1]
    assert banners(session) == ["Wave 1"]
    assert wave.key in runtime.running
    apply.assert_called_once()
    # Repeated start callbacks and triggers cannot repeat the announcement.
    for timer in timers:
        timer.run()
    case.server._schedule_map_local_event_start(runtime, wave)
    assert banners(session) == ["Wave 1"]
    assert digits(session) == [5, 4, 3, 2, 1]


def test_cancel_and_reschedule_ignores_old_start_and_countdown_callbacks(scheduled_world):
    case, session, runtime, wave, clock, apply = scheduled_world
    case.server._schedule_map_local_event_start(runtime, wave)
    old_timers = tuple(runtime.scheduler.timers)
    case.server._stop_map_local_event(runtime, wave)
    # Even identical deadlines must not let the old callback start a new wave.
    case.server._schedule_map_local_event_start(runtime, wave)
    new_start = runtime.pending[wave.key]
    clock.return_value = 105
    for timer in old_timers:
        timer.run()
    assert wave.key not in runtime.running
    assert digits(session) == []
    assert banners(session) == []
    apply.assert_not_called()
    clock.return_value = 106
    new_start.run()
    assert banners(session) == ["Wave 1"]


def test_overdue_callbacks_skip_old_digits_and_flush_starts_only_once(scheduled_world):
    case, session, runtime, wave, clock, apply = scheduled_world
    case.server._schedule_map_local_event_start(runtime, wave)
    timers = tuple(runtime.scheduler.timers)
    start = runtime.pending[wave.key]
    clock.return_value = 105.3
    for timer in timers:
        if timer is not start:
            timer.run()
    assert digits(session) == [1]
    clock.return_value = 107
    case.server._flush_due_map_event_starts(runtime)
    for timer in timers:
        timer.run()
    assert digits(session) == [1]
    assert banners(session) == ["Wave 1"]
    apply.assert_called_once()


def test_replaced_instance_suppresses_its_timers(scheduled_world):
    case, session, runtime, wave, clock, apply = scheduled_world
    case.server._schedule_map_local_event_start(runtime, wave)
    case.server._map_event_registry().pop(runtime.instance_key)
    for timer in runtime.scheduler.timers:
        clock.return_value = timer.absolute_deadline
        timer.run()
    assert digits(session) == []
    assert banners(session) == []
    apply.assert_not_called()


def test_broadcast_reaches_instance_peers_and_late_joiners_only(scheduled_world):
    case, session, runtime, wave, clock, _apply = scheduled_world

    def peer(port):
        viewer = case._session(port, port)
        viewer.gameplay_map = session.gameplay_map
        viewer.gameplay_map_access_group_id = session.gameplay_map_access_group_id
        viewer.gameplay_map_event_runtime = runtime
        case.server.sessions[viewer.addr] = viewer
        return viewer

    ready = peer(50002)
    other_instance = peer(50003)
    other_instance.gameplay_map_event_runtime = object()
    loading = peer(50004)
    loading.gameplay_presence_ready = False
    departed = peer(50005)
    departed.gameplay_presence_departed = True
    offline = peer(50006)
    offline.connected = False
    case.server._schedule_map_local_event_start(runtime, wave)
    timers = sorted(runtime.scheduler.timers, key=lambda timer: timer.absolute_deadline)
    clock.return_value = timers[0].absolute_deadline
    timers[0].run()
    assert digits(ready) == [5]
    late = peer(50007)
    for timer in timers[1:]:
        clock.return_value = timer.absolute_deadline
        timer.run()
    assert digits(session) == digits(ready) == [5, 4, 3, 2, 1]
    assert digits(late) == [4, 3, 2, 1]
    assert banners(session) == banners(ready) == banners(late) == ["Wave 1"]
    for viewer in (other_instance, loading, departed, offline):
        assert digits(viewer) == banners(viewer) == []


@pytest.mark.parametrize("event_id,label", [
    ("e_anniversary_arena_random_wave_01", "Wave 1"),
    ("pw002_spawn_wave_01A", "Wave 1"),
    ("e_dra_03_lab_monsterwave_05", "Wave 5"),
    ("ed_platform_01_wave_10_normal_01", "Wave 10"),
    ("arena_wave_02_pw", "Wave 2"),
    ("arena_wave_03_scaling", "Wave 3"),
    ("arena_wave_04_event", "Wave 4"),
    ("e_cny_wave_01_start", "Wave 1"),
    ("e_anniversary_arena_boss_wave", "Boss wave"),
    ("e_hal_01_darkwood_ghost_fire_01_monsterwaves_single", "Monster wave"),
    ("pw200_cardhun_village_04_activate_soul_01_monster_wave", "Monster wave"),
    ("e_anniversary_arena_wave_countdown_01", None),
    ("e_dra_dojo_wave_done", None),
    ("e_dra_dojo_wave_stop", None),
    ("BOSS_spawn", None),
])
def test_wave_labels_exclude_presentation_and_completion_events(scheduled_world, event_id, label):
    _case, _session, _runtime, wave, _clock, _apply = scheduled_world
    assert replace(wave, event_id=event_id).wave_label == label


def test_nonwave_event_keeps_its_existing_timing(scheduled_world):
    case, session, runtime, wave, _clock, apply = scheduled_world
    event = replace(wave, event_id="BOSS_spawn", start_delay=0)
    case.server._schedule_map_local_event_start(runtime, event)
    assert event.key in runtime.running
    assert runtime.scheduler.timers == []
    assert digits(session) == banners(session) == []
    apply.assert_called_once()
