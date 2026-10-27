"""Comeback sign-in calendars (2024 / 2026) and the 2026 recall link."""

from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest

from dso_server.persistence.seasonal_state import SeasonalStateStore
from dso_server.protocol import seasonal_activity_protocol as proto
from dso_server.protocol.seasonal_activity_protocol import (
    RecallBindOrPointsRequest,
    RecallBindUserRequest,
    RecallLinkError,
    SeasonalActivityError,
)
from dso_server.services.seasonal_activities import (
    CAMEBACK_DAYS_KEY,
    RECALL_COINS_CLAIMED_KEY,
    RECALL_LINKED_COINS_KEY,
    RECALL_RETURNER_KEY,
)
from dso_server.tests.fixtures.characters import character_profile
from dso_server.tests.test_seasonal_protocol import (  # noqa: F401
    NOW, FakeEvent, FakeTemplate, requires_2026, seasonal,
)


EVENT_2024 = "event_backe_signing_2024"
EVENT_2026 = "event_backe_signing_2026"
TODAY = proto.date_int(NOW)
SUMMONER_ID = 200_000_002


def cameback_info(world):
    world.dispatch(proto.encode_seasonal_info_request(proto.GET_CAMEBACK_SIGNIN_INFO_COMMAND))
    return world.commands(proto.GET_CAMEBACK_SIGNIN_INFO_RESULT_COMMAND)[-1].value


def claim_2024(world, bonus=False, value=proto.CAMEBACK_MODE_DAILY):
    world.dispatch(proto.encode_cameback_signin_award(bonus, value))
    return world.commands(proto.GET_CAMEBACK_SIGNIN_INFO_RESULT_COMMAND)[-1].value


# -- 2024 comeback calendar ----------------------------------------------------


def test_calendar_is_locked_while_its_event_is_inactive(seasonal):
    world = seasonal
    assert cameback_info(world) == proto.CameBackSigninInfoResult(0, 0, TODAY, ())
    result = claim_2024(world)
    assert result == proto.CameBackSigninInfoResult(0, 0, TODAY, ())
    assert not any(key.startswith("cameback-signin:") for key in world.session.gameplay_timed_offer_purchases)


def test_level_gate_follows_the_event_template(seasonal):
    world = seasonal
    world.server._seasonal_active_event = lambda template_id, now: (
        FakeEvent(FakeTemplate(template_id, minimum_level=30))
    )
    world.session.gameplay_character.level = 29
    assert cameback_info(world) == proto.CameBackSigninInfoResult(0, 0, TODAY, ())
    world.session.gameplay_character.level = 30
    assert cameback_info(world) == proto.CameBackSigninInfoResult(0, 0, 0, ())


def test_daily_and_bonus_claims_follow_the_fourteen_day_calendar(seasonal):
    world = seasonal
    world.active_events = {EVENT_2024}
    assert cameback_info(world) == proto.CameBackSigninInfoResult(0, 0, 0, ())

    # Day 1 is the class-choice costume (character class 1 = mage).
    assert claim_2024(world) == proto.CameBackSigninInfoResult(1, 1, TODAY, ())
    assert world.count("costume_event_pass_anniversary_mage_3h") == 1
    # Same-day replay is refused; the refreshed result is unchanged.
    assert claim_2024(world) == proto.CameBackSigninInfoResult(1, 1, TODAY, ())
    assert world.count("costume_event_pass_anniversary_mage_3h") == 1
    # The double reward needs a recharge, which is not modeled.
    claim_2024(world, value=proto.CAMEBACK_MODE_DOUBLE)
    assert world.session.gameplay_timed_offer_purchases[
        CAMEBACK_DAYS_KEY.format(event=EVENT_2024)
    ] == 1

    # The 7-day bonus needs seven signed days.
    claim_2024(world, bonus=True, value=7)
    assert world.count("premium_equipment_drop_buff") == 0
    for day in range(2, 8):
        world.now = NOW + timedelta(days=day - 1)
        claim_2024(world)
    assert world.count("e_anniversary_arena_key_01") == 10      # day 2
    assert world.count("dragon_bones") == 100                    # day 7
    result = claim_2024(world, bonus=True, value=7)
    assert result.bonus_claimed == (7,)
    assert world.count("premium_equipment_drop_buff") == 1
    claim_2024(world, bonus=True, value=7)                       # replay
    assert world.count("premium_equipment_drop_buff") == 1
    claim_2024(world, bonus=True, value=3)                       # no bonus row
    assert world.count("premium_equipment_drop_buff") == 1

    # Relog keeps the calendar.
    world.relog()
    assert cameback_info(world).signed_days == 7

    for day in range(8, 15):
        world.now = NOW + timedelta(days=day - 1)
        claim_2024(world)
    world.now = NOW + timedelta(days=20)
    result = claim_2024(world)  # a fifteenth day does not exist
    assert result.signed_days == 14
    assert result.last_signin_date == proto.date_int(world.now)
    assert claim_2024(world, bonus=True, value=14).bonus_claimed == (7, 14)
    assert world.count("item_ingredient_eternal_amethyst") == 1


# -- 2026 returner detection ---------------------------------------------------


@pytest.mark.parametrize("days_away, active, expected", [
    (31, True, True), (30, True, False), (None, True, False), (60, False, False),
])
def test_returner_is_detected_from_the_previous_save(seasonal, days_away, active, expected):
    world = seasonal
    if active:
        world.active_events = {EVENT_2026}
    saved_at = "" if days_away is None else (NOW - timedelta(days=days_away)).isoformat()
    world.server._seasonal_initialization(world.session, SimpleNamespace(saved_at=saved_at))
    claims = world.session.gameplay_timed_offer_purchases
    assert (RECALL_RETURNER_KEY in claims) is expected
    if expected:
        assert claims[RECALL_RETURNER_KEY] == TODAY


def make_returner(world):
    world.active_events = {EVENT_2026}
    world.server._seasonal_initialization(
        world.session, SimpleNamespace(saved_at=(NOW - timedelta(days=45)).isoformat()),
    )
    assert world.session.gameplay_timed_offer_purchases[RECALL_RETURNER_KEY]


def add_profile(world, character_id, name):
    world.server.account_store.put_profile(
        character_profile(character_id=character_id, name=name), flush=True,
    )


def save_other(world, character_id, claims):
    """Persist another character's save carrying ``claims``."""

    state = world.server._capture_account_state(world.session)
    world.server.account_store.put(
        replace(state, character_id=character_id, timed_offer_purchases=dict(claims)),
        flush=True,
    )


def recall_info(world):
    snapshot = world.server._seasonal_snapshot(world.session)
    return world.server._seasonal_recall_result(snapshot, snapshot.claims)


# -- 2026 calendar and recall: returner side -----------------------------------


def test_returner_calendar_bind_and_link_reward(seasonal):
    world = seasonal
    server, session = world.server, world.session
    add_profile(world, SUMMONER_ID, "Summoner")
    # Not yet a returner: the 2026 calendar stays locked.
    world.active_events = {EVENT_2026}
    with pytest.raises(SeasonalActivityError):
        server._seasonal_cameback_award(session, proto.CAMEBACK_2026,
                                        proto.CameBackSigninAwardRequest(False, 1))
    with pytest.raises(RecallLinkError):
        server._seasonal_recall_bind(session, RecallBindUserRequest(proto.recall_code_for(SUMMONER_ID)))

    make_returner(world)
    assert recall_info(world) == proto.CameBackUserInfo2026(
        proto.RECALL_USER_RETURNER, "", "", "", 0, 0, (), (),
    )
    # A day claim before linking credits coins but no link share.
    server._seasonal_cameback_award(session, proto.CAMEBACK_2026, proto.CameBackSigninAwardRequest(False, 1))
    assert world.count("currency_return_points") == 100
    assert RECALL_LINKED_COINS_KEY not in session.gameplay_timed_offer_purchases

    code = proto.recall_code_for(SUMMONER_ID)
    server._seasonal_recall_bind(session, RecallBindUserRequest(code.lower()))
    info = recall_info(world)
    assert (info.user_type, info.code, info.linked_name) == (proto.RECALL_USER_RETURNER, code, "Summoner")
    assert info.linked_since == NOW.strftime("%Y-%m-%dT%H:%M:%S")
    with pytest.raises(RecallLinkError) as conflict:
        server._seasonal_recall_bind(session, RecallBindUserRequest(code))
    assert conflict.value.message_key == proto.RECALL_LINK_CONFLICT_MESSAGE

    # Binding reward (cameback2026BindAwardId) once.
    server._seasonal_recall_claim(session, RecallBindOrPointsRequest(proto.RECALL_CLAIM_LINK_REWARD))
    assert world.count("difficulty_key_common") == 100
    assert world.count("currency_return_points") == 600
    assert recall_info(world).reward_claimed == 1
    with pytest.raises(SeasonalActivityError):
        server._seasonal_recall_claim(session, RecallBindOrPointsRequest(proto.RECALL_CLAIM_LINK_REWARD))
    with pytest.raises(SeasonalActivityError):  # coin shares are for code owners
        server._seasonal_recall_claim(session, RecallBindOrPointsRequest(proto.RECALL_CLAIM_COINS))
    assert world.count("difficulty_key_common") == 100

    # Linked: the next day's coins count toward the owner's share.
    world.now = NOW + timedelta(days=1)
    server._seasonal_cameback_award(session, proto.CAMEBACK_2026, proto.CameBackSigninAwardRequest(False, 1))
    assert session.gameplay_timed_offer_purchases[RECALL_LINKED_COINS_KEY] == 100

    saved = world.saved().timed_offer_purchases
    assert saved[RECALL_LINKED_COINS_KEY] == 100 and saved[RECALL_RETURNER_KEY] == TODAY
    store = SeasonalStateStore(server._seasonal_store().path)
    assert store.recall_link(session.gameplay_character.character_id).summoner_id == SUMMONER_ID


@pytest.mark.parametrize("case, message", [
    ("malformed", proto.RECALL_CODE_INVALID_MESSAGE),
    ("own", proto.RECALL_CODE_NOT_FOUND_MESSAGE),
    ("nobody", proto.RECALL_CODE_NOT_FOUND_MESSAGE),
    ("returner-owner", proto.RECALL_CODE_NOT_FOUND_MESSAGE),
    ("limit", proto.RECALL_LINK_LIMIT_MESSAGE),
])
def test_invalid_binds_are_refused_with_client_messages(seasonal, case, message):
    world = seasonal
    make_returner(world)
    add_profile(world, SUMMONER_ID, "Summoner")
    own_id = world.session.gameplay_character.character_id
    code = proto.recall_code_for(SUMMONER_ID)
    if case == "malformed":
        code = "R!!"
    elif case == "own":
        code = proto.recall_code_for(own_id)
    elif case == "nobody":
        code = proto.recall_code_for(123_456)
    elif case == "returner-owner":
        save_other(world, SUMMONER_ID, {RECALL_RETURNER_KEY: TODAY})
    else:
        store = world.server._seasonal_store()
        for index in range(proto.RECALL_LINK_LIMIT):
            store.link_recall(900 + index, SUMMONER_ID, returner_name=f"R{index}",
                              code=code, linked_at="2026-10-01T00:00:00",
                              limit=proto.RECALL_LINK_LIMIT)
    with pytest.raises(RecallLinkError) as error:
        world.server._seasonal_recall_bind(world.session, RecallBindUserRequest(code))
    assert error.value.message_key == message
    assert world.server._seasonal_store().recall_link(own_id) is None


# -- 2026 recall: code owner side -------------------------------------------------


def test_code_owner_invite_reward_and_coin_share(seasonal):
    world = seasonal
    server, session = world.server, world.session
    owner_id = session.gameplay_character.character_id
    world.active_events = {EVENT_2026}
    code = proto.recall_code_for(owner_id)
    assert recall_info(world) == proto.CameBackUserInfo2026(
        proto.RECALL_USER_SUMMONER, code, "", "", 0, 0, (), (),
    )
    with pytest.raises(SeasonalActivityError):  # nobody used the code yet
        server._seasonal_recall_claim(session, RecallBindOrPointsRequest(proto.RECALL_CLAIM_LINK_REWARD))

    store = server._seasonal_store()
    for returner_id, name, coins, day in ((300, "Ann", 600, "01"), (301, "Bo", 450, "02")):
        store.link_recall(returner_id, owner_id, returner_name=name, code=code,
                          linked_at=f"2026-10-{day}T00:00:00", limit=proto.RECALL_LINK_LIMIT)
        save_other(world, returner_id, {RECALL_RETURNER_KEY: TODAY, RECALL_LINKED_COINS_KEY: coins})
    info = recall_info(world)
    assert (info.linked_names, info.linked_coins) == (("Ann", "Bo"), (600, 450))

    server._seasonal_recall_claim(session, RecallBindOrPointsRequest(proto.RECALL_CLAIM_LINK_REWARD))
    # cameback2026InviteAwardId: 3999 ammunition, 50 keys, 1000 coins.
    assert world.count("difficulty_key_common") == 50
    assert world.count("currency_return_points") == 1000
    with pytest.raises(SeasonalActivityError):
        server._seasonal_recall_claim(session, RecallBindOrPointsRequest(proto.RECALL_CLAIM_LINK_REWARD))

    server._seasonal_recall_claim(session, RecallBindOrPointsRequest(proto.RECALL_CLAIM_COINS))
    assert world.count("currency_return_points") == 1000 + 105    # (600 + 450) / 10
    assert session.gameplay_timed_offer_purchases[RECALL_COINS_CLAIMED_KEY] == 105
    assert recall_info(world).coins_claimed == 105
    with pytest.raises(SeasonalActivityError):  # nothing new
        server._seasonal_recall_claim(session, RecallBindOrPointsRequest(proto.RECALL_CLAIM_COINS))

    save_other(world, 300, {RECALL_RETURNER_KEY: TODAY, RECALL_LINKED_COINS_KEY: 700})
    server._seasonal_recall_claim(session, RecallBindOrPointsRequest(proto.RECALL_CLAIM_COINS))
    assert world.count("currency_return_points") == 1000 + 115
    world.relog()
    assert world.session.gameplay_timed_offer_purchases[RECALL_COINS_CLAIMED_KEY] == 115


def test_recall_requests_need_the_2026_event(seasonal):
    world = seasonal
    with pytest.raises(SeasonalActivityError):
        world.server._seasonal_recall_claim(
            world.session, RecallBindOrPointsRequest(proto.RECALL_CLAIM_COINS),
        )


@requires_2026
def test_dispatch_answers_failed_binds_with_message_and_fresh_state(seasonal):
    world = seasonal
    make_returner(world)
    batches = world.dispatch(proto.encode_recall_bind_user(proto.recall_code_for(123_456)))
    labels = [label for label, _ in batches]
    assert "seasonal-message" in labels
    message = dict(batches)["seasonal-message"]
    assert proto.RECALL_CODE_NOT_FOUND_MESSAGE.encode() in message
    results = world.commands(proto.GET_CAMEBACK_USER_INFO_RESULT_2026_COMMAND)
    assert results[-1].value.user_type == proto.RECALL_USER_RETURNER

    world.dispatch(proto.encode_seasonal_info_request(proto.GET_CAMEBACK_SIGNIN_INFO_2026_COMMAND))
    value = world.commands(proto.GET_CAMEBACK_SIGNIN_INFO_RESULT_2026_COMMAND)[-1].value
    assert value == proto.CameBackSigninInfoResult(0, 0, 0, ())
    world.dispatch(proto.encode_cameback_signin_award(False, 1, proto.GET_CAMEBACK_SIGNIN_AWARD_2026_COMMAND))
    value = world.commands(proto.GET_CAMEBACK_SIGNIN_INFO_RESULT_2026_COMMAND)[-1].value
    assert value == proto.CameBackSigninInfoResult(1, 1, TODAY, ())
