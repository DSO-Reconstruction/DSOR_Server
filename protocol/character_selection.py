"""Bit-exact codec and typed model for CharacterSelectionCommand (0x0087).

The command is not encrypted.  It is a little-endian structure written to a
RakNet bit stream, and two booleans make most of the command byte-unaligned.
This module deliberately preserves fields whose gameplay meaning is not yet
known while exposing the fields confirmed by captures and ``static.db4``.
"""

from __future__ import annotations

import os
import struct
from dataclasses import asdict, dataclass, field
from typing import Any

from dso_server.protocol.command_ids import command_id


PACKET_ID = 0x84
# Command id used by the current client wire layout.
CAPTURED_COMMAND_ID = 0x0087
# What goes on the wire, resolved by class: 0x0087 is
# CharacterSelectionCommand only on the current client. The legacy build
# registers this class at 144 and puts GroupXPChangedCommand at 135, so sending
# a hard-coded 0x0087 there delivers the selection as a group-XP update, which
# is exactly what that client reports:
#   "Received GroupXPChangedCommand with invalid actor id (527438)!"
# The id it prints is really the head of the character list, read as another
# command's body.
COMMAND_ID = command_id(
    "Commands::CharacterSelectionCommand", CAPTURED_COMMAND_ID
)
APP_HEADER = bytes((PACKET_ID,)) + struct.pack("<H", COMMAND_ID)
# Decoding has to accept the capture as well as our own output: the bundled
# reference payload was recorded against the current client, so it always starts
# with CAPTURED_COMMAND_ID no matter which build this server is serving.
CAPTURED_APP_HEADER = bytes((PACKET_ID,)) + struct.pack(
    "<H", CAPTURED_COMMAND_ID
)
DECODABLE_APP_HEADERS = (APP_HEADER, CAPTURED_APP_HEADER)
# Which build's CharacterSelectionCommand record to write. The two differ in the
# progression field: this writes 64 bits, and the legacy client's decoder
# (0x00a1c7fd) reads six 32-bit fields across character_id, unknown_u32_a,
# premium_currency, progression, progression_u32 and level -- so 32 bits fewer.
# Everything around it matches, including the 16-float transform, which that
# decoder reads as four vector4 reads of four floats each.
#
# The difference accumulates per character, so the client reaches the equipment
# count 32 bits early for every character in the list and reads it out of the
# middle of the array:
#   CharacterSelectionCommand::Decode() read invalid equipment amount (-67108352)
SELECTION_WIRE = os.getenv("DSO_SELECTION_WIRE", "current").strip().lower()
CAPTURED_PROGRESSION_BITS = 64
SELECTION_PROGRESSION_BITS = 32 if SELECTION_WIRE == "legacy" else 64
# The equipment record differs the same way. This build reads two strings there
# -- 0xc4a959, which wraps the string reader -- then the tint and the enabled
# flag. The current build writes three: item, appearance and *effect*. The third
# is the weapon-effect id, and weapon effects are exactly the feature this
# client predates, which is also why its tooltip has no
# ItemValueWeaponEffectsLabelTemplate.
CAPTURED_EQUIPMENT_STRINGS = 3
SELECTION_EQUIPMENT_STRINGS = 2 if SELECTION_WIRE == "legacy" else 3
CLASS_IDS = {
    "warrior": 0,
    "mage": 1,
    "ranger": 2,
    "dwarf": 3,
    "niwalk": 4,
}
CLASS_NAMES = {value: key for key, value in CLASS_IDS.items()}

# Retail character ids visible in captures are input fixtures, not identities
# owned by the localhost service.  Locally configured and newly created
# characters live in an explicit server-controlled namespace.
LOCAL_CHARACTER_ID_BASE = 200_000_001

# Client operation 6 of CharacterSelectionCommand is the selection screen's
# delete button (Messaging::DeleteCharacter): ``u8 6, u8 0, u32 character
# id`` and zero padding, 32 bytes in all.  The server confirms with
# PlayerDeletedCommand, an actorless command whose decoder reads one u32.
SELECTION_OPERATION_DELETE = 0x06
PLAYER_DELETED_COMMAND = command_id("Commands::PlayerDeletedCommand", 0x0010)


def encode_player_deleted(character_id: int) -> tuple[bytes, int]:
    """PlayerDeletedCommand (server → client): the deleted character's id."""

    packet = bytes((PACKET_ID,)) + struct.pack(
        "<HI", PLAYER_DELETED_COMMAND, int(character_id) & 0xFFFFFFFF
    )
    return packet, len(packet) * 8

# Numeric ids carried by SkillBookInfo are zero-based indexes into the client's
# loaded SkillManager array, not the string ids used by quick slots. SQLite
# rowids are one-based, so the runtime id is ``rowid - 1``. This was verified
# directly against the running client: manager index 1890 resolves database
# row 1891 (TurretCombat), 1891 resolves row 1892 (TurretTesla), and so on.
# The current live service uses a newer content table, so its captured numeric
# range must not be copied into this older local client.
CLIENT_SKILL_MANAGER_ROW_OFFSET = -1

# QuickSlotsInfo uses _Template_Skill.Id strings, but it represents saved bar
# positions rather than the complete spell book above.  These are only
# new-character fallbacks; an existing character's actual positions belong to
# CharacterRecord.quickbar_skill_slots and may contain empty slots.
NEW_CHARACTER_QUICKBAR_SKILL_SLOTS = {
    CLASS_IDS["warrior"]: (
        "angrystrike", "mightyswing", "enragingleap", "stuncharge",
        "mightybash", "bloody360", "mighty360", "battlecry",
    ),
    CLASS_IDS["mage"]: (
        "magicmissile", "iceball", "chainlightning", "fireball",
        "teleport", "icecharge", "lightningstrike", "frozenorb",
    ),
    CLASS_IDS["ranger"]: (
        "markshot", "stab", "jump", "whirlwind", "adrenalin",
        "mightyshot", "hornetcloud", "thorntrail",
    ),
    CLASS_IDS["dwarf"]: (
        "SimpleShot", "Flamethrower", "SteamConductor", "HeavyShot",
        "HoverJump", "Grenade", "TurretCombat", "PositionBombShot",
    ),
    CLASS_IDS["niwalk"]: (),
}


class CharacterSelectionError(ValueError):
    """Raised when a character-selection packet is invalid."""


class BitReader:
    def __init__(self, data: bytes, bit_length: int | None = None) -> None:
        self.data = data
        self.bit_length = len(data) * 8 if bit_length is None else bit_length
        if self.bit_length < 0 or (self.bit_length + 7) // 8 != len(data):
            raise CharacterSelectionError(
                f"bit length {self.bit_length} does not match {len(data)} bytes"
            )
        self.position = 0

    @property
    def remaining(self) -> int:
        return self.bit_length - self.position

    def read_bits(self, count: int) -> int:
        if count < 0 or count > self.remaining:
            raise CharacterSelectionError(
                f"bit-stream overrun at bit {self.position}: "
                f"need {count}, have {self.remaining}"
            )
        result = 0
        for _ in range(count):
            byte = self.data[self.position >> 3]
            bit = (byte >> (7 - (self.position & 7))) & 1
            result = (result << 1) | bit
            self.position += 1
        return result

    def read_bool(self) -> bool:
        return bool(self.read_bits(1))

    def read_bytes(self, count: int) -> bytes:
        return bytes(self.read_bits(8) for _ in range(count))

    def read_uint8(self) -> int:
        return self.read_bits(8)

    def read_uint32_le(self) -> int:
        return struct.unpack("<I", self.read_bytes(4))[0]

    def read_uint64_le(self) -> int:
        return struct.unpack("<Q", self.read_bytes(8))[0]

    def read_float(self) -> float:
        return struct.unpack("<f", self.read_bytes(4))[0]

    def read_string(self) -> str:
        size = struct.unpack("<H", self.read_bytes(2))[0]
        try:
            return self.read_bytes(size).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CharacterSelectionError(
                f"invalid UTF-8 string at bit {self.position - size * 8}"
            ) from exc


class BitWriter:
    def __init__(self) -> None:
        self.data = bytearray()
        self.position = 0

    def write_bits(self, value: int, count: int) -> None:
        if count < 0 or value < 0 or value >= (1 << count):
            raise CharacterSelectionError(
                f"value {value!r} does not fit in {count} bits"
            )
        for shift in range(count - 1, -1, -1):
            byte_index = self.position >> 3
            if byte_index == len(self.data):
                self.data.append(0)
            if (value >> shift) & 1:
                self.data[byte_index] |= 1 << (7 - (self.position & 7))
            self.position += 1

    def write_bool(self, value: bool) -> None:
        self.write_bits(1 if value else 0, 1)

    def write_bytes(self, value: bytes) -> None:
        """Append bytes without routing every byte through eight bit writes.

        Most game commands contain many little-endian integers while the
        surrounding command can start at an arbitrary bit offset.  The old
        implementation handled those fields correctly, but visited every bit
        in Python.  Combat fan-out magnified that cost once per nearby viewer.

        At a byte boundary this is a direct append.  Otherwise, merge each
        source byte into the unused low bits of the current output byte and
        carry the remainder into one new byte.  This preserves the existing
        MSB-first wire layout exactly while making the common integer/string
        path linear in bytes rather than bits.
        """

        if not value:
            return
        bit_offset = self.position & 7
        if bit_offset == 0:
            self.data.extend(value)
            self.position += len(value) * 8
            return

        carry_shift = 8 - bit_offset
        for byte in value:
            self.data[-1] |= byte >> bit_offset
            self.data.append((byte << carry_shift) & 0xFF)
        self.position += len(value) * 8

    def write_uint8(self, value: int) -> None:
        self.write_bytes(bytes((_unsigned(value, 8, "uint8"),)))

    def write_uint32_le(self, value: int) -> None:
        self.write_bytes(struct.pack("<I", _unsigned(value, 32, "uint32")))

    def write_uint64_le(self, value: int) -> None:
        self.write_bytes(struct.pack("<Q", _unsigned(value, 64, "uint64")))

    def write_float(self, value: float) -> None:
        self.write_bytes(struct.pack("<f", float(value)))

    def write_string(self, value: str) -> None:
        encoded = value.encode("utf-8")
        if len(encoded) > 0xFFFF:
            raise CharacterSelectionError("UTF-8 string exceeds 65535 bytes")
        self.write_bytes(struct.pack("<H", len(encoded)))
        self.write_bytes(encoded)

    def finish(self) -> tuple[bytes, int]:
        return bytes(self.data), self.position


def _unsigned(value: int, bits: int, label: str) -> int:
    value = int(value)
    if value < 0 or value >= 1 << bits:
        raise CharacterSelectionError(f"{label} value {value} is out of range")
    return value


IDENTITY_CAPTURE_TRANSFORM = (
    -0.9999995231628418,
    6.894724435824173e-08,
    -4.235164736271502e-22,
    0.0,
    6.894724435824173e-08,
    1.0,
    6.894724435824173e-08,
    0.0,
    4.7537238122947415e-15,
    6.894724435824173e-08,
    -0.9999995231628418,
    0.0,
    0.0,
    0.0,
    0.0,
    1.0,
)


@dataclass
class CharacterRecord:
    name: str
    map_id: str
    class_id: int
    gender: int
    hair_style: int
    beard_style: int
    body_style: int
    body_stature: int
    character_id: int
    unknown_u32_a: int
    premium_currency: int
    progression_u64: int
    progression_u32: int
    level: int
    transform: tuple[float, ...] = IDENTITY_CAPTURE_TRANSFORM
    flag_a: bool = False
    flag_b: bool = False
    # NewPlayer +0x21. The client stores this at GamePlayer +0x689 and checks
    # it before enabling administrator-only input (including hub overrides).
    # It is deliberately separate from the overhead-name color selector.
    admin_privileges: bool = False
    # NewPlayer +0x22. This is gameplay-only and is deliberately not encoded
    # into CharacterSelectionCommand. The client maps it to OverheadAdminColor.
    admin_name_color: bool = False
    # The first eight positions of QuickSlotsInfo's active bar. None means the
    # character has no persisted bar and should receive the class fallback;
    # individual None entries are intentionally empty saved positions.
    quickbar_skill_slots: tuple[str | None, ...] | None = None

    @property
    def class_name(self) -> str:
        return CLASS_NAMES.get(self.class_id, f"unknown-{self.class_id}")

    # Compatibility with the provisional name used before the field was
    # correlated with the currency display in the character-selection UI.
    @property
    def account_scope(self) -> int:
        return self.premium_currency

    @account_scope.setter
    def account_scope(self, value: int) -> None:
        self.premium_currency = int(value)

    def validate(self) -> None:
        if not self.name:
            raise CharacterSelectionError("character name cannot be empty")
        if not self.map_id:
            raise CharacterSelectionError(f"{self.name}: map_id cannot be empty")
        _unsigned(self.class_id, 32, "class_id")
        _unsigned(self.gender, 32, "gender")
        for label, value in (
            ("hair_style", self.hair_style),
            ("beard_style", self.beard_style),
            ("body_style", self.body_style),
            ("body_stature", self.body_stature),
        ):
            _unsigned(value, 8, label)
        for label, value in (
            ("character_id", self.character_id),
            ("unknown_u32_a", self.unknown_u32_a),
            ("premium_currency", self.premium_currency),
            ("progression_u32", self.progression_u32),
            ("level", self.level),
        ):
            _unsigned(value, 32, label)
        _unsigned(self.progression_u64, 64, "progression_u64")
        if len(self.transform) != 16:
            raise CharacterSelectionError(
                f"{self.name}: transform must contain exactly 16 floats"
            )


@dataclass
class EquipmentRecord:
    item_id: str
    appearance_id: str = ""
    effect_id: str = ""
    tint: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
    enabled: bool = True

    def validate(self) -> None:
        if len(self.tint) != 4:
            raise CharacterSelectionError(
                f"{self.item_id}: equipment tint must contain four floats"
            )


@dataclass
class CharacterSelection:
    operation: int = 1
    deny_reason: int = 0
    active_character_id: int = 0
    header_field_b: int = 0
    character_slots: int = 4
    header_flag: bool = False
    header_field_d: int = 0
    locale: str = "EN"
    characters: list[CharacterRecord] = field(default_factory=list)
    equipment: list[EquipmentRecord] = field(default_factory=list)

    @property
    def character_alteration_bookings(self) -> int:
        """Account change orders carried in the selection header field."""
        return self.header_field_d

    @character_alteration_bookings.setter
    def character_alteration_bookings(self, value: int) -> None:
        self.header_field_d = int(value)

    def validate(self) -> None:
        _unsigned(self.operation, 8, "operation")
        _unsigned(self.deny_reason, 8, "deny_reason")
        for label, value in (
            ("active_character_id", self.active_character_id),
            ("header_field_b", self.header_field_b),
            ("character_slots", self.character_slots),
            ("header_field_d", self.header_field_d),
        ):
            _unsigned(value, 32, label)
        if len(self.characters) > 100:
            raise CharacterSelectionError("client accepts at most 100 characters")
        ids = [character.character_id for character in self.characters]
        if len(ids) != len(set(ids)):
            raise CharacterSelectionError("character_id values must be unique")
        if self.characters and self.active_character_id not in ids:
            raise CharacterSelectionError(
                "active_character_id must identify a serialized character"
            )
        for character in self.characters:
            character.validate()
        for equipment in self.equipment:
            equipment.validate()

    @property
    def active_character(self) -> CharacterRecord | None:
        return next(
            (
                character
                for character in self.characters
                if character.character_id == self.active_character_id
            ),
            self.characters[0] if self.characters else None,
        )

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        for character, encoded in zip(self.characters, result["characters"]):
            encoded["class_name"] = character.class_name
        return result


def decode_character_selection(
    packet: bytes,
    bit_length: int | None = None,
    *,
    progression_bits: int = CAPTURED_PROGRESSION_BITS,
    equipment_strings: int = CAPTURED_EQUIPMENT_STRINGS,
) -> CharacterSelection:
    """Parse a CharacterSelectionCommand packet.

    The two shape arguments default to the captured payload's layout, not to
    whatever this server is currently writing: the bundled capture was recorded
    against the current client and has to stay readable while the legacy build
    is being served. Pass ``progression_bits=32, equipment_strings=2`` to read a
    packet this server produced for that build.
    """
    if not any(packet.startswith(header) for header in DECODABLE_APP_HEADERS):
        expected = " or ".join(
            header.hex() for header in dict.fromkeys(DECODABLE_APP_HEADERS)
        )
        raise CharacterSelectionError(
            f"expected application header {expected}, got {packet[:3].hex()}"
        )
    if bit_length is None:
        bit_length = len(packet) * 8
    if bit_length < 24:
        raise CharacterSelectionError("packet is shorter than its application header")

    reader = BitReader(packet[3:], bit_length - 24)
    operation = reader.read_uint8()
    deny_reason = reader.read_uint8()
    active_character_id = reader.read_uint32_le()
    header_field_b = reader.read_uint32_le()
    character_slots = reader.read_uint32_le()
    header_flag = reader.read_bool()
    header_field_d = reader.read_uint32_le()
    locale = reader.read_string()

    character_count = reader.read_uint32_le()
    if character_count > 100:
        raise CharacterSelectionError(
            f"invalid character count {character_count}; client limit is 100"
        )
    characters = []
    for _ in range(character_count):
        name = reader.read_string()
        map_id = reader.read_string()
        class_id = reader.read_uint32_le()
        gender = reader.read_uint32_le()
        styles = [reader.read_uint8() for _ in range(4)]
        character_id = reader.read_uint32_le()
        unknown_u32_a = reader.read_uint32_le()
        premium_currency = reader.read_uint32_le()
        if progression_bits == 32:
            progression_u64 = reader.read_uint32_le()
        else:
            progression_u64 = reader.read_uint64_le()
        progression_u32 = reader.read_uint32_le()
        level = reader.read_uint32_le()
        transform = tuple(reader.read_float() for _ in range(16))
        flag_a = reader.read_bool()
        flag_b = reader.read_bool()
        characters.append(
            CharacterRecord(
                name=name,
                map_id=map_id,
                class_id=class_id,
                gender=gender,
                hair_style=styles[0],
                beard_style=styles[1],
                body_style=styles[2],
                body_stature=styles[3],
                character_id=character_id,
                unknown_u32_a=unknown_u32_a,
                premium_currency=premium_currency,
                progression_u64=progression_u64,
                progression_u32=progression_u32,
                level=level,
                transform=transform,
                flag_a=flag_a,
                flag_b=flag_b,
            )
        )

    equipment_count = reader.read_uint32_le()
    equipment = []
    for _ in range(equipment_count):
        equipment.append(
            EquipmentRecord(
                item_id=reader.read_string(),
                appearance_id=reader.read_string(),
                effect_id=(
                    reader.read_string() if equipment_strings == 3 else ""
                ),
                tint=tuple(reader.read_float() for _ in range(4)),
                enabled=reader.read_bool(),
            )
        )

    if reader.remaining:
        raise CharacterSelectionError(
            f"decoded command has {reader.remaining} unexpected meaningful bits"
        )
    model = CharacterSelection(
        operation=operation,
        deny_reason=deny_reason,
        active_character_id=active_character_id,
        header_field_b=header_field_b,
        character_slots=character_slots,
        header_flag=header_flag,
        header_field_d=header_field_d,
        locale=locale,
        characters=characters,
        equipment=equipment,
    )
    model.validate()
    return model


def encode_character_selection(model: CharacterSelection) -> tuple[bytes, int]:
    model.validate()
    writer = BitWriter()
    writer.write_uint8(model.operation)
    writer.write_uint8(model.deny_reason)
    writer.write_uint32_le(model.active_character_id)
    writer.write_uint32_le(model.header_field_b)
    writer.write_uint32_le(model.character_slots)
    writer.write_bool(model.header_flag)
    writer.write_uint32_le(model.header_field_d)
    writer.write_string(model.locale)
    writer.write_uint32_le(len(model.characters))

    for character in model.characters:
        writer.write_string(character.name)
        writer.write_string(character.map_id)
        writer.write_uint32_le(character.class_id)
        writer.write_uint32_le(character.gender)
        writer.write_uint8(character.hair_style)
        writer.write_uint8(character.beard_style)
        writer.write_uint8(character.body_style)
        writer.write_uint8(character.body_stature)
        writer.write_uint32_le(character.character_id)
        writer.write_uint32_le(character.unknown_u32_a)
        writer.write_uint32_le(character.premium_currency)
        if SELECTION_PROGRESSION_BITS == 32:
            writer.write_uint32_le(character.progression_u64 & 0xFFFFFFFF)
        else:
            writer.write_uint64_le(character.progression_u64)
        writer.write_uint32_le(character.progression_u32)
        writer.write_uint32_le(character.level)
        for value in character.transform:
            writer.write_float(value)
        writer.write_bool(character.flag_a)
        writer.write_bool(character.flag_b)

    writer.write_uint32_le(len(model.equipment))
    for equipment in model.equipment:
        writer.write_string(equipment.item_id)
        writer.write_string(equipment.appearance_id)
        if SELECTION_EQUIPMENT_STRINGS == 3:
            writer.write_string(equipment.effect_id)
        for value in equipment.tint:
            writer.write_float(value)
        writer.write_bool(equipment.enabled)

    body, body_bits = writer.finish()
    return APP_HEADER + body, 24 + body_bits
