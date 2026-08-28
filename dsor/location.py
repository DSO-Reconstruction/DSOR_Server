"""``NewLocationEffectCommand`` and its two relatives: the effects drawn on the ground.

These are the commands behind "je lance fury of the dragon j'ai aucun VFX" and "banner
of war pas de vfx juste le drapeau". Both skills grant *no status effect at all* --
``granted_by(earthquake)`` and ``granted_by(defiance)`` are empty, which is not a gap in
this server's tables but what the client's own database says. What those skills produce
is a thing placed on the map, and the map is what these commands describe.

Three ids, and the recordings show what separates them:

``0x003C``
    One effect at one place, and *no count*: the body is a single entry. The recorded
    payload holds two of them chained -- ``talent_warrior_skill_earthquake_rain_aura``
    and then ``..._rain_impact`` -- and their leading 16 bits are 5 and 10, which is
    the entry's own index rather than a length. Reading it as a count is what made the
    decoder ask for a 25,960-byte string.
``0x003D``
    A short command -- two 16-bit fields and the tail -- that in the recording is
    immediately followed by a ``0x003E``. Read as the "remove" or "replace" half.
``0x003E``
    A list. The recorded ones carry six and seven entries, and the leading 16 bits are
    the count: 6 for six entries, 7 for seven.

The grammar, derived from the three recordings and confirmed by an exact round trip:

    header   u16 count, u16 (0 in every recording)
    entry    u16 index, u16 flag (1 in every recording),
             u16 length + shape class name, 345 bits of geometry,
             u16 length + effect name, 460 bits
    tail     u32 actor, u8 0xFF

The two block sizes are not guesses. Every recorded entry names ``SphereEffect`` and
then an effect, and the distance from one entry's start to the next is 965 bits plus the
effect name -- 1165 for a 25-character name, 1213 for a 31-character one, 1125 for a
20-character one, across thirteen entries in three payloads. Splitting that 965 at the
name puts 345 bits before it and 460 after, and the last entry then ends exactly where
the actor begins. A grammar that is 32 bits out ends 32 bits from the tail; this one
does not.

What varies and what does not, measured across the seven entries of one payload: 7 of
the 345 geometry bits and 56 of the 460, the rest identical. So the blocks are mostly a
frozen template and the encoder here keeps them, substituting only the fields it knows.
That is the opposite of what copying a *status effect* element did -- there the copied
bits carried another effect's identity, and here the identity is the name, which is
written afresh.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from raknet.bitstream import BitReader, BitWriter

log = logging.getLogger("location")

#: The container byte every game command travels in.
MULTI = 0x85

#: What closes a command.
TERMINATOR = 0xFF

#: The three command ids.
NEW_LOCATION_EFFECT = 0x003C
REMOVE_LOCATION_EFFECT = 0x003D
LOCATION_EFFECT_LIST = 0x003E

#: The shape class every recorded entry names.
SPHERE = "SphereEffect"

#: The geometry block, in bits, between the shape's name and the effect's.
SHAPE_BITS = 345

#: The block behind the effect's name, in bits.
TAIL_BITS = 460

#: The per-entry flag, 1 in all thirteen recorded entries.
ENTRY_FLAG = 1

#: The header's second field, 0 in all three recordings.
HEADER_SPARE = 0

#: A sane ceiling, so a mis-read count cannot make this allocate for ever.
MOST_ENTRIES = 64


@dataclass
class Entry:
    """One effect placed on the ground."""

    index: int
    name: str
    shape: str = SPHERE
    flag: int = ENTRY_FLAG
    #: The geometry block, kept as bits so a round trip is exact.
    geometry: list[int] = field(default_factory=list)
    #: The block behind the name, likewise.
    trailing: list[int] = field(default_factory=list)


@dataclass
class LocationEffect:
    """A decoded ``0x003C`` / ``0x003E``, or the two fields of a ``0x003D``."""

    id: int
    entries: list[Entry] = field(default_factory=list)
    spare: int = HEADER_SPARE
    actor: int = 0
    #: For a 0x003D, which carries no entries: its two 16-bit fields.
    fields: tuple[int, ...] = ()


def _string(reader: BitReader) -> str:
    """A 16-bit length then that many bytes."""
    length = reader.read_uint(16)
    if length > 512:
        raise ValueError(f"implausible string length {length}")
    return bytes(reader.read_uint(8) for _ in range(length)).decode(
        "utf-8", "replace"
    )


def _write_string(writer: BitWriter, text: str) -> None:
    raw = text.encode()
    writer.write_uint(len(raw), 16)
    for byte in raw:
        writer.write_uint(byte, 8)


def decode(payload: bytes, at: int = 24, known: int | None = None) -> LocationEffect:
    """Read one location-effect command out of *payload*.

    *at* is where the command's body begins in bits -- 24 past the ``0x85`` and the
    16-bit id for the first command in a chain. The actor and the terminator are read
    too, so a caller can check the command ended where it should. *known* names the id
    for a command that is not the first in its chain, whose id sits behind the previous
    command's terminator rather than in the payload's header.
    """
    identifier = (
        known
        if known is not None
        else (payload[1] | (payload[2] << 8) if at == 24 else None)
    )
    reader = BitReader(payload, at)
    if identifier == REMOVE_LOCATION_EFFECT:
        two = (reader.read_uint(16), reader.read_uint(16))
        actor = reader.read_uint(32)
        if reader.read_uint(8) != TERMINATOR:
            raise ValueError("0x003D did not end with a terminator")
        return LocationEffect(REMOVE_LOCATION_EFFECT, actor=actor, fields=two)

    # 0x003C carries one entry and no count; 0x003E carries a count and a spare.
    single = identifier == NEW_LOCATION_EFFECT
    if single:
        count, spare = 1, HEADER_SPARE
    else:
        count = reader.read_uint(16)
        if count > MOST_ENTRIES:
            raise ValueError(f"implausible entry count {count}")
        spare = reader.read_uint(16)
    entries = []
    for _ in range(count):
        index = reader.read_uint(16)
        flag = reader.read_uint(16)
        shape = _string(reader)
        geometry = [reader.read_uint(1) for _ in range(SHAPE_BITS)]
        name = _string(reader)
        trailing = [reader.read_uint(1) for _ in range(TAIL_BITS)]
        entries.append(Entry(index, name, shape, flag, geometry, trailing))
    actor = reader.read_uint(32)
    if reader.read_uint(8) != TERMINATOR:
        raise ValueError("command did not end with a terminator")
    return LocationEffect(
        identifier if identifier is not None else LOCATION_EFFECT_LIST,
        entries=entries,
        spare=spare,
        actor=actor,
    )


def encode(effect: LocationEffect) -> bytes:
    """The bytes for *effect*, byte-for-byte what :func:`decode` read."""
    writer = BitWriter()
    writer.write_uint(MULTI, 8)
    writer.write_uint(effect.id, 16)
    if effect.id == REMOVE_LOCATION_EFFECT:
        for value in effect.fields:
            writer.write_uint(value, 16)
    else:
        if effect.id != NEW_LOCATION_EFFECT:
            writer.write_uint(len(effect.entries), 16)
            writer.write_uint(effect.spare, 16)
        for entry in effect.entries:
            writer.write_uint(entry.index, 16)
            writer.write_uint(entry.flag, 16)
            _write_string(writer, entry.shape)
            for bit in entry.geometry:
                writer.write_uint(bit, 1)
            _write_string(writer, entry.name)
            for bit in entry.trailing:
                writer.write_uint(bit, 1)
    writer.write_uint(effect.actor, 32)
    writer.write_uint(TERMINATOR, 8)
    return writer.to_bytes()


#: The recording the shape templates are read from. Loaded rather than written down:
#: the blocks are 345 and 460 bits of geometry whose fields are not all decoded, and a
#: transcription of them into source would be a transcription of something unread.
RECORDED = "location_effect_003e.bin"

_templates: dict[str, Entry] | None = None


def templates() -> dict[str, Entry]:
    """The recorded entries by effect name, with a default under the empty key.

    Read once from :data:`RECORDED`. The blocks are near-identical across the thirteen
    recorded entries -- 7 of 345 geometry bits and 56 of 460 differ between the seven
    entries of one payload -- so an effect the recording does not name can borrow one
    without borrowing an identity: the identity is the name, and the name is written
    from the database.
    """
    global _templates
    if _templates is not None:
        return _templates
    import pathlib

    here = pathlib.Path(__file__).resolve().parent / "data" / RECORDED
    held: dict[str, Entry] = {}
    try:
        recorded = decode(here.read_bytes())
    except (OSError, ValueError) as problem:
        log.warning("cannot read %s: %s", RECORDED, problem)
        _templates = {}
        return _templates
    for entry in recorded.entries:
        held[entry.name] = entry
        held.setdefault("", entry)
    _templates = held
    return held


def placed_by(skill_wire: int, certain_only: bool = True) -> list[str]:
    """The effect names a skill places on the ground, from the client's database.

    ``_Template_Skill.LocationEffects`` is the column, and its C:1.0 entries are what a
    real server sends: for earthquake the database gives ``skill_earthquake_graphics``,
    ``_aura``, ``_shockwave_aura``, ``_loop``, ``_end`` -- the same five names in the
    same order as the recorded 0x003E, which is the check that this is the right
    column. The recording carries two more, ``talent_warrior_skill_earthquake_rain_*``,
    which the database marks C:0.0 because they are talent-gated; the recorded
    character had the talent and this one does not.
    """
    from dsor import effects

    laid = effects.parse_entries(effects.SKILL_LOCATIONS.get(skill_wire, ""))
    return [e.effect for e in laid if e.certain or not certain_only]


def for_skill(skill_wire: int, actor: int = 0) -> bytes | None:
    """A ``0x003E`` for everything *skill_wire* places on the ground, or None.

    None when the skill places nothing, which is most of them: of the warrior's
    skills only earthquake and defiance have a LocationEffects column at all, and they
    are exactly the two whose visuals were missing.

    The actor is 0, as in both recordings. The command carries no position -- neither
    the wire coordinates of the recording's caster (24453, -31744, 25595) nor their
    described form (191.0, -248.0, 200.0) appears anywhere in the recorded payload, in
    any byte order or bit alignment -- so the client places these from the skill use it
    sent itself. That is why this encoder has no position to get wrong.
    """
    names = placed_by(skill_wire)
    if not names:
        return None
    held = templates()
    if not held:
        return None
    entries = []
    for index, name in enumerate(names):
        template = held.get(name) or held.get("")
        if template is None:
            continue
        entries.append(
            Entry(
                index=index,
                name=name,
                shape=template.shape,
                flag=template.flag,
                geometry=list(template.geometry),
                trailing=list(template.trailing),
            )
        )
    if not entries:
        return None
    return encode(LocationEffect(LOCATION_EFFECT_LIST, entries=entries, actor=actor))
