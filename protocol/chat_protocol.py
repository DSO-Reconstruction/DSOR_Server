"""Typed codecs for Drakensang's built-in RakNet chat service.

The chat connection uses the ordinary DSO service handshake on UDP/2191.
After ``ChatClient`` is accepted, chat control and message traffic is carried
by command ``0x04d2``.  Its body is one UTF-8 string prefixed by a little-
endian unsigned short; client packets use application id ``0x8b`` and server
packets use ``0x84``.
"""

from __future__ import annotations

from dataclasses import dataclass
import struct


CHAT_TEXT_COMMAND = 0x04D2
CHAT_CLIENT_SERVICE = "ChatClient"


def _read_string(payload: bytes, offset: int) -> tuple[str, int]:
    if offset + 2 > len(payload):
        raise ValueError("chat packet is missing its string length")
    length = int.from_bytes(payload[offset : offset + 2], "little")
    offset += 2
    end = offset + length
    if end > len(payload):
        raise ValueError("chat packet contains a truncated string")
    try:
        value = payload[offset:end].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("chat packet string is not valid UTF-8") from exc
    return value, end


def _write_string(value: str) -> bytes:
    encoded = value.encode("utf-8")
    if len(encoded) > 0xFFFF:
        raise ValueError("chat packet string exceeds 65535 UTF-8 bytes")
    return struct.pack("<H", len(encoded)) + encoded


@dataclass(frozen=True)
class ChatClientHello:
    service_name: str
    user_id: int
    session_id: bytes


def decode_chat_client_hello(payload: bytes) -> ChatClientHello:
    """Decode the client ``0x8a`` login sent after service discovery."""

    if not payload or payload[0] != 0x8A:
        raise ValueError("expected a chat client hello (0x8a)")
    service_name, offset = _read_string(payload, 1)
    if offset + 4 > len(payload):
        raise ValueError("chat client hello is missing its user id")
    user_id = int.from_bytes(payload[offset : offset + 4], "little")
    session_id, offset = _read_blob(payload, offset + 4)
    if offset != len(payload):
        raise ValueError("chat client hello has trailing bytes")
    return ChatClientHello(service_name, user_id, session_id)


def encode_chat_client_hello(value: ChatClientHello) -> bytes:
    if not 0 < value.user_id <= 0xFFFFFFFF:
        raise ValueError("chat user id must be a non-zero uint32")
    return (
        b"\x8a"
        + _write_string(value.service_name)
        + struct.pack("<I", value.user_id)
        + _write_blob(value.session_id)
    )


def _read_blob(payload: bytes, offset: int) -> tuple[bytes, int]:
    if offset + 2 > len(payload):
        raise ValueError("chat packet is missing its blob length")
    length = int.from_bytes(payload[offset : offset + 2], "little")
    offset += 2
    end = offset + length
    if end > len(payload):
        raise ValueError("chat packet contains a truncated blob")
    return payload[offset:end], end


def _write_blob(value: bytes) -> bytes:
    if len(value) > 0xFFFF:
        raise ValueError("chat packet blob exceeds 65535 bytes")
    return struct.pack("<H", len(value)) + value


def decode_chat_text(payload: bytes, *, from_client: bool) -> str:
    expected_message_id = 0x8B if from_client else 0x84
    if len(payload) < 5 or payload[0] != expected_message_id:
        raise ValueError(
            f"expected chat application id 0x{expected_message_id:02x}"
        )
    command_id = int.from_bytes(payload[1:3], "little")
    if command_id != CHAT_TEXT_COMMAND:
        raise ValueError(
            f"expected chat command 0x{CHAT_TEXT_COMMAND:04x}, "
            f"got 0x{command_id:04x}"
        )
    text, offset = _read_string(payload, 3)
    if offset != len(payload):
        raise ValueError("chat text command has trailing bytes")
    return text


def encode_chat_text(text: str, *, from_client: bool = False) -> bytes:
    message_id = 0x8B if from_client else 0x84
    return (
        bytes((message_id,))
        + CHAT_TEXT_COMMAND.to_bytes(2, "little")
        + _write_string(text)
    )


@dataclass(frozen=True)
class MapChatMessage:
    channel: str
    language: str
    user_type: str
    text: str


@dataclass(frozen=True)
class WhisperChatMessage:
    target_name: str
    user_type: str
    text: str


def decode_map_chat_message(command: str) -> MapChatMessage:
    """Decode a built-in chat ``sm`` command from the client.

    The third component is the language (for example ``en`` or ``bg``), and
    the fourth is the encoded user type (``ha`` for a normal retail user,
    ``hb`` for the local administrator character).  Neither value is a fixed
    protocol delimiter.
    """

    fields = command.split("#", 4)
    if len(fields) != 5 or fields[0] != "sm":
        raise ValueError("unsupported built-in chat message command")
    channel, language, user_type, text = fields[1:]
    if not channel or not language or not user_type:
        raise ValueError(
            "chat channel, language, and user type must be non-empty"
        )
    return MapChatMessage(channel, language, user_type, text)


def decode_whisper_chat_message(command: str) -> WhisperChatMessage:
    """Decode the built-in chat client's ``sw`` (send whisper) command."""

    fields = command.split("#", 3)
    if len(fields) != 4 or fields[0] != "sw":
        raise ValueError("unsupported built-in whisper command")
    target_name, user_type, text = fields[1:]
    if not target_name or not user_type:
        raise ValueError("whisper target and user type must be non-empty")
    return WhisperChatMessage(target_name, user_type, text)


def encode_received_map_chat(message: MapChatMessage, user_name: str) -> str:
    if "#" in user_name:
        raise ValueError("chat user name cannot contain '#'")
    return (
        f"rm#{message.channel}#{message.language}#{user_name}#"
        f"{message.user_type}#{message.text}"
    )


def encode_received_whisper(
    message: WhisperChatMessage, sender_name: str
) -> str:
    """Encode the server's ``rw`` (receive whisper) command.

    The tag and four-field layout come from ``BuiltinChatCodec`` in the game
    client. The client generates its own outgoing whisper loopback, so this is
    delivered only to the target character.
    """

    if "#" in sender_name:
        raise ValueError("chat user name cannot contain '#'")
    return f"rw#{sender_name}#{message.user_type}#{message.text}"
