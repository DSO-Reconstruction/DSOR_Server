"""Client-compatible deterministic rolls for generated item modifiers.

The client uses separate dSFMT-19937 phases while materializing an item.
``BaseModifier`` and ``UniqueModifier`` seed their phases directly from
``Game::ItemInfo.seedIdx``. ``BaseEnchantments`` first converts the seed to a
32-bit float and multiplies it by ``0.5f``. Those rolls are part of the item
instance: using the raw seed (or averaging the database range) makes the
server disagree with the attributes the client derives from the same packet.

Only cache misses and equipment mutations consume this code. Combat hits use
the immutable player-stat cache and never rebuild an item random stream.
"""

from __future__ import annotations

from functools import lru_cache
from itertools import islice
import struct


_UINT32_MASK = (1 << 32) - 1
_UINT64_MASK = (1 << 64) - 1
_LOW_DOUBLE_MASK = 0x000FFFFFFFFFFFFF
_HIGH_DOUBLE_ONE = 0x3FF0000000000000
_MASK_1 = 0x000FFAFFFFFFFB3F
_MASK_2 = 0x000FFDFFFC90FFFD
_FIX_1 = 0x90014964B32F4329
_PARITY_1 = 0x3D84E1AC0DC82880
_STATE_UINT32_COUNT = 768
_OUTPUT_COUNT = 382
# Item materialization consumes a handful of rolls per modifier phase.
_CACHED_ROLL_PREFIX = 64


def _uint64(value: int) -> int:
    return int(value) & _UINT64_MASK


def _initial_state(seed: int) -> list[int]:
    words = [0] * _STATE_UINT32_COUNT
    words[0] = int(seed) & _UINT32_MASK
    for index in range(1, _STATE_UINT32_COUNT):
        previous = words[index - 1]
        words[index] = (
            0x6C078965 * (previous ^ (previous >> 30)) + index
        ) & _UINT32_MASK

    state = [
        words[index] | (words[index + 1] << 32)
        for index in range(0, _STATE_UINT32_COUNT, 2)
    ]
    # dSFMT-19937 owns 382 output uint64 words followed by two raw lung
    # words. The client converts only the output area to [1, 2) doubles;
    # normalizing the lung words corrupts period certification and every
    # subsequent item-quality roll.
    for index in range(_OUTPUT_COUNT):
        value = state[index]
        state[index] = (
            (value & _LOW_DOUBLE_MASK) | _HIGH_DOUBLE_ONE
        )

    certificate = (
        ((state[382] ^ _FIX_1) & _PARITY_1) ^ (state[383] & 1)
    )
    for shift in (32, 16, 8, 4, 2, 1):
        certificate ^= certificate >> shift
    if not certificate & 1:
        state[383] ^= 1
    return state


def _refresh(state: list[int]) -> None:
    value_3 = _uint64(
        _uint64((state[382] << 13) ^ state[1]) << 19
    ) ^ (state[382] >> 32) ^ state[235]
    value_4 = _uint64(
        _uint64((state[383] << 13) ^ state[0]) << 19
    ) ^ (state[383] >> 32) ^ state[234]
    value_3 = _uint64(value_3)
    value_4 = _uint64(value_4)
    state[0] = _uint64(
        state[0] ^ (value_4 >> 12) ^ (value_4 & _MASK_1)
    )
    state[1] = _uint64(
        state[1] ^ (value_3 >> 12) ^ (value_3 & _MASK_2)
    )

    cursor = 3
    for _ in range(73):
        value_5 = _uint64(
            _uint64((value_3 << 13) ^ state[cursor - 1]) << 19
        ) ^ (value_3 >> 32) ^ state[cursor + 233]
        value_3 = _uint64(
            _uint64((value_4 << 13) ^ state[cursor]) << 19
        ) ^ (value_4 >> 32) ^ state[cursor + 234]
        value_5 = _uint64(value_5)
        value_3 = _uint64(value_3)
        state[cursor - 1] = _uint64(
            state[cursor - 1]
            ^ (value_5 & _MASK_1)
            ^ (value_5 >> 12)
        )
        state[cursor] = _uint64(
            state[cursor] ^ (value_3 >> 12) ^ (value_3 & _MASK_2)
        )
        cursor += 2
        value_4 = value_5

    cursor = 149
    for _ in range(117):
        value_4 = _uint64(
            _uint64((value_3 << 13) ^ state[cursor - 1]) << 19
        ) ^ (value_3 >> 32) ^ state[cursor - 149]
        value_3 = _uint64(
            _uint64((value_5 << 13) ^ state[cursor]) << 19
        ) ^ (value_5 >> 32) ^ state[cursor - 148]
        value_4 = _uint64(value_4)
        value_3 = _uint64(value_3)
        state[cursor - 1] = _uint64(
            state[cursor - 1]
            ^ (value_4 & _MASK_1)
            ^ (value_4 >> 12)
        )
        state[cursor] = _uint64(
            state[cursor] ^ (value_3 >> 12) ^ (value_3 & _MASK_2)
        )
        cursor += 2
        value_5 = value_4

    state[382] = value_4
    state[383] = value_3


def deterministic_item_rolls(seed: int):
    """Yield the client's inclusive [0, 1] item-quality sequence.

    An item's seed never changes, and every player-stat recalculation used
    to rebuild each equipped item's dSFMT state (a 768-step init plus a
    refresh, in Python) on the combat hit path. The first rolls of a seed
    are cached; the rare consumer that needs more continues from a fresh
    stream, so the sequence is unchanged.
    """

    prefix = _cached_roll_prefix(int(seed) & _UINT32_MASK)
    yield from prefix
    stream = _generate_item_rolls(seed)
    for _ in range(len(prefix)):
        next(stream)
    yield from stream


@lru_cache(maxsize=8192)
def _cached_roll_prefix(seed: int) -> tuple[float, ...]:
    return tuple(islice(_generate_item_rolls(seed), _CACHED_ROLL_PREFIX))


def _generate_item_rolls(seed: int):
    state = _initial_state(seed)
    cursor = _OUTPUT_COUNT
    while True:
        if cursor >= _OUTPUT_COUNT:
            _refresh(state)
            cursor = 0
        encoded = struct.pack("<Q", state[cursor])
        unit = 2.0 - struct.unpack("<d", encoded)[0]
        cursor += 1
        yield unit


def base_enchantment_seed(seed_idx: int) -> int:
    """Return the native seed used by ``GameItem::SetupModifiers``.

    The two explicit float32 roundings reproduce the client expression
    ``(int)((float)seedIdx * 0.5f)`` instead of Python's wider float math.
    """

    seed_as_float = struct.unpack(
        "<f", struct.pack("<f", float(int(seed_idx) & _UINT32_MASK))
    )[0]
    half_seed = struct.unpack(
        "<f", struct.pack("<f", seed_as_float * 0.5)
    )[0]
    return int(half_seed)


def deterministic_base_enchantment_rolls(seed_idx: int):
    """Yield the quality stream for an item's ``BaseEnchantments``."""

    return deterministic_item_rolls(base_enchantment_seed(seed_idx))
