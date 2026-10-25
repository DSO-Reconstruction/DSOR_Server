"""Honor leaderboard queries backed by persisted and live character results."""

from collections import OrderedDict
import threading
import time

from dso_server.persistence.account_state import HonorRankingRecord
from dso_server.protocol.ranking_protocol import (
    FILTER_ALL, FILTER_FRIENDS, FILTER_GUILD, FILTER_PLAYER_PAGE,
    HONOR_LOCAL_USER_RANKING_INFO_COMMAND, HONOR_OVERALL_LOCAL_USER_RANKING_INFO_COMMAND,
    MAX_RANKING_PAGE_SIZE, UNRANKED,
    RankingRow, decode_ranking_request, encode_local_ranking, encode_ranking_page,
)
from dso_server.services.base import CharacterService


RANKING_SNAPSHOT_SECONDS = 1.0
MAX_CACHED_RANKING_BOARDS = 8


class _RankingSnapshot:
    """Share bounded, briefly cached rankings across all page requests."""

    def __init__(self):
        self.lock = threading.Lock()
        self.expires_at = 0.0
        self.records = {}
        self.boards = OrderedDict()

    def board(self, loader, class_id, column, allowed=None):
        with self.lock:
            if time.monotonic() >= self.expires_at:
                records = loader()
                self.records = {row.character_id: row for row in records}
                self.boards.clear()
                self.expires_at = time.monotonic() + RANKING_SNAPSHOT_SECONDS
            key = (class_id, column)
            if allowed is None and key in self.boards:
                self.boards.move_to_end(key)
                return self.boards[key]
            candidates = (
                self.records.values() if allowed is None else
                (self.records[identity] for identity in allowed
                 if identity in self.records)
            )
            ranked = tuple(sorted(
                (row for row in candidates if class_id == 4 or row.class_id == class_id),
                key=lambda row: (-getattr(row, column, 0), row.character_id),
            ))
            result = ranked, {row.character_id: i for i, row in enumerate(ranked)}
            if allowed is None:
                self.boards[key] = result
                if len(self.boards) > MAX_CACHED_RANKING_BOARDS:
                    self.boards.popitem(last=False)
            return result


def _wire_row(record, rank):
    return RankingRow(
        record.name, min(record.honor_points, 0xFFFFFFFF), rank,
        min(record.wins, 0xFFFFFFFF), min(record.losses, 0xFFFFFFFF), 0,
        min(record.kills, 0xFFFFFFFF), min(record.deaths, 0xFFFFFFFF),
    )


class RankingService:
    """Serve career honor and all-time PvP results without client-written scores."""

    def _honor_ranking_records(self, requester):
        store = getattr(self, "account_store", None)
        records = {row.character_id: row for row in store.honor_ranking_records()} if store else {}
        # Include offline characters, then overlay scores awaiting persistence.
        # Take each session lock separately, outside the account-store lock.
        sessions = list(self._active_gameplay_sessions())
        if requester not in sessions:
            sessions.append(requester)
        for session in sessions:
            with session.flow_lock:
                character = session.gameplay_character
                if character is None:
                    continue
                records[int(character.character_id)] = HonorRankingRecord(
                    int(character.character_id), str(character.name), int(character.class_id),
                    max(0, int(session.gameplay_honor_points)),
                    max(0, int(session.gameplay_pvp_matches_won)),
                    max(0, int(session.gameplay_pvp_matches_lost)),
                    max(0, int(session.gameplay_pvp_kills)),
                    max(0, int(session.gameplay_pvp_deaths)),
                )
        return tuple(records.values())

    def _handle_ranking_request(self, s, payload: bytes) -> None:
        try:
            request = decode_ranking_request(payload, s.current_message_bit_length)
        except ValueError as exc:
            print(f"[DrasaOnlineMapServer] rejected ranking request: {exc}")
            return
        with s.flow_lock:
            character = s.gameplay_character
            if character is None:
                return
            character_id = int(character.character_id)
            s.gameplay_client_command_state[request.command_id] = request
        CharacterService._accept_gameplay_state_command(s, request.command_id)

        local = request.command_id in {
            HONOR_LOCAL_USER_RANKING_INFO_COMMAND,
            HONOR_OVERALL_LOCAL_USER_RANKING_INFO_COMMAND,
        }
        class_id = int(character.class_id) if local else request.class_id
        allowed = None
        if not local and not request.flags & FILTER_ALL:
            allowed = set()
            if request.flags & FILTER_FRIENDS:
                # Friend relationships are not yet persisted. Include self,
                # but do not substitute unrelated players for friends.
                allowed.add(character_id)
            social = getattr(self, "social_store", None)
            if request.flags & FILTER_GUILD and social is not None:
                guild = social.guild_for(character_id)
                if guild is not None:
                    allowed.update(member.character_id for member in guild.members)

        column = ("honor_points", "wins", "losses", "draws", "kills", "deaths")[request.sort_column]
        # Unrecorded periods need no roster scan at all. Real boards share a
        # one-second snapshot; pagination allocates only the requested rows.
        ranked, positions = (), {}
        if request.board == 4:
            snapshot = getattr(self, "_honor_ranking_snapshot", None)
            if snapshot is None:
                snapshot = self.__dict__.setdefault(
                    "_honor_ranking_snapshot", _RankingSnapshot()
                )
            ranked, positions = snapshot.board(
                lambda: self._honor_ranking_records(s), class_id, column, allowed,
            )
        if local:
            index = positions.get(character_id)
            own = None if index is None else _wire_row(ranked[index], index)
            response = encode_local_ranking(request, own)
        else:
            count = min(request.count, MAX_RANKING_PAGE_SIZE)
            start = request.start
            if request.flags & FILTER_PLAYER_PAGE:
                index = positions.get(character_id, 0)
                start = max(0, index - count // 2)
                start = min(start, max(0, len(ranked) - count))
            elif start == UNRANKED:
                start = 0
            start = min(start, len(ranked))
            response = encode_ranking_page(
                request, tuple(_wire_row(record, start + offset)
                               for offset, record in enumerate(ranked[start:start + count])),
                start=start, total=len(ranked),
            )
        s.send_reliable_ordered(response, track=True, label="honor-ranking")
