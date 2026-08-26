"""Who is connecting, from the message the client already sends.

Every service gets a 0x8A from the client before anything else, and the server
answered it without ever looking inside. It carries the identity:

    8a | 11 00 | "DrasaOnlineClient" | aa 04 | 09 02 01 | <account u32> |
         03 05 | 10 00 | <session id, 16 bytes>

and the longer form, the one sent to a map server and to login after a character has
been chosen, inserts one more id before the account:

    ... aa 04 | 0a 01 01 | <character u32> | 02 01 | <account u32> | 03 05 | 10 00 | ...

Confirmed against the client's own launch arguments: ``-accid`` appears as that u32
little-endian in 57 datagrams across eight sessions, and ``-sid`` as those 16 raw bytes
in 682.

The fields are read from an anchor rather than a fixed offset, because the offset moves
with the form -- 25 in the short one and 31 in the long one. ``03 05 10 00`` precedes
the session id, and the account id is the four bytes eight before it.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

#: The message the client authenticates with.
AUTH_MESSAGE = 0x8A

#: What sits immediately before the session id: two bytes whose meaning is not
#: established, then its length as a 16-bit little-endian 16.
SESSION_ANCHOR = bytes([0x03, 0x05, 0x10, 0x00])

#: A session id is a GUID.
SESSION_SIZE = 16

#: How far the account id sits before the session id's own bytes.
ACCOUNT_BACK = 8

#: And the character id before that, in the long form only: four bytes, two before the
#: account id's own four.
CHARACTER_BACK = ACCOUNT_BACK + 6

#: What marks the long form. It cannot be the ``02 01`` that sits between the two ids,
#: because that is also the tail of the short form's own ``09 02 01`` -- reading it as
#: the marker invented a character id out of the client's name, and the test written
#: for the short form is what caught it. The tag three bytes earlier is the difference:
#: ``0a 01 01`` where the short form has ``09 02 01``.
LONG_FORM_TAG = bytes([0x0A, 0x01, 0x01])


@dataclass(frozen=True)
class Identity:
    """Who the client on the other end says it is."""

    #: The account, as the launcher's ``-accid``. Stable across sessions, which is
    #: what makes it the thing to key saved characters on.
    account: int
    #: The session, as ``-sid``. New every login, so useful for telling two
    #: connections of one account apart and useless for saving anything under.
    session: uuid.UUID | None = None
    #: The chosen character, present only once one has been. None on the first
    #: connection to login and to the character service.
    character: int | None = None

    @property
    def key(self) -> str:
        """What a saved character is stored under."""
        if self.character is not None:
            return f"{self.account}:{self.character}"
        return str(self.account)


def parse(payload: bytes) -> Identity | None:
    """Read the identity out of a 0x8A, or None if it is not one or does not fit.

    None rather than a guess: a server that invents an account id would write one
    player's progress over another's.
    """
    if not payload or payload[0] != AUTH_MESSAGE:
        return None
    at = payload.find(SESSION_ANCHOR)
    if at < 0:
        return None
    session_at = at + len(SESSION_ANCHOR)
    if session_at + SESSION_SIZE > len(payload):
        return None
    account_at = session_at - ACCOUNT_BACK
    if account_at < 0:
        return None
    account = int.from_bytes(payload[account_at : account_at + 4], "little")
    if not account:
        return None
    session = uuid.UUID(bytes=payload[session_at : session_at + SESSION_SIZE])

    character = None
    tag_at = account_at - 9
    character_at = session_at - CHARACTER_BACK
    if tag_at >= 0 and payload[tag_at : tag_at + 3] == LONG_FORM_TAG:
        candidate = int.from_bytes(payload[character_at : character_at + 4], "little")
        if candidate:
            character = candidate
    return Identity(account=account, session=session, character=character)
