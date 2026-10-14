"""Honor leaderboard queries backed by persisted and live character results."""

from dso_server.persistence.account_state import HonorRankingRecord
from dso_server.protocol.ranking_protocol import (
    FILTER_ALL, FILTER_FRIENDS, FILTER_GUILD, FILTER_PLAYER_PAGE,
    HONOR_LOCAL_USER_RANKING_INFO_COMMAND, HONOR_OVERALL_LOCAL_USER_RANKING_INFO_COMMAND,
    MAX_RANKING_PAGE_SIZE, UNRANKED,
    RankingRow, decode_ranking_request, encode_local_ranking, encode_ranking_page,
)
from dso_server.services.base import CharacterService


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

        records = self._honor_ranking_records(s)
        # Historical solo/team/daily/weekly results have not been recorded.
        # Return an empty board instead of mislabelling lifetime totals.
        if request.board != 4:
            records = ()
        local = request.command_id in {
            HONOR_LOCAL_USER_RANKING_INFO_COMMAND,
            HONOR_OVERALL_LOCAL_USER_RANKING_INFO_COMMAND,
        }
        class_id = int(character.class_id) if local else request.class_id
        if class_id != 4:
            records = tuple(row for row in records if row.class_id == class_id)
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
            records = tuple(row for row in records if row.character_id in allowed)

        column = ("honor_points", "wins", "losses", "draws", "kills", "deaths")[request.sort_column]
        ranked = sorted(records, key=lambda row: (-getattr(row, column, 0), row.character_id))
        rows = [(record.character_id, RankingRow(
            record.name, min(record.honor_points, 0xFFFFFFFF), rank,
            min(record.wins, 0xFFFFFFFF), min(record.losses, 0xFFFFFFFF), 0,
            min(record.kills, 0xFFFFFFFF), min(record.deaths, 0xFFFFFFFF),
        )) for rank, record in enumerate(ranked)]
        if local:
            own = next((row for identity, row in rows if identity == character_id), None)
            response = encode_local_ranking(request, own)
        else:
            count = min(request.count, MAX_RANKING_PAGE_SIZE)
            start = request.start
            if request.flags & FILTER_PLAYER_PAGE:
                index = next((i for i, (identity, _) in enumerate(rows)
                              if identity == character_id), 0)
                start = max(0, index - count // 2)
                start = min(start, max(0, len(rows) - count))
            elif start == UNRANKED:
                start = 0
            start = min(start, len(rows))
            response = encode_ranking_page(
                request, tuple(row for _, row in rows[start:start + count]),
                start=start, total=len(rows),
            )
        s.send_reliable_ordered(response, track=True, label="honor-ranking")
