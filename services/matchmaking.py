"""PvP matchmaking: queue, invitation, and the handoff into the arena.

Reproduces the flow recorded from retail up to the point the match starts;
``battleground.py`` runs it from there.

1. The hub map server receives ``JoinMatchQueue`` and answers with
   ``JoinMatchQueueResponse`` plus a ``MatchMakingInfo``/``JoinMatchQueueInfo``
   pair, repeated while the player waits.
2. Once the mode's teams can be filled, every chosen player gets
   ``JoinBattleGround`` -- the accept dialog -- carrying the arena map id.
3. ``JoinBattlegroundResponse`` accepts; when everyone has, each session is
   transferred to the arena map through the ordinary map-transfer path.
4. The arena sends ``SetBattlegroundId``, then repeats ``PVPMatchInfo`` while
   cycling ``PVPMatchCountdown`` -> ``PVPMatchStart`` -> a decided round, until
   one team reaches the mode's round target.
5. ``PVPMatchOver`` closes the match, honor is credited and reported with
   ``HonorPointsChanged``, and ``LeavePVP`` returns the player to the hub.
"""

from __future__ import annotations

from datetime import datetime, timezone
from dso_server.runtime.context import *
from dso_server.runtime.transport import Session

from dso_server.gameplay.pvp_match_state import (
    MATCH_ACCEPT_SECONDS,
    MATCH_LOAD_TIMEOUT_SECONDS,
    MatchParticipant,
    MatchPhase,
    MatchmakingRegistry,
    PVPMatch,
    QueueEntry,
)


_REGISTRY_LOCK = threading.RLock()


class MatchmakingService:
    """PvP queue and battleground orchestration."""

    # ------------------------------------------------------------------
    # registry
    # ------------------------------------------------------------------

    @staticmethod
    def _matchmaking_registry(server) -> MatchmakingRegistry:
        """One queue per server process, created on first use.

        Service mixins have no constructor of their own, so the registry is
        attached lazily rather than in ``RakNetServer.__init__``.
        """

        # The attribute name must differ from this method's, or the
        # lookup finds the method itself instead of a missing attribute.
        registry = getattr(server, "_pvp_matchmaking", None)
        if registry is not None:
            return registry
        with _REGISTRY_LOCK:
            registry = getattr(server, "_pvp_matchmaking", None)
            if registry is None:
                registry = MatchmakingRegistry()
                server._pvp_matchmaking = registry
        return registry

    @staticmethod
    def _restore_pvp_career(s: Session, persisted_state) -> None:
        """Load the account's PvP flag, career totals, and daily stats onto a session."""

        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        saved_date = str(getattr(persisted_state, "pvp_stats_date", "") or "") if persisted_state is not None else ""
        is_same_day = (saved_date == today_str)

        s.gameplay_pvp_enabled = bool(
            getattr(persisted_state, "pvp_enabled", False)
            if persisted_state is not None
            else False
        )
        s.gameplay_pvp_stats_date = today_str

        honor_points = max(0, int(getattr(persisted_state, "honor_points", 0) or 0)) if persisted_state is not None else 0
        matches_won = max(0, int(getattr(persisted_state, "pvp_matches_won", 0) or 0)) if persisted_state is not None else 0
        matches_lost = max(0, int(getattr(persisted_state, "pvp_matches_lost", 0) or 0)) if persisted_state is not None else 0
        kills = max(0, int(getattr(persisted_state, "pvp_kills", 0) or 0)) if persisted_state is not None else 0
        deaths = max(0, int(getattr(persisted_state, "pvp_deaths", 0) or 0)) if persisted_state is not None else 0

        s.gameplay_honor_points = honor_points
        s.gameplay_pvp_matches_won = matches_won
        s.gameplay_pvp_matches_lost = matches_lost
        s.gameplay_pvp_kills = kills
        s.gameplay_pvp_deaths = deaths

        solo_won = getattr(persisted_state, "pvp_solo_matches_won", None) if persisted_state is not None else None
        solo_played = getattr(persisted_state, "pvp_solo_matches_played", None) if persisted_state is not None else None
        s.gameplay_pvp_solo_matches_won = max(0, int(solo_won)) if solo_won is not None and int(solo_won) > 0 else matches_won
        s.gameplay_pvp_solo_matches_played = max(0, int(solo_played)) if solo_played is not None and int(solo_played) > 0 else (matches_won + matches_lost)
        s.gameplay_pvp_team_matches_won = max(0, int(getattr(persisted_state, "pvp_team_matches_won", 0) or 0)) if persisted_state is not None else 0
        s.gameplay_pvp_team_matches_played = max(0, int(getattr(persisted_state, "pvp_team_matches_played", 0) or 0)) if persisted_state is not None else 0

        kills_alone = getattr(persisted_state, "pvp_kills_alone_total", None) if persisted_state is not None else None
        kills_battle = getattr(persisted_state, "pvp_kills_battle_total", None) if persisted_state is not None else None
        s.gameplay_pvp_kills_total = kills
        s.gameplay_pvp_kills_alone_total = max(0, int(kills_alone)) if kills_alone is not None and int(kills_alone) > 0 else kills
        s.gameplay_pvp_kills_group_total = max(0, int(getattr(persisted_state, "pvp_kills_group_total", 0) or 0)) if persisted_state is not None else 0
        s.gameplay_pvp_kills_battle_total = max(0, int(kills_battle)) if kills_battle is not None and int(kills_battle) > 0 else kills
        s.gameplay_pvp_dishonorable_kills = max(0, int(getattr(persisted_state, "pvp_dishonorable_kills", 0) or 0)) if persisted_state is not None else 0

        if is_same_day and persisted_state is not None:
            s.gameplay_honor_points_today = max(0, int(getattr(persisted_state, "honor_points_today", 0) or 0))
            s.gameplay_pvp_kills_today = max(0, int(getattr(persisted_state, "pvp_kills_today", 0) or 0))
            s.gameplay_pvp_kills_alone_today = max(0, int(getattr(persisted_state, "pvp_kills_alone_today", 0) or 0))
            s.gameplay_pvp_kills_group_today = max(0, int(getattr(persisted_state, "pvp_kills_group_today", 0) or 0))
            s.gameplay_pvp_kills_battle_today = max(0, int(getattr(persisted_state, "pvp_kills_battle_today", 0) or 0))
        else:
            s.gameplay_honor_points_today = 0
            s.gameplay_pvp_kills_today = 0
            s.gameplay_pvp_kills_alone_today = 0
            s.gameplay_pvp_kills_group_today = 0
            s.gameplay_pvp_kills_battle_today = 0

    def _announce_battleground(self, s: Session) -> None:
        """Send SetBattlegroundId before the arena's initialization batch.

        The client builds its PvP UI while it processes initialization, and
        the battleground manager has to be up first -- retail puts this
        command at the head of that batch. Sent after it, the manager exists
        too late and the countdown and result panel are delivered to nobody.
        """

        character_id = CharacterService._match_character_id(s)
        registry = getattr(self, "_pvp_matchmaking", None)
        if character_id is None or registry is None:
            return
        match = registry.match_for_character(character_id)
        if match is None or match.map_id != str(s.gameplay_map or ""):
            return
        self._send_pvp(
            s,
            encode_set_battleground_id(
                SetBattlegroundId(match.battleground_id)
            ),
            label="pvp-battleground-id",
        )
        # Retail batches this immediately behind the id. It is what tells the
        # manager a match exists here; with only the id it holds a
        # battleground and no match, and every later PvP command lands on
        # something that is not running one.
        self._send_pvp(
            s,
            encode_match_description(MatchDescription(active=True, value=0)),
            label="pvp-match-description",
        )

    def _entering_battleground(self, s: Session, map_id: str) -> bool:
        """Is this session loading the arena of a match it belongs to?

        Asked during world initialization, before the match roster is re-bound
        to the arriving session, so it goes to the registry rather than to
        ``s.gameplay_match``.
        """

        character_id = CharacterService._match_character_id(s)
        if character_id is None:
            return False
        registry = getattr(self, "_pvp_matchmaking", None)
        if registry is None:
            return False
        match = registry.match_for_character(character_id)
        return match is not None and match.map_id == str(map_id)

    @staticmethod
    def _match_character_id(s: Session) -> int | None:
        character = s.gameplay_character
        if character is None:
            return None
        return int(character.character_id)

    def _match_class_id(self, s: Session) -> int:
        """Return the class enum from the character being queued."""

        character = s.gameplay_character
        if character is None:
            return 0
        return max(0, int(getattr(character, "class_id", 0)))

    def _send_pvp(self, s: Session, payload, *, label: str) -> None:
        """Send one encoded PvP command, tolerating a dead socket.

        Every send is logged. A match produces on the order of ten of these,
        so the cost is nothing next to being able to answer "did the server
        actually send it" from the console instead of inferring it from what
        the client did or did not draw.
        """

        if s is None or not s.connected or s.retired:
            print(
                f"[{GAMEPLAY_SERVICE_NAME}] PVP {label} NOT SENT to "
                f"{getattr(s, 'addr', None)}: "
                f"{'no session' if s is None else 'socket closed'}"
            )
            return
        data = payload[0] if isinstance(payload, tuple) else payload
        bits = payload[1] if isinstance(payload, tuple) else len(data) * 8
        command_id = (
            int.from_bytes(data[1:3], "little") if len(data) >= 3 else -1
        )
        try:
            s.send_reliable_ordered(payload, track=True, label=label)
        except OSError as exc:
            print(
                f"[{GAMEPLAY_SERVICE_NAME}] PVP {label} not delivered to "
                f"{s.addr}: {type(exc).__name__}: {exc}"
            )
            return
        print(
            f"[{GAMEPLAY_SERVICE_NAME}] PVP -> {s.addr[0]}:{s.addr[1]} "
            f"0x{command_id:04x} {label} ({bits} bits) {data.hex()}"
        )

    # ------------------------------------------------------------------
    # queue
    # ------------------------------------------------------------------

    def _handle_join_match_queue(self, s: Session, payload: bytes) -> None:
        try:
            request = decode_join_match_queue(
                payload, s.current_message_bit_length
            )
        except (PVPMatchProtocolError, CharacterSelectionError) as exc:
            print(f"[{GAMEPLAY_SERVICE_NAME}] rejected JoinMatchQueue: {exc}")
            return
        character_id = CharacterService._match_character_id(s)
        if character_id is None:
            return
        if request.match_mode not in MATCH_TEAM_SIZES:
            self._send_pvp(
                s,
                encode_join_match_queue_response(JoinMatchQueueResponse(
                    result=0, accepted=False, actor_id=LOCAL_PLAYER_ACTOR_ID,
                )),
                label="pvp-queue-rejected",
            )
            print(
                f"[{GAMEPLAY_SERVICE_NAME}] rejected JoinMatchQueue for "
                f"unsupported mode {request.match_mode}"
            )
            return
        registry = CharacterService._matchmaking_registry(self)
        entry = registry.enqueue(QueueEntry(
            character_id=character_id,
            account_id=s.account_id,
            match_mode=int(request.match_mode),
            queue_kind=int(request.queue_kind),
            session=s,
            class_id=CharacterService._match_class_id(self, s),
        ))
        with s.flow_lock:
            s.gameplay_match_queue_entry = entry
        self._send_pvp(
            s,
            encode_join_match_queue_response(JoinMatchQueueResponse(
                result=0, accepted=True, actor_id=LOCAL_PLAYER_ACTOR_ID,
            )),
            label="pvp-queue-accepted",
        )
        self._send_queue_status(s, entry)
        print(
            f"[{GAMEPLAY_SERVICE_NAME}] queued character {character_id} for "
            f"PvP mode {entry.match_mode} (kind {entry.queue_kind}); "
            f"{registry.waiting_count(entry.match_mode)} waiting"
        )
        self._try_form_match(entry.match_mode)

    def _send_queue_status(self, s: Session, entry: QueueEntry) -> None:
        """Send the pair retail repeats while a player waits."""

        registry = CharacterService._matchmaking_registry(self)
        waiting = registry.waiting_count(entry.match_mode)
        tick = int(server_time() // 40) & 0xFFFFFFFF
        info = encode_matchmaking_info(MatchMakingInfo(
            state=MATCHMAKING_EVENT_JOINED_BY_USER,
            server_tick=tick,
            match_mode=entry.match_mode,
            message="",
        ))
        queue_info = encode_join_match_queue_info(JoinMatchQueueInfo(
            server_tick=tick,
            estimated_wait=max(0.0, time.perf_counter() - entry.joined_at),
            state=0,
            queued_players=waiting,
            queued_at=CalendarTime.from_datetime(
                datetime.now(timezone.utc)
            ),
        ))
        self._send_pvp(s, info, label="pvp-matchmaking-info")
        self._send_pvp(s, queue_info, label="pvp-queue-info")

    def _handle_abort_match_queue(self, s: Session, payload: bytes) -> None:
        try:
            decode_abort_match_queue(payload, s.current_message_bit_length)
        except (PVPMatchProtocolError, CharacterSelectionError) as exc:
            print(f"[{GAMEPLAY_SERVICE_NAME}] rejected AbortMatchQueue: {exc}")
            return
        CharacterService._leave_match_queue(self, s, reason="client abort")

    def _leave_match_queue(self, s: Session, *, reason: str) -> None:
        character_id = CharacterService._match_character_id(s)
        if character_id is None:
            return
        registry = CharacterService._matchmaking_registry(self)
        removed = registry.dequeue(character_id)
        with s.flow_lock:
            s.gameplay_match_queue_entry = None
        if removed is None:
            return
        self._send_pvp(
            s,
            encode_matchmaking_info(MatchMakingInfo(
                state=MATCHMAKING_EVENT_ABORTED_BY_USER,
                server_tick=int(server_time() // 40) & 0xFFFFFFFF,
                # Keep the mode so the client can clear the queue UI for the
                # specific queue that was just cancelled.
                match_mode=removed.match_mode,
                message="",
            )),
            label="pvp-queue-left",
        )
        self._send_pvp(
            s,
            encode_join_match_queue_info(JoinMatchQueueInfo(
                server_tick=int(server_time() // 40) & 0xFFFFFFFF,
                estimated_wait=0.0,
                state=MATCH_MODE_INVALID,
                queued_players=0,
                queued_at=CalendarTime(),
            )),
            label="pvp-queue-info-cleared",
        )
        print(
            f"[{GAMEPLAY_SERVICE_NAME}] character {character_id} left the "
            f"PvP queue ({reason})"
        )

    # ------------------------------------------------------------------
    # invitation
    # ------------------------------------------------------------------

    def _try_form_match(self, match_mode: int) -> PVPMatch | None:
        registry = CharacterService._matchmaking_registry(self)
        match = registry.form_match(match_mode)
        if match is None:
            return None
        for entry in match.participants:
            session = entry.session
            if session is None:
                continue
            with session.flow_lock:
                session.gameplay_match_queue_entry = None
                session.gameplay_match = match
                session.gameplay_match_team = entry.team
            self._send_pvp(
                session,
                encode_join_battleground(JoinBattleground(
                    server_tick=int(server_time() // 40) & 0xFFFFFFFF,
                    dialog_id=0,
                    battleground_id=match.match_id,
                    map_id=match.map_id,
                    match_mode=match.match_mode,
                )),
                label="pvp-join-battleground",
            )
        self._arm_match_timer(
            match, "accept", MATCH_ACCEPT_SECONDS,
            CharacterService._expire_match_acceptance,
        )
        print(
            f"[{GAMEPLAY_SERVICE_NAME}] formed PvP match {match.match_id} "
            f"({match.battleground_id}) on {match.map_id} for "
            f"{[e.character_id for e in match.participants]}"
        )
        return match

    def _arm_match_timer(self, match: PVPMatch, key: str, delay: float,
                         callback) -> None:
        """Replace one named match timer.

        Timers hang off a participant session so they run on the same deadline
        scheduler as the rest of gameplay. Session retirement cancels that
        session's timers, so the host has to be one that is still connected;
        if none is, the match has already lost every player and the forfeit
        path owns it.
        """

        sessions = tuple(
            session for session in match.live_sessions()
            if getattr(session, "connected", False)
            and not getattr(session, "retired", False)
        )
        if not sessions:
            return
        with match.lock:
            existing = match.timers.pop(key, None)
            generation = match.info_generation
        if existing is not None:
            existing.cancel()

        def fire(server, current_match, expected_generation):
            with current_match.lock:
                if expected_generation != current_match.info_generation:
                    return
            callback(server, current_match, expected_generation)

        timer = sessions[0].gameplay_deadline_timer(
            max(0.0, float(delay)),
            fire,
            args=(self, match, generation),
        )
        timer.daemon = True
        with match.lock:
            match.timers[key] = timer
        timer.start()

    @staticmethod
    def _cancel_match_timers(match: PVPMatch) -> None:
        with match.lock:
            match.info_generation += 1
            timers = tuple(match.timers.values())
            match.timers.clear()
        for timer in timers:
            timer.cancel()

    def _handle_join_battleground_response(
            self, s: Session, payload: bytes) -> None:
        try:
            request = decode_join_battleground_response(
                payload, s.current_message_bit_length
            )
        except (PVPMatchProtocolError, CharacterSelectionError) as exc:
            print(
                f"[{GAMEPLAY_SERVICE_NAME}] rejected "
                f"JoinBattlegroundResponse: {exc}"
            )
            return
        character_id = CharacterService._match_character_id(s)
        if character_id is None:
            return
        registry = CharacterService._matchmaking_registry(self)
        match = registry.match_for_character(character_id)
        if match is None:
            print(
                f"[{GAMEPLAY_SERVICE_NAME}] ignored battleground response "
                f"from character {character_id}: no pending match"
            )
            return
        accepted = bool(request.accepted)
        with match.lock:
            entry = match.participant(character_id)
            if entry is None or match.phase != MatchPhase.INVITING:
                return
            entry.session = s
            entry.accepted = accepted
            entry.declined = not accepted
            everyone = match.everyone_accepted()
            declined = match.anyone_declined()
        print(
            f"[{GAMEPLAY_SERVICE_NAME}] character {character_id} "
            f"{'accepted' if accepted else 'declined'} PvP match "
            f"{match.match_id}"
        )
        if declined:
            CharacterService._cancel_match(
                self, match, reason="a player declined"
            )
        elif everyone:
            CharacterService._start_battleground_transfer(self, match)

    @staticmethod
    def _expire_match_acceptance(
            server, match: PVPMatch, generation: int) -> None:
        with match.lock:
            if match.phase != MatchPhase.INVITING:
                return
        CharacterService._cancel_match(
            server, match, reason="acceptance timed out"
        )

    def _cancel_match(self, match: PVPMatch, *, reason: str) -> None:
        """Abandon a match that never started; requeue willing players."""

        CharacterService._cancel_match_timers(match)
        with match.lock:
            if match.phase in (MatchPhase.OVER, MatchPhase.CANCELLED):
                return
            match.phase = MatchPhase.CANCELLED
            participants = match.participants
        registry = CharacterService._matchmaking_registry(self)
        registry.retire(match.match_id)
        requeued = 0
        for entry in participants:
            session = entry.session
            if session is None:
                continue
            with session.flow_lock:
                session.gameplay_match = None
                session.gameplay_match_team = None
            self._send_pvp(
                session,
                encode_matchmaking_info(MatchMakingInfo(
                    state=MATCHMAKING_EVENT_ABORTED_BY_SERVER,
                    server_tick=int(server_time() // 40) & 0xFFFFFFFF,
                    match_mode=match.match_mode,
                    message="",
                )),
                label="pvp-match-cancelled",
            )
            if entry.accepted and not entry.declined:
                queued = registry.enqueue(QueueEntry(
                    character_id=entry.character_id,
                    account_id=entry.account_id,
                    match_mode=match.match_mode,
                    queue_kind=QUEUE_KIND_SINGLE,
                    session=session,
                    class_id=entry.class_id,
                ))
                with session.flow_lock:
                    session.gameplay_match_queue_entry = queued
                self._send_queue_status(session, queued)
                requeued += 1
        print(
            f"[{GAMEPLAY_SERVICE_NAME}] cancelled PvP match "
            f"{match.match_id}: {reason}; requeued {requeued}"
        )

    # ------------------------------------------------------------------
    # arena entry
    # ------------------------------------------------------------------

    def _battleground_team_entries(self, map_id: str) -> tuple:
        """Both authored positions for each side of an arena map.

        A battleground uses two per team: the staging entry the side lands on
        when the map loads, and the arena position it is moved to when a round
        starts. The map's single default spawn gives neither, and would put
        both sides on top of each other.
        """

        try:
            entries = load_local_team_entries(
                self.static_database_path, map_id
            )
        except (OSError, ValueError) as exc:
            print(
                f"[{GAMEPLAY_SERVICE_NAME}] {map_id} has no readable team "
                f"entries ({type(exc).__name__}: {exc}); both sides will use "
                "the default spawn"
            )
            return ()
        if not entries:
            print(
                f"[{GAMEPLAY_SERVICE_NAME}] {map_id} authors no per-team "
                "entries; both sides will use the default spawn"
            )
        return entries

    def _battleground_team_starts(
            self, map_id: str) -> tuple[tuple[float, float, float], ...]:
        """Each side's staging position, indexed by team number."""

        return tuple(
            entry.entry_position
            for entry in CharacterService._battleground_team_entries(
                self, map_id
            )
        )

    def _start_battleground_transfer(self, match: PVPMatch) -> None:
        with match.lock:
            if match.phase != MatchPhase.INVITING:
                return
            match.phase = MatchPhase.LOADING
            participants = match.participants
        CharacterService._cancel_match_timers(match)
        starts = CharacterService._battleground_team_starts(
            self, match.map_id
        )
        for entry in participants:
            session = entry.session
            if session is None:
                continue
            try:
                started = CharacterService._begin_map_transfer(
                    self,
                    session,
                    match.map_id,
                    spawn_position=(
                        starts[entry.team % len(starts)] if starts else None
                    ),
                    reason="pvp-battleground",
                    gameplay_switch=True,
                )
            except (OSError, ValueError) as exc:
                print(
                    f"[{GAMEPLAY_SERVICE_NAME}] could not send character "
                    f"{entry.character_id} to {match.map_id}: "
                    f"{type(exc).__name__}: {exc}"
                )
                started = False
            if not started:
                CharacterService._cancel_match(
                    self, match, reason="battleground transfer failed"
                )
                return
        self._arm_match_timer(
            match, "load", MATCH_LOAD_TIMEOUT_SECONDS,
            CharacterService._expire_match_loading,
        )

    @staticmethod
    def _expire_match_loading(
            server, match: PVPMatch, generation: int) -> None:
        with match.lock:
            if match.phase != MatchPhase.LOADING:
                return
        CharacterService._cancel_match(
            server, match, reason="a player never reached the arena"
        )

    def _bind_battleground_session(self, s: Session) -> bool:
        """Re-attach a session to its match after the arena map load.

        The map transfer replaces the socket, so the arriving session is a
        different object from the one that accepted the invitation. Called
        from the initialization pipeline once the player is ready to be seen.
        """

        character_id = CharacterService._match_character_id(s)
        if character_id is None:
            return False
        registry = CharacterService._matchmaking_registry(self)
        match = registry.match_for_character(character_id)
        if match is None:
            return False
        if str(s.gameplay_map or "") != match.map_id:
            return False
        with match.lock:
            entry = match.participant(character_id)
            if entry is None:
                return False
            entry.session = s
            entry.arrived = True
            entry.actor_id = int(
                getattr(s, "gameplay_remote_actor_id", 0) or 0
            )
            entry.honor_at_start = int(
                getattr(s, "gameplay_honor_points", 0) or 0
            )
            everyone = match.everyone_arrived()
            phase = match.phase
        with s.flow_lock:
            s.gameplay_match = match
            s.gameplay_match_team = entry.team
        # The battleground id itself already went out ahead of the
        # initialization batch; see _announce_battleground.
        CharacterService._force_battleground_pvp(self, s)
        self._send_match_info(s, match)
        print(
            f"[{GAMEPLAY_SERVICE_NAME}] character {character_id} entered PvP "
            f"match {match.match_id} on {match.map_id} (team {entry.team})"
        )
        if everyone and phase == MatchPhase.LOADING:
            CharacterService._begin_match_round(self, match)
        return True

    def _force_battleground_pvp(self, s: Session, *,
                                enabled: bool = True) -> None:
        """Flag a battleground participant for PvP, and say so on the wire.

        Setting ``gameplay_pvp_enabled`` alone only satisfies the server's own
        hostility check. The client keeps its own copy -- it is what decides
        whether an opponent can be targeted at all -- and only updates it from
        the ``PVPFlagCommand`` response and the matching presence broadcast.
        Without both, an arena where the server considers everyone hostile
        still refuses every attack on the client.

        A battleground overrides the account's global flag, so this is applied
        on entry and restored from the account on the way out.
        """

        tick = CharacterService._session_tick(s)
        with s.flow_lock:
            s.gameplay_pvp_enabled = bool(enabled)
            request = PVPFlag(bool(enabled), tick)
            s.gameplay_client_command_state[PVP_FLAG_COMMAND] = request
        self._send_pvp(
            s,
            encode_pvp_flag_response(
                request, actor_id=LOCAL_PLAYER_ACTOR_ID
            ),
            label="pvp-battleground-flag",
        )
        if s.gameplay_presence_callback is not None:
            # Peers hold their own hostility state for this actor; without
            # this they still render it as a neutral player.
            s.gameplay_presence_callback(
                s, ("pvp_flag", bool(enabled), tick)
            )

