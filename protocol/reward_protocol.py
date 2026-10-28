"""Typed premium-store, spender-reward, and daily-login helpers."""

from __future__ import annotations

from dso_server.runtime import resident_content
from dso_server.protocol.command_ids import command_id
from dataclasses import dataclass
from contextlib import closing
import json
from datetime import date, timedelta
from pathlib import Path
import re
import sqlite3

from dso_server.protocol.character_selection import BitReader
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


def resolve_ocp_booking_reward(
    offer_id: str, payment_method: str
) -> OCPBookingReward:
    offer_id = str(offer_id)
    payment_method = str(payment_method)
    if payment_method not in {method.name for method in LOCAL_OCP_METHODS}:
        raise ValueError(f'unsupported OCP payment method {payment_method!r}')
    if offer_id not in {offer.product_key for offer in LOCAL_OCP_OFFERS}:
        raise ValueError(f'unknown OCP offer {offer_id!r}')
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
    guid = bytes(instance_guid)
    if len(guid) != 16:
        raise ValueError('OCP booking GUID must contain 16 bytes')
    transaction_id = int.from_bytes(guid[:4], 'little') & 0x7FFFFFFF
    return {
        'ingameTransactionId': transaction_id or 1,
        'templates': reward.item_id,
        'amounts': str(reward.amount),
        'rarities': reward.rarity,
        'addItems': True,
        'playerCanRejectItem': False,
    }


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


def daily_login_offers(database_path: str | Path, class_name: str):
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
    return SpecialOffer(None, None, None, False, '0000-00-00 00:00:00',
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
)

LOCAL_OCP_METHODS = (
    OCPMethod("external", "Local development payment provider", 0.0, 0.0),
)
