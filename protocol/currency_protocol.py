"""Named currency state and codecs for the retail game-currency wallet.

Drakensang has two currency representations:

* ``CurrencyChangedCommand`` (0x0080) refreshes the fixed legacy balances
  embedded directly in ``NewPlayer`` (Andermant, gold, Badge of Honor,
  Draken, Glyphs of Power, and one non-wallet progression field).
* ``GameCurrencyChangedCommand`` (0x0136) updates one entry in the keyed
  ``NewPlayer + 0x1b0`` ``string -> uint64`` wallet.

The wire IDs below were recovered from the 2026-09-09 retail capture by
matching the distinctive balances supplied with that session.  They are
protocol resource IDs, not guesses based on their often misleading names.
"""

from __future__ import annotations

from dso_server.protocol.command_ids import command_id
from collections.abc import Mapping
from dataclasses import dataclass

from dso_server.protocol.character_selection import BitReader, BitWriter


GAME_CURRENCY_CHANGED_COMMAND = command_id("Commands::GameCurrencyChangedCommand", 0x0136)
DEFAULT_BADGE_OF_HONOR_BALANCE = 8_883_102
DEFAULT_DRAKEN_BALANCE = 30_424
DEFAULT_GLYPH_OF_POWER_BALANCE = 710_712_427


@dataclass(frozen=True)
class GameCurrencyDefinition:
    name: str
    wire_id: str
    default_balance: int


GAME_CURRENCY_DEFINITIONS = (
    GameCurrencyDefinition(
        "realm_fragment", "e_item_teleport_change_01", 17_513
    ),
    GameCurrencyDefinition("gilded_clover", "gilded_clover", 2_880),
    GameCurrencyDefinition(
        "materi_fragment", "materi_fragment_01", 15_931
    ),
    GameCurrencyDefinition(
        "crystal_of_truth", "e_anniversary_cot", 6_140
    ),
    GameCurrencyDefinition(
        "altered_crescerite", "currency_stickerbook_upgrade", 1_774
    ),
    GameCurrencyDefinition(
        "community_coin", "community_coins", 1_079
    ),
    GameCurrencyDefinition("solar_coin", "e_sum_coins", 2_107),
    GameCurrencyDefinition(
        "bone_coin", "currency_bone_coins_01", 343_014
    ),
    GameCurrencyDefinition("frozen_coins", "e_chr_frost_coins", 845),
    GameCurrencyDefinition("kitchen_coin", "money_cc", 163),
    GameCurrencyDefinition(
        "victory_medals", "e_endlesstower_progress_item_01", 2_175
    ),
    GameCurrencyDefinition(
        "dragon_blood_essence", "dragon_blood_essence", 1
    ),
    GameCurrencyDefinition("season_coin", "season_coin", 2_965),
    GameCurrencyDefinition(
        "lantern_coin", "cny_lantern_coin", 1_050
    ),
)

GAME_CURRENCY_BY_NAME = {
    definition.name: definition for definition in GAME_CURRENCY_DEFINITIONS
}
GAME_CURRENCY_BY_WIRE_ID = {
    definition.wire_id: definition
    for definition in GAME_CURRENCY_DEFINITIONS
}
DEFAULT_GAME_CURRENCY_BALANCES = {
    definition.wire_id: definition.default_balance
    for definition in GAME_CURRENCY_DEFINITIONS
}


@dataclass(frozen=True)
class GameCurrencyChanged:
    resource_id: str
    amount: int


def resolve_game_currency_wire_id(name_or_wire_id: str) -> str:
    """Resolve a stable server name or an original wire resource ID."""

    value = str(name_or_wire_id)
    definition = GAME_CURRENCY_BY_NAME.get(value)
    if definition is not None:
        return definition.wire_id
    if value in GAME_CURRENCY_BY_WIRE_ID:
        return value
    raise KeyError(f"unknown game currency {value!r}")


def named_game_currency_balances(
    balances: Mapping[str, int],
) -> dict[str, int]:
    """Expose a wire-keyed wallet through stable descriptive server names."""

    return {
        definition.name: int(balances.get(definition.wire_id, 0))
        for definition in GAME_CURRENCY_DEFINITIONS
    }


def normalize_game_currency_balances(
    balances: Mapping[str, int] | None = None,
    *,
    include_defaults: bool = True,
) -> dict[str, int]:
    """Return a validated, canonically ordered, wire-keyed wallet.

    Both descriptive names and retail wire IDs are accepted.  Unknown wire
    IDs are preserved after the known entries, allowing later client builds
    to round-trip currencies which this version does not display.
    """

    result = (
        dict(DEFAULT_GAME_CURRENCY_BALANCES) if include_defaults else {}
    )
    extras: dict[str, int] = {}
    for raw_key, raw_amount in (balances or {}).items():
        key = str(raw_key)
        definition = (
            GAME_CURRENCY_BY_NAME.get(key)
            or GAME_CURRENCY_BY_WIRE_ID.get(key)
        )
        wire_id = definition.wire_id if definition is not None else key
        amount = int(raw_amount)
        if not 0 <= amount <= 0xFFFFFFFFFFFFFFFF:
            raise ValueError(
                f"game currency {wire_id!r} does not fit uint64"
            )
        if definition is None:
            extras[wire_id] = amount
        else:
            result[wire_id] = amount
    return {
        **{
            definition.wire_id: result[definition.wire_id]
            for definition in GAME_CURRENCY_DEFINITIONS
            if definition.wire_id in result
        },
        **{key: extras[key] for key in sorted(extras)},
    }


def update_game_currency(
    balances: Mapping[str, int],
    name_or_wire_id: str,
    amount: int,
) -> tuple[dict[str, int], GameCurrencyChanged]:
    """Set one balance and build the matching authoritative client update."""

    wire_id = resolve_game_currency_wire_id(name_or_wire_id)
    normalized = normalize_game_currency_balances(balances)
    value = int(amount)
    if not 0 <= value <= 0xFFFFFFFFFFFFFFFF:
        raise ValueError("game currency amount does not fit uint64")
    normalized[wire_id] = value
    return normalized, GameCurrencyChanged(wire_id, value)


def read_game_currency_changed_body(
    reader: BitReader,
) -> GameCurrencyChanged:
    return GameCurrencyChanged(reader.read_string(), reader.read_uint64_le())


def write_game_currency_changed_body(
    writer: BitWriter, value: GameCurrencyChanged
) -> None:
    resource_id = resolve_game_currency_wire_id(value.resource_id)
    amount = int(value.amount)
    if not 0 <= amount <= 0xFFFFFFFFFFFFFFFF:
        raise ValueError("game currency amount does not fit uint64")
    writer.write_string(resource_id)
    writer.write_uint64_le(amount)
