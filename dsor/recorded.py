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


#: Non-player entity records lifted from the tutorial dungeon, one per index the
#: real session showed: 0x08, 0x0A, 0x0B, 0x0F, 0x10, 0x12, 0x13, 0x14.
#:
#: They are real records rather than invented ones on purpose. The zone content
#: message defines what the map contains, and an identifier it never mentioned is an
#: entity the client cannot draw — so a spawned creature has to be one the zone
#: already knows about. Only its position is rewritten.
MOB_TEMPLATES = "mob_templates.bin"


def mob_templates() -> list[bytes]:
    """The recorded non-player entity records, twenty bytes each."""
    raw = payload(MOB_TEMPLATES)
    return [raw[i : i + 20] for i in range(0, len(raw), 20)]


#: Recorded 0x85/0x002A entity descriptions, one file each.
#:
#: These are the answer to the client's 0x8B/0x001C, which is how it asks "what is
#: entity N". Its request body is four bytes and begins with the entity's index; the
#: description carries that same four-byte handle back, and finding it is what paired
#: each file with its creature.
#:
#: The handle's position is a rule rather than a table: it sits 260 bytes from the
#: end in all of them — offset 151 in the 411-byte descriptions, 137 in the 397-byte
#: ones and 146 in the 406-byte one. That it is derivable matters, because it means a
#: description can be recognised without being listed.
#:
#: Why this is the missing piece: a position update for an entity the client has
#: never heard of is ignored. In the reference session a creature does appear in a
#: 0x005F before any description, and that misled this project for a while — the
#: order is position, then the client's question, then the answer, and only then does
#: the creature exist. Leaving the question unanswered means it never does, and the
#: client asks again for as long as it is left running: 136 times against this
#: server, against 8 in the real session.
ENTITY_DESCRIPTIONS = (
    "zone_trigger_01.bin",
    "zone_trigger_03.bin",
    "zone_trigger_04.bin",
    "zone_trigger_06.bin",
    "zone_trigger_07.bin",
    "zone_trigger_08.bin",
)

#: How far from the end of a description its four-byte handle sits.
HANDLE_FROM_END = 260

#: Width of that handle, and of the client's request body.
HANDLE_SIZE = 4


def entity_handle(description: bytes) -> bytes:
    """The four bytes naming which entity *description* describes."""
    start = len(description) - HANDLE_FROM_END
    if start < 3:
        raise ValueError(
            f"a description is at least {HANDLE_FROM_END + 3} bytes, "
            f"got {len(description)}"
        )
    return description[start : start + HANDLE_SIZE]


def entity_descriptions() -> dict[bytes, bytes]:
    """Descriptions a creature can be answered with, keyed by its actor id.

    Batches, whose leading command is the description. The library's single commands
    are **not** merged in here: callers read a creature's position out of whatever this
    returns, and that offset is known for a batch and not for a single command.
    """
    out: dict[bytes, bytes] = {}
    for name in ENTITY_DESCRIPTIONS:
        body = payload(name)
        out[entity_handle(body)] = body
    return out


def combat_ready_mobs() -> list[bytes]:
    """Creature records this server can describe, hit **and** remove.

    Serving one it cannot remove leaves a corpse at zero health that never goes away
    and that the player can go on striking — which is what happened to the creature
    whose removal message arrived grouped with another actor's in the capture, so it
    could not be isolated. One of the six is dropped for that reason.
    """
    # Deliberately *not* the library, though it holds eleven creatures against these
    # six. Serving a library description needs the creature's position read out of it,
    # for the kill to announce the death in the right place — and that position cannot
    # yet be located inside a single command, only inside a batch. Switching the source
    # broke the death animation, which had just started working.
    #
    # The library and its accessors stay, because the extraction was the hard part.
    # What is missing is one field's offset. See library_records.
    hits = hit_commands()
    return [record for record in describable_mobs() if record[15:19] in hits]


def describable_mobs() -> list[bytes]:
    """Creature records this server can also describe.

    Serving an entity it cannot describe is pointless and worse than nothing: the
    client discards the position outright, asks what the entity is, and gets no
    answer — the exact dead end that kept the map empty.

    Two of the recorded records are filtered out here, and for a reason worth
    keeping: they are not monsters. The client has separate commands for each kind —
    NewMonsterCommand for creatures, NewNPCCommand and NewDestroyableCommand for the
    other two — and the reference session answered their requests with neither of the
    descriptions collected here.
    """
    known = entity_descriptions()
    return [record for record in mob_templates() if record[15:19] in known]


def first_command(description: bytes, handle: bytes) -> bytes:
    """The leading command of *description*, the one addressed to *handle*.

    The recorded descriptions are not single messages but **batches**: the client
    reads commands one after another, each ending in 0xFF, until fewer than sixteen
    bits remain. A 411-byte one holds the creature's NewMonsterCommand and then
    several more, the last of them about the player — whose actor id is what sits in
    the batch's own trailer.

    Cutting after the first command sends the client only what concerns the creature.
    The cut is found rather than tabulated: the first 0xFF whose preceding 32 bits are
    *handle* and which is followed by a plausible command id. Nothing is byte-aligned
    here, so the result is rounded up to whole bytes; the few spare bits are below the
    sixteen the client needs to try another command, so it stops there.
    """
    from raknet.bitstream import BitReader

    reader = BitReader(description)
    limit = len(description) * 8
    for position in range(40, limit - 16):
        reader.seek(position - 8)
        if reader.read_bits(8) != 0xFF:
            continue
        reader.seek(position)
        following = reader.read_uint(16)
        if not 1 <= following < 0x177:
            continue
        reader.seek(position - 40)
        if reader.read_uint(32).to_bytes(4, "little") == handle:
            return description[: (position + 7) // 8]
    raise ValueError(
        f"no command addressed to {handle.hex(' ')} found in {len(description)} bytes"
    )


#: Recorded 0x85/0x0074 ActorsEnterVicinityCommand announcements, one per creature.
#:
#: This is the step that was missing. The real flow is three-legged: the server
#: announces that an actor is near, the client asks what it is, the server describes
#: it. Only the last two were implemented here, and the client's handler for the
#: announcement calls RequestActor *directly* — so skipping it means the actor is set
#: up by a different path than the real one took.
#:
#: Each announcement is 111 bytes and names exactly one creature, and in the reference
#: session each arrived at the very frame that creature first appeared: 43835 for
#: 0x08, 44046 for 0x0A, 44072 for 0x0B, 44925 for 0x0F, 44927 for 0x10, 44956 for
#: 0x12.
VICINITY_PREFIX = "vicinity_"


def vicinity_announcements() -> dict[bytes, bytes]:
    """The recorded announcements, keyed by the actor each names."""
    out: dict[bytes, bytes] = {}
    for actor in entity_descriptions():
        try:
            out[actor] = payload(f"{VICINITY_PREFIX}{actor.hex()}.bin")
        except FileNotFoundError:
            continue
    return out


#: Recorded combat messages, one file per creature, from a session where six of them
#: were killed.
#:
#: This is the pair the emulator was missing, and finding it needed a capture that
#: did not exist before: the earlier session's player never killed anything. In it,
#: all 107 ActorStatsUpdateCommands targeted the player and not one targeted a
#: creature — which is why reporting a creature's health did nothing, over several
#: attempts. **A creature has no health message at all.** The client works its bar
#: out from the hit.
#:
#: The pattern in the killing session is exact: a large 0x85/0x006B HitCommand naming
#: both the creature and the player, then a 0x85/0x0073 ActorsLeftVicinityCommand
#: naming the creature — frames 5219 then 5410 for one, 5525 then 5733 for the next,
#: and so on for all six. The small 160-byte HitCommands name no creature; those are
#: blows that did not land, or blows taken.
HIT_PREFIX = "hit_"
DEPARTURE_PREFIX = "leave_"


def hit_commands() -> dict[bytes, bytes]:
    """Recorded hits, keyed by the creature each names."""
    return _by_actor(HIT_PREFIX)


def departure_commands() -> dict[bytes, bytes]:
    """Recorded removals, keyed by the creature each names."""
    return _by_actor(DEPARTURE_PREFIX)


def _by_actor(prefix: str) -> dict[bytes, bytes]:
    out: dict[bytes, bytes] = {}
    for actor in entity_descriptions():
        try:
            out[actor] = payload(f"{prefix}{actor.hex()}.bin")
        except FileNotFoundError:
            continue
    return out


#: A creature's blow against the player, replayed. Sixteen of these appear in the
#: killing session, 163 or 164 bytes, and none of them names a creature — which is
#: what marks them as blows *taken* rather than landed.
#:
#: This direction is worth more than the other, because it travels a channel that
#: demonstrably works: the player's actor is bound to an entity, so the health updates
#: that accompany these do take effect, where a creature's never can.
INCOMING_HIT = "incoming_hit.bin"


def incoming_hit() -> bytes:
    """A recorded blow from a creature against the player."""
    return payload(INCOMING_HIT)


def commands_for(batch: bytes, actor: bytes) -> bytes:
    """The commands inside *batch* that are addressed to *actor*, and only those.

    Neither "send the whole batch" nor "send its first command" is right, and both
    were tried. A recorded hit is ten commands or more: the blow itself, then the
    cascade the real session produced around it — the player's own stats, the loot,
    and a position for the creature where it happened to die. Sending all of it moved
    the creature and its drop to wherever the other session's player stood. Sending
    only the first dropped the effect that made it disappear, so it died at zero
    health and stayed on screen.

    Each command ends with a 32-bit actor id and a 0xFF, and the two bytes after that
    are the next command's id, so a batch can be walked and filtered. Nothing is
    byte-aligned — a batch ends three bits short of its last byte — so this is
    assembled bit by bit and padded at the end. The client stops when fewer than
    sixteen bits remain, which is what makes the padding safe.
    """
    from raknet.bitstream import BitReader, BitWriter

    reader = BitReader(batch)
    message_id = reader.read_uint(8)
    limit = len(batch) * 8

    # Walk the boundaries: a command runs from where its id starts to just past its
    # terminator.
    boundaries: list[int] = []
    for position in range(reader.position + 16 + 40, limit + 1):
        reader.seek(position - 8)
        if reader.read_bits(8) != 0xFF:
            continue
        if position + 16 <= limit:
            reader.seek(position)
            if not 1 <= reader.read_uint(16) < 0x177:
                continue
        boundaries.append(position)

    writer = BitWriter()
    writer.write_uint(message_id, 8)
    start = 8
    kept = 0
    for end in boundaries:
        reader.seek(end - 40)
        addressed = reader.read_uint(32).to_bytes(4, "little")
        # The creature's own commands, and those addressed to nobody. A zero actor id
        # marks an effect that belongs to no particular actor — which is where a death
        # animation and a dropped item plausibly live, and dropping them left the
        # creature dead at zero health, still standing and still solid.
        if addressed in (actor, b"\x00\x00\x00\x00"):
            reader.seek(start)
            writer.write_bits(reader.read_bits(end - start), end - start)
            kept += 1
        start = end
    if not kept:
        raise ValueError(
            f"no command addressed to {actor.hex(' ')} in {len(batch)} bytes"
        )
    return writer.to_bytes()


def monster_spawn(description: bytes):
    """Where a description places its creature, in world units. See dsor.gameplay."""
    from dsor.gameplay import monster_spawn as _spawn

    return _spawn(description)


def monster_template(description: bytes) -> str:
    """The blueprint name a description spawns, its first field."""
    from raknet.bitstream import BitReader

    reader = BitReader(description)
    reader.read_uint(8)          # message id
    reader.read_uint(16)         # command id
    return reader.read_string()


def with_template(description: bytes, name: str) -> bytes:
    """Return *description* spawning *name* instead.

    Only possible because the template name is the **first** field: everything behind
    it can be copied verbatim, bit for bit, without being understood. A different
    length shifts all of that, which is why this reassembles rather than patching in
    place — the same message is bit-packed and ends three bits short of its last byte,
    and an in-place write at a byte offset is what broke it once before.

    What the client accepts is limited by its own blueprint data: the name has to
    resolve in the factory under the "Monster" category, or it logs an invalid template
    id and creates nothing.

    And resolving is not enough. Tried against a0001_champion_undead_mage_01, one of
    the ten templates the tutorial dungeon's own database lists: the client logged no
    rejection and drew nothing at all. The second field of the command is a composite
    array, copied verbatim here from whichever creature the description was captured
    from, so a mage receives a creature's payload. Changing the name alone gives an
    entity whose name resolves and whose appearance does not belong to it.

    Which means the templates this server can really serve are the three it has whole
    descriptions for. Serving another needs a capture in which that creature appears —
    one of the few remaining questions where a new capture is genuinely the answer
    rather than more reading.
    """
    from raknet.bitstream import BitReader, BitWriter

    reader = BitReader(description)
    message_id = reader.read_uint(8)
    command = reader.read_uint(16)
    reader.read_string()                       # the old name, discarded
    rest = reader.remaining

    writer = BitWriter()
    writer.write_uint(message_id, 8)
    writer.write_uint(command, 16)
    writer.write_string(name)
    writer.write_bits(reader.read_bits(rest), rest)
    return writer.to_bytes()


#: Complete NewMonsterCommands, one file per creature blueprint, lifted whole out of
#: a session that walked three maps.
#:
#: This supersedes rewriting a creature's description to name a different blueprint.
#: That approach resolved the name and drew nothing, because everything behind the
#: name — including a composite array describing the creature — still belonged to the
#: original. A creature *is* its description: the command carries its blueprint, its
#: actor id, its spawn position and its health, and serving one whole is the only way
#: to get a creature that is internally consistent.
#:
#: Extracting them needed a stricter reading of a batch than "any 0xFF followed by a
#: plausible command id". That accepts false boundaries, and the giveaway was the actor
#: ids it produced: 0xFFFFFF00 and 0xFFFF8000, which are not actors. Requiring the
#: candidate to look like one — 0x0001xxxx with a low half under ten thousand, the
#: shape of every real actor in these captures — turned seven doubtful extractions
#: into eleven sound ones.
MONSTER_PREFIX = "monster_"


def monster_library() -> dict[str, bytes]:
    """Every extracted creature description, keyed by its blueprint name."""
    out: dict[str, bytes] = {}
    for path in sorted(DATA.glob(f"{MONSTER_PREFIX}*.bin")):
        out[path.stem[len(MONSTER_PREFIX) :]] = path.read_bytes()
    return out


def library_actor(description: bytes) -> bytes | None:
    """The actor id a library description carries, or None if it cannot be read."""
    from raknet.bitstream import BitReader

    total = len(description) * 8
    reader = BitReader(description)
    for padding in range(8):
        end = total - padding
        if end - 40 < 0:
            continue
        reader.seek(end - 8)
        if reader.read_bits(8) != 0xFF:
            continue
        reader.seek(end - 40)
        actor = reader.read_uint(32)
        if (actor >> 16) == 1 and (actor & 0xFFFF) < 10000:
            return actor.to_bytes(4, "little")
    return None


def library_creatures(map_prefix: str = "a0001") -> dict[bytes, bytes]:
    """Library descriptions for one map, keyed by the actor each carries.

    Filtered by map because ``actorEntities`` on the client is indexed by an actor
    id's low sixteen bits alone: two creatures from different maps can share those,
    and serving both would have one overwrite the other's binding. Two of the eleven
    extracted descriptions do exactly that.
    """
    out: dict[bytes, bytes] = {}
    for name, description in monster_library().items():
        if not name.startswith(map_prefix):
            continue
        actor = library_actor(description)
        if actor is not None:
            out[actor] = description
    return out


def library_records(map_prefix: str = "a0001") -> list[bytes]:
    """Movement records for the library creatures that can also be placed.

    A creature needs two things: a description saying what it is, and a record saying
    where it stands. The library supplies the first, the recorded movement records the
    second, and only a creature present in both can be served.

    Reading the position out of the description instead would be better, and is
    blocked. Its second field is an array of ten composite elements of *variable* size
    — two creatures whose names are the same length yield descriptions of 172 and 176
    bytes — so nothing behind that array can be reached by reading forward. The earlier
    "281 bytes from the end" rule does not rescue it either: that was measured on
    batches, and these are single commands.
    """
    placed = {record[15:19]: record for record in mob_templates()}
    return [
        placed[actor]
        for actor in sorted(library_creatures(map_prefix))
        if actor in placed
    ]


def with_actor(description: bytes, actor: bytes) -> bytes:
    """Return *description* creating *actor* instead of the one it carries.

    A description carries its own actor id in its trailer, and that is the actor the
    client creates. Serving one under a different slot without changing it creates the
    wrong actor: the client goes on asking about the one it wanted, every few seconds,
    for ever — the same signature as a request never answered at all.

    Only those 32 bits change. They are not byte-aligned, so this rewrites them in
    place at the bit offset where the trailer was found.
    """
    from raknet.bitstream import BitReader, BitWriter

    if len(actor) != 4:
        raise ValueError(f"an actor id is 4 bytes, got {len(actor)}")

    total = len(description) * 8
    reader = BitReader(description)
    for padding in range(8):
        end = total - padding
        if end - 40 < 0:
            continue
        reader.seek(end - 8)
        if reader.read_bits(8) != 0xFF:
            continue
        reader.seek(end - 40)
        candidate = reader.read_uint(32)
        if (candidate >> 16) != 1 or (candidate & 0xFFFF) >= 10000:
            continue
        writer = BitWriter()
        reader.seek(0)
        writer.write_bits(reader.read_bits(end - 40), end - 40)
        writer.write_uint(int.from_bytes(actor, "little"), 32)
        writer.write_uint(0xFF, 8)
        return writer.to_bytes()
    raise ValueError("no actor trailer found to rewrite")


#: How far from the end of a *single* NewMonsterCommand its spawn position sits, in
#: bits. Measured by cross-reference rather than guessed: three creatures appear both
#: as a batch, where the position's offset was already known, and as a single command
#: in the library. Searching each single command for the float triple its batch
#: carries puts it 448 bits from the end in all three — at byte 130, 116 and 125
#: respectively, so the rule holds across three different message lengths.
#:
#: The batch rule of 281 bytes from the end does not apply here and never could: it was
#: measured on batches, whose tails differ.
SPAWN_FROM_END_BITS = 448


def spawn_bit(
    description: bytes, points: list[tuple[float, float, float]]
) -> int | None:
    """The bit offset of the position inside *description*, found by recognising it.

    A fixed offset does not work, and assuming one corrupted the message: 448 bits
    from the end is right for an ordinary creature, but the undead mage's position
    sits at 480 and the healing champion's at 520. Writing at 448 for those wrote over
    whatever lay there, and the client answered plainly:

        Could not decode command (ID: '42', 'Commands::NewMonsterCommand')

    It could not be caught by a round-trip, either: reading and writing at the same
    wrong offset is symmetric and looks perfect.

    So the position is located by matching it against the map's own spawn points,
    which is where these creatures were captured standing. Exactly one candidate
    survives for each of the seven blueprints.
    """
    import struct

    size = len(description)
    value = int.from_bytes(description, "big")
    for shift in range(8):
        window = ((value << shift) & ((1 << (8 * size)) - 1)).to_bytes(size, "big")
        for index in range(size - 12):
            triple = struct.unpack_from("<3f", window, index)
            if any(
                abs(triple[0] - x) < 0.6 and abs(triple[2] - y) < 0.6
                for x, _elevation, y in points
            ):
                return index * 8 + shift
    return None


def library_spawn(description: bytes) -> tuple[float, float, float]:
    """Where a single-command description places its creature, in world units."""
    import struct

    from raknet.bitstream import BitReader

    start = len(description) * 8 - SPAWN_FROM_END_BITS
    if start < 24:
        raise ValueError(f"description too short: {len(description)} bytes")
    reader = BitReader(description, start)
    return tuple(
        struct.unpack("<f", reader.read_uint(32).to_bytes(4, "little"))[0]
        for _ in range(3)
    )


def with_library_spawn_at(
    description: bytes, bit: int, x: float, elevation: float, y: float
) -> bytes:
    """Return *description* with the position at *bit* replaced.

    Ninety-six bits change and nothing else, so the blueprint, the actor and the
    health stay as captured — only where it stands is chosen. The offset is passed in
    rather than assumed, because it is not the same for every creature.
    """
    import struct

    from raknet.bitstream import BitReader, BitWriter

    reader = BitReader(description)
    head = reader.read_bits(bit)
    reader.read_bits(96)
    rest = reader.remaining
    tail = reader.read_bits(rest)

    writer = BitWriter()
    writer.write_bits(head, bit)
    for value in (x, elevation, y):
        writer.write_uint(
            int.from_bytes(struct.pack("<f", value), "little"), 32
        )
    writer.write_bits(tail, rest)
    return writer.to_bytes()


def with_library_spawn(
    description: bytes, x: float, elevation: float, y: float
) -> bytes:
    """Return a single-command description placing its creature elsewhere.

    Ninety-six bits change and nothing else, so the blueprint, the actor and the
    health stay as captured — only where it stands is chosen.
    """
    import struct

    from raknet.bitstream import BitReader, BitWriter

    start = len(description) * 8 - SPAWN_FROM_END_BITS
    if start < 24:
        raise ValueError(f"description too short: {len(description)} bytes")
    reader = BitReader(description)
    writer = BitWriter()
    writer.write_bits(reader.read_bits(start), start)
    for component in (x, elevation, y):
        writer.write_uint(
            int.from_bytes(struct.pack("<f", component), "little"), 32
        )
    reader.seek(start + 96)
    remaining = reader.remaining
    writer.write_bits(reader.read_bits(remaining), remaining)
    return writer.to_bytes()
