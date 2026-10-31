"""A client never keeps location effects past the map that created them.

The client holds a location effect until it is told to discard it, map changes
included, in a table indexed by the id's low 16 bits. Every map instance numbers
its effects from 0x10000, so one left behind is hit by a later NewLocationEffect:
registration fails and the client aborts in ~LocationEffect ("!this->IsValid()").
"""

from types import SimpleNamespace
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
