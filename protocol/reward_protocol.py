"""Typed premium-store, spender-reward, and daily-login helpers."""

from __future__ import annotations

from dso_server.runtime import resident_content
from dso_server.gameplay.ocp_store import (
    ANNIVERSARY_ATTIRE_PRODUCT_KEY,
    ANNIVERSARY_DARK_NIGHTS_PACKAGE_PRODUCT_KEY,
    ANNIVERSARY_NEW_EXPLORER_PACKAGE_PRODUCT_KEY,
    ANNIVERSARY_PET_CHEST_PRODUCT_KEY,
    ANNIVERSARY_SOUL_TRUNCHEON_BULK_PRODUCT_KEY,
    ANNIVERSARY_SOUL_TRUNCHEON_BUNDLE_PRODUCT_KEY,
    ANNIVERSARY_SOUL_TRUNCHEON_SINGLE_PRODUCT_KEY,
    ANNIVERSARY_STARTER_BUNDLE_PRODUCT_KEY,
    ANNIVERSARY_WIND_OF_BLOOD_PACKAGE_PRODUCT_KEY,
    CHARGED_MARK_OF_BRAVERY_PACKAGE_PRODUCT_KEY,
    DARK_DWARF_CLOAK_FORTUNE_CHEST_PRODUCT_KEY,
    DARK_DWARF_COPPER_FARTHING_PACKAGE_PRODUCT_KEY,
    DARK_DWARF_FORGEFYRE_MAGIC_POTION_PRODUCT_KEY,
    DARK_DWARF_FORGEFYRE_POTION_PRODUCT_KEY,
    DARK_DWARF_LEVIN_CRYSTAL_PACKAGE_PRODUCT_KEY,
    DARK_DWARF_LEVIN_CRYSTAL_SUPER_PACKAGE_PRODUCT_KEY,
    DARK_DWARF_SHOULDERS_FORTUNE_CHEST_PRODUCT_KEY,
    CHINESE_NEW_YEAR_COOKING_BUNDLE_BIG_PRODUCT_KEY,
    CHINESE_NEW_YEAR_COOKING_BUNDLE_SMALL_PRODUCT_KEY,
    CHINESE_NEW_YEAR_DRAGON_BUNDLE_1_PRODUCT_KEY,
    CHINESE_NEW_YEAR_DRAGON_BUNDLE_2_PRODUCT_KEY,
    CHINESE_NEW_YEAR_GIFT_CHEST_PRODUCT_KEY,
    CHINESE_NEW_YEAR_KUNG_FU_LION_PACKAGE_PRODUCT_KEY,
    CHINESE_NEW_YEAR_SURPRISE_CHEST_PRODUCT_KEY,
    STELLAR_GOLD_ATTIRE_PRODUCT_KEY,
    STELLAR_GOLD_PACKAGE_PRODUCT_KEY,
    MONSTER_HUNT_ATTIRE_PRODUCT_KEY,
    HUNTING_RING_BUNDLE_PRODUCT_KEY,
    ROCKETMEN_ATTIRE_PRODUCT_KEY,
    ROCKETMEN_SHARK_PACKAGE_PRODUCT_KEY,
    ROCKETMEN_SOUL_DESTRUCTION_CHEST_PRODUCT_KEY,
    ROCKETMEN_SMALL_SOUL_CHEST_PRODUCT_KEY,
    HALLOWEEN_ATTIRE_PRODUCT_KEY,
    HALLOWEEN_BIG_PIE_PRODUCT_KEY,
    HALLOWEEN_BUNDLE_PRODUCT_KEY,
    HALLOWEEN_MEDIUM_PIE_PRODUCT_KEY,
    HALLOWEEN_SMALL_PIE_PRODUCT_KEY,
    SUMMER_FESTIVAL_ATTIRE_PRODUCT_KEY,
    SUMMER_FESTIVAL_PREP_START_PRODUCT_KEY,
    SUMMER_FESTIVAL_START_PRODUCT_KEY,
    SUMMER_FORTUNE_BAG_SMALL_PRODUCT_KEY,
    SUMMER_FORTUNE_BAG_MEDIUM_PRODUCT_KEY,
    SUMMER_FORTUNE_BAG_BIG_PRODUCT_KEY,
    SUMMER_NIGHTDREAM_CHEST_PRODUCT_KEY,
    SUMMER_LUCKY_CHEST_PRODUCT_KEY,
    SUMMER_SET_CHEST_PRODUCT_KEY,
    DRAGAN_ATTIRE_PRODUCT_KEY,
    DRAGAN_EYE_OF_AVERSION_PRODUCT_KEY,
    DRAGAN_EYE_OF_COLLAPSE_PRODUCT_KEY,
    DRAGAN_EYE_OF_FEAR_PRODUCT_KEY,
    DRAGAN_FALLEN_HEART_PRODUCT_KEY,
    DRAGAN_JUDGMENT_DAY_PRODUCT_KEY,
    DRAGAN_LAST_GUARDIAN_PRODUCT_KEY,
    BIG_GAME_HUNT_ATTIRE_PRODUCT_KEY,
    BIG_GAME_HUNT_BIG_BUNDLE_PRODUCT_KEY,
    BIG_GAME_HUNT_JEWEL_PACK_PRODUCT_KEY,
    BIG_GAME_HUNT_MEDIUM_BUNDLE_PRODUCT_KEY,
    BIG_GAME_HUNT_SMALL_BUNDLE_PRODUCT_KEY,
    DEFEAT_UNDEFEATABLE_BUNDLE_REWARDS,
    DEFEAT_UNDEFEATABLE_ATTIRE_PRODUCT_KEY,
    DEFEAT_UNDEFEATABLE_HERO_BAG_PRODUCT_KEY,
    DEFEAT_UNDEFEATABLE_LAVA_ESSENCE_PRODUCT_KEY,
    DEFEAT_UNDEFEATABLE_LAVA_MATERIAL_PRODUCT_KEY,
    DEFEAT_UNDEFEATABLE_ROAD_TO_TRIUMPH_PRODUCT_KEY,
    DEFEAT_UNDEFEATABLE_UNLOCK_VICTORY_PRODUCT_KEY,
    EASTER_ATTIRE_PRODUCT_KEY,
    EASTER_DAILY_SPRING_PACK_PRODUCT_KEY,
    EASTER_DAILY_SURPRISE_SPRING_PACKAGE_PRODUCT_KEY,
    EASTER_SPRING_MATERIAL_PACK_PRODUCT_KEY,
    WINTER_SOLSTICE_ATTIRE_PRODUCT_KEY,
    TEARS_OF_UNIVERSE_PACKAGE_PRODUCT_KEY,
    WINTER_EVENT_SMALL_BUNDLE_PRODUCT_KEY,
    WINTER_EVENT_BIG_BUNDLE_PRODUCT_KEY,
    MASCOT_PACKAGE_PRODUCT_KEY,
    MEMBERSHIP_PACKAGE_REWARDS,
    WINTER_SOLSTICE_CHEST_PRODUCT_KEY,
    WINTER_SOLSTICE_MATERIALS_CHEST_PRODUCT_KEY,
    TRIANGLE_OF_FORCES_PACKAGE_PRODUCT_KEY,
    KINGSHILL_SEWERS_MUTATION_PACKAGE_PRODUCT_KEY,
    KINGSHILL_SEWERS_ATTIRE_PRODUCT_KEY,
    KINGSHILL_SEWERS_MATERIAL_CHEST_PRODUCT_KEY,
    KINGSHILL_SEWERS_SURPRISE_PACK_PRODUCT_KEY,
    KINGSHILL_SEWERS_DAILY_PACK_PRODUCT_KEY,
    KINGSHILL_SEWERS_RAMPAGING_STORM_PRODUCT_KEY,
    KINGSHILL_SEWERS_RAMPAGING_HURRICANE_PRODUCT_KEY,
    DESERT_OF_ESSENCES_ATTIRE_PRODUCT_KEY,
    EASTER_SURPRISE_SPRING_PACK_PRODUCT_KEY,
    SARGON_ATTIRE_PRODUCT_KEY,
    EVENT_ATTIRE_ITEMS_BY_PRODUCT_CLASS,
    EVENT_ATTIRE_PRODUCT_KEYS,
    FULL_MOON_ATTIRE_PRODUCT_KEY,
    LEGACY_FULL_MOON_ATTIRE_PRODUCT_ITEMS,
    NEW_MOON_ATTIRE_PRODUCT_KEY,
    BLACK_FRIDAY_REALM_PATH_PACKAGE_PRODUCT_KEY,
    BLACK_FRIDAY_ANDERMANT_PACKAGE_PRODUCT_KEY,
    BLACK_FRIDAY_JEWEL_PACKAGE_1_PRODUCT_KEY,
    BLACK_FRIDAY_DELUXE_PACKAGE_1_PRODUCT_KEY,
    BLACK_FRIDAY_GEM_PACKAGE_PRODUCT_KEY,
    BLACK_FRIDAY_JEWEL_PACKAGE_2_PRODUCT_KEY,
    BLACK_FRIDAY_RUNE_PACKAGE_PRODUCT_KEY,
    BLACK_FRIDAY_DELUXE_PACKAGE_2_PRODUCT_KEY,
    EARLY_BIRD_CHEST_1_PRODUCT_KEY,
    EARLY_BIRD_CHEST_2_PRODUCT_KEY,
    EARLY_BIRD_CHEST_3_PRODUCT_KEY,
    EARLY_BIRD_CHEST_4_PRODUCT_KEY,
    EARLY_BIRD_CHEST_5_PRODUCT_KEY,
    QUEEN_CHEST_1_PRODUCT_KEY,
    QUEEN_CHEST_2_PRODUCT_KEY,
    QUEEN_CHEST_3_PRODUCT_KEY,
    QUEEN_CHEST_4_PRODUCT_KEY,
    QUEEN_CHEST_5_PRODUCT_KEY,
    QUEEN_CHEST_6_PRODUCT_KEY,
    QUEEN_CHEST_7_PRODUCT_KEY,
    QUEEN_CHEST_8_PRODUCT_KEY,
    QUEEN_CHEST_9_PRODUCT_KEY,
    QUEEN_CHEST_10_PRODUCT_KEY,
    QUEEN_CHEST_11_PRODUCT_KEY,
    QUEEN_CHEST_12_PRODUCT_KEY,
    QUEEN_CHEST_13_PRODUCT_KEY,
    QUEEN_CHEST_14_PRODUCT_KEY,
)
from dso_server.protocol.command_ids import command_id
from dataclasses import dataclass
from contextlib import closing
import json
from datetime import date, timedelta
from pathlib import Path
import re
import sqlite3

from dso_server.protocol.character_selection import (
    BitReader, BitWriter, CLASS_NAMES,
)
from dso_server.protocol.commerce_protocol import (
    PurchaseItemDefinition,
    load_purchase_item_definition,
)
from dso_server.protocol.gameplay_protocol import OCPMethod, OCPOffer


ACCEPTED_TIER_SPENDER_COMMAND = command_id("Commands::AcceptedTierSpenderChallengesCommand", 0x0139)
# Client-only request. Retail calls this Commands::ShuffleTierSpenderDeals;
# unlike server commands it is absent from the generated command-id dump.
SHUFFLE_TIER_SPENDER_DEALS_COMMAND = 0x0148
NOTIFICATION_COMMAND = command_id("Commands::NotificationCommand", 0x0088)
ACTIVITY_SIGNIN_PROTOCOL_COMMAND = 0x0149
OCP_BOOKING_COMMAND = command_id("Commands::OCPBookingCommand", 0x010F)
OCP_BOOKING_NOTIFICATION_KIND = 16
OCP_BOOKING_NOTIFICATION_VALUES = (0, 0x06A7EB00, 0x06A7EB00)
OCP_PENDING_PREFIX = 'ocp-pending:'
OCP_COMPLETED_PREFIX = 'ocp-completed:'
REDEEM_VOUCHER_COMMAND = command_id("Commands::RedeemVoucherCommand", 0x0110)
REDEEM_VOUCHER_RESPONSE_COMMAND = command_id("Commands::RedeemVoucherResponseCommand", 0x0111)
GET_BLACK_FRIDAY_TRANSACTIONS_INFO_COMMAND = command_id(
    "Commands::GetBlackFridayTransactionsInfoCommand", 0x0177
)
GET_BLACK_FRIDAY_TRANSACTIONS_INFO_RESULT_COMMAND = command_id(
    "Commands::GetBlackFridayTransactionsInfoResultCommand", 0x0178
)
# ShopWidget 0x140531dec clears the code box on 0 and shows its own localized
# general error on -1 or 500. Every other status only shows the message, so
# the remaining values are this server's.
VOUCHER_REDEEMED = 0
VOUCHER_FAILED = -1
VOUCHER_INVALID = 1
VOUCHER_ALREADY_USED = 2
VOUCHER_NOT_ELIGIBLE = 3
VOUCHER_TOO_MANY_ATTEMPTS = 4


@dataclass(frozen=True)
class NotificationAction:
    notification_type: int
    subtype: int
    kind: int
    instance_guid: bytes
    values: tuple[int, int, int]
    payload: dict
    timestamp: str


@dataclass(frozen=True)
class OCPBookingRequest:
    offer_id: str
    payment_method: str


@dataclass(frozen=True)
class OCPBookingReward:
    offer_id: str
    item_id: str
    amount: int
    rarity: str = 'Common'
    bundle_items: tuple[tuple[str, int], ...] = ()


@dataclass(frozen=True)
class RedeemVoucherResponse:
    """Answer to a voucher code; the client shows ``message`` verbatim.

    ClientIngamePaymentManager 0x1402558b0 also opens ``url`` when ``action``
    is ``PAYMENT_URL``, which this server has no use for.
    """

    message: str
    status: int
    action: str = ''
    url: str = ''


def _client_reader(
    payload: bytes, command_id: int, bit_length: int | None = None
) -> BitReader:
    reader = BitReader(payload, len(payload) * 8 if bit_length is None else bit_length)
    if reader.read_uint8() != 0x8B:
        raise ValueError("expected a client command")
    actual = int.from_bytes(reader.read_bytes(2), "little")
    if actual != command_id:
        raise ValueError(f"expected 0x{command_id:04x}, got 0x{actual:04x}")
    return reader


def _finish_padding(reader: BitReader, label: str) -> None:
    if reader.remaining >= 8:
        raise ValueError(f"{label} has {reader.remaining} unread bits")
    if reader.remaining and reader.read_bits(reader.remaining):
        raise ValueError(f"{label} has non-zero padding")


def decode_tier_spender_accept(payload: bytes) -> tuple[str, ...]:
    reader = _client_reader(payload, ACCEPTED_TIER_SPENDER_COMMAND)
    rewards = tuple(
        reader.read_string() for _ in range(reader.read_uint32_le())
    )
    _finish_padding(reader, "AcceptedTierSpender")
    if not rewards or any(not reward for reward in rewards):
        raise ValueError("spender claim must contain reward ids")
    return rewards


def decode_tier_spender_shuffle(
    payload: bytes, bit_length: int | None = None
) -> int:
    """Decode the client's tier shuffle request (one uint32 tier index)."""

    reader = _client_reader(
        payload, SHUFFLE_TIER_SPENDER_DEALS_COMMAND, bit_length
    )
    tier_index = reader.read_uint32_le()
    _finish_padding(reader, "ShuffleTierSpenderDeals")
    if tier_index not in (0, 1, 2):
        raise ValueError(f"invalid tier spender shuffle index {tier_index}")
    return tier_index


def decode_notification_action(payload: bytes) -> NotificationAction:
    reader = _client_reader(payload, NOTIFICATION_COMMAND)
    notification_type = reader.read_uint8()
    if notification_type != 2:
        raise ValueError(
            f"notification action branch {notification_type} is not modeled"
        )
    subtype = reader.read_uint8()
    kind = reader.read_uint8()
    blob_size = int.from_bytes(reader.read_bytes(2), "little")
    instance_guid = reader.read_bytes(blob_size)
    values = tuple(reader.read_uint32_le() for _ in range(3))
    raw_payload = reader.read_string()
    timestamp = reader.read_string()
    _finish_padding(reader, "Notification action")
    try:
        document = json.loads(raw_payload)
    except json.JSONDecodeError as exc:
        raise ValueError("notification payload is not JSON") from exc
    if not isinstance(document, dict):
        raise ValueError("notification payload must be a JSON object")
    return NotificationAction(
        notification_type,
        subtype,
        kind,
        instance_guid,
        values,
        document,
        timestamp,
    )


def decode_ocp_booking_request(payload: bytes) -> OCPBookingRequest:
    reader = _client_reader(payload, OCP_BOOKING_COMMAND)
    request = OCPBookingRequest(reader.read_string(), reader.read_string())
    _finish_padding(reader, 'OCPBooking')
    if not request.offer_id or not request.payment_method:
        raise ValueError('OCP booking offer and payment method are required')
    return request


def encode_ocp_booking_request(
    offer_id: str, payment_method: str = "in_game"
) -> bytes:
    """Encode an OCPBookingCommand (0x010f) from client."""
    writer = BitWriter()
    writer.write_uint8(0x8B)
    writer.write_bytes(OCP_BOOKING_COMMAND.to_bytes(2, "little"))
    writer.write_string(str(offer_id))
    writer.write_string(str(payment_method))
    packet, _ = writer.finish()
    return packet


def encode_notification_action(action: NotificationAction) -> bytes:
    """Encode a NotificationCommand (0x0088) action branch from client."""
    writer = BitWriter()
    writer.write_uint8(0x8B)
    writer.write_bytes(NOTIFICATION_COMMAND.to_bytes(2, "little"))
    writer.write_uint8(int(action.notification_type))
    writer.write_uint8(int(action.subtype))
    writer.write_uint8(int(action.kind))
    writer.write_bytes(len(action.instance_guid).to_bytes(2, "little"))
    writer.write_bytes(action.instance_guid)
    for v in action.values:
        writer.write_uint32_le(int(v))
    writer.write_string(json.dumps(action.payload))
    writer.write_string(str(action.timestamp))
    packet, _ = writer.finish()
    return packet


def decode_redeem_voucher_request(payload: bytes) -> str:
    reader = _client_reader(payload, REDEEM_VOUCHER_COMMAND)
    code = reader.read_string()
    _finish_padding(reader, 'RedeemVoucher')
    return code


def encode_redeem_voucher_response(
    value: RedeemVoucherResponse, *, actor_id: int = 0
) -> tuple[bytes, int]:
    """Serialize a standalone RedeemVoucherResponseCommand (0x0111)."""

    writer = BitWriter()
    writer.write_uint8(0x84)
    writer.write_bytes(REDEEM_VOUCHER_RESPONSE_COMMAND.to_bytes(2, 'little'))
    writer.write_string(value.message)
    writer.write_uint32_le(value.status & 0xFFFFFFFF)
    writer.write_string(value.action)
    writer.write_string(value.url)
    writer.write_uint32_le(actor_id)
    return writer.finish()


def decode_redeem_voucher_response(
    payload: bytes, bit_length: int
) -> RedeemVoucherResponse:
    reader = BitReader(payload, bit_length)
    if reader.read_uint8() != 0x84:
        raise ValueError('RedeemVoucherResponse is not a server application message')
    if int.from_bytes(reader.read_bytes(2), 'little') != REDEEM_VOUCHER_RESPONSE_COMMAND:
        raise ValueError('RedeemVoucherResponse has the wrong command ID')
    message = reader.read_string()
    status = int.from_bytes(reader.read_bytes(4), 'little', signed=True)
    response = RedeemVoucherResponse(
        message, status, reader.read_string(), reader.read_string()
    )
    reader.read_uint32_le()
    if reader.remaining:
        raise ValueError('RedeemVoucherResponse has unaccounted trailing bits')
    return response


def encode_get_black_friday_transactions_info_result(
    transactions: tuple[str, ...] | list[str], *, actor_id: int = 0
) -> tuple[bytes, int]:
    """Serialize a standalone GetBlackFridayTransactionsInfoResultCommand (0x0178)."""

    writer = BitWriter()
    writer.write_uint8(0x84)
    writer.write_bytes(
        GET_BLACK_FRIDAY_TRANSACTIONS_INFO_RESULT_COMMAND.to_bytes(2, "little")
    )
    writer.write_uint32_le(len(transactions))
    for tx in transactions:
        writer.write_string(str(tx))
    writer.write_uint32_le(actor_id)
    return writer.finish()


def decode_get_black_friday_transactions_info_result(
    payload: bytes, bit_length: int | None = None
) -> tuple[tuple[str, ...], int]:
    reader = BitReader(
        payload, len(payload) * 8 if bit_length is None else bit_length
    )
    if reader.read_uint8() != 0x84:
        raise ValueError(
            "GetBlackFridayTransactionsInfoResult is not a server application message"
        )
    cmd_id = int.from_bytes(reader.read_bytes(2), "little")
    if cmd_id != GET_BLACK_FRIDAY_TRANSACTIONS_INFO_RESULT_COMMAND:
        raise ValueError(
            f"expected GetBlackFridayTransactionsInfoResultCommand "
            f"0x{GET_BLACK_FRIDAY_TRANSACTIONS_INFO_RESULT_COMMAND:04x}, got 0x{cmd_id:04x}"
        )
    count = reader.read_uint32_le()
    transactions = tuple(reader.read_string() for _ in range(count))
    actor_id = reader.read_uint32_le()
    if 0 < reader.remaining < 8:
        reader.read_bits(reader.remaining)
    if reader.remaining:
        raise ValueError(
            "GetBlackFridayTransactionsInfoResult has unaccounted trailing bits"
        )
    return transactions, actor_id


def normalize_ocp_offer_id(offer_id: str) -> str:
    """Normalize offer id aliases (with or without '101_' prefix and '_1.0000_0_NONE' suffix)."""
    offer_id = str(offer_id)
    current_offer_keys = {
        offer.product_key for offer in LOCAL_OCP_OFFERS
    }
    if offer_id in current_offer_keys:
        return offer_id
    candidate = f"{offer_id}_1.0000_0_NONE"
    if candidate in current_offer_keys:
        return candidate
    candidate_101 = f"101_{offer_id}_1.0000_0_NONE"
    if candidate_101 in current_offer_keys:
        return candidate_101
    candidate_pref = f"101_{offer_id}"
    if candidate_pref in current_offer_keys:
        return candidate_pref
    return offer_id


def resolve_ocp_booking_reward(
    offer_id: str,
    payment_method: str,
    *,
    class_id: int | None = None,
    allow_legacy: bool = False,
) -> OCPBookingReward:
    offer_id = normalize_ocp_offer_id(str(offer_id))
    payment_method = str(payment_method)
    allowed_methods = {method.name for method in LOCAL_OCP_METHODS} | {
        "external", "in_game", "ingame", "andermant", "",
    }
    if payment_method not in allowed_methods:
        raise ValueError(f'unsupported OCP payment method {payment_method!r}')
    current_offer_keys = {
        offer.product_key for offer in LOCAL_OCP_OFFERS
    }
    if (
        offer_id not in current_offer_keys
        and not (
            allow_legacy
            and offer_id in LEGACY_FULL_MOON_ATTIRE_PRODUCT_ITEMS
        )
    ):
        raise ValueError(f'unknown OCP offer {offer_id!r}')
    membership = MEMBERSHIP_PACKAGE_REWARDS.get(offer_id)
    if membership is not None:
        return OCPBookingReward(
            offer_id, membership[2], membership[3]
        )
    if offer_id == CHARACTER_ALTERATION_PRODUCT_KEY:
        return OCPBookingReward(offer_id, 'characterStyleEdit', 1)
    if offer_id in EVENT_ATTIRE_PRODUCT_KEYS:
        if class_id is None:
            raise ValueError(
                'Event attire purchase needs the active character class'
            )
        class_name = CLASS_NAMES.get(int(class_id), '').casefold()
        attire_item_id = EVENT_ATTIRE_ITEMS_BY_PRODUCT_CLASS[
            offer_id
        ].get(class_name)
        if attire_item_id is None:
            raise ValueError(
                f'Event attire is not available for class {class_name or class_id!r}'
            )
        if offer_id == BIG_GAME_HUNT_ATTIRE_PRODUCT_KEY:
            skin_item_id = (
                "skin_dwarf_unique_backitem_attire_biggamehunt_01"
                if class_name == "dwarf"
                else "skin_all_unique_backitem_attire_biggamehunt_01"
            )
            attire_rewards = ((attire_item_id, 1), (skin_item_id, 1))
            return OCPBookingReward(
                offer_id,
                attire_item_id,
                1,
                bundle_items=attire_rewards,
            )
        if offer_id == MONSTER_HUNT_ATTIRE_PRODUCT_KEY:
            skin_item_id = (
                "skin_dwarf_unique_backitem_attire_mini_kill_01"
                if class_name == "dwarf"
                else "skin_all_unique_backitem_attire_mini_kill_01"
            )
            return OCPBookingReward(
                offer_id,
                attire_item_id,
                1,
                bundle_items=((attire_item_id, 1), (skin_item_id, 1)),
            )
        if offer_id == SUMMER_FESTIVAL_ATTIRE_PRODUCT_KEY:
            skin_item_id = (
                "skin_dwarf_unique_backitem_attire_sum_01"
                if class_name == "dwarf"
                else "skin_all_unique_backitem_attire_sum_01"
            )
            attire_rewards = ((attire_item_id, 1), (skin_item_id, 1))
            return OCPBookingReward(
                offer_id,
                attire_item_id,
                1,
                bundle_items=attire_rewards,
            )
        if offer_id == DRAGAN_ATTIRE_PRODUCT_KEY:
            skin_item_id = (
                "skin_dwarf_unique_backitem_attire_dra_01"
                if class_name == "dwarf"
                else "skin_all_unique_backitem_attire_dra_01"
            )
            attire_rewards = ((attire_item_id, 1), (skin_item_id, 1))
            return OCPBookingReward(
                offer_id,
                attire_item_id,
                1,
                bundle_items=attire_rewards,
            )
        if offer_id == ROCKETMEN_ATTIRE_PRODUCT_KEY:
            skin_item_id = (
                "skin_dwarf_unique_backitem_attire_nye_01"
                if class_name == "dwarf"
                else "skin_all_unique_backitem_attire_nye_01"
            )
            attire_rewards = ((attire_item_id, 1), (skin_item_id, 1))
            return OCPBookingReward(
                offer_id,
                attire_item_id,
                1,
                bundle_items=attire_rewards,
            )
        if offer_id == SARGON_ATTIRE_PRODUCT_KEY:
            skin_item_id = (
                "skin_dwarf_unique_backitem_attire_sargon_01"
                if class_name == "dwarf"
                else "skin_all_unique_backitem_attire_sargon_01"
            )
            attire_rewards = ((attire_item_id, 1), (skin_item_id, 1))
            return OCPBookingReward(
                offer_id,
                attire_item_id,
                1,
                bundle_items=attire_rewards,
            )
        if offer_id == ANNIVERSARY_ATTIRE_PRODUCT_KEY:
            skin_item_id = (
                "skin_dwarf_unique_backitem_attire_anniversary_01"
                if class_name == "dwarf"
                else "skin_all_unique_backitem_attire_anniversary_01"
            )
            return OCPBookingReward(
                offer_id,
                attire_item_id,
                1,
                bundle_items=((attire_item_id, 1), (skin_item_id, 1)),
            )
        if offer_id == WINTER_SOLSTICE_ATTIRE_PRODUCT_KEY:
            skin_item_id = (
                "skin_dwarf_unique_backitem_attire_xmas_01"
                if class_name == "dwarf"
                else "skin_all_unique_backitem_attire_xmas_01"
            )
            attire_rewards = ((attire_item_id, 1), (skin_item_id, 1))
            return OCPBookingReward(
                offer_id,
                attire_item_id,
                1,
                bundle_items=attire_rewards,
            )
        if offer_id == KINGSHILL_SEWERS_ATTIRE_PRODUCT_KEY:
            skin_item_id = (
                "skin_dwarf_unique_backitem_attire_sewers_01"
                if class_name == "dwarf"
                else "skin_all_unique_backitem_attire_sewers_01"
            )
            return OCPBookingReward(
                offer_id,
                attire_item_id,
                1,
                bundle_items=((attire_item_id, 1), (skin_item_id, 1)),
            )
        if offer_id == DESERT_OF_ESSENCES_ATTIRE_PRODUCT_KEY:
            skin_item_id = (
                "skin_dwarf_unique_backitem_attire_doe_01"
                if class_name == "dwarf"
                else "skin_all_unique_backitem_attire_doe_01"
            )
            return OCPBookingReward(
                offer_id,
                attire_item_id,
                1,
                bundle_items=((attire_item_id, 1), (skin_item_id, 1)),
            )
        return OCPBookingReward(offer_id, attire_item_id, 1)
    if offer_id == EARLY_BIRD_CHEST_1_PRODUCT_KEY:
        class_name = CLASS_NAMES.get(int(class_id), '').casefold() if class_id is not None else 'mage'
        cloak_white = f"thanksgiving_cloak_white_2023_{class_name}"
        cloak_purple = f"thanksgiving_cloak_pruple_2023_{class_name}"
        return OCPBookingReward(
            offer_id,
            "mount_common_horse_01",
            1,
            bundle_items=(
                ("mount_common_horse_01", 1),
                (cloak_white, 1),
                (cloak_purple, 1),
                ("difficulty_key_common", 200),
            ),
        )
    bundle_reward = DEFEAT_UNDEFEATABLE_BUNDLE_REWARDS.get(offer_id)
    if bundle_reward is not None:
        item_id, amount = bundle_reward[0]
        return OCPBookingReward(
            offer_id, item_id, amount, bundle_items=bundle_reward
        )
    attire_item_id = (
        LEGACY_FULL_MOON_ATTIRE_PRODUCT_ITEMS.get(offer_id)
        if allow_legacy else None
    )
    if attire_item_id is not None:
        return OCPBookingReward(offer_id, attire_item_id, 1)
    match = re.fullmatch(
        r'1_realCurrency_(\d+)(?:\.\d+)?_0_NONE', offer_id
    )
    if match is None:
        raise ValueError(f'unsupported OCP offer reward {offer_id!r}')
    amount = int(match.group(1))
    if amount <= 0:
        raise ValueError('OCP reward amount must be positive')
    return OCPBookingReward(offer_id, 'money_rc', amount)


def ocp_booking_pending_key(
    instance_guid: bytes, offer_id: str
) -> str:
    guid = bytes(instance_guid)
    offer_id = str(offer_id)
    if len(guid) != 16:
        raise ValueError('OCP booking GUID must contain 16 bytes')
    if not offer_id or ':' in offer_id:
        raise ValueError('OCP booking offer id is invalid')
    return f'{OCP_PENDING_PREFIX}{guid.hex()}:{offer_id}'


def parse_ocp_booking_pending_key(
    key: str,
) -> tuple[bytes, str] | None:
    key = str(key)
    if not key.startswith(OCP_PENDING_PREFIX):
        return None
    encoded_guid, separator, offer_id = key[
        len(OCP_PENDING_PREFIX):
    ].partition(':')
    if not separator or len(encoded_guid) != 32 or not offer_id:
        return None
    try:
        guid = bytes.fromhex(encoded_guid)
    except ValueError:
        return None
    if len(guid) != 16:
        return None
    return guid, offer_id


def ocp_booking_completed_key(instance_guid: bytes) -> str:
    guid = bytes(instance_guid)
    if len(guid) != 16:
        raise ValueError('OCP booking GUID must contain 16 bytes')
    return f'{OCP_COMPLETED_PREFIX}{guid.hex()}'


def ocp_booking_notification_payload(
    reward: OCPBookingReward, instance_guid: bytes
) -> dict[str, object]:
    return ocp_booking_notification_payloads((reward,), instance_guid)


def ocp_booking_notification_payloads(
    rewards: tuple[OCPBookingReward, ...], instance_guid: bytes
) -> dict[str, object]:
    """Build one OCP booking notification containing one or more rewards.

    Serialize templates, amounts, and rarities as aligned semicolon-separated
    values so the notification describes one reward package.
    """

    guid = bytes(instance_guid)
    if len(guid) != 16:
        raise ValueError('OCP booking GUID must contain 16 bytes')
    values = tuple(rewards)
    if not values:
        raise ValueError('OCP booking notification must contain a reward')
    items = tuple(
        (item_id, amount, reward.rarity)
        for reward in values
        for item_id, amount in (
            reward.bundle_items or ((reward.item_id, reward.amount),)
        )
    )
    transaction_id = int.from_bytes(guid[:4], 'little') & 0x7FFFFFFF
    return {
        'ingameTransactionId': transaction_id or 1,
        'templates': ';'.join(str(item_id) for item_id, _, _ in items),
        'amounts': ';'.join(str(amount) for _, amount, _ in items),
        'rarities': ';'.join(
            'None' if item_id == 'characterStyleEdit' else rarity
            for item_id, _, rarity in items
        ),
        # The client routes this authored package id to its special
        # CharacterStyleEdit booking UI. addItems must remain true: false is
        # the negative/cancelled-order branch, even though this is not a normal
        # inventory item.
        'addItems': True,
        'playerCanRejectItem': False,
    }


def ocp_booking_notification_kind(reward: OCPBookingReward) -> int:
    """Select the client notification branch for this OCP booking reward."""
    # The live client session ignored kind 12 for this OCP purchase. Keep the
    # OCP notification kind that the client previously rendered interactively.
    return OCP_BOOKING_NOTIFICATION_KIND


def decode_activity_signin_request(payload: bytes) -> int:
    """Decode the one-field request used when the login calendar is opened."""

    reader = _client_reader(payload, ACTIVITY_SIGNIN_PROTOCOL_COMMAND)
    operation = reader.read_uint32_le()
    _finish_padding(reader, "ActivitySigninProtocol")
    if operation not in (1, 2):
        raise ValueError(
            f"unsupported ActivitySigninProtocol operation {operation}"
        )
    return operation


@dataclass(frozen=True)
class RewardDefinition:
    claim_id: str
    item_id: str
    amount: int
    rc_needed: int = 0


def load_spender_rewards(
    database_path: str | Path, reward_ids: tuple[str, ...]
) -> tuple[RewardDefinition, ...]:
    if len(set(reward_ids)) != len(reward_ids):
        raise ValueError("spender claim contains duplicate reward ids")
    database = sqlite3.connect(str(database_path))
    database.row_factory = sqlite3.Row
    try:
        definitions = []
        for reward_id in reward_ids:
            row = database.execute(
                'SELECT "Items", "ItemAmount", "RCNeeded", "Obsolete" '
                'FROM "_Template_RCSpenderRewards" WHERE "Id" = ?',
                (reward_id,),
            ).fetchone()
            if row is None or int(row["Obsolete"] or 0):
                raise ValueError(f"unknown spender reward {reward_id!r}")
            item_id = str(row["Items"] or "")
            amount = int(row["ItemAmount"] or 0)
            rc_needed = int(row["RCNeeded"] or 0)
            if not item_id or amount <= 0 or rc_needed <= 0:
                raise ValueError(f"invalid spender reward {reward_id!r}")
            # Resolve now so malformed database links fail before mutation.
            load_purchase_item_definition(database_path, item_id)
            definitions.append(RewardDefinition(
                reward_id, item_id, amount, rc_needed
            ))
        return tuple(definitions)
    finally:
        database.close()


def load_offer_group_rewards(
    database_path: str | Path,
    reward_group_id: str,
) -> tuple[RewardDefinition, ...]:
    """Resolve every item and amount in a database offer group.

    Daily rewards are ordinary ``_Template_Offergroup`` records.  Resolving
    their referenced ``_Template_Offer`` rows is important because days two
    through five contain additional ammunition/currency rewards; deriving one
    lootbox name from the group id silently discarded those entries.
    """

    reward_group_id = str(reward_group_id or "")
    if not reward_group_id:
        raise ValueError("reward group id is empty")
    database = sqlite3.connect(str(database_path))
    database.row_factory = sqlite3.Row
    try:
        group = database.execute(
            """
            SELECT "Offers", "Obsolete"
            FROM "_Template_Offergroup"
            WHERE "Id" = ?
            """,
            (reward_group_id,),
        ).fetchone()
        if group is None or int(group["Obsolete"] or 0):
            raise ValueError(f"unknown reward group {reward_group_id!r}")
        offer_ids = tuple(
            entry
            for entry in str(group["Offers"] or "").split(";")
            if entry
        )
        if not offer_ids or len(set(offer_ids)) != len(offer_ids):
            raise ValueError(
                f"invalid reward group contents for {reward_group_id!r}"
            )
        definitions = []
        for offer_id in offer_ids:
            offer = database.execute(
                """
                SELECT "Item", "Amount"
                FROM "_Template_Offer"
                WHERE "Id" = ?
                """,
                (offer_id,),
            ).fetchone()
            if offer is None:
                raise ValueError(
                    f"reward group {reward_group_id!r} references "
                    f"unknown offer {offer_id!r}"
                )
            item_id = str(offer["Item"] or "")
            amount = int(offer["Amount"] or 0)
            if not item_id or amount <= 0:
                raise ValueError(f"invalid reward offer {offer_id!r}")
            load_purchase_item_definition(database_path, item_id)
            definitions.append(RewardDefinition(offer_id, item_id, amount))
        return tuple(definitions)
    finally:
        database.close()


def _parse_loot_award(
    raw_award: str,
    *,
    claim_id: str,
) -> RewardDefinition:
    raw_award = str(raw_award or "").strip()
    if not raw_award.startswith("Loot:"):
        raise ValueError(f"unsupported sign-in award {raw_award!r}")
    item_id, separator, raw_amount = raw_award[5:].rpartition(",")
    if not separator or not item_id:
        raise ValueError(f"invalid sign-in award {raw_award!r}")
    amount = int(raw_amount)
    if amount <= 0:
        raise ValueError(f"invalid sign-in award amount {raw_award!r}")
    return RewardDefinition(claim_id, item_id, amount)


def load_payment_signin_rewards(
    database_path: str | Path,
    day: int,
    *,
    include_paid_reward: bool = False,
) -> tuple[RewardDefinition, ...]:
    """Resolve one day of the independent 14-day sign-in calendar."""

    day = int(day)
    database = sqlite3.connect(str(database_path))
    database.row_factory = sqlite3.Row
    try:
        row = database.execute(
            """
            SELECT "Id", "PaymentSigninAwardGroup",
                   "PaymentSigninPayAwardGroup"
            FROM "_Template_PaymentSignin"
            WHERE "PaymentSigninDays" = ?
            """,
            (day,),
        ).fetchone()
        if row is None:
            raise ValueError(f"unknown payment sign-in day {day}")
        definitions = [
            _parse_loot_award(
                row["PaymentSigninAwardGroup"],
                claim_id=f"payment-signin:{row['Id']}:normal",
            )
        ]
        if include_paid_reward:
            definitions.append(
                _parse_loot_award(
                    row["PaymentSigninPayAwardGroup"],
                    claim_id=f"payment-signin:{row['Id']}:paid",
                )
            )
        for definition in definitions:
            load_purchase_item_definition(
                database_path, definition.item_id
            )
        return tuple(definitions)
    finally:
        database.close()


def pending_daily_login_day(
    claims: dict[str, int],
    *,
    today: date | None = None,
) -> int | None:
    """Return today's unclaimed one-based day in the retail five-day cycle."""

    today = today or date.today()
    today_key = f"daily-login:{today.isoformat()}"
    if int(claims.get(today_key, 0)) > 0:
        return None
    yesterday_key = (
        f"daily-login:{(today - timedelta(days=1)).isoformat()}"
    )
    previous_day = int(claims.get(yesterday_key, 0))
    if not 1 <= previous_day <= 5:
        return 1
    return 1 if previous_day == 5 else previous_day + 1


def daily_login_reward_group(class_name: str, day: int) -> str:
    class_name = str(class_name).strip().lower()
    day = int(day)
    if not class_name or not 1 <= day <= 5:
        raise ValueError("daily login class/day is invalid")
    return f"login_reward_{day:02d}_{class_name}_01"


def premium_daily_login_reward_group(class_name: str, day: int) -> str:
    class_name = str(class_name).strip().lower()
    day = int(day)
    if not class_name or not 1 <= day <= 5:
        raise ValueError("premium daily login class/day is invalid")
    return f"premium_login_reward_{day:02d}_{class_name}_01"


def load_extension_offer(
    database_path: str | Path,
    offer_id: str,
) -> OfferDetails:
    from dso_server.protocol.player_init_protocol import (
        DEFAULT_BANK_EXTENSION_OFFER,
        DEFAULT_INVENTORY_EXTENSION_OFFER,
        OfferDetails,
    )
    from dso_server.protocol.shop_protocol import OFFER_CATEGORY_IDS, OFFER_RARITY_IDS

    try:
        resolved = resident_content.resolved(database_path)
        uri = resolved.as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as db:
            db.row_factory = sqlite3.Row
            row = db.execute(
                "SELECT * FROM _Template_Offer WHERE Id=?",
                (offer_id,),
            ).fetchone()
            if row is not None:
                return OfferDetails(
                    resource_id=str(row["Id"]),
                    field_58=int(row["PriceRC"]),
                    field_5c=int(row["PriceVC"]),
                    field_50=str(row["Item"]),
                    field_74=int(row["Amount"]),
                    field_78=int(row["Difficulty"]),
                    field_94=OFFER_RARITY_IDS.get(str(row["Rarity"]), 0),
                    field_98=OFFER_CATEGORY_IDS.get(str(row["OfferCategory"]), 46),
                    values_168=(),
                    values_190=(),
                    field_c0=str(row["IconBrush"] or ""),
                    field_c8=str(row["IconConfiguration"] or ""),
                    field_80=int(row["MinPlayerLevel"]),
                    field_84=int(row["MaxPlayerLevel"]),
                    field_88=bool(row["IsRecommended"]),
                    field_8b=bool(row["AutoIdentify"]),
                )
    except Exception:
        pass
    if "bank" in offer_id:
        return DEFAULT_BANK_EXTENSION_OFFER
    return DEFAULT_INVENTORY_EXTENSION_OFFER


def resolve_inventory_extension_offer(
    database_path: str | Path,
    storage_upgrade: int,
) -> OfferDetails | None:
    if storage_upgrade >= 29:
        return None
    upg = max(1, min(29, int(storage_upgrade)))
    offer_id = f"misc_bag_extension_{upg:02d}"
    return load_extension_offer(database_path, offer_id)


def resolve_bank_extension_offer(
    database_path: str | Path,
    bank_storage_upgrade: int,
) -> OfferDetails | None:
    if bank_storage_upgrade >= 55:
        return None
    upg = max(1, min(54, int(bank_storage_upgrade)))
    offer_id = f"misc_bank_extension_{upg:02d}"
    return load_extension_offer(database_path, offer_id)


def daily_login_offers(
    database_path: str | Path,
    class_name: str,
    storage_upgrade: int | None = None,
    bank_storage_upgrade: int | None = None,
):
    """Supply the offer groups the native login widget resolves by name.

    Retail sends six display columns; the last repeats day five. The claim
    cycle itself remains five days. Both regular and premium previews are
    present regardless of the player's membership.
    """
    from dso_server.protocol.player_init_protocol import (
        OfferDetails, OfferGroup, OfferGroupEntry, SpecialOffer,
    )
    from dso_server.protocol.shop_protocol import OFFER_CATEGORY_IDS, OFFER_RARITY_IDS

    with closing(sqlite3.connect(resident_content.resolved(database_path).as_uri() + '?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row

        def group(resource_id):
            row = db.execute('SELECT * FROM _Template_Offergroup WHERE Id=?',
                             (resource_id,)).fetchone()
            if row is None or row['Obsolete']:
                raise ValueError(f'unknown daily reward group {resource_id!r}')
            entries = []
            for offer_id in str(row['Offers']).split(';'):
                if not offer_id:
                    continue
                offer = db.execute('SELECT * FROM _Template_Offer WHERE Id=?',
                                   (offer_id,)).fetchone()
                if offer is None:
                    raise ValueError(f'unknown daily reward offer {offer_id!r}')
                entries.append(OfferGroupEntry(OfferDetails(
                    resource_id=offer_id,
                    field_58=int(offer['PriceRC']), field_5c=int(offer['PriceVC']),
                    field_50=str(offer['Item']), field_74=int(offer['Amount']),
                    field_78=int(offer['Difficulty']),
                    field_94=OFFER_RARITY_IDS[str(offer['Rarity'])],
                    field_98=OFFER_CATEGORY_IDS[str(offer['OfferCategory'])],
                    values_168=(), values_190=(),
                    field_c0=str(offer['IconBrush'] or ''),
                    field_c8=str(offer['IconConfiguration'] or ''),
                    field_80=int(offer['MinPlayerLevel']),
                    field_84=int(offer['MaxPlayerLevel']),
                    field_88=bool(offer['IsRecommended']),
                    field_8b=bool(offer['AutoIdentify']),
                ), 1))
            return OfferGroup(resource_id, int(row['PriceRC']), tuple(entries))

        days = (1, 2, 3, 4, 5, 5)
        regular = tuple(group(daily_login_reward_group(class_name, d)) for d in days)
        premium = tuple(group(premium_daily_login_reward_group(class_name, d)) for d in days)
    inv_offer = (
        resolve_inventory_extension_offer(database_path, storage_upgrade)
        if storage_upgrade is not None
        else None
    )
    bank_offer = (
        resolve_bank_extension_offer(database_path, bank_storage_upgrade)
        if bank_storage_upgrade is not None
        else None
    )
    return SpecialOffer(inv_offer, bank_offer, None, False, '0000-00-00 00:00:00',
                        regular, premium, (), ())


def daily_login_item_id(action: NotificationAction) -> str:
    reward = str(action.payload.get("reward", ""))
    match = re.search(r"login_reward_(\d{2})_", reward)
    if not match:
        raise ValueError(f"unsupported daily-login reward {reward!r}")
    return f"dailylogin_lootbox_day_{match.group(1)}"


LOCAL_OCP_OFFERS = (
    OCPOffer(86_899, 1.99, True, "€", "1_realCurrency_1500.0000_0_NONE", "", ""),
    OCPOffer(86_937, 4.99, False, "€", "1_realCurrency_4000.0000_0_NONE", "", ""),
    OCPOffer(87_014, 9.99, False, "€", "1_realCurrency_8500.0000_0_NONE", "", ""),
    OCPOffer(87_052, 24.99, False, "€", "1_realCurrency_24000.0000_0_NONE", "", ""),
    OCPOffer(123_835, 49.99, False, "€", "1_realCurrency_48000.0000_0_NONE", "", ""),
    OCPOffer(123_880, 99.99, False, "€", "1_realCurrency_114000.0000_0_NONE", "", ""),
    # Authored PaymentPackages row 7_characterStyleEdit_1.0000_0_NONE.
    # The client classifies it as CharacterAlteration and displays the
    # Character Change card in the cash shop.
    OCPOffer(
        7, 99.90, False, "€",
        "7_characterStyleEdit_1.0000_0_NONE", "", "",
    ),
    # Reuse native package rows/titles. Event attire grants only the active
    # character's matching costume; the Defeat the Undefeatable offers grant
    # their corresponding event reward item after purchase confirmation.
    OCPOffer(
        180_201, 9.99, False, "€",
        FULL_MOON_ATTIRE_PRODUCT_KEY, "", "",
    ),
    OCPOffer(
        180_202, 9.90, False, "€",
        NEW_MOON_ATTIRE_PRODUCT_KEY, "", "",
    ),
    OCPOffer(
        180_203, 9.90, False, "€",
        DEFEAT_UNDEFEATABLE_ATTIRE_PRODUCT_KEY, "", "",
    ),
    OCPOffer(
        180_204, 9.90, False, "€",
        DEFEAT_UNDEFEATABLE_LAVA_ESSENCE_PRODUCT_KEY, "", "",
    ),
    OCPOffer(
        180_205, 9.90, False, "€",
        DEFEAT_UNDEFEATABLE_LAVA_MATERIAL_PRODUCT_KEY, "", "",
    ),
    OCPOffer(
        180_206, 9.90, False, "€",
        DEFEAT_UNDEFEATABLE_HERO_BAG_PRODUCT_KEY, "", "",
    ),
    OCPOffer(
        180_207, 9.90, False, "€",
        DEFEAT_UNDEFEATABLE_ROAD_TO_TRIUMPH_PRODUCT_KEY, "", "",
    ),
    OCPOffer(
        180_208, 9.90, False, "€",
        DEFEAT_UNDEFEATABLE_UNLOCK_VICTORY_PRODUCT_KEY, "", "",
    ),
    # Native Big Game Hunt packages. Prices and package icons come from these
    # existing PaymentPackages rows; rewards are routed after confirmation.
    OCPOffer(
        180_209, 9.90, False, "€",
        BIG_GAME_HUNT_ATTIRE_PRODUCT_KEY, "", "",
    ),
    OCPOffer(
        180_210, 9.90, False, "€",
        BIG_GAME_HUNT_SMALL_BUNDLE_PRODUCT_KEY, "", "",
    ),
    OCPOffer(
        180_211, 9.90, False, "€",
        BIG_GAME_HUNT_MEDIUM_BUNDLE_PRODUCT_KEY, "", "",
    ),
    OCPOffer(
        180_212, 9.90, False, "€",
        BIG_GAME_HUNT_BIG_BUNDLE_PRODUCT_KEY, "", "",
    ),
    OCPOffer(
        180_213, 9.90, False, "€",
        BIG_GAME_HUNT_JEWEL_PACK_PRODUCT_KEY, "", "",
    ),
    # Dragan event attire and packages reuse their native PaymentPackages
    # product keys. Package rewards are routed after confirmation above.
    OCPOffer(180_214, 9.90, False, "€", DRAGAN_ATTIRE_PRODUCT_KEY, "", ""),
    OCPOffer(180_215, 9.90, False, "€", DRAGAN_LAST_GUARDIAN_PRODUCT_KEY, "", ""),
    OCPOffer(180_216, 9.90, False, "€", DRAGAN_JUDGMENT_DAY_PRODUCT_KEY, "", ""),
    OCPOffer(180_217, 9.90, False, "€", DRAGAN_EYE_OF_AVERSION_PRODUCT_KEY, "", ""),
    OCPOffer(180_218, 9.90, False, "€", DRAGAN_EYE_OF_FEAR_PRODUCT_KEY, "", ""),
    OCPOffer(180_219, 9.90, False, "€", DRAGAN_EYE_OF_COLLAPSE_PRODUCT_KEY, "", ""),
    OCPOffer(180_220, 9.90, False, "€", DRAGAN_FALLEN_HEART_PRODUCT_KEY, "", ""),
    OCPOffer(180_221, 9.90, False, "€", HALLOWEEN_SMALL_PIE_PRODUCT_KEY, "", ""),
    OCPOffer(180_222, 9.90, False, "€", HALLOWEEN_MEDIUM_PIE_PRODUCT_KEY, "", ""),
    OCPOffer(180_223, 9.90, False, "€", HALLOWEEN_BIG_PIE_PRODUCT_KEY, "", ""),
    OCPOffer(180_224, 9.90, False, "€", HALLOWEEN_BUNDLE_PRODUCT_KEY, "", ""),
    OCPOffer(180_225, 9.90, False, "€", HALLOWEEN_ATTIRE_PRODUCT_KEY, "", ""),
    # The summer PaymentPackages row and event_pass_summer offer group are
    # native; route the confirmed purchase to the active character's outfit.
    OCPOffer(
        180_226, 9.90, False, "€",
        SUMMER_FESTIVAL_ATTIRE_PRODUCT_KEY, "", "",
    ),
    # All remaining Summer Solstice OCP products reuse their native package
    # keys and route the template offer-group contents after purchase.
    OCPOffer(
        180_227, 9.90, False, "€",
        SUMMER_FESTIVAL_PREP_START_PRODUCT_KEY, "", "",
    ),
    OCPOffer(
        180_228, 9.90, False, "€",
        SUMMER_FESTIVAL_START_PRODUCT_KEY, "", "",
    ),
    OCPOffer(
        180_229, 9.90, False, "€",
        SUMMER_FORTUNE_BAG_SMALL_PRODUCT_KEY, "", "",
    ),
    OCPOffer(
        180_230, 9.90, False, "€",
        SUMMER_FORTUNE_BAG_MEDIUM_PRODUCT_KEY, "", "",
    ),
    OCPOffer(
        180_231, 9.90, False, "€",
        SUMMER_FORTUNE_BAG_BIG_PRODUCT_KEY, "", "",
    ),
    OCPOffer(
        180_232, 9.90, False, "€",
        SUMMER_NIGHTDREAM_CHEST_PRODUCT_KEY, "", "",
    ),
    OCPOffer(
        180_233, 9.90, False, "€",
        SUMMER_LUCKY_CHEST_PRODUCT_KEY, "", "",
    ),
    OCPOffer(
        180_234, 9.90, False, "€",
        SUMMER_SET_CHEST_PRODUCT_KEY, "", "",
    ),
    OCPOffer(180_235, 9.90, False, "€", DARK_DWARF_LEVIN_CRYSTAL_PACKAGE_PRODUCT_KEY, "", ""),
    OCPOffer(180_236, 9.90, False, "€", DARK_DWARF_FORGEFYRE_MAGIC_POTION_PRODUCT_KEY, "", ""),
    OCPOffer(180_237, 9.90, False, "€", DARK_DWARF_FORGEFYRE_POTION_PRODUCT_KEY, "", ""),
    OCPOffer(180_238, 9.90, False, "€", DARK_DWARF_LEVIN_CRYSTAL_SUPER_PACKAGE_PRODUCT_KEY, "", ""),
    OCPOffer(180_239, 9.90, False, "€", DARK_DWARF_SHOULDERS_FORTUNE_CHEST_PRODUCT_KEY, "", ""),
    OCPOffer(180_240, 9.90, False, "€", DARK_DWARF_CLOAK_FORTUNE_CHEST_PRODUCT_KEY, "", ""),
    OCPOffer(180_241, 9.90, False, "€", DARK_DWARF_COPPER_FARTHING_PACKAGE_PRODUCT_KEY, "", ""),
    OCPOffer(180_242, 9.90, False, "€", STELLAR_GOLD_PACKAGE_PRODUCT_KEY, "", ""),
    OCPOffer(180_243, 9.90, False, "€", STELLAR_GOLD_ATTIRE_PRODUCT_KEY, "", ""),
    # Chinese New Year packages reuse their native PaymentPackages keys and
    # grant the authored offer-group contents after purchase confirmation.
    OCPOffer(
        180_244, 9.90, False, "€",
        CHINESE_NEW_YEAR_DRAGON_BUNDLE_1_PRODUCT_KEY, "", "",
    ),
    OCPOffer(
        180_245, 9.90, False, "€",
        CHINESE_NEW_YEAR_DRAGON_BUNDLE_2_PRODUCT_KEY, "", "",
    ),
    OCPOffer(
        180_246, 9.90, False, "€",
        CHINESE_NEW_YEAR_COOKING_BUNDLE_SMALL_PRODUCT_KEY, "", "",
    ),
    OCPOffer(
        180_247, 9.90, False, "€",
        CHINESE_NEW_YEAR_COOKING_BUNDLE_BIG_PRODUCT_KEY, "", "",
    ),
    OCPOffer(
        180_248, 9.90, False, "€",
        CHINESE_NEW_YEAR_KUNG_FU_LION_PACKAGE_PRODUCT_KEY, "", "",
    ),
    OCPOffer(
        180_249, 9.90, False, "€",
        CHINESE_NEW_YEAR_SURPRISE_CHEST_PRODUCT_KEY, "", "",
    ),
    OCPOffer(
        180_250, 9.90, False, "€",
        CHINESE_NEW_YEAR_GIFT_CHEST_PRODUCT_KEY, "", "",
    ),
    # Riot of the Rocketmen uses native New Year event payment packages.
    OCPOffer(180_251, 9.90, False, "€", ROCKETMEN_ATTIRE_PRODUCT_KEY, "", ""),
    OCPOffer(180_252, 9.90, False, "€", ROCKETMEN_SHARK_PACKAGE_PRODUCT_KEY, "", ""),
    OCPOffer(180_253, 9.90, False, "€", ROCKETMEN_SOUL_DESTRUCTION_CHEST_PRODUCT_KEY, "", ""),
    OCPOffer(180_254, 9.90, False, "€", ROCKETMEN_SMALL_SOUL_CHEST_PRODUCT_KEY, "", ""),
    # Monster Hunt uses the native attire and Hunting Ring payment packages.
    OCPOffer(180_255, 9.90, False, "€", MONSTER_HUNT_ATTIRE_PRODUCT_KEY, "", ""),
    OCPOffer(180_256, 9.90, False, "€", HUNTING_RING_BUNDLE_PRODUCT_KEY, "", ""),
    # Easter Spring Festival offers reuse the client package/item identifiers;
    # package contents are routed by the server after purchase confirmation.
    OCPOffer(180_257, 9.90, False, "€", EASTER_ATTIRE_PRODUCT_KEY, "", ""),
    OCPOffer(180_258, 9.90, False, "€", EASTER_SPRING_MATERIAL_PACK_PRODUCT_KEY, "", ""),
    OCPOffer(180_259, 9.90, False, "€", EASTER_SURPRISE_SPRING_PACK_PRODUCT_KEY, "", ""),
    OCPOffer(180_260, 9.90, False, "€", EASTER_DAILY_SPRING_PACK_PRODUCT_KEY, "", ""),
    OCPOffer(180_261, 9.90, False, "€", EASTER_DAILY_SURPRISE_SPRING_PACKAGE_PRODUCT_KEY, "", ""),
    # Sargon's native event pass resolves to the active character's attire and
    # its matching back skin after purchase confirmation.
    OCPOffer(
        180_262, 9.90, False, "€",
        SARGON_ATTIRE_PRODUCT_KEY, "", "",
    ),
    # Anniversary packages are only exposed while the main Anniversary event
    # is active; their payment-provider price is represented here in euros.
    OCPOffer(180_263, 9.90, False, "€", ANNIVERSARY_STARTER_BUNDLE_PRODUCT_KEY, "", ""),
    OCPOffer(180_264, 9.90, False, "€", ANNIVERSARY_NEW_EXPLORER_PACKAGE_PRODUCT_KEY, "", ""),
    OCPOffer(180_265, 9.90, False, "€", ANNIVERSARY_WIND_OF_BLOOD_PACKAGE_PRODUCT_KEY, "", ""),
    OCPOffer(180_266, 9.90, False, "€", ANNIVERSARY_DARK_NIGHTS_PACKAGE_PRODUCT_KEY, "", ""),
    OCPOffer(180_267, 9.90, False, "€", ANNIVERSARY_SOUL_TRUNCHEON_SINGLE_PRODUCT_KEY, "", ""),
    OCPOffer(180_268, 9.90, False, "€", ANNIVERSARY_SOUL_TRUNCHEON_BUNDLE_PRODUCT_KEY, "", ""),
    OCPOffer(180_269, 9.90, False, "€", ANNIVERSARY_SOUL_TRUNCHEON_BULK_PRODUCT_KEY, "", ""),
    OCPOffer(180_270, 9.90, False, "€", ANNIVERSARY_ATTIRE_PRODUCT_KEY, "", ""),
    OCPOffer(180_271, 9.90, False, "€", ANNIVERSARY_PET_CHEST_PRODUCT_KEY, "", ""),
    OCPOffer(180_272, 9.90, False, "€", CHARGED_MARK_OF_BRAVERY_PACKAGE_PRODUCT_KEY, "", ""),
    # Winter Solstice Festival OCP Cashshop offers (9.90 EUR each)
    OCPOffer(180_273, 9.90, False, "€", WINTER_SOLSTICE_ATTIRE_PRODUCT_KEY, "", ""),
    OCPOffer(180_274, 9.90, False, "€", TEARS_OF_UNIVERSE_PACKAGE_PRODUCT_KEY, "", ""),
    OCPOffer(180_275, 9.90, False, "€", WINTER_EVENT_SMALL_BUNDLE_PRODUCT_KEY, "", ""),
    OCPOffer(180_276, 9.90, False, "€", WINTER_EVENT_BIG_BUNDLE_PRODUCT_KEY, "", ""),
    OCPOffer(180_277, 9.90, False, "€", MASCOT_PACKAGE_PRODUCT_KEY, "", ""),
    OCPOffer(180_278, 9.90, False, "€", WINTER_SOLSTICE_CHEST_PRODUCT_KEY, "", ""),
    OCPOffer(180_279, 9.90, False, "€", WINTER_SOLSTICE_MATERIALS_CHEST_PRODUCT_KEY, "", ""),
    OCPOffer(180_280, 9.90, False, "€", TRIANGLE_OF_FORCES_PACKAGE_PRODUCT_KEY, "", ""),
    # Kingshill Sewers offers reuse the authored package keys. Random packs
    # stay as loot containers, while the material chest's fixed offer-group
    # contents are granted directly after booking.
    OCPOffer(180_281, 9.90, False, "€", KINGSHILL_SEWERS_MUTATION_PACKAGE_PRODUCT_KEY, "", ""),
    OCPOffer(180_282, 9.90, False, "€", KINGSHILL_SEWERS_ATTIRE_PRODUCT_KEY, "", ""),
    OCPOffer(180_283, 9.90, False, "€", KINGSHILL_SEWERS_MATERIAL_CHEST_PRODUCT_KEY, "", ""),
    OCPOffer(180_284, 9.90, False, "€", KINGSHILL_SEWERS_SURPRISE_PACK_PRODUCT_KEY, "", ""),
    OCPOffer(180_285, 9.90, False, "€", KINGSHILL_SEWERS_DAILY_PACK_PRODUCT_KEY, "", ""),
    # Rampaging Worms blitz bundles reuse the native PaymentPackages names,
    # icons and once-only descriptions; the server grants their fixed contents.
    OCPOffer(180_286, 9.90, False, "€", KINGSHILL_SEWERS_RAMPAGING_STORM_PRODUCT_KEY, "", ""),
    OCPOffer(180_287, 9.90, False, "€", KINGSHILL_SEWERS_RAMPAGING_HURRICANE_PRODUCT_KEY, "", ""),
    # The native Desert of Essences event pass resolves to the character's
    # class attire and its matching equipment appearance unlock.
    OCPOffer(
        180_288, 9.90, False, "€",
        DESERT_OF_ESSENCES_ATTIRE_PRODUCT_KEY, "", "",
    ),
    # Premium and Deluxe membership packages reuse the client's own 1, 3, 6 and
    # 12 month payment packages; settlement grants the membership time.
    OCPOffer(180_289, 9.90, False, "€", "6_premiumgold_1.0000_1_MONTHS", "", ""),
    OCPOffer(180_290, 9.90, False, "€", "6_premiumgold_1.0000_3_MONTHS", "", ""),
    OCPOffer(180_291, 9.90, False, "€", "6_premiumgold_1.0000_6_MONTHS", "", ""),
    OCPOffer(180_292, 9.90, False, "€", "6_premiumgold_1.0000_12_MONTHS", "", ""),
    OCPOffer(180_293, 9.90, False, "€", "6_deluxe_1.0000_1_MONTHS", "", ""),
    OCPOffer(180_294, 9.90, False, "€", "6_deluxe_1.0000_3_MONTHS", "", ""),
    OCPOffer(180_295, 9.90, False, "€", "6_deluxe_1.0000_6_MONTHS", "", ""),
    OCPOffer(180_296, 9.90, False, "€", "6_deluxe_1.0000_12_MONTHS", "", ""),
    # Black Friday promo packages
    OCPOffer(180_297, 9.90, False, "€", BLACK_FRIDAY_REALM_PATH_PACKAGE_PRODUCT_KEY, "", ""),
    OCPOffer(180_298, 9.90, False, "€", BLACK_FRIDAY_ANDERMANT_PACKAGE_PRODUCT_KEY, "", ""),
    OCPOffer(180_299, 9.90, False, "€", BLACK_FRIDAY_JEWEL_PACKAGE_1_PRODUCT_KEY, "", ""),
    OCPOffer(180_300, 9.90, False, "€", BLACK_FRIDAY_DELUXE_PACKAGE_1_PRODUCT_KEY, "", ""),
    OCPOffer(180_301, 9.90, False, "€", BLACK_FRIDAY_GEM_PACKAGE_PRODUCT_KEY, "", ""),
    OCPOffer(180_302, 9.90, False, "€", BLACK_FRIDAY_JEWEL_PACKAGE_2_PRODUCT_KEY, "", ""),
    OCPOffer(180_303, 9.90, False, "€", BLACK_FRIDAY_RUNE_PACKAGE_PRODUCT_KEY, "", ""),
    OCPOffer(180_304, 9.90, False, "€", BLACK_FRIDAY_DELUXE_PACKAGE_2_PRODUCT_KEY, "", ""),
    # PreSaleDiscount / Early Bird offers
    OCPOffer(180_305, 1.99, False, "€", EARLY_BIRD_CHEST_1_PRODUCT_KEY, "", ""),
    OCPOffer(180_306, 4.99, False, "€", EARLY_BIRD_CHEST_2_PRODUCT_KEY, "", ""),
    OCPOffer(180_307, 9.99, False, "€", EARLY_BIRD_CHEST_3_PRODUCT_KEY, "", ""),
    OCPOffer(180_308, 24.99, False, "€", EARLY_BIRD_CHEST_4_PRODUCT_KEY, "", ""),
    OCPOffer(180_309, 49.99, False, "€", EARLY_BIRD_CHEST_5_PRODUCT_KEY, "", ""),
    # PreSaleDiscountSpecial / Queen Chests (Happy Time)
    OCPOffer(180_310, 9.99, False, "€", QUEEN_CHEST_1_PRODUCT_KEY, "", ""),
    OCPOffer(180_311, 14.99, False, "€", QUEEN_CHEST_2_PRODUCT_KEY, "", ""),
    OCPOffer(180_312, 19.99, False, "€", QUEEN_CHEST_3_PRODUCT_KEY, "", ""),
    OCPOffer(180_313, 19.99, False, "€", QUEEN_CHEST_4_PRODUCT_KEY, "", ""),
    OCPOffer(180_314, 14.99, False, "€", QUEEN_CHEST_5_PRODUCT_KEY, "", ""),
    OCPOffer(180_315, 24.99, False, "€", QUEEN_CHEST_6_PRODUCT_KEY, "", ""),
    OCPOffer(180_316, 29.99, False, "€", QUEEN_CHEST_7_PRODUCT_KEY, "", ""),
    OCPOffer(180_317, 49.99, False, "€", QUEEN_CHEST_8_PRODUCT_KEY, "", ""),
    OCPOffer(180_318, 29.99, False, "€", QUEEN_CHEST_9_PRODUCT_KEY, "", ""),
    OCPOffer(180_319, 49.99, False, "€", QUEEN_CHEST_10_PRODUCT_KEY, "", ""),
    OCPOffer(180_320, 39.99, False, "€", QUEEN_CHEST_11_PRODUCT_KEY, "", ""),
    OCPOffer(180_321, 19.99, False, "€", QUEEN_CHEST_12_PRODUCT_KEY, "", ""),
    OCPOffer(180_322, 14.99, False, "€", QUEEN_CHEST_13_PRODUCT_KEY, "", ""),
    OCPOffer(180_323, 19.99, False, "€", QUEEN_CHEST_14_PRODUCT_KEY, "", ""),
)

CHARACTER_ALTERATION_PRODUCT_KEY = "7_characterStyleEdit_1.0000_0_NONE"

LOCAL_OCP_METHODS = (
    OCPMethod("external", "Local development payment provider", 0.0, 0.0),
    OCPMethod("in_game", "In-game purchase provider", 0.0, 0.0),
    OCPMethod("ingame", "In-game purchase provider", 0.0, 0.0),
    OCPMethod("andermant", "Andermant purchase provider", 0.0, 0.0),
    OCPMethod("", "Default in-game purchase provider", 0.0, 0.0),
)
