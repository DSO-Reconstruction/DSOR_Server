"""Seasonal activity requests: egg incubation, wishing well, anniversary cake
milestones, and returnee/recall sign-in.

Command IDs and field layouts come from the R261_1024 client serializers
(``dro_client64.exe`` release261_1024; Decode is vtable slot 5).  Client
requests carry no trailing actor id; every result below is actor scoped and
carries the inherited trailing ``uint32`` actor in the shared ``0x85`` batch.

Egg incubation (``Managers::ClientGrowthManager``)
    ``UserHatchEggsInfoCommand`` decode 0x14097ecd8: two dictionaries keyed by
    ``_Template_HatchEggs.EggItemSqlIndex``.  0x14095cfcc reads
    ``int32 count`` + (``int32`` key, ``uint32`` feed count); 0x14095d088
    reads ``int32 count`` + (``int32`` key, Nebula ``CalendarTime``).  The
    handler (0x1402369d0) stores them as ``Dictionary<int,unsigned int>`` and
    ``Dictionary<int,Timing::CalendarTime>``.  The CalendarTime reader
    0x140a98d64 fills year, day, hour, minute, second, millisecond, weekday,
    month in that wire order (same generated order as MembershipUpdate).
    GrowthWidget (0x1405af69c) shows ``time - now - feeds * reduceSeconds``,
    so the time is the un-fed hatch deadline and feeding never rewrites it.
    ``OperateHatchEggsCommand`` decode 0x14097d29c: atom egg id, int32
    operation.  Senders: 0x140235c04 sends 1 when the widget opens an egg
    without a valid time (start), 0x1402370ac sends 2 after its own cost check
    (feed), the widget's ``Confirm`` button (0x1405aed60) sends 3 (hatch).

Wishing well / selective chests
    BankWidget::OnUseItem (0x140399040) and the inventory equivalent
    (0x140478638) never send ``UseItem`` for item categories 0x3e
    (``WishingWell``), 0x3f (``Growth``) or 0x40 (``Selective``): they open the
    lamp (``giftofthelampgod``), growth or SelectiveRewardWidget locally.
    ``SelectWishingWellMsgCommand`` decode 0x14097e284: uint32 inventory item
    id, ``int32 count`` + int32 option indices, int32 amount.  The lamp widget
    (0x1405c3d2c) sends one zero-based ``wish_1..wish_4`` index and amount 1;
    the selective widget (0x1405c2534) sends its ``SelectItem_%d`` indices
    only when their count equals ``SelectMaxNum`` and the slider amount
    (1..stack size).

Anniversary cakes (``Managers::ClientCraftingCakesManager``)
    ``GetCraftingCakesInfoCommand`` has an empty body.  The result (decode
    0x14097a82c) is int32 server total + ``int32 count`` + atom ids of the
    claimed ``_Template_CraftingCakes`` rows; CakeInfoWidget (0x1405a0994)
    marks a row claimed when its Id is listed, locked while total <
    ``CraftingCakesTimes``, otherwise claimable.  ``GetCraftingCakesAwardCommand``
    (decode 0x14097a7d8) is the row Id, cut from the ``TemplateListEntry<Id>``
    element name at offset 17 (0x14059e940).

Comeback sign-in (``ClientCameBackSignInManager`` / ``ClientRecalledSignInManager``)
    Info requests are empty.  The two result commands share one layout
    (decodes 0x14097a5f0 / 0x14097a55c): int32 signed days, int32 double-reward
    claims, int32 last sign-in date (``YYYYMMDD``, client
    ``DateFormater::DateStringToInt`` 0x140a3390c on UTC ``GetSystemTime``),
    ``int32 count`` + int32 claimed bonus milestones.  The day state function
    (0x14023c9d0 / 0x14025dcf8) is 1 when last date < today, 2 when
    double claims < signed days, else 0.  Award (decodes 0x14097a4d0 /
    0x14097a46c) is a bool + int32: ``false`` with that state (1 daily, 2
    double), or ``true`` with a 7/14 day bonus (CameBackSignInWidget
    0x140599c38, ``Slot29``/``Slot30``).

Recall link 2026 (``ClientExclusiveActivationCodeManager``)
    ``GetCameBackUserInfoResult2026Command`` decode 0x14097a6d8: uint32 user
    type (0 = Summoner view, otherwise ReturnerLinked/NoLinked by whether the
    linked name is empty; handler 0x1402506c8), atom code, atom linked name,
    atom link time (``YYYY-MM-DDTHH:MM:SS``, formatted by 0x1402500ac),
    uint32 reward-claimed flag, uint32 coins already claimed, ``int32
    count`` + atom linked player names, ``int32 count`` + uint32 coins per
    linked player.  The Summoner view shows ``sum(coins) / 10 - claimed``.
    ``GetCameBackUserBindOrPointsAward2026Command`` (decode 0x14097a684) is
    int32 0 (``BindRewardBtn`` / ``GetInviteRewardBtn``) or 1
    (``GetPointBtn``); ``CameBackBindUser2026Command`` (decode 0x140979478) is
    the entered code.  Both set a client pending flag that only a new
    UserInfoResult clears, so every request is answered with one.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from contextlib import closing
import copy
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
import random
import sqlite3
from types import MappingProxyType

from dso_server.protocol.character_selection import BitReader, BitWriter
from dso_server.protocol.command_ids import UNAVAILABLE_COMMAND_ID, command_id
from dso_server.protocol.commerce_protocol import (
    PurchaseItemDefinition,
    load_purchase_item_definition,
)
from dso_server.protocol.currency_protocol import (
    GAME_CURRENCY_BY_WIRE_ID,
    GameCurrencyChanged,
    normalize_game_currency_balances,
)
from dso_server.protocol.inventory_codec import InventoryInfo
from dso_server.protocol.inventory_economy_protocol import (
    InventoryMutationBuilder,
    credit_currency_template,
    refresh_inventory_economy_values,
)
from dso_server.protocol.item_receipt_protocol import (
    ITEM_RECEIVED_TEMPLATE_CATEGORIES,
    ItemReceipt,
    ItemReceiptGrant,
    build_item_receipts,
)
from dso_server.protocol.player_init_protocol import (
    _BODY_READERS,
    _BODY_WRITERS,
    CurrencyChanged,
    HatchEggRecord,
    UserHatchEggsInfo,
)
from dso_server.protocol.world_loot_protocol import (
    roll_new_equipment_enchantments,
)


# The literals are the dro_client64-patch5 ids (docs/command_ids.json).  The
# 2026 recall classes do not exist in that build; their release261 ids
# 0x015b..0x015d belong to the hatch/wishing-well commands there, so they stay
# unavailable unless the active table names them.
GET_CRAFTING_CAKES_INFO_COMMAND = command_id(
    "Commands::GetCraftingCakesInfoCommand", 0x0150,
)
GET_CRAFTING_CAKES_INFO_RESULT_COMMAND = command_id(
    "Commands::GetCraftingCakesInfoResultCommand", 0x0151,
)
GET_CRAFTING_CAKES_AWARD_COMMAND = command_id(
    "Commands::GetCraftingCakesAwardCommand", 0x0152,
)
GET_CAMEBACK_SIGNIN_INFO_COMMAND = command_id(
    "Commands::GetCameBackSigninInfoCommand", 0x0158,
)
GET_CAMEBACK_SIGNIN_INFO_RESULT_COMMAND = command_id(
    "Commands::GetCameBackSigninInfoResultCommand", 0x0159,
)
GET_CAMEBACK_SIGNIN_AWARD_COMMAND = command_id(
    "Commands::GetCameBackSigninAwardCommand", 0x015A,
)
OPERATE_HATCH_EGGS_COMMAND = command_id(
    "Commands::OperateHatchEggsCommand", 0x015C,
)
SELECT_WISHING_WELL_MSG_COMMAND = command_id(
    "Commands::SelectWishingWellMsgCommand", 0x015D,
)
GET_CAMEBACK_SIGNIN_INFO_2026_COMMAND = command_id(
    "Commands::GetCameBackSigninInfo2026Command", UNAVAILABLE_COMMAND_ID,
)
GET_CAMEBACK_SIGNIN_INFO_RESULT_2026_COMMAND = command_id(
    "Commands::GetCameBackSigninInfoResult2026Command", UNAVAILABLE_COMMAND_ID,
)
GET_CAMEBACK_SIGNIN_AWARD_2026_COMMAND = command_id(
    "Commands::GetCameBackSigninAward2026Command", UNAVAILABLE_COMMAND_ID,
)
GET_CAMEBACK_USER_INFO_2026_COMMAND = command_id(
    "Commands::GetCameBackUserInfo2026Command", UNAVAILABLE_COMMAND_ID,
)
GET_CAMEBACK_USER_INFO_RESULT_2026_COMMAND = command_id(
    "Commands::GetCameBackUserInfoResult2026Command", UNAVAILABLE_COMMAND_ID,
)
GET_CAMEBACK_USER_BIND_OR_POINTS_AWARD_2026_COMMAND = command_id(
    "Commands::GetCameBackUserBindOrPointsAward2026Command",
    UNAVAILABLE_COMMAND_ID,
)
CAMEBACK_BIND_USER_2026_COMMAND = command_id(
    "Commands::CameBackBindUser2026Command", UNAVAILABLE_COMMAND_ID,
)

# Client requests routed to SeasonalActivityService._handle_seasonal_activity.
SEASONAL_ACTIVITY_COMMANDS: frozenset[int] = frozenset(
    command for command in (
        GET_CRAFTING_CAKES_INFO_COMMAND,
        GET_CRAFTING_CAKES_AWARD_COMMAND,
        GET_CAMEBACK_SIGNIN_INFO_COMMAND,
        GET_CAMEBACK_SIGNIN_AWARD_COMMAND,
        GET_CAMEBACK_SIGNIN_INFO_2026_COMMAND,
        GET_CAMEBACK_SIGNIN_AWARD_2026_COMMAND,
        OPERATE_HATCH_EGGS_COMMAND,
        SELECT_WISHING_WELL_MSG_COMMAND,
        GET_CAMEBACK_USER_INFO_2026_COMMAND,
        GET_CAMEBACK_USER_BIND_OR_POINTS_AWARD_2026_COMMAND,
        CAMEBACK_BIND_USER_2026_COMMAND,
    )
    if command != UNAVAILABLE_COMMAND_ID
)

# OperateHatchEggs operations (see module docstring for the three senders).
HATCH_OPERATION_START = 1
HATCH_OPERATION_FEED = 2
HATCH_OPERATION_HATCH = 3
HATCH_OPERATIONS = frozenset({
    HATCH_OPERATION_START, HATCH_OPERATION_FEED, HATCH_OPERATION_HATCH,
})
# GrowthWidget prints probabilities as value / 100 followed by '%'
# (0x1405afbd8), so the authored 500 is 5% and the per-feed 10 is 0.1%.
HATCH_CHANCE_SCALE = 10_000

# Comeback day state values reported by 0x14023c9d0 / 0x14025dcf8.
CAMEBACK_MODE_DAILY = 1
CAMEBACK_MODE_DOUBLE = 2

# GetCameBackUserBindOrPointsAward2026 values (0x1404f1060 / 0x1404f12d0).
RECALL_CLAIM_LINK_REWARD = 0
RECALL_CLAIM_COINS = 1
RECALL_USER_SUMMONER = 0
RECALL_USER_RETURNER = 1

# gui.window.exclusiveactivatoncode/descript: an inactive player is one who
# "haven't logged in for over 30 days"; "Each returning player can link with
# up to 5 inactive players, while each inactive player can only link with one
# returning player."  gui.window.recalledsignin/descript: "Each time a
# returning player claims Returned Coins, the link player gets an extra 10%",
# which is the client's sum(coins) / 10 (0x1402506c8).
RECALL_INACTIVITY_SECONDS = 30 * 24 * 60 * 60
RECALL_LINK_LIMIT = 5
RECALL_SUMMONER_SHARE_DIVISOR = 10
RETURNED_COIN_TEMPLATE = "currency_return_points"

# gui.window.messages keys localized by the ServerToClientMsg handler
# (0x1401e2690) for failed link requests.
RECALL_CODE_NOT_FOUND_MESSAGE = "invitationcodenotfound"
RECALL_LINK_LIMIT_MESSAGE = "bindingfailed"
RECALL_LINK_CONFLICT_MESSAGE = "bindingconflict"
RECALL_CODE_INVALID_MESSAGE = "invalidbindingrequest"

# Local bounds on client-controlled array lengths (the client UI shows at
# most five selective options and four lamp wishes).
MAX_WISHING_WELL_SELECTIONS = 16
MAX_STRING_BYTES = 256

INT32_MIN = -(1 << 31)
INT32_MAX = (1 << 31) - 1


class SeasonalActivityError(ValueError):
    """A seasonal request is malformed or not allowed in the current state."""


class RecallLinkError(SeasonalActivityError):
    """A link request failed with a client-localized message key."""

    def __init__(self, message_key: str, detail: str) -> None:
        super().__init__(detail)
        self.message_key = message_key


# -- client requests ---------------------------------------------------------


@dataclass(frozen=True)
class CraftingCakesAwardRequest:
    milestone_id: str


@dataclass(frozen=True)
class CameBackSigninAwardRequest:
    bonus: bool
    value: int


@dataclass(frozen=True)
class OperateHatchEggsRequest:
    egg_id: str
    operation: int


@dataclass(frozen=True)
class SelectWishingWellRequest:
    item_id: int
    selections: tuple[int, ...]
    amount: int


@dataclass(frozen=True)
class RecallBindOrPointsRequest:
    claim: int


@dataclass(frozen=True)
class RecallBindUserRequest:
    code: str


@dataclass(frozen=True)
class SeasonalInfoRequest:
    command_id: int


def _signed32(value: int) -> int:
    value = int(value) & 0xFFFFFFFF
    return value - (1 << 32) if value & 0x80000000 else value


def _unsigned32(value: int) -> int:
    value = int(value)
    if not INT32_MIN <= value <= 0xFFFFFFFF:
        raise SeasonalActivityError(f"value {value} does not fit 32 bits")
    return value & 0xFFFFFFFF


def _int32(value: int, label: str) -> int:
    value = int(value)
    if not INT32_MIN <= value <= INT32_MAX:
        raise SeasonalActivityError(f"{label} {value} does not fit int32")
    return value & 0xFFFFFFFF


def _client_reader(payload: bytes, expected: int) -> BitReader:
    if expected == UNAVAILABLE_COMMAND_ID:
        raise SeasonalActivityError("command is unavailable for this client")
    try:
        reader = BitReader(bytes(payload))
        marker = reader.read_uint8()
        actual = int.from_bytes(reader.read_bytes(2), "little")
    except ValueError as exc:
        raise SeasonalActivityError("truncated seasonal request") from exc
    if marker != 0x8B:
        raise SeasonalActivityError("expected a client command")
    if actual != expected:
        raise SeasonalActivityError(
            f"expected command 0x{expected:04x}, got 0x{actual:04x}"
        )
    return reader


def _finish(reader: BitReader, label: str) -> None:
    if reader.remaining >= 8:
        raise SeasonalActivityError(
            f"{label} has {reader.remaining} unread bits"
        )
    if reader.remaining and reader.read_bits(reader.remaining):
        raise SeasonalActivityError(f"{label} has non-zero padding")


def _read_string(reader: BitReader, label: str) -> str:
    value = reader.read_string()
    if len(value.encode("utf-8")) > MAX_STRING_BYTES:
        raise SeasonalActivityError(f"{label} is too long")
    return value


def _decode(payload: bytes, expected: int, label: str, body):
    reader = _client_reader(payload, expected)
    try:
        value = body(reader)
    except SeasonalActivityError:
        raise
    except ValueError as exc:
        raise SeasonalActivityError(f"malformed {label}: {exc}") from exc
    _finish(reader, label)
    return value


def decode_seasonal_info_request(
    payload: bytes, command: int,
) -> SeasonalInfoRequest:
    """Decode one of the four empty-bodied info requests."""

    if command not in INFO_REQUEST_COMMANDS:
        raise SeasonalActivityError(
            f"command 0x{command:04x} is not a seasonal info request"
        )
    return _decode(
        payload, command, "seasonal info request",
        lambda reader: SeasonalInfoRequest(command),
    )


def decode_crafting_cakes_award(payload: bytes) -> CraftingCakesAwardRequest:
    def body(reader: BitReader) -> CraftingCakesAwardRequest:
        milestone = _read_string(reader, "cake milestone id")
        if not milestone:
            raise SeasonalActivityError("cake milestone id is empty")
        return CraftingCakesAwardRequest(milestone)

    return _decode(
        payload, GET_CRAFTING_CAKES_AWARD_COMMAND, "GetCraftingCakesAward", body,
    )


def decode_cameback_signin_award(
    payload: bytes, command: int = GET_CAMEBACK_SIGNIN_AWARD_COMMAND,
) -> CameBackSigninAwardRequest:
    if command not in (
        GET_CAMEBACK_SIGNIN_AWARD_COMMAND,
        GET_CAMEBACK_SIGNIN_AWARD_2026_COMMAND,
    ):
        raise SeasonalActivityError("not a comeback sign-in award command")
    return _decode(
        payload, command, "GetCameBackSigninAward",
        lambda reader: CameBackSigninAwardRequest(
            reader.read_bool(), _signed32(reader.read_uint32_le()),
        ),
    )


def decode_operate_hatch_eggs(payload: bytes) -> OperateHatchEggsRequest:
    def body(reader: BitReader) -> OperateHatchEggsRequest:
        egg_id = _read_string(reader, "egg id")
        operation = _signed32(reader.read_uint32_le())
        if not egg_id:
            raise SeasonalActivityError("egg id is empty")
        if operation not in HATCH_OPERATIONS:
            raise SeasonalActivityError(
                f"unsupported hatch operation {operation}"
            )
        return OperateHatchEggsRequest(egg_id, operation)

    return _decode(
        payload, OPERATE_HATCH_EGGS_COMMAND, "OperateHatchEggs", body,
    )


def decode_select_wishing_well(payload: bytes) -> SelectWishingWellRequest:
    def body(reader: BitReader) -> SelectWishingWellRequest:
        item_id = reader.read_uint32_le()
        count = _signed32(reader.read_uint32_le())
        if not 1 <= count <= MAX_WISHING_WELL_SELECTIONS:
            raise SeasonalActivityError(
                f"wishing-well selection count {count} is invalid"
            )
        selections = tuple(
            _signed32(reader.read_uint32_le()) for _ in range(count)
        )
        amount = _signed32(reader.read_uint32_le())
        if item_id == 0:
            raise SeasonalActivityError("wishing-well item id is zero")
        if any(index < 0 for index in selections):
            raise SeasonalActivityError("negative wishing-well selection")
        if len(set(selections)) != len(selections):
            raise SeasonalActivityError("duplicate wishing-well selection")
        if amount <= 0:
            raise SeasonalActivityError("wishing-well amount must be positive")
        return SelectWishingWellRequest(item_id, selections, amount)

    return _decode(
        payload, SELECT_WISHING_WELL_MSG_COMMAND, "SelectWishingWellMsg", body,
    )


def decode_recall_bind_or_points(payload: bytes) -> RecallBindOrPointsRequest:
    def body(reader: BitReader) -> RecallBindOrPointsRequest:
        claim = _signed32(reader.read_uint32_le())
        if claim not in (RECALL_CLAIM_LINK_REWARD, RECALL_CLAIM_COINS):
            raise SeasonalActivityError(f"unsupported recall claim {claim}")
        return RecallBindOrPointsRequest(claim)

    return _decode(
        payload, GET_CAMEBACK_USER_BIND_OR_POINTS_AWARD_2026_COMMAND,
        "GetCameBackUserBindOrPointsAward2026", body,
    )


def decode_recall_bind_user(payload: bytes) -> RecallBindUserRequest:
    return _decode(
        payload, CAMEBACK_BIND_USER_2026_COMMAND, "CameBackBindUser2026",
        lambda reader: RecallBindUserRequest(
            _read_string(reader, "recall code")
        ),
    )


def _client_writer(command: int) -> BitWriter:
    if command == UNAVAILABLE_COMMAND_ID:
        raise SeasonalActivityError("command is unavailable for this client")
    writer = BitWriter()
    writer.write_uint8(0x8B)
    writer.write_bytes(int(command).to_bytes(2, "little"))
    return writer


def encode_seasonal_info_request(command: int) -> bytes:
    """Client-side request bytes, used by tests and the custom client."""

    return _client_writer(command).finish()[0]


def encode_crafting_cakes_award(milestone_id: str) -> bytes:
    writer = _client_writer(GET_CRAFTING_CAKES_AWARD_COMMAND)
    writer.write_string(str(milestone_id))
    return writer.finish()[0]


def encode_cameback_signin_award(
    bonus: bool, value: int,
    command: int = GET_CAMEBACK_SIGNIN_AWARD_COMMAND,
) -> bytes:
    writer = _client_writer(command)
    writer.write_bool(bool(bonus))
    writer.write_uint32_le(_int32(value, "award value"))
    return writer.finish()[0]


def encode_operate_hatch_eggs(egg_id: str, operation: int) -> bytes:
    writer = _client_writer(OPERATE_HATCH_EGGS_COMMAND)
    writer.write_string(str(egg_id))
    writer.write_uint32_le(_int32(operation, "operation"))
    return writer.finish()[0]


def encode_select_wishing_well(
    item_id: int, selections: Iterable[int], amount: int,
) -> bytes:
    selections = tuple(selections)
    writer = _client_writer(SELECT_WISHING_WELL_MSG_COMMAND)
    writer.write_uint32_le(_unsigned32(item_id))
    writer.write_uint32_le(len(selections))
    for index in selections:
        writer.write_uint32_le(_int32(index, "selection"))
    writer.write_uint32_le(_int32(amount, "amount"))
    return writer.finish()[0]


def encode_recall_bind_or_points(claim: int) -> bytes:
    writer = _client_writer(GET_CAMEBACK_USER_BIND_OR_POINTS_AWARD_2026_COMMAND)
    writer.write_uint32_le(_int32(claim, "claim"))
    return writer.finish()[0]


def encode_recall_bind_user(code: str) -> bytes:
    writer = _client_writer(CAMEBACK_BIND_USER_2026_COMMAND)
    writer.write_string(str(code))
    return writer.finish()[0]


INFO_REQUEST_COMMANDS = frozenset(
    command for command in (
        GET_CRAFTING_CAKES_INFO_COMMAND,
        GET_CAMEBACK_SIGNIN_INFO_COMMAND,
        GET_CAMEBACK_SIGNIN_INFO_2026_COMMAND,
        GET_CAMEBACK_USER_INFO_2026_COMMAND,
    )
    if command != UNAVAILABLE_COMMAND_ID
)


# -- server results ----------------------------------------------------------


@dataclass(frozen=True)
class CraftingCakesInfoResult:
    total: int
    claimed: tuple[str, ...]


@dataclass(frozen=True)
class CameBackSigninInfoResult:
    signed_days: int
    double_claims: int
    last_signin_date: int
    bonus_claimed: tuple[int, ...]


@dataclass(frozen=True)
class CameBackUserInfo2026:
    user_type: int
    code: str
    linked_name: str
    linked_since: str
    reward_claimed: int
    coins_claimed: int
    linked_names: tuple[str, ...]
    linked_coins: tuple[int, ...]


def _write_string_array(writer: BitWriter, values: Iterable[str]) -> None:
    values = tuple(str(value) for value in values)
    writer.write_uint32_le(len(values))
    for value in values:
        writer.write_string(value)


def _read_string_array(reader: BitReader) -> tuple[str, ...]:
    count = _signed32(reader.read_uint32_le())
    if not 0 <= count <= 1_000_000:
        raise ValueError("array count outside the client's 0..1000000 bound")
    return tuple(reader.read_string() for _ in range(count))


def _write_crafting_cakes_info_result(
    writer: BitWriter, value: CraftingCakesInfoResult,
) -> None:
    writer.write_uint32_le(_int32(value.total, "cake total"))
    _write_string_array(writer, value.claimed)


def _read_crafting_cakes_info_result(
    reader: BitReader,
) -> CraftingCakesInfoResult:
    total = _signed32(reader.read_uint32_le())
    return CraftingCakesInfoResult(total, _read_string_array(reader))


def _write_cameback_signin_info_result(
    writer: BitWriter, value: CameBackSigninInfoResult,
) -> None:
    writer.write_uint32_le(_int32(value.signed_days, "signed days"))
    writer.write_uint32_le(_int32(value.double_claims, "double claims"))
    writer.write_uint32_le(_int32(value.last_signin_date, "sign-in date"))
    bonuses = tuple(value.bonus_claimed)
    writer.write_uint32_le(len(bonuses))
    for bonus in bonuses:
        writer.write_uint32_le(_int32(bonus, "bonus milestone"))


def _read_cameback_signin_info_result(
    reader: BitReader,
) -> CameBackSigninInfoResult:
    signed_days = _signed32(reader.read_uint32_le())
    double_claims = _signed32(reader.read_uint32_le())
    last_date = _signed32(reader.read_uint32_le())
    count = _signed32(reader.read_uint32_le())
    if not 0 <= count <= 1_000_000:
        raise ValueError("array count outside the client's 0..1000000 bound")
    bonuses = tuple(_signed32(reader.read_uint32_le()) for _ in range(count))
    return CameBackSigninInfoResult(
        signed_days, double_claims, last_date, bonuses,
    )


def _write_cameback_user_info_2026(
    writer: BitWriter, value: CameBackUserInfo2026,
) -> None:
    if len(value.linked_names) != len(value.linked_coins):
        raise SeasonalActivityError("linked names and coins differ in length")
    writer.write_uint32_le(_unsigned32(value.user_type))
    writer.write_string(str(value.code))
    writer.write_string(str(value.linked_name))
    writer.write_string(str(value.linked_since))
    writer.write_uint32_le(_unsigned32(value.reward_claimed))
    writer.write_uint32_le(_unsigned32(value.coins_claimed))
    _write_string_array(writer, value.linked_names)
    writer.write_uint32_le(len(value.linked_coins))
    for coins in value.linked_coins:
        writer.write_uint32_le(_unsigned32(coins))


def _read_cameback_user_info_2026(reader: BitReader) -> CameBackUserInfo2026:
    user_type = reader.read_uint32_le()
    code = reader.read_string()
    linked_name = reader.read_string()
    linked_since = reader.read_string()
    reward_claimed = reader.read_uint32_le()
    coins_claimed = reader.read_uint32_le()
    names = _read_string_array(reader)
    count = _signed32(reader.read_uint32_le())
    if not 0 <= count <= 1_000_000:
        raise ValueError("array count outside the client's 0..1000000 bound")
    coins = tuple(reader.read_uint32_le() for _ in range(count))
    return CameBackUserInfo2026(
        user_type, code, linked_name, linked_since, reward_claimed,
        coins_claimed, names, coins,
    )


SEASONAL_BODY_WRITERS = {
    command: writer
    for command, writer in (
        (GET_CRAFTING_CAKES_INFO_RESULT_COMMAND, _write_crafting_cakes_info_result),
        (GET_CAMEBACK_SIGNIN_INFO_RESULT_COMMAND, _write_cameback_signin_info_result),
        (GET_CAMEBACK_SIGNIN_INFO_RESULT_2026_COMMAND, _write_cameback_signin_info_result),
        (GET_CAMEBACK_USER_INFO_RESULT_2026_COMMAND, _write_cameback_user_info_2026),
    )
    if command != UNAVAILABLE_COMMAND_ID
}
SEASONAL_BODY_READERS = {
    command: reader
    for command, reader in (
        (GET_CRAFTING_CAKES_INFO_RESULT_COMMAND, _read_crafting_cakes_info_result),
        (GET_CAMEBACK_SIGNIN_INFO_RESULT_COMMAND, _read_cameback_signin_info_result),
        (GET_CAMEBACK_SIGNIN_INFO_RESULT_2026_COMMAND, _read_cameback_signin_info_result),
        (GET_CAMEBACK_USER_INFO_RESULT_2026_COMMAND, _read_cameback_user_info_2026),
    )
    if command != UNAVAILABLE_COMMAND_ID
}

# Register with the shared 0x85 batch codec (the quest_protocol pattern) so
# results travel in the same batch as their inventory/currency deltas. A
# collision would mean the selected id table maps two classes to one number.
for _command, _writer in SEASONAL_BODY_WRITERS.items():
    if _BODY_WRITERS.get(_command, _writer) is not _writer:
        raise RuntimeError(
            f"seasonal result 0x{_command:04x} collides with a modeled command"
        )
_BODY_WRITERS.update(SEASONAL_BODY_WRITERS)
_BODY_READERS.update(SEASONAL_BODY_READERS)


def _calendar_values(moment: datetime) -> tuple[int, ...]:
    """CalendarTime in the generated wire order (reader 0x140a98d64)."""

    moment = moment.astimezone(timezone.utc)
    return (
        moment.year,
        moment.day,
        moment.hour,
        moment.minute,
        moment.second,
        moment.microsecond // 1000,
        # Nebula Weekday: Sunday = 0.
        moment.isoweekday() % 7,
        moment.month,
    )


def hatch_eggs_info(
    eggs: Iterable[tuple[int, int, datetime]],
) -> UserHatchEggsInfo:
    """Build UserHatchEggsInfo from (sql index, feeds, un-fed deadline)."""

    rows = sorted(
        (int(index), int(feeds), deadline) for index, feeds, deadline in eggs
    )
    counters = tuple((index & 0xFFFFFFFF, feeds) for index, feeds, _ in rows)
    records = tuple(
        HatchEggRecord(index & 0xFFFFFFFF, _calendar_values(deadline))
        for index, _, deadline in rows
    )
    return UserHatchEggsInfo(counters, records)


def date_int(moment: datetime) -> int:
    """The client's DateStringToInt value for a UTC date."""

    moment = moment.astimezone(timezone.utc)
    return moment.year * 10_000 + moment.month * 100 + moment.day


# -- authored data -----------------------------------------------------------


@dataclass(frozen=True)
class LootAward:
    template_id: str
    amount: int


@dataclass(frozen=True)
class HatchEggDefinition:
    egg_id: str
    sql_index: int
    total_seconds: int
    base_chance: int
    feed_seconds: int
    feed_chance: int
    cost_template: str
    cost_amount: int
    awards: tuple[str, ...]


@dataclass(frozen=True)
class WishingWellOption:
    resource_id: str
    amount: int | None  # None: an _Template_Offergroup id

    @property
    def offer_group(self) -> bool:
        return self.amount is None


@dataclass(frozen=True)
class WishingWellDefinition:
    template_id: str
    select_max: int
    options: tuple[WishingWellOption, ...]


@dataclass(frozen=True)
class CraftingCakeMilestone:
    milestone_id: str
    times: int
    award_group: str


@dataclass(frozen=True)
class CraftingCakesDefinition:
    recipe_id: str
    crafted_item_id: str
    event_id: str
    milestones: tuple[CraftingCakeMilestone, ...]

    def milestone(self, milestone_id: str) -> CraftingCakeMilestone | None:
        return next(
            (row for row in self.milestones if row.milestone_id == milestone_id),
            None,
        )


@dataclass(frozen=True)
class CameBackDay:
    day: int
    award: str
    bonus_award: str


@dataclass(frozen=True)
class CameBackCampaign:
    key: str
    event_id: str
    days: tuple[CameBackDay, ...]

    def day(self, number: int) -> CameBackDay | None:
        return next((row for row in self.days if row.day == number), None)

    @property
    def bonus_days(self) -> tuple[int, ...]:
        return tuple(row.day for row in self.days if row.bonus_award)


@dataclass(frozen=True)
class RecallAwardGroups:
    invite_group: str
    bind_group: str


def _connect(path: str | Path) -> sqlite3.Connection:
    database = sqlite3.connect(
        f"file:{Path(path).as_posix()}?mode=ro", uri=True,
    )
    database.row_factory = sqlite3.Row
    return database


def _globals(path: str) -> sqlite3.Row:
    with closing(_connect(path)) as database:
        row = database.execute("SELECT * FROM _Globals").fetchone()
    if row is None:
        raise SeasonalActivityError("static database has no _Globals row")
    return row


def _int_pair(raw: object, label: str) -> tuple[int, int]:
    parts = [part.strip() for part in str(raw or "").split(";")]
    if len(parts) != 2 or not all(part.lstrip("-").isdigit() for part in parts):
        raise SeasonalActivityError(f"{label} {raw!r} is not 'int;int'")
    return int(parts[0]), int(parts[1])


def _cost_pair(raw: object) -> tuple[str, int] | None:
    text = str(raw or "").strip()
    if not text:
        return None
    template, separator, amount = text.partition(";")
    if not separator or not template.strip() or not amount.strip().isdigit():
        raise SeasonalActivityError(f"egg cost {raw!r} is not 'template;int'")
    return template.strip(), int(amount)


@lru_cache(maxsize=4)
def _load_hatch_egg_definitions(path: str) -> Mapping[str, HatchEggDefinition]:
    definitions = {}
    with closing(_connect(path)) as database:
        rows = database.execute("SELECT * FROM _Template_HatchEggs").fetchall()
    for row in rows:
        egg_id = str(row["Id"])
        total_seconds, base_chance = _int_pair(
            row["EggTotalTimesChange"], "EggTotalTimesChange",
        )
        feed_seconds, feed_chance = _int_pair(
            row["EggAddTimeChange"], "EggAddTimeChange",
        )
        # GrowthWidget (0x1405afbd8) charges the item pair when its template
        # is set and the currency pair otherwise; each authored row fills
        # exactly one of them.
        cost = (
            _cost_pair(row["EggCostItemAdd"])
            or _cost_pair(row["EggCostCurrencyAdd"])
        )
        if cost is None or cost[1] <= 0:
            raise SeasonalActivityError(f"egg {egg_id!r} has no feeding cost")
        awards = tuple(
            part.strip()
            for part in str(row["EggGetAwardItem"] or "").split("&")
            if part.strip()
        )
        if (
            total_seconds <= 0 or feed_seconds <= 0 or not awards
            or base_chance < 0 or feed_chance < 0
        ):
            raise SeasonalActivityError(f"egg {egg_id!r} is incomplete")
        definitions[egg_id] = HatchEggDefinition(
            egg_id, int(row["EggItemSqlIndex"]), total_seconds, base_chance,
            feed_seconds, feed_chance, cost[0], cost[1], awards,
        )
    if len({egg.sql_index for egg in definitions.values()}) != len(definitions):
        raise SeasonalActivityError("hatch eggs reuse an EggItemSqlIndex")
    return MappingProxyType(definitions)


def load_hatch_egg_definitions(
    static_database_path: str | Path,
) -> Mapping[str, HatchEggDefinition]:
    return _load_hatch_egg_definitions(str(Path(static_database_path)))


@lru_cache(maxsize=4)
def _load_wishing_well_definitions(
    path: str,
) -> Mapping[str, WishingWellDefinition]:
    definitions = {}
    with closing(_connect(path)) as database:
        rows = database.execute("SELECT * FROM _Template_WishingWell").fetchall()
    for row in rows:
        options = []
        for entry in str(row["Items"] or "").split(";"):
            entry = entry.strip()
            if not entry:
                continue
            template, separator, amount = entry.rpartition(",")
            if separator and amount.strip().isdigit() and template.strip():
                options.append(
                    WishingWellOption(template.strip(), int(amount))
                )
            else:
                # Lamp wishes name _Template_Offergroup rows
                # (wishingwell_<lamp>_oil_lamp_<wish>).
                options.append(WishingWellOption(entry, None))
        select_max = int(row["SelectMaxNum"] or 0)
        if not options or not 1 <= select_max <= len(options):
            raise SeasonalActivityError(
                f"wishing well {row['Id']!r} has invalid options"
            )
        definitions[str(row["Id"])] = WishingWellDefinition(
            str(row["Id"]), select_max, tuple(options),
        )
    return MappingProxyType(definitions)


def load_wishing_well_definitions(
    static_database_path: str | Path,
) -> Mapping[str, WishingWellDefinition]:
    return _load_wishing_well_definitions(str(Path(static_database_path)))


@lru_cache(maxsize=4)
def _load_crafting_cakes_definition(path: str) -> CraftingCakesDefinition:
    # _Globals.CraftingCakesItemId is a recipe id: the client resolves it with
    # ClientCraftingManager::GetRecipeById (0x14024e9fc) and uses it as the
    # gui.window.cakeinfo text prefix ("Every Anniversary Cake crafted by all
    # Players adds to the server total").
    recipe_id = str(_globals(path)["CraftingCakesItemId"] or "")
    with closing(_connect(path)) as database:
        recipe = database.execute(
            "SELECT CraftedItemId FROM _Template_CraftingRecipes WHERE Id=?",
            (recipe_id,),
        ).fetchone()
        if recipe is None:
            raise SeasonalActivityError(f"cake recipe {recipe_id!r} is unknown")
        crafted_item_id = str(recipe["CraftedItemId"] or "").partition(":")[0]
        item = database.execute(
            "SELECT EventId FROM _Template_Item WHERE Id=?",
            (crafted_item_id,),
        ).fetchone()
        rows = database.execute(
            "SELECT * FROM _Template_CraftingCakes"
        ).fetchall()
    milestones = tuple(sorted(
        (
            CraftingCakeMilestone(
                str(row["Id"]), int(row["CraftingCakesTimes"]),
                str(row["CraftingCakesAwardGroup"]),
            )
            for row in rows
        ),
        key=lambda row: (row.times, row.milestone_id),
    ))
    if not milestones or any(row.times <= 0 for row in milestones):
        raise SeasonalActivityError("crafting cake milestones are invalid")
    return CraftingCakesDefinition(
        recipe_id,
        crafted_item_id,
        str(item["EventId"] or "") if item is not None else "",
        milestones,
    )


def load_crafting_cakes_definition(
    static_database_path: str | Path,
) -> CraftingCakesDefinition:
    return _load_crafting_cakes_definition(str(Path(static_database_path)))


_CAMEBACK_TABLES = {
    # key: (calendar table, _Globals event id column)
    "cameback-2024": ("_Template_CameBackSignin", "CameBackSigninEventId"),
    "cameback-2026": ("_Template_CameBackSignin2026", "CameBackSigninEventId2026"),
}
CAMEBACK_2024 = "cameback-2024"
CAMEBACK_2026 = "cameback-2026"


@lru_cache(maxsize=8)
def _load_cameback_campaign(path: str, key: str) -> CameBackCampaign:
    table, event_column = _CAMEBACK_TABLES[key]
    event_id = str(_globals(path)[event_column] or "")
    with closing(_connect(path)) as database:
        rows = database.execute(f'SELECT * FROM "{table}"').fetchall()
    days = tuple(sorted(
        (
            CameBackDay(
                int(row["Id"]),
                str(row["CameBackSigninAward"] or ""),
                str(row["CameBackSigninExtraAward"] or ""),
            )
            for row in rows
        ),
        key=lambda row: row.day,
    ))
    if (
        not event_id
        or [row.day for row in days] != list(range(1, len(days) + 1))
        or any(not row.award for row in days)
    ):
        raise SeasonalActivityError(f"comeback calendar {table} is invalid")
    return CameBackCampaign(key, event_id, days)


def load_cameback_campaign(
    static_database_path: str | Path, key: str,
) -> CameBackCampaign:
    return _load_cameback_campaign(str(Path(static_database_path)), key)


@lru_cache(maxsize=4)
def _load_recall_award_groups(path: str) -> RecallAwardGroups:
    # _Globals.CameBackBindAward2026 = "<invite group>;<bind group>".
    raw = str(_globals(path)["CameBackBindAward2026"] or "")
    parts = [part.strip() for part in raw.split(";")]
    if len(parts) != 2 or not all(parts):
        raise SeasonalActivityError(f"CameBackBindAward2026 {raw!r} is invalid")
    return RecallAwardGroups(parts[0], parts[1])


def load_recall_award_groups(
    static_database_path: str | Path,
) -> RecallAwardGroups:
    return _load_recall_award_groups(str(Path(static_database_path)))


def parse_loot_awards(raw: str, class_name: str) -> tuple[LootAward, ...]:
    """Resolve ``Loot:`` award strings authored in the activity tables.

    Entries are separated by ``&`` (``_Template_HatchEggs``); one entry is
    ``Loot:<template>,<amount>`` or the class-choice form
    ``Loot:Warrior|a;Mage|b;...,<amount>`` (comeback day one).
    """

    awards = []
    for entry in str(raw or "").split("&"):
        entry = entry.strip()
        if not entry:
            continue
        if not entry.startswith("Loot:"):
            raise SeasonalActivityError(f"unsupported award {entry!r}")
        target, separator, amount = entry[5:].rpartition(",")
        if not separator or not amount.strip().isdigit() or int(amount) <= 0:
            raise SeasonalActivityError(f"award {entry!r} has no amount")
        template = target.strip()
        if "|" in template:
            choices = {}
            for choice in template.split(";"):
                label, bar, item = choice.partition("|")
                if bar and label.strip() and item.strip():
                    choices[label.strip().casefold()] = item.strip()
            template = choices.get(str(class_name).casefold(), "")
            if not template:
                raise SeasonalActivityError(
                    f"award {entry!r} has no choice for class {class_name!r}"
                )
        if not template:
            raise SeasonalActivityError(f"award {entry!r} has no template")
        awards.append(LootAward(template, int(amount)))
    if not awards:
        raise SeasonalActivityError(f"award {raw!r} is empty")
    return tuple(awards)


def resolve_offer_group(
    static_database_path: str | Path,
    group_id: str,
    *,
    class_name: str,
    player_level: int,
) -> tuple[LootAward, ...]:
    """Rewards of one _Template_Offergroup for this character.

    Offers whose CharClass excludes the class are skipped (the shop filter);
    a level outside an offer's range rejects the whole claim instead of
    granting a partial group.
    """

    class_key = str(class_name).casefold()
    with closing(_connect(static_database_path)) as database:
        group = database.execute(
            "SELECT Offers, Obsolete FROM _Template_Offergroup WHERE Id=?",
            (str(group_id),),
        ).fetchone()
        if group is None or int(group["Obsolete"] or 0):
            raise SeasonalActivityError(f"unknown offer group {group_id!r}")
        offer_ids = tuple(
            entry.strip()
            for entry in str(group["Offers"] or "").split(";")
            if entry.strip()
        )
        if not offer_ids:
            raise SeasonalActivityError(f"offer group {group_id!r} is empty")
        awards = []
        for offer_id in offer_ids:
            offer = database.execute(
                "SELECT Item, Amount, CharClass, MinPlayerLevel, MaxPlayerLevel "
                "FROM _Template_Offer WHERE Id=?",
                (offer_id,),
            ).fetchone()
            if offer is None:
                raise SeasonalActivityError(
                    f"offer group {group_id!r} names unknown offer {offer_id!r}"
                )
            classes = {
                value.strip().casefold()
                for value in str(offer["CharClass"] or "").split(";")
                if value.strip()
            }
            if classes and class_key not in classes:
                continue
            minimum = int(offer["MinPlayerLevel"] or 0)
            maximum = int(offer["MaxPlayerLevel"] or 0) or 1_000
            if not minimum <= int(player_level) <= maximum:
                raise SeasonalActivityError(
                    f"offer {offer_id!r} requires level {minimum}..{maximum}"
                )
            item_id = str(offer["Item"] or "")
            amount = int(offer["Amount"] or 0)
            if not item_id or amount <= 0:
                raise SeasonalActivityError(f"offer {offer_id!r} is invalid")
            awards.append(LootAward(item_id, amount))
    if not awards:
        raise SeasonalActivityError(
            f"offer group {group_id!r} has no reward for {class_name!r}"
        )
    return tuple(awards)


# -- recall codes ------------------------------------------------------------

# Local choice: the client only displays/copies the code, so the server
# derives it from the summoner's character id (upper-case base 36 plus one
# mod-36 check symbol) instead of keeping a separate code table.
_CODE_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_CODE_PREFIX = "R"


def _check_symbol(digits: str) -> str:
    total = sum(
        (index + 1) * _CODE_ALPHABET.index(symbol)
        for index, symbol in enumerate(digits)
    )
    return _CODE_ALPHABET[total % len(_CODE_ALPHABET)]


def recall_code_for(character_id: int) -> str:
    value = int(character_id)
    if value <= 0:
        raise SeasonalActivityError("recall codes need a positive character id")
    digits = ""
    while value:
        value, remainder = divmod(value, len(_CODE_ALPHABET))
        digits = _CODE_ALPHABET[remainder] + digits
    return _CODE_PREFIX + digits + _check_symbol(digits)


def parse_recall_code(code: str) -> int:
    text = "".join(str(code).split()).upper()
    if (
        not 3 <= len(text) <= 16
        or not text.startswith(_CODE_PREFIX)
        or any(symbol not in _CODE_ALPHABET for symbol in text[1:])
    ):
        raise RecallLinkError(
            RECALL_CODE_INVALID_MESSAGE, f"malformed recall code {code!r}",
        )
    digits, check = text[1:-1], text[-1]
    if _check_symbol(digits) != check:
        raise RecallLinkError(
            RECALL_CODE_INVALID_MESSAGE, f"recall code {code!r} fails its check",
        )
    value = 0
    for symbol in digits:
        value = value * len(_CODE_ALPHABET) + _CODE_ALPHABET.index(symbol)
    if value <= 0:
        raise RecallLinkError(
            RECALL_CODE_INVALID_MESSAGE, f"recall code {code!r} has no player",
        )
    return value


# -- inventory mutation ------------------------------------------------------


_FIXED_CURRENCY_FIELDS = {
    # CurrencyChanged fields, the same mapping credit_currency_template uses.
    "money_rc": "andermant",
    "money_vc": "gold",
    "money_vc_gold": "gold",
    "money_gc_no_auto_use": "glyph",
    "money_uc_no_auto_use": "legacy",
    "money_hc_no_auto_use": "badge",
    "e_chr_2012_event_currency": "draken",
}


def _debit_fixed_currency(
    state: CurrencyChanged, field_name: str, amount: int,
) -> CurrencyChanged:
    balances = {
        "andermant": int(state.andermant),
        "gold": int(state.gold),
        "badge": int(state.badge_of_honor),
        "legacy": int(state.legacy_progression),
        "draken": int(state.draken),
        "glyph": int(state.glyph_of_power),
    }
    if balances[field_name] < int(amount):
        raise SeasonalActivityError(
            f"insufficient {field_name}: need {amount}, have "
            f"{balances[field_name]}"
        )
    balances[field_name] -= int(amount)
    return CurrencyChanged(
        balances["andermant"],
        balances["gold"],
        (balances["badge"], balances["legacy"], balances["draken"]),
        balances["glyph"],
        False,
        state.wallet_base,
    )


@dataclass(frozen=True)
class SeasonalRewardMutation:
    inventory: InventoryInfo
    currency: CurrencyChanged
    game_currencies: dict[str, int]
    game_currency_updates: tuple[GameCurrencyChanged, ...]
    currency_changed: bool
    delta: InventoryInfo
    final: InventoryInfo
    next_item_id: int
    receipts: tuple[ItemReceipt, ...]
    granted: tuple[LootAward, ...]


def apply_reward_mutation(
    static_database_path: str | Path,
    inventory: InventoryInfo,
    currency: CurrencyChanged,
    game_currencies: Mapping[str, int],
    *,
    next_item_id: int,
    player_level: int,
    class_name: str,
    map_difficulty: int = 0,
    consume_items: Iterable[tuple[int, int]] = (),
    costs: Iterable[tuple[str, int]] = (),
    rewards: Iterable[LootAward] = (),
    rng: random.Random | None = None,
) -> SeasonalRewardMutation:
    """Consume, pay and grant atomically; mirrors open_mapped_chest.

    ``costs`` are paid from a fixed currency (Draken, ...), then inventory
    stacks of the template, then the keyed wallet.  Currency rewards are
    credited directly; everything else is materialized.  Nothing is mutated
    when any step fails.
    """

    builder = InventoryMutationBuilder(inventory, next_item_id)
    updated_currency = currency
    wallet = normalize_game_currency_balances(game_currencies)
    wallet_updates: dict[str, GameCurrencyChanged] = {}
    for item_id, amount in consume_items:
        builder.consume(int(item_id), int(amount))
    for template_id, amount in costs:
        template_id, amount = str(template_id), int(amount)
        if amount <= 0:
            raise SeasonalActivityError("costs must be positive")
        field_name = _FIXED_CURRENCY_FIELDS.get(template_id)
        if field_name is not None:
            updated_currency = _debit_fixed_currency(
                updated_currency, field_name, amount,
            )
            continue
        owned = sum(
            int(item.amount) for item in builder.inventory.items
            if item.template_id == template_id
        )
        if owned >= amount:
            builder.consume_template(template_id, amount)
            continue
        if (
            template_id in GAME_CURRENCY_BY_WIRE_ID
            and int(wallet.get(template_id, 0)) >= amount
        ):
            wallet[template_id] = int(wallet[template_id]) - amount
            wallet_updates[template_id] = GameCurrencyChanged(
                template_id, wallet[template_id],
            )
            continue
        raise SeasonalActivityError(f"insufficient {template_id!r}: need {amount}")

    granted = tuple(rewards)
    grants: list[ItemReceiptGrant] = []
    definitions: dict[str, PurchaseItemDefinition] = {}
    for reward in granted:
        if reward.amount <= 0:
            raise SeasonalActivityError("reward amounts must be positive")
        definition = definitions.get(reward.template_id)
        if definition is None:
            definition = load_purchase_item_definition(
                static_database_path, reward.template_id,
            )
            definitions[reward.template_id] = definition
        credit = credit_currency_template(
            static_database_path,
            updated_currency,
            wallet,
            reward.template_id,
            reward.amount,
            scale_by_item_value=False,
        )
        materialized = credit is None
        if credit is not None:
            updated_currency = credit.currency
            wallet = dict(credit.game_currencies)
            if credit.game_currency_update is not None:
                wallet_updates[credit.game_currency_update.resource_id] = (
                    credit.game_currency_update
                )
        else:
            item_level = max(
                int(definition.minimum_item_level),
                min(max(1, int(player_level)), int(definition.maximum_item_level)),
            )
            builder.add_item(definition, reward.amount, item_level=item_level)
        grants.append(ItemReceiptGrant(
            reward.template_id,
            reward.amount,
            materialized=materialized,
            include_template_id=(
                definition.item_category in ITEM_RECEIVED_TEMPLATE_CATEGORIES
            ),
        ))

    changed_ids = tuple(int(item.item_id) for item in builder.changed_items)
    if changed_ids:
        roll_new_equipment_enchantments(
            static_database_path,
            builder.inventory,
            changed_ids,
            class_name=class_name,
            map_difficulty=map_difficulty,
            rng=rng,
        )
        refresh_inventory_economy_values(
            static_database_path,
            builder.inventory,
            item_ids=changed_ids,
            force_melt_value=True,
        )
        current = {int(item.item_id): item for item in builder.inventory.items}
        builder.changed_items = [
            copy.deepcopy(current.get(int(item.item_id), item))
            for item in builder.changed_items
        ]
    mutation = builder.finish()
    receipts, receipt_next_item_id = build_item_receipts(
        inventory,
        mutation.inventory,
        (item.item_id for item in mutation.delta.items),
        mutation.delta.field_158,
        grants,
        next_item_id=mutation.next_item_id,
    )
    return SeasonalRewardMutation(
        inventory=mutation.inventory,
        currency=updated_currency,
        game_currencies=wallet,
        game_currency_updates=tuple(wallet_updates.values()),
        currency_changed=updated_currency != currency,
        delta=mutation.delta,
        final=mutation.final,
        next_item_id=receipt_next_item_id,
        receipts=receipts,
        granted=granted,
    )
