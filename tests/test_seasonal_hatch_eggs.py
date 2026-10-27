"""Egg incubation: start, feed, hatch, persistence and invalid requests."""

from dataclasses import replace
from datetime import timedelta

import pytest

from dso_server.protocol import seasonal_activity_protocol as proto
from dso_server.protocol.player_init_protocol import (
    CURRENCY_CHANGED_COMMAND,
    INVENTORY_INFO_COMMAND,
    USER_HATCH_EGGS_INFO_COMMAND,
)
from dso_server.services.seasonal_activities import (
    HATCH_DEADLINE_KEY,
    HATCH_FEEDS_KEY,
)
from dso_server.tests.test_seasonal_protocol import NOW, seasonal  # noqa: F401


GOOSE = "super_golden_goose"
DRAGON = "magic_dragon_egg_fossil"


def operate(world, egg, operation):
    return world.dispatch(proto.encode_operate_hatch_eggs(egg, operation))


def set_draken(world, amount):
    currency = world.session.gameplay_currency_state
    world.session.gameplay_currency_state = replace(
        currency, values=(currency.values[0], currency.values[1], amount),
    )


def hatch_info(world):
    infos = world.commands(USER_HATCH_EGGS_INFO_COMMAND)
    assert infos, "every operation answers with UserHatchEggsInfo"
    return infos[-1].value


def calendar(moment):
    return proto.hatch_eggs_info(((1, 0, moment),)).eggs[0].values


def test_goose_lifecycle_start_feed_hatch(seasonal):
    world = seasonal
    world.add_item(GOOSE, 2)
    set_draken(world, 250)

    operate(world, GOOSE, proto.HATCH_OPERATION_START)
    info = hatch_info(world)
    deadline = NOW + timedelta(seconds=259200)
    assert info.counters == ((1, 0),)
    assert info.eggs[0].egg_id == 1 and info.eggs[0].values == calendar(deadline)
    assert world.count(GOOSE) == 1  # the Growth item is consumed on start
    assert world.session.gameplay_timed_offer_purchases[
        HATCH_DEADLINE_KEY.format(egg=GOOSE)
    ] == int(deadline.timestamp())
    batch = world.batches()[-1][1]
    assert [c.command_id for c in batch][0] == INVENTORY_INFO_COMMAND
    assert batch[-1].command_id == USER_HATCH_EGGS_INFO_COMMAND

    # A widget reopen without a known time resends; nothing is consumed again.
    operate(world, GOOSE, proto.HATCH_OPERATION_START)
    assert world.count(GOOSE) == 1
    assert hatch_info(world).counters == ((1, 0),)

    operate(world, GOOSE, proto.HATCH_OPERATION_FEED)
    assert world.session.gameplay_currency_state.draken == 150
    assert hatch_info(world).counters == ((1, 1),)
    # The un-fed deadline is never rewritten; the client subtracts feeds.
    assert hatch_info(world).eggs[0].values == calendar(deadline)
    assert CURRENCY_CHANGED_COMMAND in [c.command_id for c in world.batches()[-1][1]]
    operate(world, GOOSE, proto.HATCH_OPERATION_FEED)
    assert world.session.gameplay_currency_state.draken == 50

    # Not enough Draken for a third feed: nothing changes.
    before = dict(world.session.gameplay_timed_offer_purchases)
    operate(world, GOOSE, proto.HATCH_OPERATION_FEED)
    assert world.session.gameplay_currency_state.draken == 50
    assert world.session.gameplay_timed_offer_purchases == before
    assert hatch_info(world).counters == ((1, 2),)  # refresh after rejection

    # Hatching before the (fed) deadline is refused.
    world.now = deadline - timedelta(seconds=2 * 3600 + 1)
    operate(world, GOOSE, proto.HATCH_OPERATION_HATCH)
    assert world.count("super_golden_goose_egg") == 0
    assert world.session.gameplay_timed_offer_purchases == before

    # Two feeds remove two hours; the roll uses 500 + 2 * 10 of 10000.
    world.now = deadline - timedelta(seconds=2 * 3600)
    world.rolls = [519]
    operate(world, GOOSE, proto.HATCH_OPERATION_HATCH)
    assert world.count("super_golden_goose_egg") == 1
    assert world.count("golden_goose_feather") == 0
    assert hatch_info(world) == proto.hatch_eggs_info(())
    claims = world.session.gameplay_timed_offer_purchases
    assert HATCH_DEADLINE_KEY.format(egg=GOOSE) not in claims
    assert HATCH_FEEDS_KEY.format(egg=GOOSE) not in claims

    # Replaying the hatch grants nothing.
    operate(world, GOOSE, proto.HATCH_OPERATION_HATCH)
    assert world.count("super_golden_goose_egg") == 1


def test_failed_roll_grants_the_second_award(seasonal):
    world = seasonal
    world.add_item(GOOSE)
    operate(world, GOOSE, proto.HATCH_OPERATION_START)
    world.now = NOW + timedelta(seconds=259200)
    world.rolls = [500]  # 5.00% base chance fails at 500
    operate(world, GOOSE, proto.HATCH_OPERATION_HATCH)
    assert world.count("golden_goose_feather") == 1
    assert world.count("super_golden_goose_egg") == 0


def test_dragon_egg_feeds_from_wallet_currency_and_persists_across_relog(seasonal):
    world = seasonal
    world.add_item(DRAGON)
    world.session.gameplay_game_currencies["dragon_blood_essence"] = 3
    operate(world, DRAGON, proto.HATCH_OPERATION_START)
    operate(world, DRAGON, proto.HATCH_OPERATION_FEED)
    assert world.session.gameplay_game_currencies["dragon_blood_essence"] == 2
    assert hatch_info(world).counters == ((2, 1),)

    saved = world.saved()
    assert saved.timed_offer_purchases[HATCH_FEEDS_KEY.format(egg=DRAGON)] == 1
    assert saved.game_currencies["dragon_blood_essence"] == 2

    restored = world.relog()
    assert restored.gameplay_timed_offer_purchases[HATCH_FEEDS_KEY.format(egg=DRAGON)] == 1
    # Initialization: the retail topology carries the state, the local
    # projection drops it, and the follow-up batch republishes it.
    info = world.server._seasonal_initialization(restored, None)
    assert info.counters == ((2, 1),)
    world.sent.reset_mock()
    world.server._seasonal_send_initial_hatch_info(restored)
    assert hatch_info(world).counters == ((2, 1),)

    # 30 days less one 12-hour feed.
    world.now = NOW + timedelta(seconds=2592000 - 43200)
    operate(world, DRAGON, proto.HATCH_OPERATION_HATCH)
    assert world.count("magic_dragon_egg_12baby") == 1


def test_no_followup_without_incubation(seasonal):
    world = seasonal
    world.sent.reset_mock()
    world.server._seasonal_send_initial_hatch_info(world.session)
    world.sent.assert_not_called()


@pytest.mark.parametrize("case", ["unknown", "no-item", "level", "feed-idle", "feed-ready"])
def test_invalid_hatch_requests_change_nothing(seasonal, case):
    world = seasonal
    set_draken(world, 1000)
    egg = GOOSE
    if case == "unknown":
        egg = "not_an_egg"
        operation = proto.HATCH_OPERATION_START
    elif case == "no-item":
        operation = proto.HATCH_OPERATION_START
    elif case == "level":
        world.add_item(GOOSE)
        world.session.gameplay_character.level = 59
        operation = proto.HATCH_OPERATION_START
    elif case == "feed-idle":
        operation = proto.HATCH_OPERATION_FEED
    else:
        world.add_item(GOOSE)
        operate(world, GOOSE, proto.HATCH_OPERATION_START)
        world.now = NOW + timedelta(seconds=259200)
        operation = proto.HATCH_OPERATION_FEED
    before = (
        dict(world.session.gameplay_timed_offer_purchases),
        world.count(GOOSE),
        world.session.gameplay_currency_state,
    )
    operate(world, egg, operation)
    assert (
        dict(world.session.gameplay_timed_offer_purchases),
        world.count(GOOSE),
        world.session.gameplay_currency_state,
    ) == before


def test_real_initialization_keeps_the_batch_and_republishes_incubations(seasonal):
    from unittest.mock import Mock

    from dso_server.protocol.player_init_protocol import ExitInfo
    from dso_server.runtime.transport import Session
    from dso_server.services.player_init_world import PlayerInitializationWorld

    world = seasonal
    world.add_item(GOOSE)
    operate(world, GOOSE, proto.HATCH_OPERATION_START)
    server = world.server
    session = Session(("127.0.0.1", 48011), Mock())
    session.gameplay_character = server.character_selection.active_character
    session.gameplay_map = "a0200_kingscity"
    state = server._resolve_initialization_character(session)
    server._resolve_initialization_inventory(state)
    server._encode_initialization_new_player(state)
    server._resolve_initialization_talents(state)
    server._resolve_initialization_persistent_effects(session, state)
    server._resolve_initialization_vitals(state)
    server._apply_initialization_state(session, state)
    init_world = PlayerInitializationWorld((), ExitInfo(()), None, None, (), None)
    commands = server._build_initialization_commands(session, state, init_world)
    # The login batch keeps its topology for this client build ...
    assert USER_HATCH_EGGS_INFO_COMMAND not in [c.command_id for c in commands]
    # ... and the incubation follows in its own batch after PlayerReady.
    world.sent.reset_mock()
    server._send_initialization_followups(session, state)
    labels = [call.kwargs.get("label") for call in world.sent.call_args_list]
    assert "restore-hatch-eggs" in labels
    assert hatch_info(world).counters == ((1, 0),)
