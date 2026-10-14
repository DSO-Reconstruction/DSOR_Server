"""Queue and battleground state for PvP matchmaking.

Pure domain state: no sockets, no packet encoding.  ``services/matchmaking.py``
owns the transport side and calls into here for every decision about who is
matched with whom, when a round ends, and what a match is worth.

Timings and clocks follow the recorded retail 1v1:

* Match clocks are 25 Hz ticks.  Retail's three rounds began at ticks 329, 730
  and 1084 -- gaps of 401 and 354 ticks against wall-clock gaps of 16.04 s and
  14.16 s.
* Every ``PVPMatchInfo`` carried a 3000-tick round duration, i.e. two minutes.
* ``PVPMatchCountdown`` preceded each ``PVPMatchStart`` by four seconds, in all
  three rounds of both recorded sessions.
* Roughly three seconds separated a decided round from the next countdown, and
  the same again from the last round ending to ``PVPMatchOver``.
"""

from __future__ import annotations

import itertools
import threading
import time
from dataclasses import dataclass, field

from dso_server.protocol.pvp_match_protocol import (
    BATTLEGROUND_IDS,
    BATTLEGROUND_MAPS,
    MATCH_MODE_BEST_OF_3_1V1,
    MATCH_ROUNDS_TO_WIN,
    MATCH_TEAM_SIZES,
    MATCH_TICK_HZ,
    ROUND_DURATION_TICKS,
)


MATCH_ACCEPT_SECONDS = 30.0
MATCH_COUNTDOWN_SECONDS = 4.0
MATCH_LOAD_TIMEOUT_SECONDS = 60.0
ROUND_INTERMISSION_SECONDS = 3.0
MATCH_OVER_DELAY_SECONDS = 3.0
MATCH_INFO_INTERVAL_SECONDS = 2.0

# Honor awarded for one match.  Retail's own numbers cannot be recovered from
# a single recorded match -- its scoreboard showed a participation figure that
# stepped 110 -> 275 and a round-win figure that stepped 27 -> 68 -- so these
# are the server's own schedule, shaped to land in the same range.
HONOR_PER_ROUND_PLAYED = 55
HONOR_PER_ROUND_WON = 27
HONOR_PER_KILL = 5
HONOR_MATCH_WIN_BONUS = 110


class MatchPhase:
    """Lifecycle of one battleground instance."""

    INVITING = "inviting"
    LOADING = "loading"
    COUNTDOWN = "countdown"
    RUNNING = "running"
    INTERMISSION = "intermission"
    OVER = "over"
    CANCELLED = "cancelled"


@dataclass
class QueueEntry:
    """One player waiting for a match."""

    character_id: int
    account_id: int | None
    match_mode: int
    queue_kind: int
    joined_at: float = field(default_factory=time.perf_counter)
    session: object | None = None


@dataclass
class MatchParticipant:
    """One player's slot and running score inside a battleground."""

    character_id: int
    account_id: int | None
    name: str
    level: int
    team: int
    session: object | None = None
    accepted: bool = False
    declined: bool = False
    arrived: bool = False
    left: bool = False
    # The actor id peers know this player by in the arena, captured on
    # arrival. The scoreboard still has to name a departed player, and by then
    # the session that owned the id is gone.
    actor_id: int = 0
    # The current round's start, in *this client's* tick base. The client
    # counts the round down against its own clock, which the server only
    # learns by echo, so the value it is given has to be in that base and has
    # to stay fixed for the round or the countdown jumps.
    client_round_start_tick: int = 0
    # Captured career-honor metadata for the scoreboard's rating field.
    # The actual match award is sent separately in MatchPlayer.honor_total.
    honor_at_start: int = 0
    kills: int = 0
    deaths: int = 0
    damage_dealt: int = 0
    rounds_won: int = 0
    rounds_lost: int = 0

    @property
    def in_match(self) -> bool:
        return self.arrived and not self.left

    def honor_earned(self, rounds_played: int, *, match_won: bool) -> int:
        """Honor this row has banked so far.

        Called both during the match, to fill the live scoreboard, and once at
        the end to credit the account.
        """

        total = HONOR_PER_ROUND_PLAYED * max(0, int(rounds_played))
        total += HONOR_PER_ROUND_WON * int(self.rounds_won)
        total += HONOR_PER_KILL * int(self.kills)
        if match_won:
            total += HONOR_MATCH_WIN_BONUS
        return total


_MATCH_IDS = itertools.count(1)


class PVPMatch:
    """One battleground instance shared by every participating session.

    Instances are mutated from timer threads and from combat resolution, so
    every field that a caller reads across more than one statement is read
    under ``lock``.
    """

    def __init__(self, match_mode: int,
                 participants: tuple[MatchParticipant, ...],
                 *, match_id: int | None = None,
                 team_count: int = 2) -> None:
        self.lock = threading.RLock()
        self.match_id = int(
            match_id if match_id is not None else next(_MATCH_IDS)
        )
        self.match_mode = int(match_mode)
        self.battleground_id = BATTLEGROUND_IDS.get(
            self.match_mode, BATTLEGROUND_IDS[MATCH_MODE_BEST_OF_3_1V1]
        )
        self.map_id = BATTLEGROUND_MAPS.get(
            self.match_mode, BATTLEGROUND_MAPS[MATCH_MODE_BEST_OF_3_1V1]
        )
        self.rounds_to_win = MATCH_ROUNDS_TO_WIN.get(self.match_mode, 2)
        self.team_count = max(1, int(team_count))
        self.participants = tuple(participants)
        self.phase = MatchPhase.INVITING
        self.created_at = time.perf_counter()
        self.round_index = 0
        self.round_start_tick = 0
        self.team_rounds_won = [0] * self.team_count
        self.winning_team = 0
        self.timers: dict[str, object] = {}
        self.info_generation = 0
        # Completion can be reached from the round timer or from the last
        # participant leaving, so the payout is claimed once.
        self.settled = False

    def claim_settlement(self) -> bool:
        """Claim the one-shot end-of-match payout for this match."""

        with self.lock:
            if self.settled:
                return False
            self.settled = True
            return True

    # -- clocks ------------------------------------------------------------

    def tick(self, now: float | None = None) -> int:
        """Match clock in 25 Hz ticks since the battleground was created."""

        moment = time.perf_counter() if now is None else float(now)
        return max(0, int((moment - self.created_at) * MATCH_TICK_HZ))

    @property
    def round_duration_ticks(self) -> int:
        return ROUND_DURATION_TICKS

    def round_deadline_seconds(self) -> float:
        """Wall-clock seconds left before the current round times out."""

        elapsed = self.tick() - self.round_start_tick
        return max(0.0, (ROUND_DURATION_TICKS - elapsed) / MATCH_TICK_HZ)

    # -- roster ------------------------------------------------------------

    def participant(self, character_id: int) -> MatchParticipant | None:
        character_id = int(character_id)
        for entry in self.participants:
            if entry.character_id == character_id:
                return entry
        return None

    def team_members(self, team: int) -> tuple[MatchParticipant, ...]:
        return tuple(e for e in self.participants if e.team == int(team))

    def live_sessions(self) -> tuple[object, ...]:
        return tuple(
            entry.session
            for entry in self.participants
            if entry.session is not None and not entry.left
        )

    def everyone_accepted(self) -> bool:
        return all(e.accepted for e in self.participants)

    def anyone_declined(self) -> bool:
        return any(e.declined for e in self.participants)

    def everyone_arrived(self) -> bool:
        return all(e.arrived for e in self.participants if not e.left)

    def active_teams(self) -> tuple[int, ...]:
        return tuple(sorted({
            e.team for e in self.participants if e.in_match
        }))

    # -- scoring -----------------------------------------------------------

    def record_damage(self, attacker_id: int, amount: int) -> None:
        attacker = self.participant(attacker_id)
        if attacker is None or amount <= 0:
            return
        with self.lock:
            attacker.damage_dealt += int(amount)

    def record_kill(self, killer_id: int, victim_id: int) -> bool:
        """Credit one kill. Return whether it decided the current round.

        A round is decided when only one team still has a living member,
        which for the 1v1 best-of-three is simply the first kill.
        """

        with self.lock:
            if self.phase != MatchPhase.RUNNING:
                return False
            victim = self.participant(victim_id)
            if victim is None or not victim.in_match:
                return False
            victim.deaths += 1
            killer = self.participant(killer_id)
            if killer is not None and killer is not victim:
                killer.kills += 1
            return self._round_is_decided_locked(dead={int(victim_id)})

    def _round_is_decided_locked(self, *, dead: set[int]) -> bool:
        surviving = {
            entry.team
            for entry in self.participants
            if entry.in_match and entry.character_id not in dead
        }
        return len(surviving) <= 1

    def award_round(self, winning_team: int) -> bool:
        """Close the current round. Return whether the match is now over."""

        with self.lock:
            winning_team = int(winning_team)
            if 0 <= winning_team < self.team_count:
                self.team_rounds_won[winning_team] += 1
            for entry in self.participants:
                if entry.team == winning_team:
                    entry.rounds_won += 1
                else:
                    entry.rounds_lost += 1
            self.round_index += 1
            if self.team_rounds_won[winning_team] >= self.rounds_to_win:
                self.winning_team = winning_team
                self.phase = MatchPhase.OVER
                return True
            self.phase = MatchPhase.INTERMISSION
            return False

    def forfeit(self, leaving_character_id: int) -> bool:
        """Mark a player gone. Return whether that ends the match."""

        with self.lock:
            entry = self.participant(leaving_character_id)
            if entry is not None:
                entry.left = True
                entry.session = None
            if self.phase in (MatchPhase.OVER, MatchPhase.CANCELLED):
                return False
            remaining = self.active_teams()
            if len(remaining) == 1:
                self.winning_team = remaining[0]
                self.phase = MatchPhase.OVER
                return True
            if not remaining:
                # Everyone is gone. There is no winner to pay, so the match is
                # abandoned rather than settled.
                self.phase = MatchPhase.CANCELLED
            return False

    def rounds_played(self) -> int:
        with self.lock:
            if self.phase in (MatchPhase.RUNNING, MatchPhase.COUNTDOWN):
                return self.round_index + 1
            return self.round_index

    def win_chances(self) -> tuple[float, ...]:
        """Per-team win probability for the scoreboard's float field.

        Retail filled it with two values that summed to one.  Without a rating
        system to weigh the sides, split it by rounds already won so the bar
        tracks the visible scoreline, falling back to an even split.
        """

        with self.lock:
            total = sum(self.team_rounds_won)
            if total <= 0:
                even = 1.0 / self.team_count
                return tuple(even for _ in range(self.team_count))
            return tuple(won / total for won in self.team_rounds_won)


class MatchmakingRegistry:
    """Server-wide queue and the battlegrounds it has produced."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.queue: list[QueueEntry] = []
        self.matches: dict[int, PVPMatch] = {}

    # -- queue -------------------------------------------------------------

    def enqueue(self, entry: QueueEntry) -> QueueEntry:
        with self.lock:
            self._remove_character_locked(entry.character_id)
            self.queue.append(entry)
            return entry

    def dequeue(self, character_id: int) -> QueueEntry | None:
        with self.lock:
            return self._remove_character_locked(character_id)

    def _remove_character_locked(self, character_id: int) -> QueueEntry | None:
        character_id = int(character_id)
        for index, entry in enumerate(self.queue):
            if entry.character_id == character_id:
                return self.queue.pop(index)
        return None

    def queued_for(self, match_mode: int) -> tuple[QueueEntry, ...]:
        with self.lock:
            return tuple(
                e for e in self.queue if e.match_mode == int(match_mode)
            )

    def waiting_count(self, match_mode: int) -> int:
        return len(self.queued_for(match_mode))

    def form_match(self, match_mode: int) -> PVPMatch | None:
        """Take players off the queue if the mode's teams can be filled."""

        match_mode = int(match_mode)
        sizes = MATCH_TEAM_SIZES.get(match_mode)
        if not sizes:
            return None
        needed = sum(sizes)
        with self.lock:
            candidates = [
                e for e in self.queue if e.match_mode == match_mode
            ]
            if len(candidates) < needed:
                return None
            chosen = sorted(candidates, key=lambda e: e.joined_at)[:needed]
            for entry in chosen:
                self._remove_character_locked(entry.character_id)
            participants = []
            index = 0
            for team, size in enumerate(sizes):
                for _ in range(size):
                    entry = chosen[index]
                    index += 1
                    session = entry.session
                    character = getattr(session, "gameplay_character", None)
                    participants.append(MatchParticipant(
                        character_id=entry.character_id,
                        account_id=entry.account_id,
                        name=str(getattr(character, "name", "")),
                        level=max(1, int(getattr(character, "level", 1))),
                        team=team,
                        session=session,
                    ))
            match = PVPMatch(
                match_mode,
                tuple(participants),
                team_count=len(sizes),
            )
            self.matches[match.match_id] = match
            return match

    # -- matches -----------------------------------------------------------

    def match(self, match_id: int) -> PVPMatch | None:
        with self.lock:
            return self.matches.get(int(match_id))

    def match_for_character(self, character_id: int) -> PVPMatch | None:
        character_id = int(character_id)
        with self.lock:
            for match in self.matches.values():
                entry = match.participant(character_id)
                if entry is not None and not entry.left:
                    return match
        return None

    def retire(self, match_id: int) -> PVPMatch | None:
        with self.lock:
            return self.matches.pop(int(match_id), None)
