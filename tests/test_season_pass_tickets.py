"""Gold and Red Pass items follow the client's purchase rules."""

from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from dso_server.gameplay.season_pass import (
    SEASON_PASS_NO_SEASON_MESSAGE, SEASON_PASS_TICKETS, SeasonPassCatalog,
    load_season_pass_catalog, season_pass_state_for_season, season_pass_ticket_types,
)
from dso_server.protocol.gameplay_protocol import Message
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
    # A double ticket in the bag upgrades a track that has the standard pass.
    ("gold_pass_2", (1,), (3,)),
    ("gold_pass_2", (1, 3), ()),
    ("red_pass", (), (2,)),
    ("red_pass", (2,), (4,)),
    ("red_pass", (2, 4), ()),
    ("red_pass_2", (), (2, 4)),
    ("red_pass_2", (2,), (4,)),
    # The two tracks do not affect each other.
    ("red_pass_2", (1, 3), (2, 4)),
    ("gold_pass", (2, 4), (1,)),
])
def test_ticket_rules(template, active, expected):
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
    double = add_item(session, "gold_pass_2")
    standard = add_item(session, "gold_pass")
    _use(server, session, double)
    _use(server, session, standard)
    assert session.gameplay_season_pass["pass_types"] == (1, 3)
    assert _stack(session, "gold_pass").amount == 1


def _claim(server, session, *award_ids):
    """The money_cc a claim adds: level 1 pays 10 free and 30 gold."""
    catalog = SeasonPassCatalog({}, ((1, 0),), {1: ("Loot:money_cc,10", "Loot:money_cc,30", "")})
    before = session.gameplay_game_currencies["money_cc"]
    server._handle_season_pass_awards(session, SeasonPassQuery(
        GET_SEASON_PASS_AWARD_COMMAND, award_ids=award_ids,
    ), session.gameplay_season_pass, catalog)
    return session.gameplay_game_currencies["money_cc"] - before


def test_a_double_track_pays_every_level_twice(player):
    server, session, _ = player
    session.gameplay_game_currencies = {"money_cc": 1000}
    _use(server, session, add_item(session, "gold_pass_2"))
    # The client claims one ID per entitlement, all at once.
    assert _claim(server, session, 1, 101, 301) == 70
    assert _claim(server, session, 101, 301) == 0


def test_a_standard_track_pays_once(player):
    server, session, _ = player
    session.gameplay_game_currencies = {"money_cc": 1000}
    _use(server, session, add_item(session, "gold_pass"))
    assert _claim(server, session, 1, 101) == 40


def test_the_upgrade_pays_the_second_copy_of_claimed_levels(player):
    server, session, _ = player
    session.gameplay_game_currencies = {"money_cc": 1000}
    _use(server, session, add_item(session, "gold_pass"))
    assert _claim(server, session, 101) == 30
    _use(server, session, add_item(session, "gold_pass_2"))
    assert session.gameplay_season_pass["pass_types"] == (1, 3)
    assert _claim(server, session, 301) == 30
    assert _claim(server, session, 301) == 0


def _notifications(server, session, *award_ids):
    """(amount, item id, template name or None) of each item-received message."""
    catalog = SeasonPassCatalog({}, ((1, 0),), {
        1: ("Loot:money_cc,10", "Loot:item_rune_extension_stone,2", ""),
    })
    session.send_reliable_ordered_split = Mock()
    server._handle_season_pass_awards(session, SeasonPassQuery(
        GET_SEASON_PASS_AWARD_COMMAND, award_ids=award_ids,
    ), session.gameplay_season_pass, catalog)
    (call,) = session.send_reliable_ordered_split.call_args_list
    commands = decode_server_command_batch(call.args[0], call.kwargs["total_bit_length"])
    messages = [command.value for command in commands if isinstance(command.value, Message)]
    assert all(message.message_type == 2 for message in messages)
    return [
        (message.arguments[1].uint32_value, message.arguments[2].uint32_value,
         message.arguments[3].string_value if len(message.arguments) > 3 else None)
        for message in messages
    ]


def test_a_claim_shows_what_was_received(player):
    server, session, _ = player
    session.gameplay_game_currencies = {"money_cc": 1000}
    _use(server, session, add_item(session, "gold_pass"))
    (money, stones) = _notifications(server, session, 1, 101)
    assert money[0] == 10 and money[2] == "money_cc"
    stack = _stack(session, "item_rune_extension_stone")
    assert stones == (2, stack.item_id, None)
    # A virtual currency has no item, so it gets a fresh id of its own.
    assert money[1] < session.gameplay_next_item_id
    assert money[1] not in {item.item_id for item in session.gameplay_inventory.items}


def test_the_copies_of_a_reward_share_a_notification(player):
    server, session, _ = player
    session.gameplay_game_currencies = {"money_cc": 1000}
    _use(server, session, add_item(session, "gold_pass_2"))
    held = _stack(session, "item_rune_extension_stone").amount
    (stones,) = _notifications(server, session, 101, 301)
    assert stones[0] == 4
    assert _stack(session, "item_rune_extension_stone").amount == held + 4


def test_a_replayed_claim_shows_nothing(player):
    server, session, _ = player
    session.gameplay_game_currencies = {"money_cc": 1000}
    _use(server, session, add_item(session, "gold_pass"))
    assert _notifications(server, session, 1)
    assert _notifications(server, session, 1) == []


def test_the_second_copy_needs_the_double_ticket(player):
    server, session, _ = player
    session.gameplay_game_currencies = {"money_cc": 1000}
    _use(server, session, add_item(session, "gold_pass"))
    assert _claim(server, session, 301) == 0
    assert 301 not in session.gameplay_season_pass["claimed_awards"]


def test_two_standard_tickets_make_the_track_double(player):
    server, session, _ = player
    _use(server, session, add_item(session, "gold_pass", amount=2))
    _use(server, session, _stack(session, "gold_pass").item_id)
    assert session.gameplay_season_pass["pass_types"] == (1, 3)
    assert _stack(session, "gold_pass") is None
