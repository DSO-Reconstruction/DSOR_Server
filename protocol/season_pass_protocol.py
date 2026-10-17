"""Season Pass client requests confirmed against the release261 client."""

from __future__ import annotations

from dataclasses import dataclass

from dso_server.protocol.character_selection import BitReader
from dso_server.protocol.command_ids import command_id


GET_SEASON_PASS_REWARDS_INFO_COMMAND = command_id(
    "Commands::GetSeasonPassRewardsInfoCommand", 0x0161,
)
GET_SEASON_PASS_AWARD_COMMAND = command_id(
    "Commands::GetSeasonPassAwardCommand", 0x0163,
)
ADD_SEASON_PASS_EXP_COMMAND = command_id(
    "Commands::AddSeasonPassExpCommand", 0x0164,
)
GET_SEASON_PASS_MISSIONS_INFO_COMMAND = command_id(
    "Commands::GetSeasonPassMissionsInfoCommand", 0x0165,
)
SEASON_PASS_QUERY_COMMANDS = frozenset({
    GET_SEASON_PASS_REWARDS_INFO_COMMAND,
    GET_SEASON_PASS_MISSIONS_INFO_COMMAND,
})
SEASON_PASS_COMMANDS = frozenset({
    *SEASON_PASS_QUERY_COMMANDS,
    ADD_SEASON_PASS_EXP_COMMAND,
    GET_SEASON_PASS_AWARD_COMMAND,
})


@dataclass(frozen=True)
class SeasonPassQuery:
    command_id: int
    experience: int = 0
    award_ids: tuple[int, ...] = ()


def decode_season_pass_query(
    payload: bytes, bit_length: int | None = None,
) -> SeasonPassQuery:
    reader = BitReader(payload, bit_length)
    if reader.read_uint8() != 0x8B:
        raise ValueError("not a client Season Pass command")
    command = int.from_bytes(reader.read_bytes(2), "little")
    if command not in SEASON_PASS_COMMANDS:
        raise ValueError(f"unsupported Season Pass query 0x{command:04x}")
    experience = (
        reader.read_uint32_le()
        if command == ADD_SEASON_PASS_EXP_COMMAND else 0
    )
    award_ids = ()
    if command == GET_SEASON_PASS_AWARD_COMMAND:
        count = reader.read_uint32_le()
        if count > 512:
            raise ValueError("Season Pass claim contains too many reward IDs")
        award_ids = tuple(reader.read_uint32_le() for _ in range(count))
        if not award_ids or len(set(award_ids)) != len(award_ids):
            raise ValueError("Season Pass claim must contain unique reward IDs")
    if reader.remaining:
        raise ValueError(f"Season Pass query has {reader.remaining} unread bits")
    return SeasonPassQuery(command, experience, award_ids)
