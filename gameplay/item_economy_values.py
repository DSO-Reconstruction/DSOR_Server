"""Database-authored per-instance values carried by ``Game::ItemInfo``.

The client does not calculate these values when an item is received.  In the
generated structure ``field_30`` is the complete VC value of the stack and
``field_34`` is the Glyphs-of-Power value of one equipment instance.  A zero
value makes the corresponding blacksmith melt action unavailable.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from dso_server.protocol.inventory_codec import ItemInfo


GEM_MELT_CATEGORIES = frozenset(("EquipmentGem", "EquipmentJewel"))
RUNE_MELT_CATEGORIES = frozenset(("EquipmentRune",))
_MAX_ITEM_VC_VALUE = 0xFFFF_FFFF


def item_stack_vc_value(item: ItemInfo, item_row: Mapping[str, Any]) -> int:
    """Return the stack VC value representable by ``Game::ItemInfo``."""

    return min(
        _MAX_ITEM_VC_VALUE,
        max(0, int(item_row.get("PriceVC") or 0)) * max(1, int(item.amount)),
    )


def is_meltable_equipment(item_row: Mapping[str, Any]) -> bool:
    """Whether a template is ordinary equipment rather than a socket stone."""

    category = str(item_row.get("ItemCategory") or "")
    if category in GEM_MELT_CATEGORIES or category in RUNE_MELT_CATEGORIES:
        return False
    slots = tuple(
        token.strip()
        for token in str(item_row.get("SlotType") or "")
        .replace(";", ",").split(",")
        if token.strip()
    )
    return bool(slots) and slots != ("ConsumableSlot",)


def _enchantment_range(
    definition: Mapping[str, Any] | Any,
) -> tuple[float, float]:
    if isinstance(definition, Mapping):
        minimum = definition.get("MinVCValue", 0)
        maximum = definition.get("MaxVCValue", minimum)
    else:
        minimum = getattr(definition, "min_vc_value", 0)
        maximum = getattr(definition, "max_vc_value", minimum)
    low = max(0.0, float(minimum or 0.0))
    high = max(low, float(maximum or low))
    return low, high


def equipment_melt_value(
    item: ItemInfo,
    item_row: Mapping[str, Any],
    enchantments_by_id: Mapping[str, Mapping[str, Any] | Any],
) -> int:
    """Calculate one equipment instance's Glyphs-of-Power melt yield.

    Retail stores a normalized roll in every ``ItemEnchantmentInfo`` and its
    VC range in ``_Template_Enchantment``.  Scaling their interpolated values
    by the item's progression level reproduces the per-instance behaviour:
    better rolls on otherwise identical equipment melt for more glyphs.
    The fallback is used only for authored/start equipment without random
    enchantments and deliberately remains level/rarity/value dependent.
    """

    if not is_meltable_equipment(item_row):
        return 0
    contribution = 0.0
    for enchantment in item.enchantments:
        definition = enchantments_by_id.get(enchantment.template_id)
        if definition is None:
            continue
        low, high = _enchantment_range(definition)
        roll = max(0.0, min(1.0, float(enchantment.value)))
        contribution += low + (high - low) * roll
    level = max(1, int(item.item_level))
    if contribution > 0.0:
        # The level-15 origin is the progression baseline used by this client
        # content revision.  At level 100 this resolves the captured rare
        # belt to ~21.3k glyphs (retail: 21,291), while retaining its actual
        # random-roll variance instead of assigning a rarity constant.
        progression = max(1.0, (level - 15.0) / 5.0)
        return max(1, round(contribution * progression))

    rarity = max(1, int(item.field_08))
    unit_vc = max(1, int(item_row.get("PriceVC") or 1))
    progression = max(1.0, (level - 15.0) / 5.0)
    return max(1, round(progression * rarity * (300.0 + unit_vc)))
