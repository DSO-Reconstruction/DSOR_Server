"""Messages replayed verbatim from a capture, because they are not yet decoded.

Some gates in the protocol cannot be passed with a synthesised message. The
character-selection screen is one: after the client says it is ready, the real
service sends a 437-byte record and the client will not offer a character to
choose until it arrives. Nothing in it is readable — no name in ASCII, no obvious
field boundaries — so there is no honest way to build one from scratch yet.

Replaying the recorded bytes is the standard way past that, and it is worth being
precise about what it does and does not give:

* it will show **the character that was recorded**, not an arbitrary one;
* it cannot serve a second, different character, or a character list of another
  length;
* every byte of account and character state inside it is whatever was true at
  capture time.

So this is scaffolding that gets the client into the world, not an
implementation. Each payload replaced by a real encoder is one file deleted from
here.

The bytes are the user's own session, captured from their own account.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

DATA = Path(__file__).parent / "data"

#: Sent by the character service after the client's 0x8d, in this order. The
#: first is the 0x84/0x0087 character record the selection screen needs; the
#: second is the short 0x84/0x001b that followed it on the wire, kept because the
#: real service sent it and the client is the only judge of whether it matters.
#: Everything the real service sends after the client's 0x8d, in ordering-index
#: order 4-5-6. The third file is 717 KB and is the reason this list has three
#: entries instead of one: the real service starts that transfer **immediately
#: after the roster**, at frame 103 of the reference session, long before the
#: player touches anything. Withholding it until the client asks to enter the
#: world is a deadlock — the client will not arm its Play button until the
#: transfer is under way, so the request that would have released the data never
#: comes.
#:
#: It does not have to *finish* first: in the reference session the client pressed
#: Play with 534 of 596 fragments delivered. That transfer was severely lossy —
#: 512 fragments were retransmitted, one of them 230 times — which is why it only
#: completed at frame 36658, seventeen thousand frames after Play.
CHARACTER_SELECTION_SEQUENCE = (
    "character_list.bin",        # 0x84/0x0087, ordering index 4, the roster
    "map_enter_ack.bin",         # 0x84/0x001B, 5 — the same bytes open a map entry
    "character_enter_bulk.bin",  # 0x84/0x00DD, 6 — 717 KB, 596 fragments
)

#: Acknowledgement a map server sends immediately after the client's 0x8d. Nine
#: bytes with no map name in them, so it is replayed for any map.
#:
#: The 1258-byte 0x85/0x0114 that follows it on the wire is deliberately *not*
#: replayed. It was briefly taken for the message that places the character, on
#: the strength of its size and its position in the sequence; reading it with a
#: bit-level scan showed it holds nothing but skin_unlock_amount_01..09 counters.
#: What actually places the player is 0x85/0x005F, and that this server now
#: generates.
MAP_ENTRY_ACK = "map_enter_ack.bin"

#: The character service's answer to a character choice. It is **not** an echo of
#: the request: the two 29-byte bodies differ in six places, and the reply carries
#: the id of the character being validated where the request has zeros. Echoing
#: the request back therefore confirms nothing, and the client goes straight to
#: spawning without ever showing the character.
#:
#: Every file in this directory now comes from one session
#: (session-20260818-033440), which matters more than any individual field: an
#: earlier set mixed two captures, and the character confirmed on one did not exist
#: in the other's world. Consistency here is by construction, not by inspection.
#:
#: A tempting explanation for that failure was that the confirmed id must appear in
#: the entity snapshot. It does not — not in the mixed set, and **not in any of the
#: three coherent real sessions either**. So entities are not addressed by this id,
#: and that hypothesis is recorded here as refuted rather than acted on.
CHARACTER_CHOSEN_REPLY = "character_chosen_reply.bin"
CHARACTER_CHOSEN_NAME = "balenciagas"

#: The zone content a map server sends between the acknowledgement and the first
#: entity update, in this order. Recorded on a0006_grimford_hub, so they belong to
#: that map only.
#:
#: Sending the placement alone was tried and was not enough: the client accepted
#: the position, talked for sixteen seconds and gave up. It needs the zone before
#: it can put anything in it. The middle file is 494 KB and fragments into some
#: four hundred datagrams, which is the largest thing this server sends by far.
ZONE_SEQUENCE = ("zone_cosmetics.bin", "zone_content.bin", "zone_extra.bin")

#: The map the recorded zone belongs to.
ZONE_MAP = "a0001_start_tutorial_dun"

#: The per-tick companion of a position update. The real map server alternates
#: 0x85/0x004F (93 bytes) and 0x85/0x005F (20 bytes) from the moment the client is
#: ready — 4412 and 4506 of them in one session, so one pair per tick.
#:
#: Never sending this was the largest single omission in the server: the second
#: densest stream in the protocol was simply absent.
TICK_STATE = "tick_state.bin"

#: What follows the zone: a 240-byte multi-entity 0x85/0x005F and a 37-byte
#: 0x85/0x00A7, in that order.
#:
#: The snapshot is the piece that was missing. A single 20-byte entity update was
#: sent instead, and the client asked to be readied four times and then waited
#: eighteen seconds and gave up — it wants the whole initial entity set, not one
#: position. Its first position is the arrival position, so it holds the player
#: and their surroundings together.
#:
#: Both are replays, so the surroundings are the recorded ones. Movement echoes
#: after this point are still generated.
ENTITY_SNAPSHOT_SEQUENCE = ("entity_snapshot.bin", "zone_ready.bin")

#: A real 0x85/0x005F **body** — 20 bytes, without the message id and opcode,
#: unlike the other files here which hold complete messages. Used as the template
#: a generated position is written into; see dsor.gameplay.encode_entity_update.
ENTITY_UPDATE_TEMPLATE = "entity_update_template.bin"


@lru_cache(maxsize=None)
def payload(name: str) -> bytes:
    """Load a recorded message by file name, complete with its id and opcode."""
    path = DATA / name
    if not path.exists():
        raise FileNotFoundError(
            f"recorded payload {name!r} is missing from {DATA}. "
            "Re-extract it from a capture with tools/extract_capture.py, or "
            "implement the encoder it stands in for."
        )
    return path.read_bytes()


def character_selection() -> list[bytes]:
    """The recorded messages that make a character appear on the selection screen."""
    return [payload(name) for name in CHARACTER_SELECTION_SEQUENCE]


def map_entry_ack() -> bytes:
    """The nine-byte acknowledgement a map server sends after the ready signal."""
    return payload(MAP_ENTRY_ACK)


def character_chosen_reply() -> bytes:
    """The recorded confirmation of a character choice."""
    return payload(CHARACTER_CHOSEN_REPLY)


#: The map entry, in the order the real map server's ordering indices give.
#:
#: Index 6 — the 631 KB 0x85/0x001D — was missing from this list for a long time,
#: and its absence has a precise symptom: the client reaches the map, finds no
#: entities in it, and dies on the assertion that its own player actor is valid.
#: The zone content is what populates the world, the player included.
#:
#: Frame order in a capture is misleading here — several of these travel in one
#: datagram, and a split message's pieces interleave — so the ordering index is
#: what this list follows.
MAP_ENTRY_SEQUENCE = (
    "map_enter_ack.bin",     # 0x84/0x001B, ordering index 4
    "zone_cosmetics.bin",    # 0x85/0x0114, 5
    "zone_content.bin",      # 0x85/0x001D, 6 — 631 KB in 513 fragments
    "zone_ready.bin",        # 0x85/0x00A7, 7
    "zone_extra.bin",        # 0x85/0x0074, 8
)


def map_entry_sequence() -> list[bytes]:
    """Everything a map server sends before it starts ticking."""
    return [payload(name) for name in MAP_ENTRY_SEQUENCE]


#: What the character service sends to release a client, in this order.
#:
#: The trigger is **0x8B/0x0087 with operation 3**, and getting that wrong cost more
#: than any other mistake in this project. 0x0087 is CharacterSelectionCommand and
#: 0x0086 is CharacterGenerationCommand — character *creation* — which is the
#: opposite of what this assumed. The client's Play button sends 0x0087 operation 3
#: and then waits for operation 5; the 0x0086 in the reference session at frame
#: 19147 was the player creating a character, not clicking Play.
#:
#: So the two 0x0087 that were dismissed as "ignored by the real service" were the
#: Play clicks themselves, and this server answered neither.
#:
#: The 112-byte 0x84/0x0086 the real service sent is deliberately not here: it
#: answers a creation command, and a client selecting an existing character never
#: sends one.
#:
#: The order here is the real service's ordering indices 7 to 13, which is not the
#: order an earlier version of this file guessed: the confirmation comes *first*
#: and the 0x0086 acknowledgement third, with a six-byte 0x0070 between them. The
#: 717 KB transfer is not part of this sequence at all — see
#: CHARACTER_SELECTION_SEQUENCE, which is where the real service sends it.
CHARACTER_RELEASE_SEQUENCE = (
    "character_chosen_reply.bin",   # 0x84/0x0087 operation 5, the grant
    "character_release_extra.bin",  # 0x84/0x0070, the empty handoff, six bytes
)

#: The answer to a client 0x8B/0x010B, one pair per request.
#:
#: These were briefly part of the release sequence above, because their ordering
#: indices (10 to 13) follow it. Ordering index says what order messages are
#: delivered in, not what prompted them: the client sends 0x010B twice at frame
#: 36660 and these four follow at 36663 and 36666. Sent unprompted they are two
#: messages the client never asked for.
CLIENT_QUERY_REPLY_SEQUENCE = (
    "release_010e_a.bin",  # 0x84/0x010E, ordering index 10, 1573 bytes (it splits)
    "release_010c_a.bin",  # 0x84/0x010C, 11, eleven bytes
)

#: The second 0x010B gets a larger 0x010E, so the two are not interchangeable.
CLIENT_QUERY_REPLY_SECOND = (
    "release_010e_b.bin",  # 0x84/0x010E, ordering index 12, 2008 bytes
    "release_010c_b.bin",  # 0x84/0x010C, 13, eleven bytes
)


def character_release() -> list[bytes]:
    """The three messages that precede the empty handoff."""
    return [payload(name) for name in CHARACTER_RELEASE_SEQUENCE]


def client_query_reply(seen: int) -> list[bytes]:
    """The answer to the client's *seen*-th 0x8B/0x010B, counting from zero.

    Only two were recorded. A third request gets the second answer again, which is
    a guess — but a repeated answer is likelier to be tolerated than silence, and
    the real client only ever asked twice.
    """
    names = CLIENT_QUERY_REPLY_SEQUENCE if seen == 0 else CLIENT_QUERY_REPLY_SECOND
    return [payload(name) for name in names]


def tick_state() -> bytes:
    """The 0x85/0x004F that precedes each position update."""
    return payload(TICK_STATE)


def zone_content(map_name: str) -> list[bytes]:
    """The recorded zone a map server sends before placing the player.

    Refuses any map but the one it was recorded on: another zone's content would
    describe geometry the loaded map does not have.
    """
    if map_name != ZONE_MAP:
        raise ValueError(
            f"the recorded zone belongs to {ZONE_MAP!r}, not {map_name!r}. "
            "Capture an arrival on that map, or decode the format."
        )
    return [payload(name) for name in ZONE_SEQUENCE]


def entity_snapshot(map_name: str) -> list[bytes]:
    """The initial entity set, recorded on the same map as the zone."""
    if map_name != ZONE_MAP:
        raise ValueError(
            f"the recorded entity snapshot belongs to {ZONE_MAP!r}, not {map_name!r}"
        )
    return [payload(name) for name in ENTITY_SNAPSHOT_SEQUENCE]


def entity_update_template() -> bytes:
    """The 20-byte body a generated entity update is written over."""
    return payload(ENTITY_UPDATE_TEMPLATE)
