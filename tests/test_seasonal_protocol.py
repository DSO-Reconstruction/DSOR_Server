"""Seasonal activity wire layouts against the R261_1024 client decoders.

Byte vectors follow the decoder call sequences documented in
protocol/seasonal_activity_protocol.py (vtable slot 5 of each class).
Shared fixtures for the other test_seasonal_* modules live here.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import struct
import unittest
from unittest.mock import Mock

import pytest

from dso_server.paths import DEFAULT_STATIC_DB_PATH
from dso_server.protocol import seasonal_activity_protocol as proto
from dso_server.protocol.command_ids import UNAVAILABLE_COMMAND_ID
from dso_server.protocol.inventory_codec import ItemInfo
from dso_server.protocol.player_init_protocol import (
    LOCAL_CLIENT_UNSUPPORTED_INIT_COMMANDS,
    USER_HATCH_EGGS_INFO_COMMAND,
    ServerCommand,
    UserHatchEggsInfo,
    decode_server_command_batch,
    encode_server_command_batch,
)
from dso_server.services.character_server import CharacterServer
from dso_server.tests.fixtures.characters import character_server
from dso_server.tests.test_recipe_progression import initialize


TABLE_1024 = Path(__file__).resolve().parents[1] / "docs" / "command_ids_rel261_1024.json"
NOW = datetime(2026, 10, 27, 12, 0, 0, tzinfo=timezone.utc)

AVAILABLE_2026 = proto.GET_CAMEBACK_USER_INFO_2026_COMMAND != UNAVAILABLE_COMMAND_ID
requires_2026 = pytest.mark.skipif(
    not AVAILABLE_2026, reason="the selected client table has no 2026 recall commands",
)


# -- shared fixtures ---------------------------------------------------------


@dataclass
class FakeTemplate:
    resource_id: str
    minimum_level: int = 1
    maximum_level: int = 200


@dataclass
class FakeEvent:
    template: FakeTemplate


class SeasonalWorld:
    """A real composed server, one initialized character, a fake clock."""

    def __init__(self, server, session, sent, case):
        self.server = server
        self.session = session
        self.sent = sent
        self.case = case
        self.now = NOW
        self.active_events = set()
        self.rolls = []
        server._seasonal_now = lambda: self.now
        server._seasonal_active_event = self._active
        server._seasonal_random = lambda: self

    # rng protocol used by the hatch roll
    def randrange(self, stop):
        value = self.rolls.pop(0) if self.rolls else 0
        assert 0 <= value < stop
        return value

    def random(self):
        return 0.5

    def _active(self, template_id, now):
        if str(template_id).casefold() in {e.casefold() for e in self.active_events}:
            return FakeEvent(FakeTemplate(str(template_id)))
        return None

    def dispatch(self, payload):
        self.sent.reset_mock()
        self.server._dispatch_char_op_now(self.session, payload)
        return self.batches()

    def batches(self):
        result = []
        for call in self.sent.call_args_list:
            packet, bits = call.args[1], call.args[2]
            if packet[:1] == b"\x85":
                try:
                    result.append((call.kwargs.get("label"), decode_server_command_batch(packet, bits)))
                    continue
                except ValueError:
                    pass
            result.append((call.kwargs.get("label"), packet))
        return result

    def commands(self, command_id):
        return [
            command
            for _, batch in self.batches() if not isinstance(batch, bytes)
            for command in batch if command.command_id == command_id
        ]

    def add_item(self, template_id, amount=1):
        session = self.session
        item_id = session.gameplay_next_item_id
        session.gameplay_next_item_id += 1
        session.gameplay_inventory.items.append(ItemInfo(
            item_id=item_id, template_id=template_id, amount=amount, item_level=1,
        ))
        session.gameplay_inventory.storage_58[item_id] = (
            max(session.gameplay_inventory.storage_58.values(), default=-1) + 1
        )
        return item_id

    def count(self, template_id):
        return sum(
            int(item.amount) for item in self.session.gameplay_inventory.items
            if item.template_id == template_id
        )

    def saved(self):
        return self.server.account_store.get(
            self.session.gameplay_character.character_id
        )

    def relog(self, port=48001):
        restored = initialize(self.server, port)
        restored.gameplay_mode = True
        restored.gameplay_persist_callback = self.session.gameplay_persist_callback
        self.session = restored
        return restored


@pytest.fixture
def seasonal(monkeypatch):
    case = unittest.TestCase()
    try:
        server = character_server(case)
        session = initialize(server, 47981)
        session.gameplay_mode = True
        session.gameplay_persist_callback = (
            lambda s, reason, flush: server._persist_gameplay_session(
                s, reason=reason, flush=flush,
            )
        )
        sent = Mock()
        monkeypatch.setattr(CharacterServer, "_send_world_loot_state_batch", sent)
        yield SeasonalWorld(server, session, sent, case)
    finally:
        case.doCleanups()


# -- command identities ------------------------------------------------------


def test_release261_1024_table_ids_match_the_client():
    table = json.loads(TABLE_1024.read_text(encoding="utf-8"))
    expected = {
        "Commands::GetCraftingCakesInfoCommand": 0x150,
        "Commands::GetCraftingCakesInfoResultCommand": 0x151,
        "Commands::GetCraftingCakesAwardCommand": 0x152,
        "Commands::GetCameBackSigninInfoCommand": 0x158,
        "Commands::GetCameBackSigninInfoResultCommand": 0x159,
        "Commands::GetCameBackSigninAwardCommand": 0x15A,
        "Commands::GetCameBackSigninInfo2026Command": 0x15B,
        "Commands::GetCameBackSigninInfoResult2026Command": 0x15C,
        "Commands::GetCameBackSigninAward2026Command": 0x15D,
        "Commands::UserHatchEggsInfoCommand": 0x15E,
        "Commands::OperateHatchEggsCommand": 0x15F,
        "Commands::SelectWishingWellMsgCommand": 0x160,
        "Commands::GetCameBackUserInfo2026Command": 0x179,
        "Commands::GetCameBackUserInfoResult2026Command": 0x17A,
        "Commands::GetCameBackUserBindOrPointsAward2026Command": 0x17B,
        "Commands::CameBackBindUser2026Command": 0x17C,
    }
    for name, value in expected.items():
        entry = table[name]
        assert (entry["id"] if isinstance(entry, dict) else entry) == value, name


def test_request_set_is_only_client_requests_and_never_collides():
    results = {
        proto.GET_CRAFTING_CAKES_INFO_RESULT_COMMAND,
        proto.GET_CAMEBACK_SIGNIN_INFO_RESULT_COMMAND,
        proto.GET_CAMEBACK_SIGNIN_INFO_RESULT_2026_COMMAND,
        proto.GET_CAMEBACK_USER_INFO_RESULT_2026_COMMAND,
        USER_HATCH_EGGS_INFO_COMMAND,
    } - {UNAVAILABLE_COMMAND_ID}
    assert UNAVAILABLE_COMMAND_ID not in proto.SEASONAL_ACTIVITY_COMMANDS
    assert not results & proto.SEASONAL_ACTIVITY_COMMANDS
    assert proto.OPERATE_HATCH_EGGS_COMMAND in proto.SEASONAL_ACTIVITY_COMMANDS
    assert proto.SELECT_WISHING_WELL_MSG_COMMAND in proto.SEASONAL_ACTIVITY_COMMANDS
    if not AVAILABLE_2026:
        # patch5 has no 2026 classes; their 1024 numbers are hatch/wish there.
        assert proto.GET_CAMEBACK_SIGNIN_INFO_2026_COMMAND == UNAVAILABLE_COMMAND_ID


def test_init_projection_still_omits_hatch_info_from_the_login_batch():
    # The persisted value is republished in its own batch after PlayerReady.
    assert USER_HATCH_EGGS_INFO_COMMAND in LOCAL_CLIENT_UNSUPPORTED_INIT_COMMANDS


# -- client requests ---------------------------------------------------------


def _request(command, body=b""):
    return b"\x8b" + struct.pack("<H", command) + body


def test_empty_info_requests_decode_and_reject_trailing_data():
    for command in proto.INFO_REQUEST_COMMANDS:
        assert proto.encode_seasonal_info_request(command) == _request(command)
        assert proto.decode_seasonal_info_request(_request(command), command).command_id == command
        with pytest.raises(ValueError):
            proto.decode_seasonal_info_request(_request(command, b"\x00"), command)
    with pytest.raises(ValueError):
        proto.decode_seasonal_info_request(
            _request(proto.GET_CRAFTING_CAKES_AWARD_COMMAND),
            proto.GET_CRAFTING_CAKES_AWARD_COMMAND,
        )


def test_operate_hatch_eggs_layout_is_atom_then_int32():
    # decode 0x14097d29c: string(u16 + bytes) at +0x20, int32 at +0x28.
    payload = _request(
        proto.OPERATE_HATCH_EGGS_COMMAND,
        struct.pack("<H", 18) + b"super_golden_goose" + struct.pack("<i", 2),
    )
    assert proto.encode_operate_hatch_eggs("super_golden_goose", 2) == payload
    assert proto.decode_operate_hatch_eggs(payload) == proto.OperateHatchEggsRequest(
        "super_golden_goose", proto.HATCH_OPERATION_FEED,
    )


@pytest.mark.parametrize("payload", [
    b"",
    b"\x85\x00\x00",
    _request(0x0001),
    _request(proto.OPERATE_HATCH_EGGS_COMMAND, struct.pack("<H", 0) + struct.pack("<i", 1)),
    _request(proto.OPERATE_HATCH_EGGS_COMMAND, struct.pack("<H", 3) + b"egg" + struct.pack("<i", 4)),
    _request(proto.OPERATE_HATCH_EGGS_COMMAND, struct.pack("<H", 3) + b"egg" + struct.pack("<i", 0)),
    _request(proto.OPERATE_HATCH_EGGS_COMMAND, struct.pack("<H", 3) + b"egg"),
    _request(proto.OPERATE_HATCH_EGGS_COMMAND, struct.pack("<H", 3) + b"egg" + struct.pack("<i", 1) + b"\x00"),
])
def test_malformed_hatch_requests_are_rejected(payload):
    with pytest.raises(ValueError):
        proto.decode_operate_hatch_eggs(payload)


def test_select_wishing_well_layout_is_item_array_amount():
    # decode 0x14097e284: uint32 +0x20, int32 array +0x28, int32 +0x48.
    payload = _request(
        proto.SELECT_WISHING_WELL_MSG_COMMAND,
        struct.pack("<IiiiI", 0x01020304, 2, 0, 3, 7),
    )
    assert proto.encode_select_wishing_well(0x01020304, (0, 3), 7) == payload
    assert proto.decode_select_wishing_well(payload) == proto.SelectWishingWellRequest(
        0x01020304, (0, 3), 7,
    )


@pytest.mark.parametrize("body", [
    struct.pack("<Iii", 5, 0, 1),            # no selection
    struct.pack("<Iiii", 5, 1, -1, 1),       # negative option
    struct.pack("<Iiiii", 5, 2, 1, 1, 1),    # duplicate option
    struct.pack("<Iiii", 5, 1, 0, 0),        # zero amount
    struct.pack("<Iiii", 0, 1, 0, 1),        # null item
    struct.pack("<Ii", 5, 99),               # truncated / oversized count
    struct.pack("<Iiii", 5, 1, 0, 1) + b"\x01",
])
def test_malformed_wishing_well_requests_are_rejected(body):
    with pytest.raises(ValueError):
        proto.decode_select_wishing_well(_request(proto.SELECT_WISHING_WELL_MSG_COMMAND, body))


def test_crafting_cakes_award_is_one_atom():
    payload = _request(proto.GET_CRAFTING_CAKES_AWARD_COMMAND, struct.pack("<H", 1) + b"3")
    assert proto.encode_crafting_cakes_award("3") == payload
    assert proto.decode_crafting_cakes_award(payload).milestone_id == "3"
    with pytest.raises(ValueError):
        proto.decode_crafting_cakes_award(
            _request(proto.GET_CRAFTING_CAKES_AWARD_COMMAND, struct.pack("<H", 0))
        )


def test_cameback_award_is_unaligned_bool_then_int32():
    # decode 0x14097a4d0: bool (1 bit) +0x20, int32 +0x24.
    payload = proto.encode_cameback_signin_award(True, 7)
    header = _request(proto.GET_CAMEBACK_SIGNIN_AWARD_COMMAND)
    # MSB-first bit stream: one set bit, the four LE bytes of 7, zero pad.
    stream = (1 << 32) | int.from_bytes(struct.pack("<i", 7), "big")
    assert payload == header + (stream << 7).to_bytes(5, "big")
    request = proto.decode_cameback_signin_award(payload)
    assert request == proto.CameBackSigninAwardRequest(True, 7)
    daily = proto.decode_cameback_signin_award(proto.encode_cameback_signin_award(False, 1))
    assert daily == proto.CameBackSigninAwardRequest(False, proto.CAMEBACK_MODE_DAILY)
    with pytest.raises(ValueError):
        proto.decode_cameback_signin_award(payload + b"\x00")


@requires_2026
def test_recall_requests_layouts():
    claim = proto.encode_recall_bind_or_points(1)
    assert claim == _request(proto.GET_CAMEBACK_USER_BIND_OR_POINTS_AWARD_2026_COMMAND, struct.pack("<i", 1))
    assert proto.decode_recall_bind_or_points(claim).claim == proto.RECALL_CLAIM_COINS
    with pytest.raises(ValueError):
        proto.decode_recall_bind_or_points(proto.encode_recall_bind_or_points(2))
    bind = proto.encode_recall_bind_user("R3B2OZLW")
    assert bind == _request(proto.CAMEBACK_BIND_USER_2026_COMMAND, struct.pack("<H", 8) + b"R3B2OZLW")
    assert proto.decode_recall_bind_user(bind).code == "R3B2OZLW"


# -- server results ----------------------------------------------------------


def _body(command, value):
    packet, bits = encode_server_command_batch(
        (ServerCommand(command, value, 1),), include_terminator=True,
    )
    assert packet[:3] == b"\x85" + struct.pack("<H", command)
    assert packet[-1:] == b"\xff"
    # body + trailing actor (1) for the actor-scoped wrapper
    assert packet[-5:-1] == struct.pack("<I", 1)
    assert decode_server_command_batch(packet, bits)[0].value == value
    return packet[3:-5]


def test_hatch_info_two_maps_and_calendar_time_order():
    deadline = datetime(2026, 10, 30, 13, 14, 15, 678000, tzinfo=timezone.utc)
    info = proto.hatch_eggs_info(((2, 0, deadline), (1, 3, deadline)))
    # 0x14095cfcc: int32 count, (int32 key, uint32 feeds); 0x14095d088: int32
    # count, (int32 key, CalendarTime year/day/hour/min/sec/ms/weekday/month).
    calendar = struct.pack("<8I", 2026, 30, 13, 14, 15, 678, 5, 10)  # Friday
    assert _body(USER_HATCH_EGGS_INFO_COMMAND, info) == (
        struct.pack("<i", 2) + struct.pack("<iI", 1, 3) + struct.pack("<iI", 2, 0)
        + struct.pack("<i", 2) + struct.pack("<i", 1) + calendar
        + struct.pack("<i", 2) + calendar
    )
    assert _body(USER_HATCH_EGGS_INFO_COMMAND, UserHatchEggsInfo((), ())) == struct.pack("<ii", 0, 0)


def test_crafting_cakes_result_layout():
    value = proto.CraftingCakesInfoResult(812, ("1", "2"))
    assert _body(proto.GET_CRAFTING_CAKES_INFO_RESULT_COMMAND, value) == (
        struct.pack("<ii", 812, 2) + struct.pack("<H", 1) + b"1" + struct.pack("<H", 1) + b"2"
    )


def test_cameback_result_layout():
    value = proto.CameBackSigninInfoResult(8, 8, 20261027, (7,))
    expected = struct.pack("<iiiii", 8, 8, 20261027, 1, 7)
    assert _body(proto.GET_CAMEBACK_SIGNIN_INFO_RESULT_COMMAND, value) == expected
    if AVAILABLE_2026:
        assert _body(proto.GET_CAMEBACK_SIGNIN_INFO_RESULT_2026_COMMAND, value) == expected


@requires_2026
def test_recall_user_info_layout():
    value = proto.CameBackUserInfo2026(0, "R1", "", "", 1, 20, ("Ann", "Bo"), (100, 50))
    assert _body(proto.GET_CAMEBACK_USER_INFO_RESULT_2026_COMMAND, value) == (
        struct.pack("<I", 0) + struct.pack("<H", 2) + b"R1" + struct.pack("<H", 0)
        + struct.pack("<H", 0) + struct.pack("<II", 1, 20)
        + struct.pack("<i", 2) + struct.pack("<H", 3) + b"Ann" + struct.pack("<H", 2) + b"Bo"
        + struct.pack("<i", 2) + struct.pack("<II", 100, 50)
    )
    with pytest.raises(ValueError):
        _body(proto.GET_CAMEBACK_USER_INFO_RESULT_2026_COMMAND,
              proto.CameBackUserInfo2026(0, "", "", "", 0, 0, ("x",), ()))


def test_date_int_matches_client_date_formatter():
    assert proto.date_int(datetime(2026, 1, 5, 23, 59, tzinfo=timezone.utc)) == 20260105


# -- authored data -----------------------------------------------------------


def test_authored_activity_data_is_resolved_from_static_db():
    eggs = proto.load_hatch_egg_definitions(DEFAULT_STATIC_DB_PATH)
    goose = eggs["super_golden_goose"]
    assert (goose.sql_index, goose.total_seconds, goose.base_chance) == (1, 259200, 500)
    assert (goose.feed_seconds, goose.feed_chance) == (3600, 10)
    assert (goose.cost_template, goose.cost_amount) == ("e_chr_2012_event_currency", 100)
    dragon = eggs["magic_dragon_egg_fossil"]
    assert (dragon.sql_index, dragon.total_seconds, dragon.feed_seconds) == (2, 2592000, 43200)
    assert (dragon.cost_template, dragon.cost_amount) == ("dragon_blood_essence", 1)

    wells = proto.load_wishing_well_definitions(DEFAULT_STATIC_DB_PATH)
    assert len(wells) == 15
    lamp = wells["magic_lamp_1_filthy"]
    assert lamp.select_max == 1 and all(option.offer_group for option in lamp.options)
    assert wells["mount_selection_chest_2"].options[-1] == proto.WishingWellOption("money_rc", 200000)

    cakes = proto.load_crafting_cakes_definition(DEFAULT_STATIC_DB_PATH)
    assert (cakes.recipe_id, cakes.crafted_item_id, cakes.event_id) == (
        "anniversary_cook_cake_01", "e_anniversary_cake", "anniversary_event",
    )
    assert [m.times for m in cakes.milestones] == [500, 800, 1500, 3000, 5000]

    for key, event in ((proto.CAMEBACK_2024, "event_backe_signing_2024"),
                       (proto.CAMEBACK_2026, "event_backe_signing_2026")):
        campaign = proto.load_cameback_campaign(DEFAULT_STATIC_DB_PATH, key)
        assert campaign.event_id == event and len(campaign.days) == 14
        assert campaign.bonus_days == ((7, 14) if key == proto.CAMEBACK_2024 else ())
    assert proto.load_recall_award_groups(DEFAULT_STATIC_DB_PATH) == proto.RecallAwardGroups(
        "cameback2026InviteAwardId", "cameback2026BindAwardId",
    )


def test_loot_award_parsing_and_class_choice():
    awards = proto.parse_loot_awards(
        "Loot:Warrior|w_item;Mage|m_item,1&Loot:x,3", "mage",
    )
    assert awards == (proto.LootAward("m_item", 1), proto.LootAward("x", 3))
    for bad in ("", "Item:x,1", "Loot:x", "Loot:x,0", "Loot:Warrior|w,1"):
        with pytest.raises(ValueError):
            proto.parse_loot_awards(bad, "mage")


def test_recall_codes_round_trip_and_reject_tampering():
    for character_id in (1, 35, 36, 200_000_001, 0x7FFFFFFF):
        code = proto.recall_code_for(character_id)
        assert proto.parse_recall_code(code) == character_id
        assert proto.parse_recall_code(" " + code.lower()) == character_id
    code = proto.recall_code_for(200_000_001)
    tampered = code[:-1] + ("0" if code[-1] != "0" else "1")
    for bad in (tampered, "", "X12", "R", "R!!", code + "A"):
        with pytest.raises(proto.RecallLinkError):
            proto.parse_recall_code(bad)


def test_dispatch_routes_every_seasonal_request_to_the_service(seasonal, monkeypatch):
    calls = []
    monkeypatch.setattr(
        CharacterServer, "_handle_seasonal_activity",
        lambda self, s, payload, command: calls.append(command),
    )
    for command in sorted(proto.SEASONAL_ACTIVITY_COMMANDS):
        seasonal.server._dispatch_char_op_now(seasonal.session, _request(command))
    assert calls == sorted(proto.SEASONAL_ACTIVITY_COMMANDS)
