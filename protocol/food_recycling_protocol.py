"""Current-client food recycling request and authored Kitchen Coin yields.

Release261_1024 dro_client64.exe: SalvageCookItemCommand's vtable is
0x141179ba0. Encoder 0x140985564 calls the u32-array serializer 0x140960df0:
u32 count, then one u32 inventory item ID per selected stack. There is no
amount, NPC ID, or salvage mode. FoodRecyclingWidget exposes nine slots.

Widget validation 0x1405aa998 requires ItemTemplate+0x1e4 > 0. The template
loader (0x1408a144f/0x1408a36d7) binds that member to the PriceCC attribute
at 0x1413bfd40. Preview 0x1405abee4 sums PriceCC * GameItem.amount; this is
the whole selected stack, without a level, rarity, or sell-factor multiplier.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
import sqlite3
from types import MappingProxyType
from typing import Mapping

from dso_server.runtime import resident_content
from dso_server.protocol.character_selection import BitReader
from dso_server.protocol.command_ids import command_id
from dso_server.protocol.currency_protocol import resolve_game_currency_wire_id


SALVAGE_COOK_ITEM_COMMAND = command_id("Commands::SalvageCookItemCommand", 0x0032)
FOOD_RECYCLING_SLOT_COUNT = 9


@dataclass(frozen=True)
class FoodRecyclingRequest:
    item_ids: tuple[int, ...]


def decode_food_recycling_request(payload: bytes) -> FoodRecyclingRequest:
    reader = BitReader(payload)
    if reader.read_uint8() != 0x8B or int.from_bytes(reader.read_bytes(2), "little") != SALVAGE_COOK_ITEM_COMMAND:
        raise ValueError("expected SalvageCookItem client command")
    count = reader.read_uint32_le()
    if not 1 <= count <= FOOD_RECYCLING_SLOT_COUNT:
        raise ValueError("food recycling requires one to nine selected stacks")
    if reader.remaining != count * 32:
        raise ValueError("food recycling item count does not match payload")
    item_ids = tuple(reader.read_uint32_le() for _ in range(count))
    if 0 in item_ids or len(set(item_ids)) != count:
        raise ValueError("food recycling requires unique nonzero item IDs")
    return FoodRecyclingRequest(item_ids)


@dataclass(frozen=True)
class FoodRecyclingCatalog:
    currency_id: str
    unit_yields: Mapping[str, int]


@lru_cache(maxsize=4)
def _load_food_recycling_catalog(path: str) -> FoodRecyclingCatalog:
    with sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True) as database:
        currency = database.execute("SELECT CookCurrency FROM _Globals").fetchone()
        if currency is None or not currency[0]:
            raise ValueError("the client content has no cooking currency")
        currency_id = resolve_game_currency_wire_id(str(currency[0]))
        # Positive PriceCC is the client predicate, not ItemCategory or an ID
        # prefix: the prepared dishes are authored as IngredientItem.
        rows = database.execute("SELECT Id, PriceCC FROM _Template_Item WHERE PriceCC > 0").fetchall()
    return FoodRecyclingCatalog(currency_id, MappingProxyType({str(key): int(value) for key, value in rows}))


def load_food_recycling_catalog(database_path: str | Path) -> FoodRecyclingCatalog:
    return _load_food_recycling_catalog(str(resident_content.resolved(database_path)))
