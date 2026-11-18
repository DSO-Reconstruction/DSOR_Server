"""A client never keeps location effects past the map that created them.

The client holds a location effect until it is told to discard it, map changes
included, in a table indexed by the id's low 16 bits. Every map instance numbers
its effects from 0x10000, so one left behind is hit by a later NewLocationEffect:
registration fails and the client aborts in ~LocationEffect ("!this->IsValid()").
"""

from types import SimpleNamespace
from threading import Event, Thread
from unittest.mock import Mock

from dso_server.protocol.gameplay_protocol import decode_discard_location_effect_batch
from dso_server.services.base import CharacterService
from dso_server.tests.test_pvp_matchmaking import _session


def _visual(instance_id):
    return SimpleNamespace(
        instance_id=instance_id, owner_actor_id=7, position=(0.0, 0.0, 0.0), yaw=0.0,
        expires_tick=100, starts_tick=1, follows_owner=False,
        definition=SimpleNamespace(
            aura_radius=2.0, aura_height=2.0, resource_id="effect", values=(),
        ),
    )


def _client(port):
    session = _session(port, 1, "L")
    session.send_reliable_ordered = Mock()
    return session


def test_leaving_a_map_discards_every_effect_the_client_holds():
    session = _client(57101)
    session.gameplay_location_effect_ids = {0x10082, 0x10001}
    CharacterService._discard_client_location_effects(session)
    assert session.gameplay_location_effect_ids == set()
    (call,) = session.send_reliable_ordered.call_args_list
    assert call.kwargs["label"] == "map-exit-location-effect-discard"
    assert set(decode_discard_location_effect_batch(*call.args[0])[0]) == {0x10001, 0x10082}


def test_nothing_is_sent_when_the_client_holds_no_effects():
    session = _client(57102)
    CharacterService._discard_client_location_effects(session)
    session.send_reliable_ordered.assert_not_called()


def test_a_leaving_client_receives_no_new_effects():
    session = _client(57103)
    session.gameplay_exit_transition_started = True
    CharacterService._send_player_location_visuals_to(session, (_visual(0x10005),))
    session.send_reliable_ordered.assert_not_called()
    assert session.gameplay_location_effect_ids == set()


def test_a_client_still_on_the_map_keeps_receiving_effects():
    session = _client(57104)
    CharacterService._send_player_location_visuals_to(session, (_visual(0x10005),))
    assert session.gameplay_location_effect_ids == {0x10005}
    session.send_reliable_ordered.assert_called_once()


def test_effect_creation_and_discard_keep_packet_order_across_threads():
    session = _client(57105)
    creation_started = Event()
    finish_creation = Event()
    discard_started = Event()
    discard_finished = Event()
    sent = []

    def send(_payload, *, label, **_kwargs):
        if label == "player-location-effect-new":
            creation_started.set()
            assert finish_creation.wait(2)
        sent.append(label)

    session.send_reliable_ordered.side_effect = send

    def discard():
        discard_started.set()
        CharacterService._send_player_location_visuals_to(session, ())
        discard_finished.set()

    creating = Thread(target=CharacterService._send_player_location_visuals_to,
                      args=(session, (_visual(0x10018),)))
    discarding = Thread(target=discard)
    try:
        creating.start()
        assert creation_started.wait(2)
        discarding.start()
        assert discard_started.wait(2)
        assert not discard_finished.wait(0.05)
        assert sent == []
    finally:
        finish_creation.set()
        creating.join(2)
        if discarding.ident is not None:
            discarding.join(2)
    assert not creating.is_alive() and not discarding.is_alive()
    assert sent == ["player-location-effect-new", "player-location-effect-discard"]


def test_location_effect_allocator_stays_within_client_table_limits():
    """Client has a 10,000-entry table for (id & 0xFFFF).
    Index 0 is InvalidLocationEffectId, and indices >= 10,000 abort in ~LocationEffect.
    """
    combat = SimpleNamespace(
        player_location_visuals={},
        _next_player_location_visual_slot=1,
        _player_location_visual_epoch=1,
    )
    from dso_server.gameplay.local_combat_state import LocalCombatState
    allocate = LocalCombatState._allocate_player_location_visual_id.__get__(combat)

    allocated_ids = []
    for _ in range(25000):
        effect_id = allocate()
        allocated_ids.append(effect_id)
        slot = effect_id & 0xFFFF
        assert 1 <= slot < 10000, f"slot {slot} outside [1, 9999]"

    # Epoch must advance when slots wrap around
    assert combat._player_location_visual_epoch > 1


def test_location_effect_allocator_skips_active_slots():
    """An active visual's slot must not be reused to avoid colliding in client table."""
    combat = SimpleNamespace(
        player_location_visuals={
            0x10001: SimpleNamespace(instance_id=0x10001),
            0x10002: SimpleNamespace(instance_id=0x10002),
        },
        _next_player_location_visual_slot=1,
        _player_location_visual_epoch=1,
    )
    from dso_server.gameplay.local_combat_state import LocalCombatState
    allocate = LocalCombatState._allocate_player_location_visual_id.__get__(combat)

    # Slots 1 and 2 are active, so the first allocation must skip to slot 3
    id3 = allocate()
    assert (id3 & 0xFFFF) == 3


def test_send_visuals_forces_discard_on_slot_collision():
    """If an undiscarded effect still occupies the slot, the old effect must be discarded first."""
    session = _client(57106)
    # Session still holds 0x10005 (slot 5)
    session.gameplay_location_effect_ids = {0x10005}

    # Now a new visual arrives with ID 0x20005 (also slot 5)
    new_visual = _visual(0x20005)
    CharacterService._send_player_location_visuals_to(session, (new_visual,))

    # Must have sent discard for 0x10005 before or alongside creation
    calls = session.send_reliable_ordered.call_args_list
    labels = [c.kwargs.get("label") for c in calls]
    assert "player-location-effect-discard" in labels
    assert "player-location-effect-new" in labels

    # Check that 0x10005 was discarded
    discard_call = [c for c in calls if c.kwargs.get("label") == "player-location-effect-discard"][0]
    discarded_ids, _ = decode_discard_location_effect_batch(*discard_call.args[0])
    assert 0x10005 in discarded_ids
    assert session.gameplay_location_effect_ids == {0x20005}

