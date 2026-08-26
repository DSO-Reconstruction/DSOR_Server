"""The action bar, as the client reports it.

``QuickSlotsCommand`` 0x0051 was the most frequent thing the client sent after its own
movement -- 180 times in one short session, 1991 across the live captures -- and this
server never looked at it. Reading it answers a question that looked like a bug in the
skills: "les sorts marchent pas du tout".

    11 00 00 00                 seventeen slots
    00 00 00 00
    0b 00 "angrystrike"         slot 0
    ff ff ff ff  x 16           the rest, empty
    03 00 00 00                 a counter, one higher each message

The bar had exactly one skill on it. Pressing any other did nothing because there was
nothing there to press, and no amount of granting skills in the *book* puts them on the
*bar* -- the client owns the bar and reports it.

Worth being clear about what this is not: the live service never answers a
QuickSlotsCommand either. 1991 from the client and not one reply, so the repetition is
the client keeping the server informed rather than retrying an unanswered request. I
had begun to blame the missing reply and the captures refuted it.

A full bar from the live service reads: angrystrike, mightybash, warshout, frenzyshout,
bloody360, in slots 0, 3, 7, 10 and 13.
"""

from __future__ import annotations

from dataclasses import dataclass

#: 0x0051.
QUICK_SLOTS = 0x0051

#: An empty slot.
EMPTY = bytes([0xFF, 0xFF, 0xFF, 0xFF])

#: Where the slots begin: the count, then four bytes whose meaning is not established.
SLOTS_AT = 8

#: The longest a skill id is allowed to be before this decides the message is not
#: what it thinks. Real ids run to 19 characters (laceratingstrike is 16,
#: bloody360_Chimera 17, chainlightning_SetLight2026_warrior is longer but is not a bar
#: skill), and a length past this means the walk has lost the boundary.
LONGEST_ID = 48


@dataclass(frozen=True)
class QuickSlots:
    """What is on the bar."""

    #: One entry per slot, in order, None where the slot is empty.
    slots: tuple[str | None, ...]

    @property
    def filled(self) -> dict[int, str]:
        """The slots that hold something, by index."""
        return {i: name for i, name in enumerate(self.slots) if name}

    def __len__(self) -> int:
        return len(self.slots)


def decode(body: bytes) -> QuickSlots | None:
    """Read a QuickSlotsCommand body, or None if it does not read as one.

    None rather than a partial answer: half a bar is not information, and the trailing
    bytes of this message are not understood, so a walk that loses the boundary has to
    say so.
    """
    if len(body) < SLOTS_AT + 4:
        return None
    count = int.from_bytes(body[0:4], "little")
    if not 0 < count <= 64:
        return None
    at = SLOTS_AT
    slots: list[str | None] = []
    for _ in range(count):
        if at + 4 <= len(body) and body[at : at + 4] == EMPTY:
            slots.append(None)
            at += 4
            continue
        if at + 2 > len(body):
            return None
        length = int.from_bytes(body[at : at + 2], "little")
        at += 2
        if length == 0 or length > LONGEST_ID or at + length > len(body):
            return None
        try:
            slots.append(body[at : at + length].decode("ascii"))
        except UnicodeDecodeError:
            return None
        at += length
    return QuickSlots(slots=tuple(slots))
