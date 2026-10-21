"""Battleground runtime: rounds, scoreboard, and match completion.

Split out of ``matchmaking.py``, which owns everything up to the point a match
is formed and its participants are handed to the arena map. From there this
module runs the match itself: the countdown/start cycle, the per-round
force-transform into the arena, the repeated ``PVPMatchInfo`` scoreboard, and
the result.

See ``docs/PVP_MATCHMAKING.md`` for the recorded flow these reproduce.
"""

from __future__ import annotations

from dso_server.runtime.context import *
from dso_server.runtime.transport import Session
from dso_server.protocol.respawn_protocol import encode_revive
from functools import lru_cache, partial
from dso_server.persistence.readonly_db import connect
from dso_server.protocol.gameplay_protocol import Message, MessageArgument
from dso_server.protocol.player_init_protocol import MESSAGE_COMMAND
from dso_server.protocol.pvp_match_protocol import MATCH_MODE_BEST_OF_3_1V1

from dso_server.gameplay.map_status_effects import (
    map_status_effect_definitions,
    map_status_effect_entry,
)

from dso_server.gameplay.pvp_match_state import (
    MATCH_COUNTDOWN_SECONDS,
    MATCH_INFO_INTERVAL_SECONDS,
    MATCH_OVER_DELAY_SECONDS,
    ROUND_INTERMISSION_SECONDS,
    MatchPhase,
    PVPMatch,
)

@lru_cache(maxsize=16)
def _round_effects(database_path: str, start: bool = False):
    with connect(database_path) as database:
        return LocalCombatState._load_victim_status_effects(
            database, "pvp_cooldown_reset" if start else
            "pvp_respawn;pvp_invulnerable;pvp_freeze",
        )


@lru_cache(maxsize=8)
def _arena_debuff_ids(database_path: str):
    with connect(database_path) as database:
        return frozenset(int(row[0]) for row in database.execute(
            "SELECT rowid - 1 FROM _Template_StatusEffect "
            "WHERE IconConfiguration = 'statuseffect_debuff'"
        ))


class BattlegroundService:
    """Rounds, scoreboard, and completion for one battleground instance."""

    # ------------------------------------------------------------------
    # rounds
    # ------------------------------------------------------------------

    def _move_to_arena_position(self, s: Session, entries, team: int) -> None:
        """Teleport one participant from its staging entry into the arena.

        The map's team ``Entry`` is a holding area off to one end; the fight
        happens at the team's ``Respawn``. Retail bridges the two with a
        ``ForceTransformCommand`` in the countdown batch rather than by
        spawning players in the arena directly, which is also what keeps the
        two sides apart until the round actually begins.
        """

        if not entries:
            return
        entry = entries[team % len(entries)]
        with s.flow_lock:
            # Keep the server's own idea of where this player is in step; the
            # client's next MoveCommand will confirm it.
            s.gameplay_spawn_position = entry.arena_position
            frame = getattr(s, "gameplay_coordinate_frame", None)
            if frame is not None:
                s.gameplay_last_move = Move.from_values(
                    frame.world_to_wire(entry.arena_position), 0.0,
                    entry.arena_heading, entry.arena_heading,
                    int(s.gameplay_chase_server_tick), 0,
                )
                s.gameplay_move_budget = 0.0
                s.gameplay_movement_allowance = None
                s.gameplay_move_correction_at = 0.0
        self._send_pvp(
            s,
            encode_force_transform_batch(
                ForceTransform(entry.arena_position, entry.arena_heading),
                actor_id=LOCAL_PLAYER_ACTOR_ID,
            ),
            label="pvp-arena-transform",
        )

    def _begin_match_round(self, match: PVPMatch) -> None:
        with match.lock:
            if match.phase in (MatchPhase.OVER, MatchPhase.CANCELLED):
                return
            match.phase = MatchPhase.COUNTDOWN
            match.round_start_tick = 0
            for participant in match.participants:
                participant.client_round_start_tick = 0
        CharacterService._cancel_match_timers(match)
        entries = CharacterService._battleground_team_entries(
            self, match.map_id
        )
        CharacterService._clear_battleground_world_effects(self, match)
        for participant in match.participants:
            session = participant.session
            if session is None:
                continue
            self._send_pvp(
                session, encode_match_countdown(),
                label="pvp-match-countdown",
            )
            # Retail pairs the countdown with a ForceTransform out of the
            # staging entry and into the arena proper, so the round begins
            # with both sides already in position.
            CharacterService._move_to_arena_position(
                self, session, entries, participant.team
            )
            CharacterService._clear_battleground_round_effects(self, session)
            CharacterService._restore_battleground_health(self, session)
            CharacterService._send_battleground_effects(self, session)
        CharacterService._broadcast_round_message(self, match, 28, match.round_index + 1)
        self._broadcast_match_info(match)
        # C3 changes the match phase; Message(5, seconds) draws the digits.
        # Retail sends 3/2/1 at +1/+2/+3 seconds, then C2 at +4 seconds.
        for seconds in (3, 2, 1):
            self._arm_match_timer(
                match, f"countdown-{seconds}", MATCH_COUNTDOWN_SECONDS - seconds,
                partial(CharacterService._match_countdown_digit, seconds=seconds),
            )
        self._arm_match_timer(
            match, "countdown", MATCH_COUNTDOWN_SECONDS,
            CharacterService._start_match_round,
        )

    @staticmethod
    def _start_match_round(server, match: PVPMatch, generation: int) -> None:
        with match.lock:
            if (match.phase != MatchPhase.COUNTDOWN
                    or generation != match.info_generation):
                return
            match.phase = MatchPhase.RUNNING
            match.round_start_tick = match.tick()
            # Each client counts the round down against its own clock, so the
            # start has to be stamped once per participant in that client's
            # tick base and then held for the round.
            for entry in match.participants:
                entry.client_round_start_tick = (
                    CharacterService._session_tick(entry.session)
                )
            participants = match.participants
        for entry in participants:
            session = entry.session
            if session is None:
                continue
            CharacterService._send_battleground_effects(server, session, start=True)
            server._send_pvp(
                session,
                encode_match_start(MatchStart(
                    round_start_tick=entry.client_round_start_tick,
                    round_duration_ticks=match.round_duration_ticks,
                    flag=False,
                )),
                label="pvp-match-start",
            )
        server._broadcast_match_info(match)
        CharacterService._arm_match_timer(
            server, match, "round", match.round_deadline_seconds(),
            CharacterService._expire_match_round,
        )
        CharacterService._arm_match_timer(
            server, match, "info", MATCH_INFO_INTERVAL_SECONDS,
            CharacterService._repeat_match_info,
        )
        print(
            f"[{GAMEPLAY_SERVICE_NAME}] PvP match {match.match_id} round "
            f"{match.round_index + 1} started at tick "
            f"{match.round_start_tick} "
            f"({match.round_duration_ticks} ticks, "
            f"{match.round_deadline_seconds():.0f}s)"
        )

    @staticmethod
    def _broadcast_round_message(server, match, message_type, value):
        packet = encode_server_command_batch((ServerCommand(
            MESSAGE_COMMAND, Message(message_type, (MessageArgument(1, value),)), 0,
        ),))
        for session in match.live_sessions():
            server._send_pvp(session, packet, label="pvp-round-message")

    @staticmethod
    def _match_countdown_digit(server, match, generation, *, seconds):
        with match.lock:
            if (match.phase != MatchPhase.COUNTDOWN
                    or generation != match.info_generation):
                return
        CharacterService._broadcast_round_message(server, match, 5, seconds)

    @staticmethod
    def _send_battleground_effects(server, s, *, start=False):
        # The zero-duration cooldown reset is a one-shot client modifier and
        # must still be sent; the ordinary skill-buff helper skips duration 0.
        definitions = _round_effects(str(server.static_database_path), start)
        tick = CharacterService._session_tick(s)
        entries = []
        with s.flow_lock:
            if start:
                # End the setup locks explicitly even if a client/server
                # effect clock is a few ticks behind the match timer.
                setup_ids = {effect.effect_id for effect in _round_effects(
                    str(server.static_database_path), False,
                )}
                for key, (entry, _expiry) in tuple(s.gameplay_runtime_status_effects.items()):
                    if entry.effect_id in setup_ids:
                        s.gameplay_runtime_status_effects.pop(key)
                        entries.append(replace(entry, has_details=False, values=()))
            for definition in definitions:
                duration = int(definition.duration_ticks)
                instance_id = s.gameplay_next_status_effect_instance_id
                s.gameplay_next_status_effect_instance_id += 1
                entry = StatusEffectEntry(
                    effect_id=definition.effect_id, field_04=instance_id,
                    field_08=tick, field_0c=(tick + duration) & 0xFFFFFFFF,
                    field_10=duration, field_14=SERVER_TICKS_PER_SECOND,
                    field_18=0, field_1c=100, field_20=LOCAL_PLAYER_ACTOR_ID,
                    field_25=False, field_26=False, field_39=False,
                    has_details=True, values=definition.values,
                )
                entries.append(entry)
                if duration:
                    s.gameplay_runtime_status_effects[instance_id] = (
                        entry, time.perf_counter() + duration / SERVER_TICKS_PER_SECOND,
                    )
        if entries:
            value = StatusEffect(False, tuple(entries))
            server._send_pvp(s, encode_status_effect_batch(
                value, actor_id=LOCAL_PLAYER_ACTOR_ID,
            ), label="pvp-round-effects")
            if s.gameplay_presence_callback is not None:
                s.gameplay_presence_callback(s, ("status_effects", (value,)))

    @staticmethod
    def _apply_battleground_map_effects(server, s: Session) -> None:
        """Install the arena's authored map effects (``pvp_char_modifier``).

        The PvP balance buff is not carried in the character save; it belongs
        to the arena's map-access row. Applying it here, after the arena's
        initialization batch, is what gives the client -- and the authoritative
        stat engine -- the PvP damage/health/speed/mitigation ruleset.
        """

        access_id = str(
            getattr(s, "gameplay_map_access_id", "") or s.gameplay_map or ""
        )
        definitions = map_status_effect_definitions(
            str(server.static_database_path), access_id,
        )
        if not definitions:
            return
        entries = []
        instance_ids = set()
        seen_effect_ids = set()
        with s.flow_lock:
            for definition in definitions:
                effect_id = int(definition.effect_id)
                if effect_id in seen_effect_ids:
                    continue
                seen_effect_ids.add(effect_id)
                # A re-bound session must refresh the buff, never stack it.
                for instance_id, entry in tuple(
                    s.gameplay_persistent_status_effects.items()
                ):
                    if int(entry.effect_id) == effect_id:
                        s.gameplay_persistent_status_effects.pop(instance_id)
                        entries.append(replace(
                            entry, has_details=False, values=(),
                        ))
                instance_id = s.gameplay_next_status_effect_instance_id
                s.gameplay_next_status_effect_instance_id += 1
                entry = map_status_effect_entry(
                    definition, instance_id=instance_id,
                    actor_id=LOCAL_PLAYER_ACTOR_ID,
                )
                s.gameplay_persistent_status_effects[instance_id] = entry
                instance_ids.add(instance_id)
                entries.append(entry)
            s.gameplay_map_status_effect_instance_ids = frozenset(
                instance_ids
            )
        value = StatusEffect(False, tuple(entries))
        server._send_pvp(s, encode_status_effect_batch(
            value, actor_id=LOCAL_PLAYER_ACTOR_ID,
        ), label="pvp-map-effects")
        if s.gameplay_presence_callback is not None:
            s.gameplay_presence_callback(s, ("status_effects", (value,)))
        CharacterService._refresh_player_combat_vitals(
            s, inventory_changed=False,
        )
        print(
            f"[{GAMEPLAY_SERVICE_NAME}] applied map effects "
            f"{sorted(seen_effect_ids)} on {access_id}"
        )

    @staticmethod
    def _clear_battleground_mercenary_effects(server, s: Session) -> None:
        """Remove the Shadow Soul and its owner aura from an arena entrant.

        The summoned actor is retired with the arena's player-summon cleanup,
        but the permanent ShadowSoul status is tracked separately on the
        session and would otherwise remain as a player buff after the pet
        disappears. Keep the selected mercenary in saved character state so
        it can be restored after the player leaves the arena.
        """

        removed: dict[int, object] = {}
        with s.flow_lock:
            for attribute in (
                "gameplay_mercenary_status_effect",
                "gameplay_mercenary_aura_status_effect",
            ):
                entry = getattr(s, attribute, None)
                setattr(s, attribute, None)
                if entry is None:
                    continue
                instance_id = int(entry.field_04)
                current = s.gameplay_persistent_status_effects.pop(
                    instance_id, entry,
                )
                removed[instance_id] = current
                s.gameplay_timed_status_effect_expiries.pop(
                    instance_id, None,
                )
            for entry in tuple(getattr(
                s, "gameplay_mercenary_aura_status_effects", ()
            )):
                instance_id = int(entry.field_04)
                current = s.gameplay_persistent_status_effects.pop(
                    instance_id, entry,
                )
                removed[instance_id] = current
                s.gameplay_timed_status_effect_expiries.pop(
                    instance_id, None,
                )
            s.gameplay_mercenary_aura_status_effects = ()
            # The active selection remains saved, but must not respawn its
            # actor or recreate the buff while this arena session is active.
            s.gameplay_mercenary_restore_pending = False

        if not removed:
            return
        value = StatusEffect(False, tuple(replace(
            entry, has_details=False, values=(), field_25=False,
            field_39=False, field_38=False, field_3c=0xFF, shape_values=(),
        ) for entry in removed.values()))
        server._send_pvp(s, encode_status_effect_batch(
            value, actor_id=LOCAL_PLAYER_ACTOR_ID,
        ), label="pvp-shadow-soul-cleanse")
        if s.gameplay_presence_callback is not None:
            s.gameplay_presence_callback(s, ("status_effects", (value,)))
        CharacterService._refresh_player_combat_vitals(
            s, inventory_changed=False,
        )

    @staticmethod
    def _clear_battleground_map_effects(server, s: Session) -> None:
        """Retire the arena's map-scoped effects before leaving it.

        They are permanent while on the map, so the removal batch has to say
        so explicitly; otherwise the client keeps the PvP stats in the hub.
        """

        with s.flow_lock:
            instance_ids = tuple(
                getattr(s, "gameplay_map_status_effect_instance_ids", ())
            )
            s.gameplay_map_status_effect_instance_ids = frozenset()
            entries = []
            for instance_id in instance_ids:
                s.gameplay_timed_status_effect_expiries.pop(
                    instance_id, None
                )
                entry = s.gameplay_persistent_status_effects.pop(
                    instance_id, None
                )
                if entry is not None:
                    entries.append(entry)
        if not entries:
            return
        value = StatusEffect(False, tuple(replace(
            entry, has_details=False, values=(), field_25=False,
            field_39=False, field_38=False, field_3c=0xFF, shape_values=(),
        ) for entry in entries))
        server._send_pvp(s, encode_status_effect_batch(
            value, actor_id=LOCAL_PLAYER_ACTOR_ID,
        ), label="pvp-map-effects-cleanse")
        if s.gameplay_presence_callback is not None:
            s.gameplay_presence_callback(s, ("status_effects", (value,)))
        CharacterService._refresh_player_combat_vitals(
            s, inventory_changed=False,
        )

    @staticmethod
    def _clear_battleground_world_effects(server, match):
        """Retire arena casts, including their map actors and client visuals."""
        sessions = tuple(p.session for p in match.participants if p.session is not None)
        worlds = {}
        for session in sessions:
            CharacterService._clear_battleground_mercenary_effects(
                server, session,
            )
            with session.flow_lock:
                timers = tuple(session.gameplay_projectile_timers.values())
                session.gameplay_projectile_timers.clear()
                session.gameplay_pending_pvp_hits.clear()
                combat = session.gameplay_combat_state
            for timer in timers:
                timer.cancel()
            if combat is not None and hasattr(combat, "expire_player_summons_for_owner"):
                worlds.setdefault(id(combat), (combat, []))[1].append(session)
        for combat, viewers in worlds.values():
            owners = {int(s.gameplay_remote_actor_id) for s in viewers}
            retired = set()
            with combat.lock:
                # Include client actors already waiting for a deferred despawn.
                # Preserve any active summons owned by unrelated map actors.
                unrelated = {actor_id for summon in combat.player_summons.values()
                             if summon.owner_actor_id not in owners
                             for actor_id in summon.client_actor_ids}
                for owner in owners:
                    retired.update(combat.expire_player_summons_for_owner(owner))
                    visuals = combat.expire_player_location_visuals_for_owner(owner)
                traps = tuple(trap.trap_id for trap in combat.player_traps.values()
                              if trap.owner_actor_id in owners)
                for trap_id in traps:
                    combat.remove_player_trap(trap_id)
                for pending_hits in (combat.pending_player_hits, combat.pending_runtime_player_hits):
                    for key, pending in tuple(pending_hits.items()):
                        if pending.source_actor_id in owners or pending.target_actor_id in retired:
                            pending_hits.pop(key)
                combat.player_location_aura_members.intersection_update(
                    key for key in combat.player_location_aura_members
                    if key[0] in combat.player_location_visuals
                )
            for viewer in viewers:
                with viewer.flow_lock:
                    retired.update(viewer.gameplay_summon_actor_ids - unrelated)
            # Existing presence events reach every peer, including viewers
            # who moved away after registering a summon or ground effect.
            source = viewers[0]
            if retired:
                CharacterService._publish_player_summon_despawn(source, tuple(sorted(retired)))
            for trap_id in traps:
                CharacterService._publish_player_trap_discard(source, trap_id)
            callback = source.gameplay_presence_callback
            if callback is not None:
                callback(source, ("player_location_visuals", visuals))
            else:
                CharacterService._send_player_location_visuals_to(source, visuals)

    @staticmethod
    def _clear_battleground_round_effects(server, s):
        """Remove the previous round's transient effects from both clients."""
        debuffs = _arena_debuff_ids(str(server.static_database_path))
        removed = []
        timers = []
        with s.flow_lock:
            removed.extend(entry for entry, _ in s.gameplay_runtime_status_effects.values())
            s.gameplay_runtime_status_effects.clear()
            for key, (entry, _expiry) in tuple(s.gameplay_victim_status_effect_instances.items()):
                if int(key[0]) == LOCAL_PLAYER_ACTOR_ID:
                    removed.append(entry)
                    s.gameplay_victim_status_effect_instances.pop(key)
            for key, entry in tuple(s.gameplay_persistent_status_effects.items()):
                if int(entry.effect_id) in debuffs:
                    removed.append(entry)
                    s.gameplay_persistent_status_effects.pop(key)
                    s.gameplay_timed_status_effect_expiries.pop(key, None)
            for key, timer in tuple(s.gameplay_projectile_timers.items()):
                if isinstance(key, tuple) and key[0] in (
                        "player-status-tick", "pvp-status-damage", "pvp-hit"):
                    timers.append(timer)
                    s.gameplay_projectile_timers.pop(key)
            s.gameplay_pending_pvp_hits.clear()
            s.gameplay_revive_movement_until = 0.0
            s.gameplay_disabled_feature_cache = None
        for timer in timers:
            timer.cancel()
        if removed:
            value = StatusEffect(False, tuple(replace(
                entry, has_details=False, values=(), field_25=False,
                field_39=False, field_38=False, field_3c=0xFF, shape_values=(),
            ) for entry in removed))
            server._send_pvp(s, encode_status_effect_batch(
                value, actor_id=LOCAL_PLAYER_ACTOR_ID,
            ), label="pvp-round-cleanse")
            if s.gameplay_presence_callback is not None:
                s.gameplay_presence_callback(s, ("status_effects", (value,)))
            CharacterService._refresh_player_combat_vitals(s)

    @staticmethod
    def _session_tick(s) -> int:
        """This client's current tick, as the server last observed it.

        The server does not own the clock the client counts against: it learns
        it from the tick every MoveCommand carries, and the combat loop
        advances it from wall time between moves. Sending a match clock in any
        other base makes the client compute a nonsensical remaining time and
        the round timer never moves.
        """

        if s is None:
            return 0
        return int(getattr(s, "gameplay_chase_server_tick", 0) or 0) & 0xFFFFFFFF

    @staticmethod
    def _restore_battleground_health(server, s: Session) -> None:
        """Bring a participant back to full for the next round.

        ActorStatsUpdate restores the pools. ReviveCommand removes the
        client's Death state and ends the corpse animation; it is emitted
        for both participants during countdown setup, as in the capture.
        """

        combat = s.gameplay_combat_state
        if combat is None:
            return
        # The combat world keys players by the character-scoped actor id that
        # world initialization registered them under, never by the client's
        # own actor 1 -- that number only exists on the wire, addressed to the
        # player's own socket. Looking the pool up by 1 silently found nothing
        # and made this whole restore a no-op, so a player who died stayed at
        # zero health for the rest of the match and every later hit on them
        # resolved to zero damage and was dropped.
        actor_id = int(
            getattr(s, "gameplay_remote_actor_id", 0)
            or CharacterService._remote_actor_id(
                CharacterService._match_character_id(s) or 0
            )
        )
        with combat.lock:
            health = combat.player_health.get(actor_id)
            if health is None:
                print(
                    f"[{GAMEPLAY_SERVICE_NAME}] WARNING: actor {actor_id} is "
                    "not registered in the arena combat world; cannot restore "
                    f"{s.addr} for the next round"
                )
                return
            health[0] = int(health[1])
            current_health = int(health[0])
            resource = combat.player_resource.get(actor_id, [0.0])[0]
        with s.flow_lock:
            s.gameplay_death_pending = False
            inventory = s.gameplay_inventory
            if inventory is not None:
                inventory.health_points = float(current_health)
        server._send_pvp(
            s,
            encode_actor_stats_update_batch(
                ActorStatsUpdate(current_health, resource),
                actor_id=LOCAL_PLAYER_ACTOR_ID,
            ),
            label="pvp-round-restore",
        )
        server._send_pvp(
            s, encode_revive(actor_id=LOCAL_PLAYER_ACTOR_ID),
            label="pvp-round-revive",
        )
        if s.gameplay_presence_callback is not None and s.gameplay_spawn_position is not None:
            move = s.gameplay_last_move
            s.gameplay_presence_callback(s, (
                "player_revived", s.gameplay_spawn_position,
                move.yaw if move is not None else 0.0, current_health, resource,
            ))

    @staticmethod
    def _repeat_match_info(server, match: PVPMatch, generation: int) -> None:
        with match.lock:
            if match.phase not in (MatchPhase.RUNNING, MatchPhase.COUNTDOWN):
                return
        server._broadcast_match_info(match)
        CharacterService._arm_match_timer(
            server, match, "info", MATCH_INFO_INTERVAL_SECONDS,
            CharacterService._repeat_match_info,
        )

    @staticmethod
    def _expire_match_round(server, match: PVPMatch, generation: int) -> None:
        """Decide a round that ran out its 3000 ticks with nobody dead.

        Retail's tiebreak is not visible in any recording -- all six recorded
        rounds ended on a kill well inside the limit -- so the round goes to
        whichever side dealt the most damage, and to the lowest active team
        only if even that is level.
        """

        with match.lock:
            if match.phase != MatchPhase.RUNNING:
                return
            teams = match.active_teams()
            if not teams:
                return
            winner = max(teams, key=lambda team: (
                sum(e.damage_dealt for e in match.team_members(team)),
                -team,
            ))
        CharacterService._finish_match_round(server, match, winner)

    @staticmethod
    def _pvp_match_opponents(source: Session, target: Session) -> bool:
        """Are these two sessions on opposing sides of a running round?

        Combat resolution asks this before allowing lethal damage, so it must
        be cheap and must not require a server handle.
        """

        match = getattr(source, "gameplay_match", None)
        if match is None or getattr(target, "gameplay_match", None) is not match:
            return False
        with match.lock:
            if match.phase != MatchPhase.RUNNING:
                return False
            source_team = getattr(source, "gameplay_match_team", None)
            target_team = getattr(target, "gameplay_match_team", None)
            if source_team is None or target_team is None:
                return False
            return int(source_team) != int(target_team)

    def _handle_match_event(self, s: Session, event) -> None:
        """Apply one battleground scoring event raised by combat resolution."""

        if not isinstance(event, tuple) or not event:
            return
        kind = event[0]
        if kind == "match_damage":
            match = getattr(s, "gameplay_match", None)
            character_id = CharacterService._match_character_id(s)
            if match is not None and character_id is not None:
                match.record_damage(character_id, int(event[1]))
        elif kind == "match_kill":
            CharacterService._report_match_kill(self, s, event[1])

    def _report_match_kill(self, killer: Session | None,
                           victim: Session) -> None:
        """Record one arena kill and close the round if it was decisive."""

        match = getattr(victim, "gameplay_match", None)
        if match is None:
            return
        victim_id = CharacterService._match_character_id(victim)
        killer_id = (
            CharacterService._match_character_id(killer)
            if killer is not None
            else None
        )
        if victim_id is None:
            return
        with match.lock:
            previous_deaths = match.participant(victim_id).deaths if match.participant(victim_id) else 0
        decided = match.record_kill(
            killer_id if killer_id is not None else -1, victim_id
        )
        with match.lock:
            entry = match.participant(victim_id)
            victim_team = entry.team if entry is not None else 0
            teams = [t for t in match.active_teams() if t != victim_team]
        if killer is not None and entry is not None and entry.deaths > previous_deaths:
            self._daily_challenge_feed(killer, 'PlayersKilled')
        self._broadcast_match_info(match)
        if not decided:
            return
        CharacterService._finish_match_round(
            self, match, teams[0] if teams else victim_team
        )

    @staticmethod
    def _finish_match_round(server, match: PVPMatch, winning_team: int) -> None:
        CharacterService._cancel_match_timers(match)
        over = match.award_round(winning_team)
        server._broadcast_match_info(match)
        print(
            f"[{GAMEPLAY_SERVICE_NAME}] PvP match {match.match_id} round "
            f"{match.round_index} won by team {winning_team}; score "
            f"{match.team_rounds_won}"
        )
        if over:
            CharacterService._arm_match_timer(
                server, match, "over", MATCH_OVER_DELAY_SECONDS,
                CharacterService._finish_match,
            )
            return
        CharacterService._arm_match_timer(
            server, match, "intermission", ROUND_INTERMISSION_SECONDS,
            CharacterService._resume_match_rounds,
        )

    @staticmethod
    def _resume_match_rounds(
            server, match: PVPMatch, generation: int) -> None:
        with match.lock:
            if match.phase != MatchPhase.INTERMISSION:
                return
        CharacterService._begin_match_round(server, match)

    # ------------------------------------------------------------------
    # completion
    # ------------------------------------------------------------------

    @staticmethod
    def _finish_match(server, match: PVPMatch, generation: int) -> None:
        CharacterService._cancel_match_timers(match)
        # Honor is credited exactly once, but the overview is not gated on
        # that claim: the result timer and a participant leaving can both
        # arrive here, and whoever is still standing in the arena has to be
        # shown the result either way.
        credit = match.claim_settlement()
        with match.lock:
            winning_team = int(match.winning_team)
            rounds = match.round_index
            participants = match.participants
            match.phase = MatchPhase.OVER
        over = encode_match_over(MatchOver(
            server_tick=match.tick(),
            winning_team=winning_team,
            flag=False,
        ))
        for entry in participants:
            session = entry.session
            if session is None:
                continue
            won = entry.team == winning_team
            honor = entry.honor_earned(rounds, match_won=won)
            total = int(getattr(session, "gameplay_honor_points", 0))
            if credit:
                with session.flow_lock:
                    session.gameplay_honor_points = total + honor
                    session.gameplay_honor_points_today = (
                        int(getattr(session, "gameplay_honor_points_today", 0)) + honor
                    )
                    attribute = (
                        "gameplay_pvp_matches_won" if won
                        else "gameplay_pvp_matches_lost"
                    )
                    setattr(session, attribute, int(
                        getattr(session, attribute, 0)
                    ) + 1)
                    total = int(session.gameplay_honor_points)
                    session.gameplay_pvp_kills = (
                        int(getattr(session, "gameplay_pvp_kills", 0)) + entry.kills)
                    session.gameplay_pvp_deaths = (
                        int(getattr(session, "gameplay_pvp_deaths", 0)) + entry.deaths)
                    session.gameplay_pvp_kills_today = (
                        int(getattr(session, "gameplay_pvp_kills_today", 0)) + entry.kills)
                    session.gameplay_pvp_kills_battle_total = (
                        int(getattr(session, "gameplay_pvp_kills_battle_total", 0)) + entry.kills)
                    session.gameplay_pvp_kills_battle_today = (
                        int(getattr(session, "gameplay_pvp_kills_battle_today", 0)) + entry.kills)

                    is_solo = (
                        getattr(match, "mode", None) == MATCH_MODE_BEST_OF_3_1V1
                        or len(match.participants) <= 2
                    )
                    if is_solo:
                        session.gameplay_pvp_solo_matches_played = (
                            int(getattr(session, "gameplay_pvp_solo_matches_played", 0)) + 1
                        )
                        if won:
                            session.gameplay_pvp_solo_matches_won = (
                                int(getattr(session, "gameplay_pvp_solo_matches_won", 0)) + 1
                            )
                        session.gameplay_pvp_kills_alone_total = (
                            int(getattr(session, "gameplay_pvp_kills_alone_total", 0)) + entry.kills
                        )
                        session.gameplay_pvp_kills_alone_today = (
                            int(getattr(session, "gameplay_pvp_kills_alone_today", 0)) + entry.kills
                        )
                    else:
                        session.gameplay_pvp_team_matches_played = (
                            int(getattr(session, "gameplay_pvp_team_matches_played", 0)) + 1
                        )
                        if won:
                            session.gameplay_pvp_team_matches_won = (
                                int(getattr(session, "gameplay_pvp_team_matches_won", 0)) + 1
                            )
                        session.gameplay_pvp_kills_group_total = (
                            int(getattr(session, "gameplay_pvp_kills_group_total", 0)) + entry.kills
                        )
                        session.gameplay_pvp_kills_group_today = (
                            int(getattr(session, "gameplay_pvp_kills_group_today", 0)) + entry.kills
                        )
            # Publish each row's final payout and this account's new career
            # total before opening the result panel.
            server._send_match_info(session, match)
            server._send_pvp(
                session,
                encode_honor_points_changed(HonorPointsChanged(
                    total, actor_id=LOCAL_PLAYER_ACTOR_ID
                )),
                label="pvp-honor-points",
            )
            server._send_pvp(session, over, label="pvp-match-over")
            if credit:
                server._daily_challenge_feed(session, 'BattlegroundsPlayed')
                if won:
                    server._daily_challenge_feed(session, 'MatchesWon')
                # Arena quests count finished battles and wins per
                # battleground id (<Event type="battle|victory"
                # value="BestOf5_5v5_01_default">).
                try:
                    server._quest_feed(session, "battle", match.battleground_id)
                    if won:
                        server._quest_feed(
                            session, "victory", match.battleground_id
                        )
                except Exception as exc:
                    print(
                        f"[{GAMEPLAY_SERVICE_NAME}] PvP quest progress failed "
                        f"for character {entry.character_id}: "
                        f"{type(exc).__name__}: {exc}"
                    )
                CharacterService._request_account_save(
                    session, "pvp match finished", flush=True
                )
            print(
                f"[{GAMEPLAY_SERVICE_NAME}] character {entry.character_id} "
                f"{'won' if won else 'lost'} PvP match {match.match_id} "
                f"(winning team {winning_team}): +{honor} honor "
                f"(total {total}), {entry.kills}/{entry.deaths} K/D, "
                f"{entry.damage_dealt} damage"
            )

    def _handle_leave_pvp(self, s: Session, payload: bytes) -> None:
        try:
            decode_leave_pvp(payload, s.current_message_bit_length)
        except (PVPMatchProtocolError, CharacterSelectionError) as exc:
            print(f"[{GAMEPLAY_SERVICE_NAME}] rejected LeavePVP: {exc}")
            return
        CharacterService._leave_battleground(self, s, reason="client request")

    def _forfeit_battleground(self, match: PVPMatch, character_id: int, *,
                              reason: str) -> None:
        """Remove one player from a battleground and resolve what is left.

        A player who leaves -- by pressing leave, by quitting, or by dropping
        the connection -- concedes. With one side gone the match is decided
        immediately and awarded to the last team standing, rather than being
        left to run its rounds out against nobody.
        """

        ended = match.forfeit(character_id)
        with match.lock:
            phase = match.phase
            winning_team = match.winning_team
            anyone_left = any(
                e.session is not None and not e.left
                for e in match.participants
            )
        print(
            f"[{GAMEPLAY_SERVICE_NAME}] character {character_id} left PvP "
            f"match {match.match_id} ({reason})"
        )
        if ended and phase == MatchPhase.OVER:
            print(
                f"[{GAMEPLAY_SERVICE_NAME}] PvP match {match.match_id} "
                f"awarded to team {winning_team} by forfeit"
            )
            CharacterService._finish_match(self, match, 0)
        if not anyone_left:
            CharacterService._cancel_match_timers(match)
            CharacterService._matchmaking_registry(self).retire(
                match.match_id
            )

    def _leave_battleground(self, s: Session, *, reason: str) -> None:
        """Detach a session from its match and send it back to the hub."""

        match = getattr(s, "gameplay_match", None)
        character_id = CharacterService._match_character_id(s)
        CharacterService._clear_battleground_map_effects(self, s)
        with s.flow_lock:
            s.gameplay_match = None
            s.gameplay_match_team = None
        if match is None or character_id is None:
            return
        CharacterService._forfeit_battleground(
            self, match, character_id, reason=reason
        )
        # The battleground forced the PvP flag on; the hub gets the account's
        # own setting back.
        store = getattr(self, "account_store", None)
        persisted = store.get(character_id) if store is not None else None
        CharacterService._force_battleground_pvp(
            self, s,
            enabled=bool(getattr(persisted, "pvp_enabled", False)),
        )
        try:
            CharacterService._begin_map_transfer(
                self,
                s,
                DEFAULT_GAMEPLAY_MAP,
                reason="pvp-leave",
                gameplay_switch=True,
            )
        except (OSError, ValueError) as exc:
            print(
                f"[{GAMEPLAY_SERVICE_NAME}] could not return character "
                f"{character_id} to {DEFAULT_GAMEPLAY_MAP}: "
                f"{type(exc).__name__}: {exc}"
            )

    def _release_matchmaking_session(self, s: Session) -> None:
        """Drop a disconnecting session from the queue and any match."""

        character_id = CharacterService._match_character_id(s)
        if character_id is None:
            return
        registry = getattr(self, "_pvp_matchmaking", None)
        if registry is None:
            return
        registry.dequeue(character_id)
        match = getattr(s, "gameplay_match", None)
        with s.flow_lock:
            s.gameplay_match_queue_entry = None
            s.gameplay_match = None
            s.gameplay_match_team = None
        if match is None:
            return
        with match.lock:
            entry = match.participant(character_id)
            if entry is None or entry.session is not s:
                # The slot already belongs to a newer socket; nothing to do.
                return
            if not entry.arrived:
                # A map transfer into the arena retires the source socket
                # before the destination connects. That is not a departure --
                # the arriving session re-binds this slot.
                entry.session = None
                return
        # Anything else is a player leaving a battleground they were standing
        # in: a quit, a crash, or a dropped connection. Treat it exactly like
        # LeavePVP so the match resolves instead of stalling.
        CharacterService._forfeit_battleground(
            self, match, character_id, reason="connection lost"
        )

    # ------------------------------------------------------------------
    # scoreboard
    # ------------------------------------------------------------------

    def _match_info_for(self, s: Session, match: PVPMatch) -> MatchInfo:
        """Build the scoreboard as this viewer's client knows the actors.

        Every client calls its own player actor 1 and knows its peers by the
        remote actor ids presence handed out, so the rows cannot be shared
        between viewers.
        """

        viewer_id = CharacterService._match_character_id(s)
        with match.lock:
            chances = match.win_chances()
            rounds = match.rounds_played()
            viewer = match.participant(viewer_id)
            viewer_team = viewer.team if viewer is not None else -1
            teams = []
            for team in range(match.team_count):
                rows = []
                for entry in match.team_members(team):
                    # A row must never carry actor 0: the client resolves
                    # these ids against its own actor table, and a departed
                    # player still has to appear on the overview.
                    # Match the actor table on this socket: our NewPlayer
                    # assigns actor 1 locally, unlike retail's shared allocator.
                    actor_id = int(
                        getattr(entry.session, "gameplay_remote_actor_id", 0)
                        or entry.actor_id
                        or CharacterService._remote_actor_id(
                            entry.character_id
                        )
                    )
                    if entry.character_id == viewer_id:
                        actor_id = LOCAL_PLAYER_ACTOR_ID
                    honor = entry.honor_earned(
                        rounds, match_won=(match.phase == MatchPhase.OVER
                                           and entry.team == match.winning_team),
                    )
                    rows.append(MatchPlayer(
                        actor_id=actor_id,
                        honor_total=honor,
                        honor_rounds_won=(
                            HONOR_PER_ROUND_WON * entry.rounds_won
                        ),
                        honor_participation=(
                            HONOR_PER_ROUND_PLAYED * rounds
                        ),
                        honor_bonus=HONOR_PER_KILL * entry.kills,
                        rounds_won=entry.rounds_won,
                        kills=entry.kills,
                        deaths=entry.deaths,
                        damage_dealt=entry.damage_dealt,
                        score=entry.damage_dealt,
                        support=0.0,
                        # Career honor is retained as captured metadata;
                        # honor_total above is the actual match payout.
                        rating=entry.honor_at_start,
                        level=entry.level,
                        enemy=entry.team != viewer_team,
                        name=entry.name,
                        class_id=entry.class_id,
                        stats=(),
                        rounds_lost=entry.rounds_lost,
                    ))
                teams.append(MatchTeam(
                    team_id=team,
                    rounds_won=match.team_rounds_won[team],
                    score=0,
                    win_chance=(
                        chances[team] if team < len(chances) else 0.0
                    ),
                    players=tuple(rows),
                ))
            return MatchInfo(
                # Same per-client tick base as PVPMatchStart; the HUD timer
                # reads its remaining time from here between rounds.
                round_start_tick=(
                    viewer.client_round_start_tick if viewer is not None else 0
                ),
                round_duration_ticks=match.round_duration_ticks,
                teams=tuple(teams),
                battleground_id=match.battleground_id,
                stage=0,
            )

    def _send_match_info(self, s: Session, match: PVPMatch) -> None:
        try:
            payload = encode_match_info(
                CharacterService._match_info_for(self, s, match)
            )
        except (PVPMatchProtocolError, CharacterSelectionError) as exc:
            print(
                f"[{GAMEPLAY_SERVICE_NAME}] could not build PVPMatchInfo for "
                f"match {match.match_id}: {exc}"
            )
            return
        self._send_pvp(s, payload, label="pvp-match-info")

    def _broadcast_match_info(self, match: PVPMatch) -> None:
        for session in match.live_sessions():
            CharacterService._send_match_info(self, session, match)
