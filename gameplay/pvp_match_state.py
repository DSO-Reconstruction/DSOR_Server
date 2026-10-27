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
    MATCH_WINNER_DRAW,
    ROUND_DURATION_TICKS,
    SUPPORTED_MATCH_MODES,
    honor_level_for,
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
    # Snapshot the selected character's class when joining the queue. The
    # participant slot survives the map-transfer socket replacement.
    class_id: int | None = None
    # Players a group leader queued together (``queue_kind`` 1) share one
    # non-zero party id and are always placed on the same team. Zero is a
    # solo entry.
    party_id: int = 0
    # The group leader who queued the party; their abort removes everyone.
    party_leader_id: int = 0
    # Search inputs captured at queue time: character level and career honor
    # (whose ``_Template_HonorLevels`` rank is the PvP level).
    level: int = 0
    honor_points: int = 0

    @property
    def party_key(self) -> tuple[str, int]:
        if self.party_id:
            return ("party", int(self.party_id))
        return ("solo", int(self.character_id))

    @property
    def effective_level(self) -> int:
        if self.level:
            return max(1, int(self.level))
        character = getattr(self.session, "gameplay_character", None)
        return max(1, int(getattr(character, "level", 1) or 1))

    @property
    def pvp_level(self) -> int:
        return honor_level_for(self.honor_points)


@dataclass(frozen=True)
class MatchmakingRules:
    """One battleground's authored search windows.

    ``_Template_BattlegroundSettings`` authors, per battleground,
    ``PlayerXpLevelSearchRange`` (character levels), ``PlayerPvPLevelSearchRange``
    (``_Template_HonorLevels`` ranks) and ``PlayerRatingMaxWaitTime`` (seconds).
    How retail combined them is not recoverable from the client, which only
    reads the table for display. The local rule, used by
    :meth:`MatchmakingRegistry.form_match`: a party is compatible with the
    oldest waiting party (the *anchor*) when its mean level and mean PvP level
    are each within the authored range of the anchor's; once the anchor has
    waited ``PlayerRatingMaxWaitTime`` the windows no longer restrict it, so
    nobody waits forever for an exact peer. ``None`` disables a window.
    """

    level_range: int | None = None
    pvp_level_range: int | None = None
    max_wait_seconds: float | None = None

    def lifts_at(self, anchor_joined_at: float) -> float | None:
        if self.max_wait_seconds is None:
            return None
        return float(anchor_joined_at) + max(0.0, float(self.max_wait_seconds))


@dataclass
class MatchParticipant:
    """One player's slot and running score inside a battleground."""

    character_id: int
    account_id: int | None
    name: str
    level: int
    class_id: int
    team: int
    session: object | None = None
    # Shared by players a group leader queued together; zero when solo.
    party_id: int = 0
    party_leader_id: int = 0
    honor_points: int = 0
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
    # Objective-mode counters (capture the flag, fortress).
    flag_captures: int = 0
    flag_returns: int = 0
    flag_carrier_kills: int = 0
    objectives_destroyed: int = 0
    objective_points: int = 0
    # When this dead participant is due to respawn (objective modes only).
    respawn_due: float = 0.0

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
        self.round_eliminated: set[int] = set()
        self.round_damage_dealt: dict[int, int] = {}
        # Both duelists can initialize the arena concurrently, before either
        # socket is presence-ready. Keep their player-only combat world on the
        # match itself so that readiness timing cannot create two isolated
        # combat states (which would make every PvP hit look cross-instance).
        self.gameplay_combat_state = None
        self.timers: dict[str, object] = {}
        self.info_generation = 0
        # Completion can be reached from the round timer or from the last
        # participant leaving, so the payout is claimed once.
        self.settled = False
        # Elimination rounds use the recorded 3000-tick round. Objective
        # modes replace it with their authored ``RoundDuration``.
        self.duration_ticks = ROUND_DURATION_TICKS
        # Objective modes (capture the flag, fortress) attach their rules and
        # state here; ``None`` for the elimination modes.
        self.settings = None
        self.objective = None

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
        return int(self.duration_ticks)

    def round_deadline_seconds(self) -> float:
        """Wall-clock seconds left before the current round times out."""

        elapsed = self.tick() - self.round_start_tick
        return max(0.0, (self.round_duration_ticks - elapsed) / MATCH_TICK_HZ)

    def elapsed_round_seconds(self) -> float:
        if self.phase != MatchPhase.RUNNING:
            return 0.0
        return max(0.0, (self.tick() - self.round_start_tick) / MATCH_TICK_HZ)

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

    def living_teams(self) -> tuple[int, ...]:
        """Teams with a participant still alive in this round."""

        with self.lock:
            return tuple(sorted({
                entry.team for entry in self.participants
                if entry.in_match
                and entry.character_id not in self.round_eliminated
            }))

    def reset_round(self) -> None:
        """Restore round eligibility without erasing the match scoreboard."""

        with self.lock:
            self.round_eliminated.clear()
            self.round_damage_dealt.clear()

    # -- scoring -----------------------------------------------------------

    def record_damage(self, attacker_id: int, amount: int) -> None:
        attacker = self.participant(attacker_id)
        if attacker is None or amount <= 0:
            return
        with self.lock:
            if self.phase != MatchPhase.RUNNING or not attacker.in_match:
                return
            attacker.damage_dealt += int(amount)
            self.round_damage_dealt[attacker.character_id] = (
                self.round_damage_dealt.get(attacker.character_id, 0) + int(amount)
            )

    def record_kill(self, killer_id: int, victim_id: int) -> bool:
        """Credit one kill. Return whether it decided the current round.

        A round is decided when only one team still has a living member,
        which for the 1v1 best-of-three is simply the first kill.
        """

        with self.lock:
            if self.phase != MatchPhase.RUNNING:
                return False
            victim = self.participant(victim_id)
            if (victim is None or not victim.in_match
                    or victim.character_id in self.round_eliminated):
                return False
            killer = self.participant(killer_id)
            if killer is not None and killer is not victim and killer.team == victim.team:
                return False
            self.round_eliminated.add(victim.character_id)
            victim.deaths += 1
            if killer is not None and killer is not victim and killer.in_match:
                killer.kills += 1
            return len(self.living_teams()) <= 1

    def award_round(self, winning_team: int) -> bool:
        """Close the current round. Return whether the match is now over."""

        with self.lock:
            winning_team = int(winning_team)
            if (self.phase != MatchPhase.RUNNING
                    or not 0 <= winning_team < self.team_count):
                return False
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

    def declare_draw(self) -> bool:
        """Close a running objective match without a winner.

        ``PVPMatchOver`` then names no team, which the client shows as
        ``scorescreenTeamsTied``.
        """

        with self.lock:
            if self.phase != MatchPhase.RUNNING:
                return False
            self.round_index += 1
            self.winning_team = MATCH_WINNER_DRAW
            self.phase = MatchPhase.OVER
            return True

    def revive(self, character_id: int) -> bool:
        """Return a dead objective-mode participant to play."""

        with self.lock:
            entry = self.participant(character_id)
            if entry is None or not entry.in_match:
                return False
            if entry.character_id not in self.round_eliminated:
                return False
            self.round_eliminated.discard(entry.character_id)
            entry.respawn_due = 0.0
            return True

    def is_dead(self, character_id: int) -> bool:
        with self.lock:
            return int(character_id) in self.round_eliminated

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


def _parties(entries) -> tuple[tuple[QueueEntry, ...], ...]:
    """Group queue entries by party, oldest party (by first join) first."""

    grouped: dict[tuple[str, int], list[QueueEntry]] = {}
    for entry in entries:
        grouped.setdefault(entry.party_key, []).append(entry)
    parties = [
        tuple(sorted(group, key=lambda e: (e.joined_at, e.character_id)))
        for group in grouped.values()
    ]
    parties.sort(key=lambda party: (
        min(e.joined_at for e in party), party[0].character_id,
    ))
    return tuple(parties)


def _party_level(party) -> float:
    return sum(e.effective_level for e in party) / len(party)


def _party_pvp_level(party) -> float:
    return sum(e.pvp_level for e in party) / len(party)


def _window_lifted(party, rules: MatchmakingRules, now: float) -> bool:
    lifts = rules.lifts_at(min(e.joined_at for e in party))
    return lifts is not None and now >= lifts


def parties_compatible(anchor, party, rules: MatchmakingRules | None,
                       now: float) -> bool:
    """Whether ``party`` may share a match with ``anchor``.

    See :class:`MatchmakingRules` for the (local) window rule.
    """

    if rules is None or party is anchor:
        return True
    if _window_lifted(anchor, rules, now) or _window_lifted(party, rules, now):
        return True
    if (rules.level_range is not None and abs(
            _party_level(party) - _party_level(anchor)) > rules.level_range):
        return False
    if (rules.pvp_level_range is not None and abs(
            _party_pvp_level(party) - _party_pvp_level(anchor))
            > rules.pvp_level_range):
        return False
    return True


def _choose_parties(pool, anchor, sizes) -> list | None:
    """The longest-waiting parties of ``pool`` that exactly fill every team.

    ``pool`` is in queue order and must contain ``anchor``, which is always
    chosen. A party is only taken if all teams can still be filled without
    splitting any party.
    """

    failed: set[tuple[int, tuple[int, ...], bool]] = set()

    def search(index, caps, anchor_done, chosen):
        if not any(caps):
            return list(chosen) if anchor_done else None
        if index >= len(pool):
            return None
        key = (index, caps, anchor_done)
        if key in failed:
            return None
        party = pool[index]
        size = len(party)
        is_anchor = party is anchor
        for cap in sorted(set(caps), reverse=True):
            if cap < size:
                continue
            remaining = list(caps)
            remaining.remove(cap)
            remaining.append(cap - size)
            found = search(
                index + 1, tuple(sorted(remaining, reverse=True)),
                anchor_done or is_anchor, chosen + [party],
            )
            if found is not None:
                return found
        if not is_anchor:
            found = search(index + 1, caps, anchor_done, chosen)
            if found is not None:
                return found
        failed.add(key)
        return None

    return search(0, tuple(sorted(sizes, reverse=True)), False, [])


def _seat_parties(parties, sizes) -> list[list[tuple[QueueEntry, ...]]] | None:
    """Seat whole parties on teams, balancing summed levels between teams.

    Every assignment that fills each team exactly is considered (identical
    empty teams are not permuted); the closest spread of summed character
    levels wins, then the closest spread of summed PvP levels, then queue
    order.
    """

    team_count = len(sizes)
    if all(size == 1 for size in sizes):
        # Free-for-all: one competitor per team, in queue order.
        return [[party] for party in parties[:team_count]]
    best: list | None = None
    best_score = None
    teams: list[list] = [[] for _ in sizes]
    free = list(sizes)

    def score():
        levels = [sum(e.effective_level for p in team for e in p) for team in teams]
        pvp = [sum(e.pvp_level for p in team for e in p) for team in teams]
        return (max(levels) - min(levels), max(pvp) - min(pvp))

    def place(index):
        nonlocal best, best_score
        if index == len(parties):
            if any(free):
                return
            candidate = score()
            if best_score is None or candidate < best_score:
                best_score = candidate
                best = [list(team) for team in teams]
            return
        party = parties[index]
        opened_empty = False
        for team in range(team_count):
            if free[team] < len(party):
                continue
            if not teams[team]:
                if opened_empty:
                    continue
                opened_empty = True
            teams[team].append(party)
            free[team] -= len(party)
            place(index + 1)
            free[team] += len(party)
            teams[team].pop()

    place(0)
    return best


def select_match_teams(parties, sizes, *, rules: MatchmakingRules | None = None,
                       now: float | None = None):
    """Pick and seat parties for one match, or ``None`` if none can form.

    Each waiting party is tried as the anchor in queue order, so a party
    nobody is compatible with does not block younger parties.
    """

    moment = time.perf_counter() if now is None else float(now)
    largest = max(sizes)
    eligible = [party for party in parties if len(party) <= largest]
    for anchor in eligible:
        pool = [
            party for party in eligible
            if parties_compatible(anchor, party, rules, moment)
        ]
        if sum(len(party) for party in pool) < sum(sizes):
            continue
        chosen = _choose_parties(pool, anchor, sizes)
        if chosen is None:
            continue
        teams = _seat_parties(chosen, sizes)
        if teams is not None:
            return teams
    return None


class MatchmakingRegistry:
    """Server-wide queue and the battlegrounds it has produced."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.queue: list[QueueEntry] = []
        self.matches: dict[int, PVPMatch] = {}
        # One pending re-match timer per mode (see _arm_queue_retry).
        self.queue_timers: dict[int, object] = {}

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

    def queued_parties(self, match_mode: int) -> tuple[tuple[QueueEntry, ...], ...]:
        """This mode's queue as parties, oldest party first."""

        with self.lock:
            return _parties(
                e for e in self.queue if e.match_mode == int(match_mode)
            )

    def party_members(self, party_id: int) -> tuple[QueueEntry, ...]:
        with self.lock:
            return tuple(
                e for e in self.queue
                if party_id and e.party_id == int(party_id)
            )

    def next_window_change(self, match_mode: int,
                           rules: MatchmakingRules | None) -> float | None:
        """When the oldest waiting party's search window next widens.

        ``None`` when nothing is waiting or the windows never lift. The
        service re-runs matchmaking at that moment, because a lifted window
        can complete a match without anyone new joining the queue.
        """

        if rules is None:
            return None
        parties = self.queued_parties(match_mode)
        moments = [
            rules.lifts_at(min(e.joined_at for e in party))
            for party in parties
        ]
        moments = [moment for moment in moments if moment is not None]
        now = time.perf_counter()
        pending = [moment for moment in moments if moment > now]
        return min(pending) if pending else None

    def form_match(self, match_mode: int, *,
                   rules: MatchmakingRules | None = None,
                   now: float | None = None) -> PVPMatch | None:
        """Take players off the queue if the mode's teams can be filled.

        Parties are never split: every member of a party lands on one team,
        and a party larger than a team is never matched. Selection prefers
        the longest-waiting parties within the authored search windows
        (:class:`MatchmakingRules`); team assignment then picks, among the
        ways to seat the selected parties, the one with the closest summed
        character levels and then PvP levels between teams.
        """

        match_mode = int(match_mode)
        if match_mode not in SUPPORTED_MATCH_MODES:
            return None
        sizes = MATCH_TEAM_SIZES.get(match_mode)
        if not sizes:
            return None
        needed = sum(sizes)
        moment = time.perf_counter() if now is None else float(now)
        with self.lock:
            candidates = [
                e for e in self.queue if e.match_mode == match_mode
            ]
            if len(candidates) < needed:
                return None
            teams = select_match_teams(
                _parties(candidates), sizes, rules=rules, now=moment,
            )
            if teams is None:
                return None
            participants = []
            for team, parties in enumerate(teams):
                for party in parties:
                    for entry in party:
                        self._remove_character_locked(entry.character_id)
                        session = entry.session
                        character = getattr(session, "gameplay_character", None)
                        participants.append(MatchParticipant(
                            character_id=entry.character_id,
                            account_id=entry.account_id,
                            name=str(getattr(character, "name", "")),
                            level=entry.effective_level,
                            class_id=max(0, int(
                                entry.class_id if entry.class_id is not None
                                else getattr(character, "class_id", 0)
                            )),
                            team=team,
                            session=session,
                            party_id=int(entry.party_id),
                            party_leader_id=int(entry.party_leader_id),
                            honor_points=int(entry.honor_points),
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
