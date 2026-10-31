"""Typed codec for ``CharacterGenerationCommand`` (command ``0x0086``).

The layout comes from the registered serializer in ``dro_client64.exe`` and
was verified against the complete create-character exchange in the retail
capture.  Two one-bit flags make the final fields byte-unaligned.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import struct

from dso_server.protocol.character_selection import (
    BitReader,
    BitWriter,
    CharacterSelectionError,
    IDENTITY_CAPTURE_TRANSFORM,
)
from dso_server.protocol.command_ids import command_id


# This class registers under a bare name rather than ``Commands::...``, which is
# why neither dump named it until the extractors stopped requiring a namespace.
# It is 134 on the current client and 143 on the legacy one, and it is what the
# Play button sends: with the wrong number the server never sees the request and
# the client waits on a reply that is not coming.
COMMAND_ID = command_id("CharacterGenerationCommand", 0x0086)
CLIENT_HEADER = b"\x8b" + struct.pack("<H", COMMAND_ID)
SERVER_HEADER = b"\x84" + struct.pack("<H", COMMAND_ID)
DEFAULT_GENERATION_REVISION = 1362


@dataclass(frozen=True)
class CharacterGeneration:
    operation: int
    deny_reason: int = 0
    class_id: int = 0
    gender: int = 0
    character_id: int = 0
    field_7c: int = 0
    hair_style: int = 0
    beard_style: int = 0
    body_style: int = 0
    body_stature: int = 0
    name: str = ""
    fields_84: tuple[int, int, int, int] = (0, 0, 0, 0)
    transform: tuple[float, ...] = IDENTITY_CAPTURE_TRANSFORM
    flag_a: bool = False
    flag_b: bool = False
    locale: str = ""
    tail_value: int = 0
    tail_bits: int = 16

    def validate(self) -> None:
        for label, value in (
            ("operation", self.operation),
            ("deny_reason", self.deny_reason),
            ("class_id", self.class_id),
            ("gender", self.gender),
            ("hair_style", self.hair_style),
            ("beard_style", self.beard_style),
            ("body_style", self.body_style),
            ("body_stature", self.body_stature),
        ):
            if not 0 <= int(value) <= 0xFF:
                raise CharacterSelectionError(f"{label} must fit in a byte")
        for label, value in (
            ("character_id", self.character_id),
            ("field_7c", self.field_7c),
            *(
                (f"fields_84[{index}]", entry)
                for index, entry in enumerate(self.fields_84)
            ),
        ):
            if not 0 <= int(value) <= 0xFFFFFFFF:
                raise CharacterSelectionError(f"{label} must fit in uint32")
        if len(self.fields_84) != 4:
            raise CharacterSelectionError("fields_84 must contain four values")
        if len(self.transform) != 16:
            raise CharacterSelectionError("transform must contain sixteen floats")
        if not 0 <= self.tail_bits <= 64:
            raise CharacterSelectionError("tail_bits is outside the serializer limit")
        if not 0 <= self.tail_value < (1 << self.tail_bits):
            raise CharacterSelectionError("tail_value does not fit in tail_bits")


def decode_character_generation(
    payload: bytes, bit_length: int | None = None
) -> CharacterGeneration:
    if len(payload) < 4 or payload[:3] not in (CLIENT_HEADER, SERVER_HEADER):
        raise CharacterSelectionError("not a CharacterGenerationCommand")
    # Client operations 1 and 3 both finish at bit offset 2 within their last
    # byte. Runtime dispatch supplies the byte-rounded payload, so recover the
    # six discarded padding bits when the RakNet bit length is unavailable.
    if bit_length is None:
        bit_length = len(payload) * 8
        if payload[0] == 0x8B and payload[3] in (1, 3):
            bit_length -= 6
    reader = BitReader(payload[3:], bit_length - 24)
    operation = reader.read_uint8()
    value = CharacterGeneration(
        operation=operation,
        deny_reason=reader.read_uint8(),
        class_id=reader.read_uint8(),
        gender=reader.read_uint8(),
        character_id=reader.read_uint32_le(),
        field_7c=reader.read_uint32_le(),
        hair_style=reader.read_uint8(),
        beard_style=reader.read_uint8(),
        body_style=reader.read_uint8(),
        body_stature=reader.read_uint8(),
        name=reader.read_string(),
        fields_84=tuple(reader.read_uint32_le() for _ in range(4)),
        transform=tuple(reader.read_float() for _ in range(16)),
        flag_a=reader.read_bool(),
        flag_b=reader.read_bool(),
        locale=reader.read_string(),
        tail_value=0,
        tail_bits=reader.remaining,
    )
    value = replace(value, tail_value=reader.read_bits(value.tail_bits))
    value.validate()
    return value


def encode_character_generation(
    value: CharacterGeneration, *, server_to_client: bool = True
) -> tuple[bytes, int]:
    value.validate()
    writer = BitWriter()
    writer.write_uint8(value.operation)
    writer.write_uint8(value.deny_reason)
    writer.write_uint8(value.class_id)
    writer.write_uint8(value.gender)
    writer.write_uint32_le(value.character_id)
    writer.write_uint32_le(value.field_7c)
    writer.write_uint8(value.hair_style)
    writer.write_uint8(value.beard_style)
    writer.write_uint8(value.body_style)
    writer.write_uint8(value.body_stature)
    writer.write_string(value.name)
    for entry in value.fields_84:
        writer.write_uint32_le(entry)
    for entry in value.transform:
        writer.write_float(entry)
    writer.write_bool(value.flag_a)
    writer.write_bool(value.flag_b)
    writer.write_string(value.locale)
    writer.write_bits(value.tail_value, value.tail_bits)
    body, body_bits = writer.finish()
    return (
        (SERVER_HEADER if server_to_client else CLIENT_HEADER) + body,
        24 + body_bits,
    )


def character_generation_editor_response(
    *, locale: str, preview_id: int, character=None, operation: int = 2
) -> CharacterGeneration:
    """Build a server editor response (op 2 create, op 9 alter).

    When ``character`` is supplied the same editor is preloaded for a
    character-alteration booking instead of a new character preview.
    """

    return CharacterGeneration(
        operation=operation,
        class_id=int(getattr(character, "class_id", 0)),
        gender=int(getattr(character, "gender", 0)),
        character_id=int(getattr(character, "character_id", 0)),
        field_7c=max(1, int(preview_id)),
        hair_style=int(getattr(character, "hair_style", 18)),
        beard_style=int(getattr(character, "beard_style", 8)),
        body_style=int(getattr(character, "body_style", 0)),
        body_stature=int(getattr(character, "body_stature", 0)),
        name=str(getattr(character, "name", "")),
        fields_84=(0, 0, 0, DEFAULT_GENERATION_REVISION),
        transform=IDENTITY_CAPTURE_TRANSFORM,
        locale=str(locale),
        tail_bits=48,
    )


def character_generation_result(
    request: CharacterGeneration,
    *,
    operation: int,
    character_id: int = 0,
    deny_reason: int = 0,
) -> CharacterGeneration:
    """Return an editor denial or a create/edit success response."""

    if operation not in (4, 5, 11):
        raise ValueError(
            "character generation result must be operation 4, 5 or 11"
        )
    return replace(
        request,
        operation=operation,
        deny_reason=deny_reason,
        character_id=character_id,
        field_7c=0,
        locale="",
        tail_value=0,
        tail_bits=48,
    )
