"""Global schedule changes update occupied worlds without a map reconnect."""

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from threading import Event, RLock, Thread
from types import SimpleNamespace

import pytest

from dso_server.gameplay.event_system import load_event_catalog
from dso_server.gameplay import event_system
from dso_server.gameplay.local_map_state import (
    load_local_map_exits, load_local_map_initialization, load_local_npc_definitions,
)
from dso_server.gameplay.map_event_state import EventSetDefinition, MapEventContent, MapEventRuntime
from dso_server.protocol.player_init_protocol import (
    ACTORS_ENTER_VICINITY_COMMAND, EVENT_UPDATE_COMMAND, EXIT_INFO_COMMAND,
    NEW_NPC_COMMAND, NPC_INFO_COMMAND, VARIATION_INFO_COMMAND,
    ServerCommand, decode_event_update, decode_server_command_batch,
)
from dso_server.runtime import file_revisions
from dso_server.runtime.transport import SessionRegistry
from dso_server.services import events as game_events
from dso_server.services import map_events
from dso_server.services import admin_events, admin_event_refresh
from dso_server.services.admin_events import EventAdministration
from dso_server.services.global_event_refresh import refresh_map_events
from dso_server.tests.test_event_waves_and_vendors import enter
from dso_server.tests import test_map_events as map_fixtures


MAP = "a0200_kingscity"
SARGON_DESTINATION = "g0001_beach_01_night_dun_00"
PHESTOS = "e_anniversary_merchant_phestos"


def decoded_world_commands(viewer):
    """Keep command order and exact bit counts from the captured send stream."""
    result = []
    for data, bits, _label in viewer.recorder.messages:
        command_id = int.from_bytes(data[1:3], "little")
        if data[0] == 0x84 and command_id == EVENT_UPDATE_COMMAND:
            result.append(ServerCommand(command_id, decode_event_update(data, bits), None))
        elif data[0] == 0x85 and command_id in {
            NEW_NPC_COMMAND, NPC_INFO_COMMAND, EXIT_INFO_COMMAND,
            VARIATION_INFO_COMMAND, ACTORS_ENTER_VICINITY_COMMAND,
        }:
            result.extend(decode_server_command_batch(data, bits))
    return result


@pytest.fixture
def occupied_world(tmp_path, monkeypatch):
    """Real catalogs and shared map state, with only network/timers isolated."""
    case = map_fixtures.MapEventServiceTests(methodName="runTest")
    case.setUp()
    server = case.server
    server.sessions = SessionRegistry(RLock())
    server.sessions[case.session.addr] = case.session
    server.admin_commands = SimpleNamespace(call=lambda operation: operation())
    schedule = tmp_path / "events.json"
    monkeypatch.setattr(game_events, "EVENT_SCHEDULE_PATH", schedule)
    app = SimpleNamespace(
        characters=server,
        login=SimpleNamespace(session_snapshot=lambda: ()),
    )
    manager = EventAdministration(SimpleNamespace(
        application=app, static_database_path=server.static_database_path,
        repository=SimpleNamespace(audit=lambda *a, **kw: None),
    ), schedule)
    state = SimpleNamespace(
        server=server, sessions=(), catalog=None, runtime=None,
        manager=manager, schedule=schedule,
    )

    def write(template, active):
        schedule.write_text(json.dumps({"events": [{
            "event_id": 65001, "template_id": template,
            "state": "active" if active else "expired",
        }]}), encoding="utf-8")
        # Writers publish their edit, as the CMS does (runtime/file_revisions.py).
        file_revisions.invalidate(schedule)
        return load_event_catalog(server.static_database_path, schedule)

    def initialize(template, active=False):
        state.catalog = write(template, active)
        first, runtime = enter(
            case, MAP,
            state.catalog.active_event_flags() | frozenset({"test_quest_phase"}),
        )
        second = case._session(50002, 2)
        second.gameplay_map = MAP
        second.gameplay_map_access_group_id = MAP
        second.gameplay_coordinate_frame = first.gameplay_coordinate_frame
        second.gameplay_monster_encounter = first.gameplay_monster_encounter
        second.gameplay_combat_state = first.gameplay_combat_state
        second.gameplay_last_move = None
        second.gameplay_spawn_position = (0, 0, 0)
        server.sessions[second.addr] = second
        assert server._attach_map_event_runtime(
            second, map_id=MAP, initial_flags=runtime.initial_flags,
            monster_level=100, scaling_difficulty=0, fresh_instance=False,
        ) is runtime
        runtime.begin_event(runtime.catalog.event("a0200_unlock_thabo"), now=123.0)
        state.local_running = dict(runtime.running)
        state.sessions = (first, second)
        state.runtime = runtime
        state.template = template
        initial, _ = load_local_map_initialization(
            server.static_database_path, MAP,
            active_event_flags=state.catalog.active_event_flags(),
            include_inactive_event_sets=True,
        )
        announced = {
            command.value.actor_id for command in initial
            if command.command_id == NEW_NPC_COMMAND
        }
        for viewer in state.sessions:
            viewer.gameplay_announced_npc_actor_ids = set(announced)
            viewer.gameplay_map_exits = tuple(
                definition for definition in load_local_map_exits(
                    server.static_database_path, MAP,
                    active_event_flags=state.catalog.active_event_flags(),
                ) if definition.active and definition.destination_map_id
            )
            # The older shared fixture rounds split bit lengths to bytes.
            # Preserve exact bits here so packet-order assertions decode the wire.
            viewer.send_reliable_ordered_split = (
                lambda packet, *, total_bit_length, label_prefix="split", _viewer=viewer, **_:
                _viewer.recorder((packet, total_bit_length), label=label_prefix)
            )
            viewer.recorder.messages.clear()
        return state

    def publish(active, *, updated=2, deferred=0):
        previous = state.catalog
        state.catalog = write(state.template, active)
        for viewer in state.sessions:
            viewer.recorder.messages.clear()
        result = manager._publish(previous, state.catalog)
        assert result["updated"] == updated, result
        assert result["deferred"] == deferred, result
        assert not result["errors"], result
        # Global edits must not reset local quest/access phases or running events.
        assert "test_quest_phase" in state.runtime.active_flags()
        assert state.runtime.running == state.local_running
        assert (state.template in state.runtime.active_flags()) is active
        return [decoded_world_commands(viewer) for viewer in state.sessions]

    state.initialize = initialize
    state.publish = publish
    state.write_catalog = write
    state.new_session = case._session
    try:
        yield state
    finally:
        case.doCleanups()


@pytest.mark.parametrize("initially_active", [False, True])
def test_sargon_portal_updates_for_both_players_across_repeated_toggles(
    occupied_world, initially_active,
):
    world = occupied_world.initialize("sargon_event", initially_active)
    portal = next(
        definition for definition in load_local_map_exits(
            world.server.static_database_path, MAP,
            active_event_flags=frozenset({"sargon_event"}),
        ) if definition.destination_map_id == SARGON_DESTINATION
    )
    for active in (not initially_active, initially_active, not initially_active):
        packets = world.publish(active)
        for viewer, commands in zip(world.sessions, packets):
            updates = [i for i, command in enumerate(commands)
                       if command.command_id == EVENT_UPDATE_COMMAND
                       and any(event.event_name == "sargon_event"
                               and event.flags[0] is active
                               for event in command.value.events)]
            assert updates, "the player's global event snapshot was not updated"
            snapshots = [(i, entry) for i, command in enumerate(commands)
                         if command.command_id == EXIT_INFO_COMMAND
                         for entry in command.value.exits
                         if entry.instance_guid == portal.instance_guid]
            assert snapshots, "the occupied map never refreshed the Sargon exit"
            index, latest = snapshots[-1]
            assert latest.field_10 == int(active)
            assert index > updates[0]
            assert any(definition.instance_guid == portal.instance_guid
                       and definition.active
                       for definition in viewer.gameplay_map_exits) is active


@pytest.mark.parametrize("initially_active", [False, True])
def test_anniversary_npc_refresh_reuses_registered_actors_for_every_viewer(
    occupied_world, initially_active,
):
    world = occupied_world.initialize("anniversary_event", initially_active)
    phestos = next(
        definition for definition in load_local_npc_definitions(
            world.server.static_database_path, MAP,
        ) if definition.template_id == PHESTOS
    )
    for active in (not initially_active, initially_active, not initially_active):
        packets = world.publish(active)
        for viewer, commands in zip(world.sessions, packets):
            assert not any(command.command_id == NEW_NPC_COMMAND for command in commands), (
                "the client already registered every map NPC during initialization"
            )
            eligible = world.server._active_npc_definitions(viewer)
            assert any(definition.template_id == PHESTOS for definition in eligible) is active
            update = next(i for i, command in enumerate(commands)
                          if command.command_id == EVENT_UPDATE_COMMAND
                          and any(event.event_name == "anniversary_event"
                                  and event.flags[0] is active
                                  for event in command.value.events))
            infos = [(i, command.value) for i, command in enumerate(commands)
                     if command.command_id == NPC_INFO_COMMAND
                     and command.value.actor_id == phestos.actor_id]
            assert infos, "the event change never refreshed the NPC's interactive state"
            assert all(index > update for index, _ in infos)
            assert infos[-1][1].enabled is active
            assert phestos.actor_id in viewer.gameplay_announced_npc_actor_ids


@pytest.mark.parametrize("departed", ["gameplay_exit_transition_started", "gameplay_presence_departed"])
def test_event_world_refresh_skips_a_viewer_already_leaving_the_map(occupied_world, departed):
    world = occupied_world.initialize("sargon_event")
    leaving = world.sessions[1]
    setattr(leaving, departed, True)
    packets = world.publish(True, updated=1, deferred=1)
    assert not leaving.recorder.messages
    assert not any(definition.destination_map_id == SARGON_DESTINATION
                   for definition in leaving.gameplay_map_exits)
    assert any(command.command_id == EXIT_INFO_COMMAND
               and any(entry.map_id == SARGON_DESTINATION and entry.field_10 == 1
                       for entry in command.value.exits)
               for command in packets[0])


def test_last_event_exit_closes_without_advertising_unseen_inactive_guids(
    occupied_world, monkeypatch,
):
    world = occupied_world.initialize("sargon_event", True)
    viewer = world.sessions[0]
    portal = next(definition for definition in viewer.gameplay_map_exits
                  if definition.destination_map_id == SARGON_DESTINATION)
    viewer.gameplay_map_exits = (portal,)
    # A map can have no remaining active exits. An inactive overlapping scene
    # variant must not be inserted into the client's exit cache during closure.
    foreign = replace(portal, instance_guid=b"\xf1" * 16, active=False)
    monkeypatch.setattr(map_events, "load_local_map_exits", lambda *a, **kw: (foreign,))
    world.server._publish_map_exits(
        world.runtime, (viewer,), disabled_event_sets=(portal.event_set_row,),
    )
    assert viewer.gameplay_map_exits == ()
    snapshots = [entry for command in decoded_world_commands(viewer)
                 if command.command_id == EXIT_INFO_COMMAND
                 for entry in command.value.exits]
    assert [(entry.instance_guid, entry.field_10) for entry in snapshots] == [
        (portal.instance_guid, 0),
    ]


def test_schedule_deadlines_open_and_close_an_occupied_map_without_rejoin(
    occupied_world, monkeypatch,
):
    now = datetime(2026, 10, 26, 12, tzinfo=timezone.utc)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now if tz is not None else now.replace(tzinfo=None)

    for module in (admin_events, admin_event_refresh, event_system):
        monkeypatch.setattr(module, "datetime", Clock)
    world = occupied_world.initialize("sargon_event")
    world.schedule.write_text(json.dumps({"events": [{
        "event_id": 65001, "template_id": "sargon_event", "state": "scheduled",
        "starts_at": (now + timedelta(seconds=1)).isoformat(),
        "ends_at": (now + timedelta(seconds=2)).isoformat(),
    }]}), encoding="utf-8")
    file_revisions.invalidate(world.schedule)
    world.manager.refresh_schedule()  # The observer's initial upcoming snapshot.
    for active in (True, False):
        for viewer in world.sessions:
            viewer.recorder.messages.clear()
        now += timedelta(seconds=1)
        world.manager.refresh_schedule()
        assert ("sargon_event" in world.runtime.active_flags()) is active
        assert world.runtime.running == world.local_running
        for viewer in world.sessions:
            commands = decoded_world_commands(viewer)
            assert any(command.command_id == EVENT_UPDATE_COMMAND
                       and any(entry.event_name == "sargon_event" and entry.flags[0] is active
                               for entry in command.value.events)
                       for command in commands)
            entries = [entry for command in commands if command.command_id == EXIT_INFO_COMMAND
                       for entry in command.value.exits if entry.map_id == SARGON_DESTINATION]
            assert entries and entries[-1].field_10 == int(active)


def test_global_flag_replacement_preserves_or_negative_and_suppressed_map_rules():
    rules = {
        0: EventSetDefinition(0, frozenset({"alpha", "beta"}), frozenset({"blocked"})),
        1: EventSetDefinition(1, frozenset(), frozenset({"alpha"})),
        2: EventSetDefinition(2, frozenset({"alpha"}), frozenset()),
        3: EventSetDefinition(3, frozenset({"quest_phase"}), frozenset()),
    }
    content = MapEventContent(
        "flag-rules", rules, frozenset({"alpha", "beta", "blocked", "quest_phase"}),
        (), (), (),
    )
    runtime = MapEventRuntime(
        content, SimpleNamespace(), instance_key=("flag-rules",),
        initial_flags=frozenset({"beta", "quest_phase", "selected_access"}),
    )
    runtime.running["local_encounter"] = 123.0
    pending = object()
    runtime.pending["next_wave"] = pending
    runtime.suppress_event_sets((2,))
    assert runtime.enabled_event_sets == {0, 1, 3}
    known = {"alpha", "beta", "blocked"}
    for active, expected in (
        ({"alpha"}, {0, 3}),       # OR alternative remains; inverse gate closes.
        ({"alpha", "blocked"}, {3}),
        ({"beta"}, {0, 1, 3}),
        (set(), {1, 3}),
        ({"alpha"}, {0, 3}),       # A spent/suppressed set must not respawn.
    ):
        with runtime.lock:
            runtime.replace_global_flags(known, active)
        assert runtime.enabled_event_sets == expected
        assert {"quest_phase", "selected_access", "local_encounter"} <= runtime.active_flags()
        assert runtime.running == {"local_encounter": 123.0}
        assert runtime.pending["next_wave"] is pending
        assert runtime.suppressed_event_sets == {2}


@pytest.mark.parametrize("initially_active", [False, True])
def test_failed_exit_delivery_is_repaired_by_the_schedule_watcher_without_another_edit(
    occupied_world, monkeypatch, initially_active,
):
    world = occupied_world.initialize("sargon_event", initially_active)
    active = not initially_active
    world.manager.refresh_schedule()
    published = []
    publish = world.manager._publish

    def observe_publish(*args):
        result = publish(*args)
        published.append(result)
        return result

    monkeypatch.setattr(world.manager, "_publish", observe_publish)
    viewer = world.sessions[1]
    send = viewer.send_reliable_ordered
    split = viewer.send_reliable_ordered_split
    failed = []

    def fail_once(label):
        if label.startswith("map-event-exit-info") and not failed:
            failed.append(label)
            raise OSError("simulated one-shot exit delivery failure")

    def send_once(packet, **kwargs):
        fail_once(kwargs.get("label", ""))
        return send(packet, **kwargs)

    def split_once(packet, **kwargs):
        fail_once(kwargs.get("label_prefix", ""))
        return split(packet, **kwargs)

    monkeypatch.setattr(viewer, "send_reliable_ordered", send_once)
    monkeypatch.setattr(viewer, "send_reliable_ordered_split", split_once)
    world.catalog = world.write_catalog("sargon_event", active)
    world.manager.refresh_schedule()
    assert len(failed) == 1
    assert len(published) == 1 and published[0]["deferred"] > 0
    assert published[0]["errors"]
    assert world.manager._pending_refresh

    monkeypatch.setattr(viewer, "send_reliable_ordered", send)
    monkeypatch.setattr(viewer, "send_reliable_ordered_split", split)
    # The first viewer may already have received the closed-exit edge. Keep
    # that client's delivered history; the failed viewer must receive it now.
    viewer.recorder.messages.clear()
    world.manager.refresh_schedule()  # The schedule bytes and state are unchanged.
    assert len(published) == 2
    assert published[-1]["updated"] == 2 and not published[-1]["deferred"]
    assert not published[-1]["errors"]
    assert not world.manager._pending_refresh
    for session in world.sessions:
        commands = decoded_world_commands(session)
        assert not any(command.command_id == NEW_NPC_COMMAND for command in commands)
        exits = [entry for command in commands if command.command_id == EXIT_INFO_COMMAND
                 for entry in command.value.exits if entry.map_id == SARGON_DESTINATION]
        assert exits and exits[-1].field_10 == int(active)
        assert any(definition.destination_map_id == SARGON_DESTINATION
                   for definition in session.gameplay_map_exits) is active


@pytest.mark.parametrize("initially_active", [False, True])
def test_late_joiner_uses_the_updated_shared_event_state(occupied_world, initially_active):
    world = occupied_world.initialize("sargon_event", initially_active)
    before = world.runtime.initial_flags
    world.publish(not initially_active)
    current_flags = world.runtime.active_flags()
    late = world.new_session(50003, 3)
    late.gameplay_map = MAP
    late.gameplay_map_access_group_id = MAP
    late.gameplay_last_move = None
    late.gameplay_spawn_position = (0, 0, 0)
    world.server.sessions[late.addr] = late
    # A join may have sampled the catalog before a concurrent schedule edit.
    # Attaching to the occupied instance must not reset its reconciled flags.
    attached = world.server._attach_map_event_runtime(
        late, map_id=MAP, initial_flags=before,
        monster_level=100, scaling_difficulty=0, fresh_instance=False,
    )
    assert attached is world.runtime
    assert attached.active_flags() == current_flags
    assert attached.running == world.local_running
    existing_npcs = {definition.actor_id
                     for definition in world.server._active_npc_definitions(world.sessions[0])}
    assert {definition.actor_id for definition in world.server._active_npc_definitions(late)} == existing_npcs
    update, _, _ = world.server._event_initialization_state(late)
    assert any(entry.event_name == "sargon_event" and entry.flags[0]
               for entry in update.events) is (not initially_active)
    world.server._publish_map_exits(attached, (late,))
    assert {definition.instance_guid for definition in late.gameplay_map_exits} == {
        definition.instance_guid for definition in world.sessions[0].gameplay_map_exits
    }
    assert any(definition.destination_map_id == SARGON_DESTINATION
               for definition in late.gameplay_map_exits) is (not initially_active)


def test_waiting_for_a_viewer_releases_other_gates_and_revalidates_a_map_transfer(
    occupied_world,
):
    world = occupied_world.initialize("sargon_event")
    first, leaving = world.sessions
    previous = world.catalog
    current = world.write_catalog("sargon_event", True)
    snapshots = tuple(
        (session, session.gameplay_character.character_id,
         (session.gameplay_map, session.gameplay_chase_generation))
        for session in world.sessions
    )
    attempted = Event()
    complete = Event()
    results, errors = [], []
    gate = leaving.gameplay_mutation_lock

    class ObservedGate:
        def acquire(self, *args, **kwargs):
            acquired = gate.acquire(*args, **kwargs)
            if not acquired:
                attempted.set()
            return acquired

        def release(self):
            gate.release()

    leaving.gameplay_mutation_lock = ObservedGate()

    def refresh():
        try:
            results.append(refresh_map_events(
                world.server, world.runtime, snapshots, previous, current,
            ))
        except Exception as error:
            errors.append(error)
        finally:
            complete.set()

    gate.acquire()
    worker = Thread(target=refresh, daemon=True)
    worker.start()
    try:
        assert attempted.wait(1), "world refresh never attempted the occupied mutation gate"
        acquired = first.gameplay_mutation_lock.acquire(timeout=0.5)
        assert acquired, "refresh held one player gate while waiting for another player's gate"
        first.gameplay_mutation_lock.release()
        assert not complete.is_set()
        # A transfer can finish while the refresh is waiting. The original
        # map/generation snapshot must be checked again after all gates are held.
        leaving.gameplay_map = "b1100_hub"
        leaving.gameplay_chase_generation += 1
        leaving.gameplay_map_event_runtime = None
    finally:
        gate.release()
        worker.join(timeout=5)
        leaving.gameplay_mutation_lock = gate
    assert complete.is_set(), "world refresh did not finish after the busy gate was released"
    assert not errors
    assert results == [((first.gameplay_character.character_id,), ())]
    assert not leaving.recorder.messages
    assert any(command.command_id == EXIT_INFO_COMMAND
               for command in decoded_world_commands(first))
