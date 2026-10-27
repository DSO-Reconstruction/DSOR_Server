"""Assault the Fortress (``Ruleset`` CastleRush) on the objective engine.

The match runs on ``battleground_objectives.py``'s single objective round;
this mixin supplies what is specific to the fortress:

* **The structures are the map's own content.** ``f4103_atf`` places every
  defense tower, the two stage targets (drawbridge mechanism, thunder-gun
  controls) and the ``*_invincible`` stand-ins as ``_Instance_SpawnPoint``
  rows owned by ``BlueTeam``. The map's monster encounter and the map-event
  runtime spawn them into the match's own combat world (the match has its
  own instance id, so concurrent matches never share one), and the authored
  ``MonstersEliminated`` signals start the map-local events that swap
  stand-ins for vulnerable towers. The server starts the one event no
  signal starts -- ``pvp_castlerush_initial`` -- when the round begins, and
  replaces it with ``pvp_castlerush_stage_1`` when stage 0 is cleared.
* **Team ownership** (``LocalCombatState`` ``template_teams``): structures
  belong to the defenders (team 0, BlueTeam), the control points' spawners
  and their rammers/bombs to the attackers (team 1, RedTeam, the
  ``OwnerTeam`` of the control points). A structure only targets the other
  side and its own side cannot harm it.
* **Stage gating** (``FortressState``): a stage target is only vulnerable in
  its own stage -- the drawbridge mechanism once the core tower has fallen
  (its fall starts ``pvp_castlerush_bridge_mechanism``) -- and stand-ins
  never are. The server enforces this by toggling the monsters'
  ``invulnerable`` flag after every kill.
* **Client state:** ``PVPStateCommand`` 8/9 (attacker/defender objective line
  of stage 1/2), ``StageInfoCommand`` (current stage target and its health
  fraction, CastleRush maps only), ``MessageCommand`` 24 when stage 0 falls
  (``attackerInfoClearedStage1`` for team index 1, ``defenderInfoCleared
  Stage1`` otherwise) and the scoreboard's Score column, which on ruleset 13
  is ``scoredPoints`` -- "damage caused to machines or targets".
* **Control points:** an attacker who channels a point for its authored
  ``CaptureTime`` (10 s) inside ``PickingRange`` captures it, and its
  ``_Template_PropMechanism.MonsterId`` spawner appears for the attackers.
* **Clearing stage 0:** the drawbridge's fall starts the authored
  ``pvp_castlerush_kill_stage_01_tower`` event, whose destroying location
  auras wipe the first stage's towers and siege units inside their box; the
  server retires those monsters the same moment.
* **Winning:** the attackers win when the last stage target falls; the
  defenders win when the authored ``RoundDuration`` runs out. Clearing a
  stage extends the round by ``_Globals.PVPRoundTimeModifier`` (240 s) and
  reports it as PVPMatchInfo's time bonus (see below).
"""

from __future__ import annotations

from functools import partial

from dso_server.runtime.context import *
from dso_server.runtime.transport import Session
from dso_server.protocol.gameplay_protocol import Message
from dso_server.protocol.player_init_protocol import MESSAGE_COMMAND
from dso_server.protocol.pvp_match_protocol import (
    MATCH_TICK_HZ,
    PVP_STATE_CASTLE_RUSH_STAGE_1,
    PVP_STATE_CASTLE_RUSH_STAGE_2,
    PVPState,
    StageInfo,
    encode_stage_info,
)
from dso_server.gameplay.map_event_state import GatedSpawnRow
from dso_server.gameplay.pvp_match_state import MatchPhase, PVPMatch
from dso_server.gameplay.pvp_objectives import (
    CASTLE_RUSH_INITIAL_EVENT,
    CASTLE_RUSH_STAGE_EVENT,
    INVINCIBLE_SUFFIX,
    FORTRESS_ATTACKERS_WON,
    FORTRESS_STAGE_COMPLETED,
    FortressState,
    horizontal_distance,
    load_fortress_layout,
    load_globals_value,
)


# ``MessageCommand`` 24: stage 0 cleared. No arguments; the client picks the
# attacker or defender text from its own team index (0x140616b9c).
FORTRESS_MESSAGE_STAGE_CLEARED = 24
# Capture is lost when the capturer strays this far beyond the point's
# authored PickingRange during the channel (movement tolerance; local).
CAPTURE_RANGE_TOLERANCE = 1.0
# Runtime spawn rows for captured control points; below every authored and
# every other synthetic row id (the f4203 ram uses -2_100_000 - n).
_CAPTURE_SPAWN_ROW_BASE = -3_100_000


class BattlegroundFortressService:
    """Assault-the-Fortress rules for the battleground runtime."""

    # ------------------------------------------------------------------
    # setup
    # ------------------------------------------------------------------

    @staticmethod
    def _prepare_fortress_objective(server, match: PVPMatch, settings) -> bool:
        layout = load_fortress_layout(server.static_database_path, match.map_id)
        if layout is None or match.team_count != 2:
            print(
                f"[{GAMEPLAY_SERVICE_NAME}] {match.map_id} has no complete "
                "fortress layout"
            )
            return False
        with match.lock:
            match.objective = FortressState(layout)
            match.duration_ticks = max(1, int(round(
                settings.round_duration_seconds * MATCH_TICK_HZ
            )))
            match.rounds_to_win = 1
            match.time_bonus_ticks = 0
            match.fortress_captures = {}
        return True

    @staticmethod
    def _is_fortress_match(match) -> bool:
        return isinstance(getattr(match, "objective", None), FortressState)

    @staticmethod
    def _fortress_combat(match: PVPMatch):
        combat = getattr(match, "gameplay_combat_state", None)
        if combat is not None:
            return combat
        for session in match.live_sessions():
            combat = getattr(session, "gameplay_combat_state", None)
            if combat is not None:
                return combat
        return None

    @staticmethod
    def _fortress_bind_session(server, s: Session, match: PVPMatch) -> None:
        """Give an arriving participant, and the structures, their teams."""

        objective = match.objective
        combat = getattr(s, "gameplay_combat_state", None)
        if combat is None:
            return
        character_id = CharacterService._match_character_id(s)
        entry = match.participant(character_id) if character_id is not None else None
        if entry is None:
            return
        actor_id = int(
            getattr(s, "gameplay_remote_actor_id", 0) or entry.actor_id
            or CharacterService._remote_actor_id(entry.character_id)
        )
        with combat.lock:
            combat.assign_template_teams(objective.layout.team_templates())
            combat.set_player_team(actor_id, entry.team)
        CharacterService._sync_fortress_vulnerability(server, match)
        CharacterService._send_objective_state(server, s, match)

    @staticmethod
    def _start_fortress(server, match: PVPMatch) -> None:
        """Round start: open stage 0 in the map's event runtime."""

        for entry in match.participants:
            if entry.session is not None:
                CharacterService._fortress_bind_session(server, entry.session, match)
        CharacterService._fortress_set_event(
            server, match, CASTLE_RUSH_INITIAL_EVENT, True,
        )
        CharacterService._sync_fortress_vulnerability(server, match)
        CharacterService._broadcast_objective_state(server, match)

    @staticmethod
    def _fortress_runtime(match: PVPMatch):
        for session in match.live_sessions():
            runtime = getattr(session, "gameplay_map_event_runtime", None)
            if runtime is not None:
                return runtime
        return None

    @staticmethod
    def _fortress_set_event(server, match: PVPMatch, event_id: str,
                            active: bool) -> bool:
        """Start or stop a map-local event in this match's map runtime."""

        runtime = CharacterService._fortress_runtime(match)
        if runtime is None:
            print(
                f"[{GAMEPLAY_SERVICE_NAME}] PvP match {match.match_id}: no "
                f"map-event runtime to {'start' if active else 'stop'} {event_id}"
            )
            return False
        definition = runtime.catalog.event(event_id)
        if definition is None:
            return False
        if active:
            CharacterService._start_map_local_event(server, runtime, definition)
        else:
            CharacterService._stop_map_local_event(server, runtime, definition)
        return True

    # ------------------------------------------------------------------
    # stage gating
    # ------------------------------------------------------------------

    @staticmethod
    def _sync_fortress_vulnerability(server, match: PVPMatch) -> None:
        """Make the combat world's structures match ``FortressState``."""

        objective = match.objective
        combat = CharacterService._fortress_combat(match)
        if combat is None or not isinstance(objective, FortressState):
            return
        with match.lock:
            vulnerable_templates = {
                o.template_id.casefold() for o in objective.layout.objectives
                if objective.vulnerable(o.row_id)
            }
            structure_templates = {
                o.template_id.casefold() for o in objective.layout.objectives
            }
        with combat.lock:
            protect, expose = [], []
            for actor_id, monster in combat.monsters.items():
                template = str(monster.template_id).casefold()
                if template not in structure_templates:
                    continue
                if template in vulnerable_templates:
                    expose.append(actor_id)
                else:
                    protect.append(actor_id)
            combat.set_monster_invulnerable(protect, True)
            combat.set_monster_invulnerable(expose, False)

    # ------------------------------------------------------------------
    # kills, damage, stages
    # ------------------------------------------------------------------

    def _fortress_monsters_killed(self, s: Session, monsters) -> None:
        match = getattr(s, "gameplay_match", None)
        objective = getattr(match, "objective", None)
        if not isinstance(objective, FortressState):
            return
        CharacterService._fortress_refresh_captures(self, match)
        character_id = CharacterService._match_character_id(s)
        events = []
        with match.lock:
            entry = match.participant(character_id) if character_id is not None else None
            if entry is None or match.phase != MatchPhase.RUNNING:
                return
            for monster in monsters:
                result = objective.destroy_template(
                    str(getattr(monster, "template_id", "")),
                    attacker_team=entry.team,
                )
                if result:
                    entry.objectives_destroyed += 1
                events.extend(result)
        if not events:
            return
        CharacterService._fortress_apply_clear_effects(self, match)
        CharacterService._sync_fortress_vulnerability(self, match)
        for kind, spawn_id, stage in events:
            print(
                f"[{GAMEPLAY_SERVICE_NAME}] PvP match {match.match_id}: "
                f"{kind} {spawn_id} (stage {stage}) by character {character_id}"
            )
        kinds = [kind for kind, *_rest in events]
        if FORTRESS_ATTACKERS_WON in kinds:
            CharacterService._finish_objective_match(
                self, match, objective.layout.attacker_team,
            )
            return
        if FORTRESS_STAGE_COMPLETED in kinds:
            CharacterService._fortress_stage_completed(self, match)
            return
        CharacterService._broadcast_objective_state(self, match)
        self._broadcast_match_info(match)

    @staticmethod
    def _fortress_apply_clear_effects(server, match: PVPMatch) -> None:
        """Run destroying auras whose EventSet just became active."""

        objective = match.objective
        with match.lock:
            due = objective.due_clear_effects()
            for index, _effect in due:
                objective.apply_clear_effect(index)
        if not due:
            return
        combat = CharacterService._fortress_combat(match)
        if combat is None:
            return
        with combat.lock:
            doomed = []
            for actor_id, monster in combat.monsters.items():
                if combat.monster_health.get(actor_id, 0) <= 0:
                    continue
                template = str(monster.template_id).casefold()
                if template.endswith(INVINCIBLE_SUFFIX):
                    template = template[:-len(INVINCIBLE_SUFFIX)]
                position = combat.monster_positions.get(actor_id, monster.position)
                if any(template in effect.templates
                       and effect.volume.contains(position)
                       for _index, effect in due):
                    doomed.append(actor_id)
            retired = combat.retire_monsters(tuple(sorted(doomed)))
        CharacterService._fortress_discard_monsters(server, match, combat, retired)
        CharacterService._fortress_refresh_captures(server, match)
        print(
            f"[{GAMEPLAY_SERVICE_NAME}] PvP match {match.match_id}: stage "
            f"clearing auras destroyed {len(retired)} monster(s)"
        )

    @staticmethod
    def _fortress_discard_monsters(server, match, combat, actor_ids) -> None:
        """Tell every viewer that retired arena monsters are gone."""

        retired = set(int(actor_id) for actor_id in actor_ids)
        if not retired:
            return
        for viewer in match.live_sessions():
            with viewer.flow_lock:
                if viewer.gameplay_combat_state is not combat:
                    continue
                discard = tuple(sorted(
                    retired & viewer.gameplay_spawned_monster_actor_ids
                ))
                viewer.gameplay_spawned_monster_actor_ids.difference_update(retired)
                CharacterService._forget_published_monsters(viewer, retired)
                viewer.gameplay_announced_monster_actor_ids.difference_update(retired)
                viewer.gameplay_requested_actor_ids.difference_update(retired)
                for actor_id in retired:
                    viewer.gameplay_delivered_monster_positions.pop(actor_id, None)
            if discard:
                server._send_pvp(
                    viewer, encode_discard_monster_batch(discard),
                    label="pvp-fortress-discard-monster",
                )

    @staticmethod
    def _fortress_refresh_captures(server, match: PVPMatch) -> None:
        """Free a control point whose spawner is gone, for recapture."""

        objective = match.objective
        combat = CharacterService._fortress_combat(match)
        if combat is None:
            return
        with match.lock:
            spawned = {
                row_id: state["spawned"]
                for row_id, state in match.fortress_captures.items()
                if state.get("spawned")
            }
        if not spawned:
            return
        points = {p.row_id: p for p in objective.layout.control_points}
        with combat.lock:
            alive = [
                (str(monster.template_id).casefold(),
                 combat.monster_positions.get(actor_id, monster.position))
                for actor_id, monster in combat.monsters.items()
                if combat.monster_health.get(actor_id, 0) > 0
            ]
        freed = [
            row_id for row_id, template in spawned.items()
            if row_id in points and not any(
                name == str(template).casefold()
                and horizontal_distance(position, points[row_id].position) <= 2.0
                for name, position in alive
            )
        ]
        with match.lock:
            for row_id in freed:
                match.fortress_captures.pop(row_id, None)

    def _fortress_stage_completed(self, match: PVPMatch) -> None:
        """Stage 0 fell: open stage 1, extend the round, tell everyone."""

        objective = match.objective
        CharacterService._fortress_set_event(
            self, match, CASTLE_RUSH_INITIAL_EVENT, False,
        )
        CharacterService._fortress_set_event(
            self, match, CASTLE_RUSH_STAGE_EVENT.format(stage=objective.stage),
            True,
        )
        CharacterService._sync_fortress_vulnerability(self, match)
        packet = encode_server_command_batch((ServerCommand(
            MESSAGE_COMMAND, Message(FORTRESS_MESSAGE_STAGE_CLEARED, ()), 0,
        ),))
        for session in match.live_sessions():
            self._send_pvp(session, packet, label="pvp-fortress-stage-cleared")
        try:
            bonus_seconds = float(load_globals_value(
                str(self.static_database_path), "PVPRoundTimeModifier",
            ) or 0.0)
        except ValueError:
            bonus_seconds = 0.0
        bonus_ticks = max(0, int(round(bonus_seconds * MATCH_TICK_HZ)))
        with match.lock:
            match.duration_ticks += bonus_ticks
            match.time_bonus_ticks = bonus_ticks
            deadline = match.round_deadline_seconds()
        if bonus_ticks:
            CharacterService._arm_match_timer(
                self, match, "round", deadline,
                CharacterService._expire_match_round,
            )
        CharacterService._broadcast_objective_state(self, match)
        # The time bonus is shown once, with the longer duration.
        self._broadcast_match_info(match)
        with match.lock:
            match.time_bonus_ticks = 0
        # Every respawn now uses the stage's entries.
        print(
            f"[{GAMEPLAY_SERVICE_NAME}] PvP match {match.match_id}: fortress "
            f"stage {objective.stage} opened (+{bonus_seconds:.0f}s)"
        )

    def _fortress_monster_damage(self, s: Session, hits) -> None:
        """Score structure damage for the Score column (``scoredPoints``)."""

        match = getattr(s, "gameplay_match", None)
        if not CharacterService._is_fortress_match(match):
            return
        combat = getattr(s, "gameplay_combat_state", None)
        character_id = CharacterService._match_character_id(s)
        if combat is None or character_id is None:
            return
        total = 0
        for actor_id, damage in hits:
            team = combat.monster_team(int(actor_id))
            if team is not None and int(damage) > 0:
                total += int(damage)
        if not total:
            return
        with match.lock:
            entry = match.participant(character_id)
            if entry is not None and match.phase == MatchPhase.RUNNING:
                entry.objective_points += total

    # ------------------------------------------------------------------
    # control points
    # ------------------------------------------------------------------

    def _fortress_prop_interaction(self, s: Session, definition) -> bool:
        """Start capturing a control point. ``True`` when it is one."""

        match = getattr(s, "gameplay_match", None)
        objective = getattr(match, "objective", None)
        if not isinstance(objective, FortressState):
            return False
        point = next((
            candidate for candidate in objective.layout.control_points
            if candidate.template_id.casefold()
            == str(getattr(definition, "template_id", "")).casefold()
            and horizontal_distance(candidate.position, definition.position) <= 1.0
        ), None)
        if point is None:
            return False
        position = CharacterService._session_world_position(s)
        with match.lock:
            entry = CharacterService._objective_actor(match, s)
            captures = match.fortress_captures
            state = captures.get(point.row_id)
            allowed = (
                entry is not None
                and entry.team == point.owner_team
                and objective.control_point_present(point)
                and position is not None
                and horizontal_distance(point.position, position)
                <= point.picking_range + CAPTURE_RANGE_TOLERANCE
                and (state is None or state.get("spawned") is None)
                and (state is None or state.get("capturer") is None)
            )
            if allowed:
                captures[point.row_id] = {
                    "capturer": entry.character_id, "spawned": None,
                }
        if not allowed:
            return True
        CharacterService._arm_match_timer(
            self, match, f"capture-{point.row_id}", point.capture_seconds,
            partial(
                CharacterService._fortress_capture_complete,
                row_id=point.row_id,
            ),
        )
        print(
            f"[{GAMEPLAY_SERVICE_NAME}] PvP match {match.match_id}: character "
            f"{entry.character_id} capturing {point.template_id} "
            f"({point.capture_seconds:.0f}s)"
        )
        return True

    @staticmethod
    def _fortress_capture_complete(server, match: PVPMatch, generation: int,
                                   *, row_id: int) -> bool:
        objective = getattr(match, "objective", None)
        if not isinstance(objective, FortressState):
            return False
        point = next((
            candidate for candidate in objective.layout.control_points
            if candidate.row_id == int(row_id)
        ), None)
        if point is None:
            return False
        with match.lock:
            state = match.fortress_captures.get(point.row_id)
            capturer = state.get("capturer") if state else None
            entry = match.participant(capturer) if capturer is not None else None
            session = entry.session if entry is not None else None
        position = (
            CharacterService._session_world_position(session)
            if session is not None else None
        )
        with match.lock:
            held = (
                match.phase == MatchPhase.RUNNING
                and entry is not None and entry.in_match
                and entry.character_id not in match.round_eliminated
                and objective.control_point_present(point)
                and position is not None
                and horizontal_distance(point.position, position)
                <= point.picking_range + CAPTURE_RANGE_TOLERANCE
            )
            if not held:
                match.fortress_captures.pop(point.row_id, None)
                return False
            state["capturer"] = None
            state["spawned"] = point.monster_id
        runtime = CharacterService._fortress_runtime(match)
        if runtime is not None:
            sessions = CharacterService._map_event_sessions(server, runtime)
            if session not in sessions:
                sessions = (*sessions, session)
            CharacterService._spawn_event_monsters(
                server, runtime, sessions, (),
                extra_rows=(GatedSpawnRow(
                    _CAPTURE_SPAWN_ROW_BASE - int(point.row_id), -1,
                    point.monster_id, point.position, point.rotation,
                ),),
            )
        print(
            f"[{GAMEPLAY_SERVICE_NAME}] PvP match {match.match_id}: "
            f"{point.template_id} captured by character {entry.character_id}; "
            f"{point.monster_id} spawned for team {point.owner_team}"
        )
        return True

    # ------------------------------------------------------------------
    # client state
    # ------------------------------------------------------------------

    @staticmethod
    def _fortress_state_for(match: PVPMatch) -> PVPState:
        objective = match.objective
        with match.lock:
            stage = objective.stage
        return PVPState(
            PVP_STATE_CASTLE_RUSH_STAGE_1 if stage == 0
            else PVP_STATE_CASTLE_RUSH_STAGE_2,
            (),
        )

    @staticmethod
    def _fortress_stage_info(match: PVPMatch) -> StageInfo:
        """The current stage target and its remaining health fraction."""

        objective = match.objective
        combat = CharacterService._fortress_combat(match)
        with match.lock:
            templates = objective.stage_target_templates()
        entries = []
        for template in templates:
            fraction = 0.0
            if combat is not None:
                with combat.lock:
                    for actor_id, monster in combat.monsters.items():
                        if str(monster.template_id).casefold() != template.casefold():
                            continue
                        maximum = max(1, int(monster.maximum_health))
                        fraction = max(0.0, min(1.0, float(
                            combat.monster_health.get(actor_id, 0)
                        ) / maximum))
                        break
            entries.append((template, fraction))
        return StageInfo(tuple(entries[:1]))

    @staticmethod
    def _send_fortress_stage_info(server, match: PVPMatch) -> None:
        packet = encode_stage_info(CharacterService._fortress_stage_info(match))
        for session in match.live_sessions():
            server._send_pvp(session, packet, label="pvp-fortress-stage-info")

    @staticmethod
    def _fortress_respawn(match: PVPMatch, team: int):
        """The team's respawn entry for the fortress's current stage."""

        objective = getattr(match, "objective", None)
        if not isinstance(objective, FortressState):
            return None
        with match.lock:
            return objective.respawn(int(team))
