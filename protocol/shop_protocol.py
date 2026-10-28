"""Typed protocol and database loader for ordinary NPC shops.

The ordinary NPC commerce flow is separate from the OCP commands used by the
global store.  This module models the generated client serializers for
``RequestOfferCommand`` (0x0059) and ``OfferCommand`` (0x005a).  Shop contents
are resolved from ``_Template_Shop``/``_Template_Offer`` in ``static.db4``;
no captured application payload is replayed.
"""

from __future__ import annotations

from dso_server.runtime import resident_content
from dso_server.protocol.command_ids import command_id
from dataclasses import dataclass, replace
from pathlib import Path
import re
import sqlite3

from dso_server.protocol.character_selection import BitReader, BitWriter, CLASS_NAMES
from dso_server.protocol.inventory_codec import ItemInfo, encode_item_info, decode_item_info


REQUEST_OFFER_COMMAND = command_id("Commands::RequestOfferCommand", 0x0059)
OFFER_COMMAND = command_id("Commands::OfferCommand", 0x005A)
TIMED_OFFERS_INFO_COMMAND = command_id("Commands::TimedOffersInfoCommand", 0x0082)
# Values are IngamePaymentData::StringToOfferCategory results recovered from
# this exact client executable.  Unknown strings fall back to Misc (46) in the
# client; ``Key`` is present in static.db4 but not in the converter's named
# cases, so it is recorded with that same value explicitly.
OFFER_CATEGORY_IDS = {
    "Potion": 0,
    "Elixir": 1,
    "Tonic": 2,
    "BuffItem": 3,
    "ItemGem": 4,
    "OneHand": 5,
    "TwoHand": 6,
    "Armor": 7,
    "Shield": 8,
    "Jewelry": 9,
    "Glyph": 10,
    "Essence": 11,
    "Color": 12,
    "Mount": 13,
    "Pakete": 14,
    "Pet": 15,
    "Event": 16,
    "GamblerBarter": 17,
    "Privileged": 18,
    "PrivilegedDaily": 19,
    "Cloak": 20,
    "OneUseEmote": 21,
    "Emote": 22,
    "TempCostume": 23,
    "Costume": 24,
    "RCSurpriseBoxes": 25,
    "ECSurpriseBoxes": 26,
    "ECOffers": 27,
    "LimitedOffers": 28,
    "TransferItem": 29,
    "ItemGemCe55": 30,
    "RecipeVC": 31,
    "RecipeRC": 32,
    "RecipeDraken": 33,
    "RecipeMateriFragment": 34,
    "RecipeHonor": 35,
    "IngredientVC": 36,
    "IngredientRC": 37,
    "IngredientDraken": 38,
    "IngredientMateriFragment": 39,
    "IngredientHonor": 40,
    "TravelItem": 41,
    "TravelItemEvent": 42,
    "TravelItemParallelWorld": 43,
    "SkinConsumable": 44,
    "SeasonPassItem": 45,
    "Misc": 46,
    "Key": 46,
    "QA": 47,
}

# Game::ItemRarity::FromString values recovered from this client executable.
# Offer rows also use "None" to mean that no rarity presentation is requested;
# the retail jeweler response confirms that it is encoded as zero.
OFFER_RARITY_IDS = {
    "None": 0,
    "Common": 0,
    "Uncommon": 1,
    "Magic": 2,
    "Rare": 3,
    "Epic": 4,
    "Unique": 5,
    "Set": 6,
    "Mythic": 7,
}


@dataclass(frozen=True)
class RequestOffer:
    field_20: bool
    npc_actor_id: int
    offer_id: str


@dataclass(frozen=True)
class OfferStringFloat:
    resource_id: str
    value: float


@dataclass(frozen=True)
class ShopOffer:
    resource_id: str
    price_rc: int
    price_vc: int
    price_gc: int
    price_ec: int
    price_uc: int
    price_arena: int
    price_ic: int
    amount: int
    difficulty: int
    timed_offer_amount: int
    min_player_level: int
    max_player_level: int
    is_recommended: bool
    dynamic_price: bool
    is_new: bool
    auto_identify: bool
    once_per_character: bool
    scale_to_player_level: bool
    hide_not_suitable: bool
    event_timed: bool
    directly_buy_offer: bool
    rarity: int
    category: int
    icon_brush: str
    icon_configuration: str
    item_id: str
    discount_events: tuple[OfferStringFloat, ...] = ()
    privilege_discounts: tuple[OfferStringFloat, ...] = ()
    disabled_events: tuple[str, ...] = ()
    completed_quest_requirements: tuple[str, ...] = ()
    conversion_stories: tuple[OfferStringFloat, ...] = ()
    veteran_discounts: tuple[OfferStringFloat, ...] = ()
    timed_offer: bool = False
    # Server-side purchase metadata; these values are not part of the wire
    # offer, but let special jeweler offers grant a fully socketed item.
    unlocked_socket_count: int = 0
    item_level_override: int = 0
    enchantment_roll_quality_override: int = -1
    base_enchantment_tier_override: int = -1


@dataclass(frozen=True)
class PaymentItem:
    resource_id: str
    payment_item_id: int


@dataclass(frozen=True)
class OfferCommand:
    shop_id: str
    field_60: bool
    player_can_sell: bool
    offers: tuple[ShopOffer | None, ...]
    payment_items: tuple[PaymentItem | None, ...]
    categories: tuple[int, ...]
    sold_items: tuple[ItemInfo, ...] = ()


def decode_request_offer(
    payload: bytes, bit_length: int | None = None
) -> RequestOffer:
    """Decode the generated RequestOfferCommand body.

    NPC requests use an empty offer id (73 meaningful bits), while the global
    premium store carries ``global_rc_trader`` in the same string field.
    """

    if bit_length is None:
        bit_length = len(payload) * 8
    reader = BitReader(payload, bit_length)
    if reader.read_uint8() != 0x8B:
        raise ValueError("RequestOffer is not a client command")
    command_id = int.from_bytes(reader.read_bytes(2), "little")
    if command_id != REQUEST_OFFER_COMMAND:
        raise ValueError(
            f"expected RequestOffer 0x0059, got 0x{command_id:04x}"
        )
    result = RequestOffer(
        reader.read_bool(), reader.read_uint32_le(), reader.read_string()
    )
    if reader.remaining >= 8:
        raise ValueError(f"RequestOffer has {reader.remaining} unread bits")
    if reader.remaining and reader.read_bits(reader.remaining):
        raise ValueError("RequestOffer has non-zero padding")
    return result


def _write_string_floats(
    writer: BitWriter, values: tuple[OfferStringFloat, ...]
) -> None:
    writer.write_uint32_le(len(values))
    for value in values:
        writer.write_string(value.resource_id)
        writer.write_float(value.value)


def _read_string_floats(reader: BitReader) -> tuple[OfferStringFloat, ...]:
    return tuple(
        OfferStringFloat(reader.read_string(), reader.read_float())
        for _ in range(reader.read_uint32_le())
    )


def _write_strings(writer: BitWriter, values: tuple[str, ...]) -> None:
    writer.write_uint32_le(len(values))
    for value in values:
        writer.write_string(value)


def _read_strings(reader: BitReader) -> tuple[str, ...]:
    return tuple(reader.read_string() for _ in range(reader.read_uint32_le()))


def _write_shop_offer(writer: BitWriter, value: ShopOffer | None) -> None:
    writer.write_bool(value is not None)
    if value is None:
        return
    writer.write_string(value.resource_id)
    for number in (
        value.price_rc,
        value.price_vc,
        # Native Offer +0x60 is Honor; +0x68 is Draken. The database
        # columns are not in wire order (0x140a04e14 / 0x140517d94).
        value.price_arena,
        value.price_gc,
        value.price_ec,
        value.price_uc,
        value.price_ic,
        value.amount,
        value.difficulty,
        value.timed_offer_amount,
        value.min_player_level,
        value.max_player_level,
    ):
        writer.write_uint32_le(number)
    for flag in (
        value.is_recommended,
        value.dynamic_price,
        value.is_new,
        value.auto_identify,
        value.once_per_character,
        value.scale_to_player_level,
        value.hide_not_suitable,
        value.event_timed,
        # release261 reads DirectlyBuyOffer as the ninth flag before Rarity.
        value.directly_buy_offer,
    ):
        writer.write_bool(flag)
    writer.write_uint8(value.rarity)
    writer.write_bytes(int(value.category).to_bytes(2, "little"))
    writer.write_string(value.icon_brush)
    writer.write_string(value.icon_configuration)
    writer.write_string(value.item_id)
    _write_string_floats(writer, value.discount_events)
    _write_string_floats(writer, value.privilege_discounts)
    _write_strings(writer, value.disabled_events)
    _write_strings(writer, value.completed_quest_requirements)
    _write_string_floats(writer, value.conversion_stories)
    _write_string_floats(writer, value.veteran_discounts)
    writer.write_bool(value.timed_offer)


def _read_shop_offer(reader: BitReader) -> ShopOffer | None:
    if not reader.read_bool():
        return None
    resource_id = reader.read_string()
    wire = tuple(reader.read_uint32_le() for _ in range(12))
    numbers = (wire[0], wire[1], wire[3], wire[4], wire[5], wire[2], *wire[6:])
    flags = tuple(reader.read_bool() for _ in range(9))
    rarity = reader.read_uint8()
    category = int.from_bytes(reader.read_bytes(2), "little")
    strings = tuple(reader.read_string() for _ in range(3))
    discount_events = _read_string_floats(reader)
    privilege_discounts = _read_string_floats(reader)
    disabled_events = _read_strings(reader)
    completed_quest_requirements = _read_strings(reader)
    conversion_stories = _read_string_floats(reader)
    veteran_discounts = _read_string_floats(reader)
    timed_offer = reader.read_bool()
    return ShopOffer(
        resource_id,
        *numbers,
        *flags,
        rarity,
        category,
        *strings,
        discount_events,
        privilege_discounts,
        disabled_events,
        completed_quest_requirements,
        conversion_stories,
        veteran_discounts,
        timed_offer,
    )


def write_offer_command_body(writer: BitWriter, value: OfferCommand) -> None:
    writer.write_string(value.shop_id)
    writer.write_bool(value.field_60)
    writer.write_bool(value.player_can_sell)
    writer.write_uint32_le(len(value.offers))
    for offer in value.offers:
        _write_shop_offer(writer, offer)
    writer.write_uint32_le(len(value.payment_items))
    for item in value.payment_items:
        writer.write_bool(item is not None)
        if item is not None:
            writer.write_string(item.resource_id)
            writer.write_uint32_le(item.payment_item_id)
    writer.write_uint32_le(len(value.categories))
    for category in value.categories:
        writer.write_bytes(int(category).to_bytes(2, "little"))
    # ClientTradingManager::HandleOfferCommand (0x14027b128) rebuilds its
    # repurchase list from +0xc8, including when field_60 is true (refresh).
    writer.write_uint32_le(len(value.sold_items))
    for item in value.sold_items:
        encode_item_info(writer, item)


def read_offer_command_body(reader: BitReader) -> OfferCommand:
    shop_id = reader.read_string()
    field_60 = reader.read_bool()
    player_can_sell = reader.read_bool()
    offers = tuple(
        _read_shop_offer(reader) for _ in range(reader.read_uint32_le())
    )
    payment_items = []
    for _ in range(reader.read_uint32_le()):
        payment_items.append(
            PaymentItem(reader.read_string(), reader.read_uint32_le())
            if reader.read_bool()
            else None
        )
    categories = tuple(
        int.from_bytes(reader.read_bytes(2), "little")
        for _ in range(reader.read_uint32_le())
    )
    sold_items = tuple(
        decode_item_info(reader) for _ in range(reader.read_uint32_le())
    )
    return OfferCommand(
        shop_id,
        field_60,
        player_can_sell,
        offers,
        tuple(payment_items),
        categories,
        sold_items,
    )


def encode_offer_command(
    value: OfferCommand, *, actor_id: int = 0
) -> tuple[bytes, int]:
    writer = BitWriter()
    writer.write_uint8(0x84)
    writer.write_bytes(OFFER_COMMAND.to_bytes(2, "little"))
    write_offer_command_body(writer, value)
    writer.write_uint32_le(actor_id)
    return writer.finish()


def decode_offer_command(
    payload: bytes, bit_length: int
) -> tuple[OfferCommand, int]:
    reader = BitReader(payload, bit_length)
    if reader.read_uint8() != 0x84:
        raise ValueError("OfferCommand is not a single server command")
    command_id = int.from_bytes(reader.read_bytes(2), "little")
    if command_id != OFFER_COMMAND:
        raise ValueError(f"expected OfferCommand, got 0x{command_id:04x}")
    value = read_offer_command_body(reader)
    actor_id = reader.read_uint32_le()
    if reader.remaining:
        raise ValueError(f"OfferCommand has {reader.remaining} unread bits")
    return value, actor_id


def encode_offer_response_batch(
    value: OfferCommand,
    *,
    timed_offers_actor_id: int,
    offer_actor_id: int = 0,
) -> tuple[bytes, int]:
    """Encode the retail 0x0082 -> 0x005a response ordering."""

    writer = BitWriter()
    writer.write_uint8(0x85)
    writer.write_bytes(TIMED_OFFERS_INFO_COMMAND.to_bytes(2, "little"))
    writer.write_uint64_le(0)  # server time; zero in the captured response
    writer.write_uint32_le(0)  # no active timed offers
    writer.write_uint32_le(timed_offers_actor_id)
    writer.write_uint8(0xFF)
    writer.write_bytes(OFFER_COMMAND.to_bytes(2, "little"))
    write_offer_command_body(writer, value)
    writer.write_uint32_le(offer_actor_id)
    writer.write_uint8(0xFF)
    return writer.finish()


def _split_strings(value: object, separator: str = ";") -> tuple[str, ...]:
    return tuple(
        part.strip()
        for part in str(value or "").split(separator)
        if part.strip()
    )


def _parse_string_floats(value: object) -> tuple[OfferStringFloat, ...]:
    result = []
    for part in re.split(r"[;|]", str(value or "")):
        part = part.strip()
        if not part:
            continue
        resource_id, separator, number = part.partition(":")
        if not separator:
            raise ValueError(f"invalid shop modifier {part!r}")
        result.append(OfferStringFloat(resource_id.strip(), float(number)))
    return tuple(result)


def _open_read_only(path: Path) -> sqlite3.Connection:
    if not resident_content.is_file(path):
        raise FileNotFoundError(f"static database not found: {path}")
    database = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    database.row_factory = sqlite3.Row
    return database


def _max_tier_trinket_gems(
    database: sqlite3.Connection, class_name: str
) -> tuple[sqlite3.Row, ...]:
    """Return the strongest authored trinket jewels and runes for a class."""

    rows = database.execute(
        "SELECT * FROM _Template_Item "
        "WHERE ItemCategory IN ('EquipmentJewel', 'EquipmentRune') "
        "AND GemSlotType IN ('JewelTrinket', 'RuneTrinket') "
        "AND Obsolete=0 ORDER BY Id"
    ).fetchall()
    candidates = []
    for row in rows:
        allowed_classes = {
            value.strip().casefold()
            for value in str(row["CharClass"] or "").replace(";", ",").split(",")
            if value.strip()
        }
        if allowed_classes and class_name not in allowed_classes:
            continue
        if not any(
            row[name]
            for name in (
                "BaseEnchantments", "Enchantments", "UniqueModifier",
                "StatusEffectId", "StatusEffectValue0", "StatusEffectValue1",
                "StatusEffectValue2", "StatusEffectValue3", "StatusEffectValue4",
            )
        ):
            continue
        candidates.append(row)

    rarity_tiers = {
        "common": 0,
        "uncommon": 1,
        "magic": 2,
        "rare": 3,
        "epic": 4,
    }
    strongest: dict[tuple[str, str], tuple[tuple[int, int], sqlite3.Row]] = {}
    for row in candidates:
        template_id = str(row["Id"])
        suffix = template_id.rsplit("_", 1)
        if len(suffix) == 2 and suffix[1].casefold() in rarity_tiers:
            key = (str(row["ItemCategory"]), suffix[0])
            tier = (1, rarity_tiers[suffix[1].casefold()])
        elif len(suffix) == 2 and re.fullmatch(r"\d{2}", suffix[1]):
            key = (str(row["ItemCategory"]), suffix[0])
            tier = (2, int(suffix[1]))
        else:
            # Non-tiered seasonal and special templates have no stronger
            # variant, so retain them as their own max-tier offer.
            key = (str(row["ItemCategory"]), template_id)
            tier = (0, 0)
        current = strongest.get(key)
        if current is None or tier > current[0]:
            strongest[key] = (tier, row)
    return tuple(
        row for _, row in sorted(
            strongest.values(), key=lambda entry: str(entry[1]["Id"])
        )
    )


def _socket_limit_at_level(schedule: object, level: int) -> int:
    text = str(schedule or "0")
    if ":" not in text:
        return max(0, int(text))
    result = 0
    for entry in text.split(","):
        if not entry.strip():
            continue
        threshold, count = (int(value) for value in entry.split(":", 1))
        if level >= threshold:
            result = count
    return max(0, min(255, result))


def _maximum_socket_setup(schedule: object, max_item_level: int) -> tuple[int, int]:
    """Return the allowed item level and socket count at its highest tier."""

    text = str(schedule or "0")
    if ":" not in text:
        return 1, max(0, min(255, int(text)))
    thresholds = [
        int(entry.split(":", 1)[0])
        for entry in text.split(",")
        if entry.strip()
    ]
    if not thresholds:
        return 1, 0
    level = min(max(1, max_item_level), max(thresholds))
    return level, _socket_limit_at_level(text, level)


def load_shop_offers(
    static_database_path: str | Path,
    shop_id: str,
    *,
    player_level: int,
    class_id: int,
    active_event_ids: tuple[str, ...] | frozenset[str] = (),
) -> OfferCommand:
    """Build the visible offers for one NPC shop from the shared database."""

    database = _open_read_only(Path(static_database_path))
    try:
        shop = database.execute(
            "SELECT * FROM _Template_Shop WHERE Id=?", (shop_id,)
        ).fetchone()
        if shop is None:
            raise ValueError(f"unknown NPC shop {shop_id!r}")
        from dso_server.persistence.npc_shop_overrides import NpcShopOverrideStore
        cms_overrides = NpcShopOverrideStore(static_database_path).overrides(shop_id)
        class_name = CLASS_NAMES.get(int(class_id), "").casefold()
        active_events = {
            str(value).strip().casefold()
            for value in active_event_ids
            if str(value).strip()
        }
        offers: list[ShopOffer] = []
        categories: list[int] = []
        for offer_id in _split_strings(shop["Offers"]):
            row = database.execute(
                "SELECT * FROM _Template_Offer WHERE Id=?", (offer_id,)
            ).fetchone()
            if row is None:
                continue
            offer_events = {
                value.casefold()
                for value in _split_strings(row["EventId"])
            }
            disabled_events = {
                value.casefold()
                for value in _split_strings(row["DisabledForEvents"])
            }
            item_row = None
            item_id = str(row["Item"] or "")
            if item_id:
                item_row = database.execute(
                    "SELECT ActiveEvents, EventId, OnlyActiveDuringEvent "
                    "FROM _Template_Item WHERE Id=? LIMIT 1",
                    (item_id,),
                ).fetchone()
            item_events = set()
            item_event_only = False
            if item_row is not None:
                item_events.update(
                    value.casefold()
                    for value in _split_strings(item_row["ActiveEvents"])
                )
                item_events.update(
                    value.casefold()
                    for value in _split_strings(item_row["EventId"])
                )
                item_event_only = bool(item_row["OnlyActiveDuringEvent"])
            required_events = offer_events | item_events
            if required_events and not required_events.intersection(
                active_events
            ):
                continue
            if item_event_only and not active_events:
                continue
            if disabled_events.intersection(active_events):
                continue
            if not (
                int(row["MinPlayerLevel"])
                <= int(player_level)
                <= int(row["MaxPlayerLevel"])
            ):
                continue
            allowed_classes = {
                name.casefold() for name in _split_strings(row["CharClass"])
            }
            if allowed_classes and class_name not in allowed_classes:
                continue
            try:
                category = OFFER_CATEGORY_IDS[str(row["OfferCategory"])]
                rarity = OFFER_RARITY_IDS[str(row["Rarity"])]
            except KeyError as exc:
                raise ValueError(
                    f"shop {shop_id!r} uses an unrecovered enum {exc.args[0]!r}"
                ) from exc
            price_override = cms_overrides["prices"].get(str(row["Id"]), {})
            offers.append(
                ShopOffer(
                    resource_id=str(row["Id"]),
                    price_rc=int(price_override.get("price_rc", row["PriceRC"])),
                    price_vc=int(price_override.get("price_vc", row["PriceVC"])),
                    price_gc=int(price_override.get("price_gc", row["PriceGC"])),
                    price_ec=int(price_override.get("price_ec", row["PriceEC"])),
                    price_uc=int(price_override.get("price_uc", row["PriceUC"])),
                    price_arena=int(price_override.get("price_arena", row["PriceArena"])),
                    price_ic=int(price_override.get("price_ic", row["PriceIC"])),
                    amount=int(row["Amount"]),
                    difficulty=int(row["Difficulty"]),
                    timed_offer_amount=int(row["TimedOfferAmount"]),
                    min_player_level=int(row["MinPlayerLevel"]),
                    max_player_level=int(row["MaxPlayerLevel"]),
                    is_recommended=bool(row["IsRecommended"]),
                    dynamic_price=bool(row["DynamicPrice"]),
                    is_new=bool(row["IsNew"]),
                    auto_identify=bool(row["AutoIdentify"]),
                    once_per_character=bool(row["OncePerCharacter"]),
                    scale_to_player_level=bool(row["ScaleToPlayerLevel"]),
                    hide_not_suitable=bool(row["HideNotSuitable"]),
                    event_timed=bool(row["EventTimed"]),
                    directly_buy_offer=bool(row["DirectlyBuyOffer"]),
                    rarity=rarity,
                    category=category,
                    icon_brush=str(row["IconBrush"] or ""),
                    icon_configuration=str(row["IconConfiguration"] or ""),
                    item_id=item_id,
                    discount_events=_parse_string_floats(row["DiscountEvent"]),
                    privilege_discounts=_parse_string_floats(
                        row["PrivilegeDiscount"]
                    ),
                    disabled_events=_split_strings(row["DisabledForEvents"]),
                    completed_quest_requirements=_split_strings(
                        row["CompletedQuestsRequirements"]
                    ),
                    conversion_stories=_parse_string_floats(
                        row["ConversionStory"]
                    ),
                    veteran_discounts=_parse_string_floats(
                        row["VeteranDiscount"]
                    ),
                    timed_offer=bool(row["TimedOffer"]),
                )
            )
            if category not in categories:
                categories.append(category)
        for category in cms_overrides["categories"]:
            category_id = int(category["category_id"])
            if category_id not in categories:
                categories.append(category_id)
        cms_offers = []
        # The game client resolves shop offers by their resource id against
        # its static offer templates. CMS-created ids do not exist there, so
        # use an otherwise-unused static offer id for the same item on the
        # wire. Keep the CMS id in the override database for administration.
        # The purchase handler keys off the ShopOffer sent here, so purchasing
        # the aliased id still uses this CMS row's item and prices.
        used_offer_ids = set(_split_strings(shop["Offers"]))
        # If the same item was added repeatedly while troubleshooting, give
        # the single client-known static offer id to the newest CMS row.
        # Older duplicate rows retain their CMS ids and cannot steal the
        # visible slot from the latest addition.
        custom_rows = sorted(
            cms_overrides["offers"],
            key=lambda row: float(row.get("created_at", 0)),
            reverse=True,
        )
        for row in custom_rows:
            category_id = int(row["category_id"])
            if category_id not in categories:
                categories.append(category_id)
            item = database.execute(
                "SELECT DefaultRarity,CharClass FROM _Template_Item WHERE Id=?",
                (row["item_id"],),
            ).fetchone()
            if item is None:
                continue
            allowed_classes = {
                name.casefold()
                for name in _split_strings(item["CharClass"], ",")
            }
            if allowed_classes and class_name not in allowed_classes:
                continue
            rarity_name = str(item["DefaultRarity"] or "Common")
            try:
                rarity = OFFER_RARITY_IDS[rarity_name]
            except KeyError as exc:
                raise ValueError(
                    f"item {row['item_id']!r} uses unknown rarity {rarity_name!r}"
                ) from exc
            candidates = database.execute(
                "SELECT Id,OfferCategory FROM _Template_Offer WHERE Item=? "
                "ORDER BY Id",
                (row["item_id"],),
            ).fetchall()
            category_candidates = [
                str(candidate["Id"])
                for candidate in candidates
                if OFFER_CATEGORY_IDS.get(str(candidate["OfferCategory"]), 46)
                == category_id
            ]
            other_candidates = [
                str(candidate["Id"])
                for candidate in candidates
                if str(candidate["Id"]) not in category_candidates
            ]
            resource_id = next(
                (
                    candidate_id
                    for candidate_id in category_candidates
                    if candidate_id not in used_offer_ids
                ),
                None,
            )
            if resource_id is None and not category_candidates:
                resource_id = next(
                    (
                        candidate_id
                        for candidate_id in other_candidates
                        if candidate_id not in used_offer_ids
                    ),
                    str(row["offer_id"]),
                )
            elif resource_id is None:
                resource_id = str(row["offer_id"])
            used_offer_ids.add(resource_id)
            cms_offers.append(
                ShopOffer(
                    resource_id=resource_id,
                    price_rc=int(row["price_rc"]),
                    price_vc=int(row["price_vc"]),
                    price_gc=int(row["price_gc"]),
                    price_ec=int(row["price_ec"]),
                    price_uc=int(row["price_uc"]),
                    price_arena=int(row["price_arena"]),
                    price_ic=int(row["price_ic"]),
                    amount=int(row["amount"]),
                    difficulty=0,
                    timed_offer_amount=0,
                    min_player_level=1,
                    max_player_level=200,
                    is_recommended=False,
                    dynamic_price=False,
                    is_new=False,
                    auto_identify=False,
                    once_per_character=False,
                    scale_to_player_level=False,
                    hide_not_suitable=False,
                    event_timed=False,
                    directly_buy_offer=False,
                    rarity=rarity,
                    category=category_id,
                    icon_brush="",
                    icon_configuration="",
                    item_id=str(row["item_id"]),
                )
            )
        # Place CMS additions before static offers. The client preserves offer
        # order within a category, and long NPC inventories otherwise bury new
        # items at the very end of the shop list.
        offers = cms_offers + offers
        if str(shop["Id"]).casefold().startswith("smith_hub"):
            # The local smith is the class-filtered source for the current
            # Mythic equipment, both mythic Dragan set versions, plus unique
            # belts and weapon modifiers. Items are generated at level 145
            # with their strongest supported socket layout, 100% enchantment
            # rolls, and maximum base values; no static.db4 rows are rewritten.
            equipment_rows = database.execute(
                "SELECT Id,DefaultRarity,CharClass,SlotType,ItemCategory,"
                "MaxSocketCount,IconBrush,IconConfiguration "
                "FROM _Template_Item WHERE MinDropLevel<=145 "
                "AND MaxDropLevel>=145 AND ("
                "(DefaultRarity='Mythic' AND lower(CharClass)=? "
                "AND ItemCategory IN ('Armor','Weapon','SmallWeapon',"
                "'Jewelry','OffenseBackitem','DefenseBackitem')) OR "
                "(DefaultRarity='Set' AND lower(CharClass)=? "
                "AND lower(Id) LIKE '%mythic%dragan%' "
                "AND ItemCategory IN ('Armor','Weapon','SmallWeapon',"
                "'Jewelry','OffenseBackitem','DefenseBackitem')) OR "
                "(DefaultRarity='Unique' AND ("
                "(lower(SlotType) LIKE '%beltslot%' AND "
                "(lower(CharClass)=? OR coalesce(CharClass,'')='')) OR "
                "(ItemCategory='WeaponModifier' AND lower(CharClass)=?)))"
                ") ORDER BY Id",
                (class_name, class_name, class_name, class_name),
            ).fetchall()
            present_items = {offer.item_id for offer in offers}
            for item in equipment_rows:
                template_id = str(item["Id"])
                slot_types = tuple(
                    value.strip()
                    for value in str(item["SlotType"] or "").split(",")
                    if value.strip()
                )
                category_name = {
                    "Armor": "Armor",
                    "Jewelry": "Jewelry",
                    "OffenseBackitem": "Cloak",
                    "DefenseBackitem": "Cloak",
                    "WeaponModifier": "Armor",
                }.get(str(item["ItemCategory"]), "TwoHand"
                    if "LeftHandSlot" in slot_types else "OneHand")
                category = OFFER_CATEGORY_IDS[category_name]
                if category not in categories:
                    categories.append(category)
                socket_count = _socket_limit_at_level(
                    item["MaxSocketCount"], 145
                )
                if template_id in present_items:
                    offers = [
                        replace(
                            offer,
                            unlocked_socket_count=socket_count,
                            item_level_override=145,
                            enchantment_roll_quality_override=100,
                            base_enchantment_tier_override=16,
                        )
                        if offer.item_id == template_id else offer
                        for offer in offers
                    ]
                    continue
                rarity = OFFER_RARITY_IDS[str(item["DefaultRarity"])]
                offers.append(
                    ShopOffer(
                        resource_id=(
                            f"smith_{str(item['DefaultRarity']).lower()}"
                            f"_145_{template_id}"
                        ),
                        price_rc=0,
                        price_vc=1,
                        price_gc=0,
                        price_ec=0,
                        price_uc=0,
                        price_arena=0,
                        price_ic=0,
                        amount=1,
                        difficulty=0,
                        timed_offer_amount=0,
                        min_player_level=1,
                        max_player_level=200,
                        is_recommended=False,
                        dynamic_price=False,
                        is_new=False,
                        auto_identify=False,
                        once_per_character=False,
                        scale_to_player_level=False,
                        hide_not_suitable=False,
                        event_timed=False,
                        directly_buy_offer=False,
                        rarity=rarity,
                        category=category,
                        icon_brush=str(item["IconBrush"] or ""),
                        icon_configuration=str(item["IconConfiguration"] or ""),
                        item_id=template_id,
                        unlocked_socket_count=socket_count,
                        item_level_override=145,
                        enchantment_roll_quality_override=100,
                        base_enchantment_tier_override=16,
                    )
                )
                present_items.add(template_id)
        if str(shop["Id"]) == "jewelry_hub_all":
            for item in _max_tier_trinket_gems(database, class_name):
                template_id = str(item["Id"])
                resource_id = f"jewelry_max_tier_{template_id}"
                price_override = cms_overrides["prices"].get(resource_id, {})
                rarity_name = str(item["DefaultRarity"] or "Common")
                try:
                    rarity = OFFER_RARITY_IDS[rarity_name]
                except KeyError as exc:
                    raise ValueError(
                        f"item {template_id!r} uses unknown rarity {rarity_name!r}"
                    ) from exc
                offers.append(
                    ShopOffer(
                        resource_id=resource_id,
                        price_rc=int(price_override.get("price_rc", 0)),
                        price_vc=int(price_override.get("price_vc", 1)),
                        price_gc=int(price_override.get("price_gc", 0)),
                        price_ec=int(price_override.get("price_ec", 0)),
                        price_uc=int(price_override.get("price_uc", 0)),
                        price_arena=int(price_override.get("price_arena", 0)),
                        price_ic=int(price_override.get("price_ic", 0)),
                        amount=1,
                        difficulty=0,
                        timed_offer_amount=0,
                        min_player_level=1,
                        max_player_level=200,
                        is_recommended=False,
                        dynamic_price=False,
                        is_new=False,
                        auto_identify=False,
                        once_per_character=False,
                        scale_to_player_level=False,
                        hide_not_suitable=False,
                        event_timed=False,
                        directly_buy_offer=False,
                        rarity=rarity,
                        category=OFFER_CATEGORY_IDS["ItemGemCe55"],
                        icon_brush="",
                        icon_configuration="",
                        item_id=template_id,
                    )
                )
                if OFFER_CATEGORY_IDS["ItemGemCe55"] not in categories:
                    categories.append(OFFER_CATEGORY_IDS["ItemGemCe55"])
            trinkets = database.execute(
                "SELECT Id, DefaultRarity, MaxSocketCount, MaxDropLevel "
                "FROM _Template_Item "
                "WHERE ItemCategory IN ('RuneTrinket', 'JewelTrinket') "
                "AND Obsolete=0 ORDER BY Id"
            ).fetchall()
            for item in trinkets:
                template_id = str(item["Id"])
                rarity_name = str(item["DefaultRarity"] or "Common")
                try:
                    rarity = OFFER_RARITY_IDS[rarity_name]
                except KeyError as exc:
                    raise ValueError(
                        f"item {template_id!r} uses unknown rarity {rarity_name!r}"
                    ) from exc
                max_item_level = max(1, int(item["MaxDropLevel"] or 200))
                item_level, socket_count = _maximum_socket_setup(
                    item["MaxSocketCount"], max_item_level
                )
                resource_id = f"jewelry_max_sockets_{template_id}"
                price_override = cms_overrides["prices"].get(resource_id, {})
                offers.append(
                    ShopOffer(
                        resource_id=resource_id,
                        price_rc=int(price_override.get("price_rc", 0)),
                        price_vc=int(price_override.get("price_vc", 1)),
                        price_gc=int(price_override.get("price_gc", 0)),
                        price_ec=int(price_override.get("price_ec", 0)),
                        price_uc=int(price_override.get("price_uc", 0)),
                        price_arena=int(price_override.get("price_arena", 0)),
                        price_ic=int(price_override.get("price_ic", 0)),
                        amount=1,
                        difficulty=0,
                        timed_offer_amount=0,
                        min_player_level=1,
                        max_player_level=200,
                        is_recommended=False,
                        dynamic_price=False,
                        is_new=False,
                        auto_identify=False,
                        once_per_character=False,
                        scale_to_player_level=False,
                        hide_not_suitable=False,
                        event_timed=False,
                        directly_buy_offer=False,
                        rarity=rarity,
                        category=OFFER_CATEGORY_IDS["ItemGemCe55"],
                        icon_brush="",
                        icon_configuration="",
                        item_id=template_id,
                        unlocked_socket_count=socket_count,
                        item_level_override=item_level,
                    )
                )
                if OFFER_CATEGORY_IDS["ItemGemCe55"] not in categories:
                    categories.append(OFFER_CATEGORY_IDS["ItemGemCe55"])
        return OfferCommand(
            str(shop["Id"]),
            False,
            bool(shop["PlayerCanSell"]),
            tuple(offers),
            (),
            tuple(categories),
        )
    finally:
        database.close()
