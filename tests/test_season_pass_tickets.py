"""Gold and Red Pass items follow the client's purchase rules."""

from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from dso_server.gameplay.season_pass import (
    SEASON_PASS_NO_SEASON_MESSAGE, SEASON_PASS_TICKETS, load_season_pass_catalog,
    season_pass_state_for_season, season_pass_ticket_types,
)
from dso_server.protocol.player_init_protocol import decode_server_command_batch
from dso_server.protocol.season_pass_protocol import (
    GET_SEASON_PASS_AWARD_COMMAND, SeasonPassQuery,
)
from dso_server.services.base import CharacterService
from dso_server.services.chat import ChatServer
from dso_server.tests.test_rune_progression import act, add_item, player  # noqa: F401 (fixture)


USE_ITEM = 0x13
SEASON = 70001


@pytest.mark.parametrize("template,active,expected", [
    ("gold_pass", (), (1,)),
    # "Can be purchased 2 times": the second standard ticket doubles the track.
    ("gold_pass", (1,), (3,)),
    ("gold_pass", (1, 3), ()),
    ("gold_pass_2", (), (1, 3)),
    ("gold_pass_2", (1,), ()),
    ("gold_pass_2", (1, 3), ()),
    ("red_pass", (), (2,)),
    ("red_pass", (2,), (4,)),
    ("red_pass", (2, 4), ()),
    ("red_pass_2", (), (2, 4)),
    ("red_pass_2", (2,), ()),
    # The two tracks do not affect each other.
    ("red_pass_2", (1, 3), (2, 4)),
    ("gold_pass", (2, 4), (1,)),
])
def test_ticket_rules_match_the_client_shop_filter(template, active, expected):
    assert season_pass_ticket_types(template, active) == expected


def test_other_items_are_not_tickets():
    assert season_pass_ticket_types("item_rune_extension_stone", ()) is None


@pytest.fixture(autouse=True)
def no_starter_tickets(player):
    """New characters carry one of each ticket; these tests add their own."""
    inventory = player[1].gameplay_inventory
    tickets = {item.item_id for item in inventory.items
               if item.template_id in SEASON_PASS_TICKETS}
    inventory.items[:] = [item for item in inventory.items if item.item_id not in tickets]
    for item_id in tickets:
        inventory.storage_58.pop(item_id, None)


def _use(server, session, item_id, *, season=SEASON):
    with patch.object(CharacterService, "_season_pass_season", return_value=season):
        return act(server, session, item_id, USE_ITEM)


def _stack(session, template):
    return next((item for item in session.gameplay_inventory.items
                 if item.template_id == template), None)


def test_a_ticket_opens_its_track_and_tells_the_client_at_once(player):
    server, session, _ = player
    session.send_reliable_ordered_split = Mock()
    _use(server, session, add_item(session, "red_pass"))
    assert session.gameplay_season_pass["pass_types"] == (2,)
    assert _stack(session, "red_pass") is None
    (call,) = [
        call for call in session.send_reliable_ordered_split.call_args_list
        if call.kwargs.get("label_prefix") == "season-pass-ticket-activated"
    ]
    commands = decode_server_command_batch(
        call.args[0], call.kwargs["total_bit_length"],
    )
    (result,) = [command.value for command in commands
                 if getattr(command.value, "pass_types", None) is not None]
    assert result.pass_types == (2,) and result.field_20 == 1


def test_a_season_opens_on_level_one():
    assert season_pass_state_for_season({}, SEASON)["level"] == 1
    # A character that met this season at level 0 is lifted too.
    assert season_pass_state_for_season({"season_id": SEASON}, SEASON)["level"] == 1
    # A new run starts over, levels already earned in this one stay.
    assert season_pass_state_for_season({"season_id": 1, "level": 9}, SEASON)["level"] == 1
    assert season_pass_state_for_season({"season_id": SEASON, "level": 9}, SEASON)["level"] == 9
    assert season_pass_state_for_season({}, None)["level"] == 0


def test_experience_keeps_the_opening_level(player):
    server = player[0]
    catalog = load_season_pass_catalog(server.static_database_path)
    first_threshold = dict(catalog.level_thresholds)[2]
    assert catalog.level_for_experience(0) == 1
    assert catalog.level_for_experience(first_threshold - 1) == 1
    assert catalog.level_for_experience(first_threshold) == 2


def test_the_first_row_opens_on_the_tracks_a_ticket_paid_for(player):
    server, session, _ = player
    _use(server, session, add_item(session, "red_pass"))
    catalog = load_season_pass_catalog(server.static_database_path)
    state = session.gameplay_season_pass
    # Free and red open, gold has no ticket, level 2 is not reached.
    server._handle_season_pass_awards(session, SeasonPassQuery(
        GET_SEASON_PASS_AWARD_COMMAND, award_ids=(1, 101, 201, 2),
    ), state, catalog)
    assert set(session.gameplay_season_pass["claimed_awards"]) == {1, 201}


def test_a_ticket_is_kept_while_no_season_runs_and_the_player_is_told(player):
    server, session, _ = player
    server.system_chat_callback = Mock()
    _use(server, session, add_item(session, "red_pass"), season=None)
    assert _stack(session, "red_pass").amount == 1
    assert not session.gameplay_season_pass.get("pass_types")
    server.system_chat_callback.assert_called_once_with(
        session.gameplay_character.character_id, SEASON_PASS_NO_SEASON_MESSAGE,
    )


def test_a_used_ticket_sends_no_chat_message(player):
    server, session, _ = player
    server.system_chat_callback = Mock()
    _use(server, session, add_item(session, "red_pass"))
    server.system_chat_callback.assert_not_called()


def test_a_system_message_goes_to_the_players_chat_session():
    sent = []
    session = object()
    chat = SimpleNamespace(
        _chat_session_for_user_id=lambda user_id: session if user_id == 7 else None,
        _send_system_message=lambda target, text: sent.append((target, text)),
    )
    ChatServer.send_system_message_to_user(chat, 7, "hello")
    ChatServer.send_system_message_to_user(chat, 8, "nobody online")
    assert sent == [(session, "hello")]


def test_a_refused_ticket_stays_in_the_bag(player):
    server, session, _ = player
    standard = add_item(session, "gold_pass")
    double = add_item(session, "gold_pass_2")
    _use(server, session, standard)
    _use(server, session, double)
    assert session.gameplay_season_pass["pass_types"] == (1,)
    assert _stack(session, "gold_pass_2").amount == 1


def test_two_standard_tickets_make_the_track_double(player):
    server, session, _ = player
    _use(server, session, add_item(session, "gold_pass", amount=2))
    _use(server, session, _stack(session, "gold_pass").item_id)
    assert session.gameplay_season_pass["pass_types"] == (1, 3)
    assert _stack(session, "gold_pass") is None
