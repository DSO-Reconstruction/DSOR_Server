"""DailyChallengeSelectionCommand: template ID followed by a refresh bit."""

from dataclasses import dataclass

from dso_server.protocol.character_selection import BitReader
from dso_server.protocol.command_ids import command_id


DAILY_CHALLENGE_SELECTION_COMMAND = command_id(
    "Commands::DailyChallengeSelectionCommand", 0x0094)


@dataclass(frozen=True)
class DailyChallengeSelection:
    challenge_id: str
    refresh: bool


def decode_daily_challenge_selection(payload: bytes, bit_length=None):
    reader = BitReader(payload, bit_length)
    if (reader.read_uint8() != 0x8b
            or int.from_bytes(reader.read_bytes(2), "little") != DAILY_CHALLENGE_SELECTION_COMMAND):
        raise ValueError("not a Daily Challenge selection")
    value = DailyChallengeSelection(reader.read_string(), reader.read_bool())
    # Byte-only callers may include the zero padding of the final bool byte.
    if bit_length is None and reader.remaining < 8:
        if reader.read_bits(reader.remaining):
            raise ValueError("nonzero Daily Challenge padding")
    if not value.challenge_id or reader.remaining:
        raise ValueError("invalid Daily Challenge selection length")
    return value
