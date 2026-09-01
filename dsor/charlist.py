"""The character selection screen, and putting the saved level on it.

Saving a character server-side does nothing for what the selection screen shows,
because that screen is drawn from a replayed recording: ``character_list.bin``, the
``0x0087`` the character service sends. The recorded character is level 1, so every
login showed level 1 however much this server had stored -- "tu sauvegardes rien vu que
tu rejoues une connexion au debut".

The message is small and its head is byte-aligned::

    24 bits   0x84, then the command id 0x0087
    u16       how many characters, 1 in the recording
    u32       the character id      111886222
    u32       the account id        112162098
    ...
    bit 168   u16, the level        1
    ...
    bit 233   u16 length + the name "balenciagas"
    bit 353   the map, "a0001_start_tutorial_dun"

Only the level is written. The fields around it are not identified -- 0x2923 and 0x8000
follow it and nothing here knows what they mean -- and rewriting a field whose meaning
is a guess is how a player state gets corrupted rather than corrected.

The write is guarded: it happens only when the sixteen bits at that offset already read
as a plausible level, and only when the name is where it should be. A patcher that
writes into the wrong place on a slightly different recording would corrupt the message
silently, which is worse than a screen showing the wrong number.
"""

from __future__ import annotations

import logging

from raknet.bitstream import BitReader

log = logging.getLogger("charlist")

#: Where the level sits, in bits from the start of the payload.
LEVEL_AT = 168

#: Its width.
LEVEL_BITS = 16

#: Where the name's length prefix sits, used to check the layout before writing.
NAME_AT = 233

#: The name in the recording, and its length.
RECORDED_NAME = "balenciagas"

#: The levels the curve runs to.
LOWEST_LEVEL = 1
HIGHEST_LEVEL = 110


def level_of(message: bytes) -> int | None:
    """The level the message states, or None when the layout is not the recorded one."""
    if len(message) * 8 < NAME_AT + 16:
        return None
    try:
        level = BitReader(message, LEVEL_AT).read_uint(LEVEL_BITS)
        reader = BitReader(message, NAME_AT)
        length = reader.read_uint(16)
        if length != len(RECORDED_NAME):
            return None
        name = bytes(reader.read_uint(8) for _ in range(length)).decode("ascii")
    except (IndexError, ValueError, UnicodeDecodeError):
        return None
    if name != RECORDED_NAME:
        return None
    if not LOWEST_LEVEL <= level <= HIGHEST_LEVEL:
        return None
    return level


def with_level(message: bytes, level: int) -> bytes:
    """*message* with the character's level set to *level*.

    Returns it unchanged, and says so, when the layout is not the one this was measured
    against. Sixteen bits written in place, so nothing moves and no length changes.
    """
    found = level_of(message)
    if found is None:
        log.warning(
            "character list is not the recorded layout -- leaving the level alone"
        )
        return message
    wanted = max(LOWEST_LEVEL, min(HIGHEST_LEVEL, int(level)))
    if found == wanted:
        return message
    out = bytearray(message)
    # The field is byte-aligned at bit 168, which is byte 21.
    at = LEVEL_AT // 8
    out[at] = wanted & 0xFF
    out[at + 1] = (wanted >> 8) & 0xFF
    log.info("character list: level %d -> %d", found, wanted)
    return bytes(out)
