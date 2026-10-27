"""Objective battlegrounds: capture the flag and the fortress.

Assault the Fortress lives in ``battleground_fortress.py``; this module runs
the shared objective round and capture the flag.

``battleground.py`` runs every match through the same countdown, start,
scoreboard and completion cycle. An objective match differs in three ways,
all handled here:

* it is a single round of the battleground's authored ``RoundDuration``,
  decided by its objective (``WinningCondition`` captures) or, on timeout, by
  the objective score -- a level score is a draw;
* a killed player respawns at the team's ``Respawn`` entry after the
  authored ``PvpRespawnTimeout`` instead of staying out of the round;
* the objective itself -- for capture the flag, two flag *items* standing on
  the ``CTFTeamBase`` trigger volumes -- is server state, published as world
  items, a carrier status effect, ``PVP::PVPStateCommand`` (the HUD
  objective line) and per-recipient ``MessageCommand`` announcements. The
  client's only objective input is ``PVP::PVPEventCommand`` type 2, sent
  every tick while it stands in a ``CTFTeamBase`` volume; it triggers the
  same position-checked capture test the server runs on every move.

The flag items are the ``_Globals.CTFFlagItem*Team`` templates
(``pvp_ctf_blue_flag``/``pvp_ctf_red_flag``: ItemCategory ``Flag``,
AutoPickUp ``All``). Carrying one applies the item's own ``StatusEffectId``
(``pvp_banner_blue``/``pvp_banner_red``, a banner sequence plus
``Speed:$0,relative,Movement`` with the item's ``StatusEffectValue0`` = -0.3).

Every decision lives in ``gameplay/pvp_objectives.py``; this mixin only
publishes. Static methods take the composed server explicitly, like the rest
of the battleground runtime.
"""

from __future__ import annotations

from functools import lru_cache, partial

from dso_server.runtime.context import *
from dso_server.runtime.transport import Session
from dso_server.persistence.readonly_db import connect
from dso_server.protocol.commerce_protocol import _new_item
from dso_server.protocol.gameplay_protocol import Message, MessageArgument
from dso_server.protocol.player_init_protocol import MESSAGE_COMMAND
from dso_server.protocol.pvp_match_protocol import (
    CTF_MESSAGE_ENEMY_FLAG_RESET,
    CTF_MESSAGE_ENEMY_LOST_FLAG,
    CTF_MESSAGE_ENEMY_RECAPTURED,
    CTF_MESSAGE_ENEMY_SCORED,
    CTF_MESSAGE_ENEMY_TOOK_FLAG,
    CTF_MESSAGE_TEAM_FLAG_RESET,
    CTF_MESSAGE_TEAM_LOST_FLAG,
    CTF_MESSAGE_TEAM_RECAPTURED,
    CTF_MESSAGE_TEAM_SCORED,
    CTF_MESSAGE_WE_TOOK_FLAG_SELF,
    CTF_MESSAGE_WE_TOOK_FLAG_TEAMMATE,
    MATCH_MODE_ASSAULT_THE_FORTRESS_5V5,
    MATCH_MODE_CAPTURE_THE_FLAG_5V5,
    MATCH_TICK_HZ,
    MESSAGE_ARGUMENT_ACTOR,
    OBJECTIVE_MATCH_MODES,
    PLAYABLE_OBJECTIVE_MATCH_MODES,
    PVP_EVENT_IN_TEAM_BASE,
    PVP_STATE_BLUE_FLAG_CARRIED,
    PVP_STATE_BLUE_FLAG_DROPPED,
    PVP_STATE_BOTH_FLAGS_CARRIED,
    PVP_STATE_BOTH_FLAGS_DROPPED,
    PVP_STATE_FLAGS_AT_BASE,
    PVP_STATE_NONE,
    PVP_STATE_RED_FLAG_CARRIED,
    PVP_STATE_RED_FLAG_DROPPED,
    PVPState,
    ShowPVPDeathDialog,
    decode_pvp_event,
    encode_pvp_state,
    encode_show_pvp_death_dialog,
)
from dso_server.protocol.world_loot_protocol import WorldLootDrop
from dso_server.services.battleground_fortress import (
    BattlegroundFortressService,
)
from dso_server.gameplay.pvp_match_state import (
    MATCH_INFO_INTERVAL_SECONDS,
    MatchPhase,
    PVPMatch,
)
from dso_server.gameplay.pvp_objectives import (
    CTF_FLAG_CAPTURED,
    CTF_FLAG_DROPPED,
    CTF_FLAG_RESET,
    CTF_FLAG_RETURNED,
    CTF_FLAG_TAKEN,
    FLAG_AT_BASE,
    FLAG_CARRIED,
    FLAG_DROPPED,
    FLAG_PICKUP_REQUEST_RADIUS,
    FLAG_RESET_SECONDS,
    CaptureTheFlagState,
    FortressState,
    flag_touch_radius,
    horizontal_distance,
    load_battleground_settings,
    load_capture_the_flag_layout,
)

# Team indices of a two-sided arena: load_local_team_entries sorts the team
# names, and the client's TeamId enum agrees (BlueTeam 0, RedTeam 1).
BLUE_TEAM, RED_TEAM = 0, 1


_ITEM_ID_LOCK = threading.RLock()


@lru_cache(maxsize=8)
def _respawn_effects(database_path: str):
    """``pvp_respawn`` (2 s respawn sequence) and ``pvp_invulnerable`` (4 s).

    Retail's round setup applies these two with ``pvp_freeze``; a respawn in
    the middle of an objective round reuses them without the freeze, so the
    player is visibly protected while leaving the spawn but can act at once.
    That combination is a local choice.
    """

    with connect(database_path) as database:
        return LocalCombatState._load_victim_status_effects(
            database, "pvp_respawn;pvp_invulnerable",
        )


@lru_cache(maxsize=16)
def _flag_item_content(database_path: str, template_id: str):
    """The flag item's world-drop metadata and its carrier status effect."""

    database = connect(database_path)
    row = database.execute(
        "SELECT COALESCE(AutoPickUp, ''), COALESCE(StatusEffectId, ''), "
        "COALESCE(StatusEffectValue0, 0) FROM _Template_Item WHERE Id = ?",
        (template_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"flag item {template_id!r} is not authored")
    auto_pick_up, effect_id, value = row
    definitions = ()
    if effect_id:
        definitions = LocalCombatState._load_victim_status_effects(
            database, f"{effect_id},$0:{float(value)!r}",
        )
    return str(auto_pick_up), definitions


class BattlegroundObjectiveService(BattlegroundFortressService):
    """Objective rules for capture-the-flag and fortress battlegrounds."""

    # ------------------------------------------------------------------
    # setup
    # ------------------------------------------------------------------

    @staticmethod
    def _prepare_match_objectives(server, match: PVPMatch) -> bool:
        """Attach the authored rules, and an objective mode's state.

        Returns ``False`` when an objective mode's content cannot be loaded;
        such a match must not start, since it could never be decided.
        """

        try:
            settings = load_battleground_settings(
                server.static_database_path, match.battleground_id
            )
        except Exception as exc:
            print(
                f"[{GAMEPLAY_SERVICE_NAME}] battleground settings for "
                f"{match.battleground_id} unavailable: "
                f"{type(exc).__name__}: {exc}"
            )
            settings = None
        with match.lock:
            match.settings = settings
        if match.match_mode not in OBJECTIVE_MATCH_MODES:
            return True
        if (settings is None
                or match.match_mode not in PLAYABLE_OBJECTIVE_MATCH_MODES):
            return False
        if match.match_mode == MATCH_MODE_CAPTURE_THE_FLAG_5V5:
            layout = load_capture_the_flag_layout(
                server.static_database_path, match.map_id
            )
            if layout is None or layout.team_count != match.team_count:
                print(
                    f"[{GAMEPLAY_SERVICE_NAME}] {match.map_id} has no "
                    "complete capture-the-flag layout"
                )
                return False
            with match.lock:
                match.objective = CaptureTheFlagState(
                    layout, settings.winning_condition
                )
                # One round of the authored length, decided by captures.
                match.duration_ticks = max(1, int(round(
                    settings.round_duration_seconds * MATCH_TICK_HZ
                )))
                match.rounds_to_win = 1
            return True
        if match.match_mode == MATCH_MODE_ASSAULT_THE_FORTRESS_5V5:
            return CharacterService._prepare_fortress_objective(
                server, match, settings,
            )
        return False

    @staticmethod
    def _is_objective_match(match) -> bool:
        return getattr(match, "objective", None) is not None

    @staticmethod
    def _start_match_objectives(server, match: PVPMatch) -> None:
        """Stand both flags on their bases when the round starts."""

        objective = getattr(match, "objective", None)
        if isinstance(objective, FortressState):
            CharacterService._start_fortress(server, match)
            server._broadcast_match_info(match)
            return
        if not isinstance(objective, CaptureTheFlagState):
            return
        with match.lock:
            flags = tuple(objective.flags.values())
        for flag in flags:
            CharacterService._place_flag_item(server, match, flag)
        CharacterService._broadcast_objective_state(server, match)
        server._broadcast_match_info(match)

    # ------------------------------------------------------------------
    # flag world items
    # ------------------------------------------------------------------

    @staticmethod
    def _allocate_match_item_id(server, match: PVPMatch) -> int:
        """One world item id no participant's client already uses.

        Item ids share a space with each client's inventory and personal
        loot, so the id is taken above every participant's high-water mark
        and the shared world allocator's, and all of them move past it.
        """

        sessions = tuple(
            entry.session for entry in match.participants
            if entry.session is not None
        )
        lock = getattr(server, "gameplay_world_lock", None) or _ITEM_ID_LOCK
        with lock:
            counters = getattr(server, "gameplay_world_next_item_ids", None)
            if counters is None:
                counters = {}
                server.gameplay_world_next_item_ids = counters
            keys = {match.map_id}
            for session in sessions:
                try:
                    keys.add(CharacterService._gameplay_instance_key(session))
                except Exception:
                    pass
            item_id = max(
                [counters.get(key, RUNTIME_ITEM_ID_BASE) for key in keys]
                + [int(getattr(s, "gameplay_next_item_id", 0) or 0)
                   for s in sessions]
            )
            for key in keys:
                counters[key] = item_id + 1
        for session in sessions:
            with session.flow_lock:
                session.gameplay_next_item_id = max(
                    int(getattr(session, "gameplay_next_item_id", 0) or 0),
                    item_id + 1,
                )
        return int(item_id)

    @staticmethod
    def _flag_drop(server, flag, item_id: int) -> WorldLootDrop:
        definition = load_purchase_item_definition(
            server.static_database_path, flag.item_template
        )
        auto_pick_up, _effects = _flag_item_content(
            str(server.static_database_path), flag.item_template
        )
        item = _new_item(definition, int(item_id), 1)
        item.field_100 = tuple(float(value) for value in flag.position)
        return WorldLootDrop(item, definition.item_category, auto_pick_up)

    @staticmethod
    def _place_flag_item(server, match: PVPMatch, flag) -> None:
        """Show a flag lying at ``flag.position`` to every participant."""

        item_id = CharacterService._allocate_match_item_id(server, match)
        with match.lock:
            if not flag.on_ground or flag.item_id:
                return
            flag.item_id = item_id
        drop = CharacterService._flag_drop(server, flag, item_id)
        for session in match.live_sessions():
            CharacterService._register_flag_item(server, session, drop)

    @staticmethod
    def _register_flag_item(server, s: Session, drop) -> None:
        with s.flow_lock:
            s.gameplay_world_loot[int(drop.item.item_id)] = drop
        try:
            CharacterService._reconcile_world_loot_visibility(
                s, label="pvp-flag-item",
            )
        except Exception as exc:
            print(
                f"[{GAMEPLAY_SERVICE_NAME}] could not show flag item to "
                f"{getattr(s, 'addr', None)}: {type(exc).__name__}: {exc}"
            )

    @staticmethod
    def _remove_flag_item(server, match: PVPMatch, item_id: int) -> None:
        """Take a flag item off the ground for every participant."""

        if not item_id:
            return
        for entry in match.participants:
            session = entry.session
            if session is None:
                continue
            with session.flow_lock:
                removed = session.gameplay_world_loot.pop(int(item_id), None)
            if removed is None:
                continue
            try:
                CharacterService._reconcile_world_loot_visibility(
                    session, label="pvp-flag-item-removed",
                )
            except Exception as exc:
                print(
                    f"[{GAMEPLAY_SERVICE_NAME}] could not discard flag item "
                    f"for {getattr(session, 'addr', None)}: "
                    f"{type(exc).__name__}: {exc}"
                )

    @staticmethod
    def _show_ground_flags(server, s: Session, match: PVPMatch) -> None:
        """Register every flag on the ground for a (re)arriving session.

        For the fortress: give the arriving player (and the structures) a
        team in the combat world.
        """

        objective = getattr(match, "objective", None)
        if isinstance(objective, FortressState):
            CharacterService._fortress_bind_session(server, s, match)
            return
        if not isinstance(objective, CaptureTheFlagState):
            return
        with match.lock:
            flags = tuple(
                flag for flag in objective.flags.values()
                if flag.on_ground and flag.item_id
            )
        for flag in flags:
            CharacterService._register_flag_item(
                server, s, CharacterService._flag_drop(server, flag, flag.item_id)
            )
        CharacterService._send_objective_state(server, s, match)

    # ------------------------------------------------------------------
    # carrier status effect
    # ------------------------------------------------------------------

    @staticmethod
    def _set_flag_carrier_effect(server, s: Session, flag, *,
                                 carrying: bool) -> None:
        """Apply or remove the carried flag's banner/slow effect."""

        if s is None:
            return
        _auto, definitions = _flag_item_content(
            str(server.static_database_path), flag.item_template
        )
        entries = []
        with s.flow_lock:
            current = getattr(s, "gameplay_flag_carrier_effects", None)
            if current is None:
                current = {}
                s.gameplay_flag_carrier_effects = current
            previous = current.pop(int(flag.team), None)
            if previous is not None:
                entry = s.gameplay_persistent_status_effects.pop(
                    int(previous.field_04), previous
                )
                entries.append(replace(
                    entry, has_details=False, values=(), field_25=False,
                    field_39=False, field_38=False, field_3c=0xFF,
                    shape_values=(),
                ))
            if carrying:
                for definition in definitions:
                    instance_id = s.gameplay_next_status_effect_instance_id
                    s.gameplay_next_status_effect_instance_id += 1
                    # Same permanent shape as the map/event buffs
                    # (map_status_effect_entry): no duration, the item's
                    # $0 in the detail values. It lasts until the flag
                    # leaves the carrier.
                    entry = StatusEffectEntry(
                        effect_id=int(definition.effect_id),
                        field_04=instance_id, field_0c=0, field_14=0,
                        field_08=0, field_18=0, field_10=0, field_1c=100,
                        field_20=LOCAL_PLAYER_ACTOR_ID, field_25=False,
                        field_26=False, field_39=True, has_details=True,
                        values=tuple(float(value) for value in definition.values),
                    )
                    s.gameplay_persistent_status_effects[instance_id] = entry
                    current[int(flag.team)] = entry
                    entries.append(entry)
        if not entries:
            return
        value = StatusEffect(False, tuple(entries))
        server._send_pvp(s, encode_status_effect_batch(
            value, actor_id=LOCAL_PLAYER_ACTOR_ID,
        ), label="pvp-flag-carrier-effect")
        if s.gameplay_presence_callback is not None:
            s.gameplay_presence_callback(s, ("status_effects", (value,)))
        try:
            CharacterService._refresh_player_combat_vitals(
                s, inventory_changed=False,
            )
        except Exception as exc:
            print(
                f"[{GAMEPLAY_SERVICE_NAME}] flag carrier stats refresh "
                f"failed: {type(exc).__name__}: {exc}"
            )

    @staticmethod
    def _clear_flag_carrier_effects(server, s: Session) -> None:
        """Remove every flag banner a session still wears (leaving/ending)."""

        with s.flow_lock:
            teams = tuple(getattr(s, "gameplay_flag_carrier_effects", {}) or {})
        for team in teams:
            CharacterService._drop_carrier_effect_entry(server, s, team)

    @staticmethod
    def _drop_carrier_effect_entry(server, s: Session, team: int) -> None:
        with s.flow_lock:
            current = getattr(s, "gameplay_flag_carrier_effects", None) or {}
            previous = current.pop(int(team), None)
            if previous is None:
                return
            entry = s.gameplay_persistent_status_effects.pop(
                int(previous.field_04), previous
            )
        value = StatusEffect(False, (replace(
            entry, has_details=False, values=(), field_25=False,
            field_39=False, field_38=False, field_3c=0xFF, shape_values=(),
        ),))
        server._send_pvp(s, encode_status_effect_batch(
            value, actor_id=LOCAL_PLAYER_ACTOR_ID,
        ), label="pvp-flag-carrier-effect-cleared")
        if s.gameplay_presence_callback is not None:
            s.gameplay_presence_callback(s, ("status_effects", (value,)))

    # ------------------------------------------------------------------
    # player interaction
    # ------------------------------------------------------------------

    @staticmethod
    def _session_world_position(s: Session, wire_position=None):
        with s.flow_lock:
            encounter = s.gameplay_monster_encounter
            if wire_position is None:
                move = s.gameplay_last_move
                wire_position = (
                    move.relative_position if move is not None else None
                )
            spawn = getattr(s, "gameplay_spawn_position", None)
        if wire_position is None:
            return spawn
        if encounter is None:
            return tuple(float(value) for value in wire_position)
        return encounter.coordinate_frame.wire_to_world(wire_position)

    @staticmethod
    def _objective_actor(match: PVPMatch, s: Session):
        """The participant entry behind ``s`` if it may act on objectives."""

        character_id = CharacterService._match_character_id(s)
        if character_id is None:
            return None
        entry = match.participant(character_id)
        if (entry is None or not entry.in_match or entry.session is not s
                or match.phase != MatchPhase.RUNNING
                or entry.character_id in match.round_eliminated):
            return None
        return entry

    def _objective_moved(self, s: Session, wire_position) -> None:
        """Touch flags in reach and capture at the home base."""

        match = getattr(s, "gameplay_match", None)
        objective = getattr(match, "objective", None)
        if not isinstance(objective, CaptureTheFlagState):
            return
        position = CharacterService._session_world_position(s, wire_position)
        if position is None:
            return
        events = []
        with match.lock:
            entry = CharacterService._objective_actor(match, s)
            if entry is None:
                return
            radius = flag_touch_radius(self.static_database_path)
            for flag in tuple(objective.flags.values()):
                if (flag.on_ground and horizontal_distance(
                        flag.position, position) <= radius):
                    events.extend(CharacterService._objective_touch_locked(
                        match, objective, entry, flag, position,
                    ))
            events.extend(CharacterService._objective_capture_locked(
                match, objective, entry, position,
            ))
        CharacterService._publish_objective_events(self, match, events)

    def _objective_pickup(self, s: Session, item_id: int) -> bool:
        """A PickupItem for a flag item. Returns whether it was a flag."""

        match = getattr(s, "gameplay_match", None)
        objective = getattr(match, "objective", None)
        if not isinstance(objective, CaptureTheFlagState):
            return False
        with match.lock:
            flag = objective.flag_for_item(item_id)
        if flag is None:
            return False
        position = CharacterService._session_world_position(s)
        events = []
        with match.lock:
            entry = CharacterService._objective_actor(match, s)
            if (entry is not None and position is not None
                    and objective.flag_for_item(item_id) is flag
                    and horizontal_distance(flag.position, position)
                    <= FLAG_PICKUP_REQUEST_RADIUS):
                events.extend(CharacterService._objective_touch_locked(
                    match, objective, entry, flag, position,
                ))
                events.extend(CharacterService._objective_capture_locked(
                    match, objective, entry, position,
                ))
        CharacterService._publish_objective_events(self, match, events)
        return True

    def _objective_base_signal(self, s: Session) -> None:
        """``PVPEventCommand`` type 2: the client says it is in a team base.

        The client names neither the volume nor its team, so the claim only
        triggers the capture test at the server's own last accepted
        position; standing in the wrong base, or without a flag, does
        nothing.
        """

        match = getattr(s, "gameplay_match", None)
        objective = getattr(match, "objective", None)
        if not isinstance(objective, CaptureTheFlagState):
            return
        position = CharacterService._session_world_position(s)
        if position is None:
            return
        with match.lock:
            entry = CharacterService._objective_actor(match, s)
            if entry is None:
                return
            events = CharacterService._objective_capture_locked(
                match, objective, entry, position,
            )
        CharacterService._publish_objective_events(self, match, events)

    def _handle_objective_event_command(self, s: Session, payload: bytes) -> None:
        try:
            event = decode_pvp_event(payload, s.current_message_bit_length)
        except (ValueError, CharacterSelectionError) as exc:
            print(f"[{GAMEPLAY_SERVICE_NAME}] rejected PVPEventCommand: {exc}")
            return
        if event.event_type == PVP_EVENT_IN_TEAM_BASE:
            CharacterService._objective_base_signal(self, s)
            return
        print(
            f"[{GAMEPLAY_SERVICE_NAME}] ignored PVPEventCommand type "
            f"{event.event_type}"
        )

    @staticmethod
    def _objective_touch_locked(match, objective, entry, flag, position):
        """Apply one touch; return ``(event, item_id_to_remove)`` pairs."""

        item_id = flag.item_id
        events = objective.touch(entry.character_id, entry.team, flag.team, position)
        result = []
        for event in events:
            if event.kind == CTF_FLAG_RETURNED:
                entry.flag_returns += 1
            result.append((event, item_id))
        return result

    @staticmethod
    def _objective_capture_locked(match, objective, entry, position):
        events = objective.reach_base(entry.character_id, entry.team, position)
        result = []
        for event in events:
            if event.kind == CTF_FLAG_CAPTURED:
                entry.flag_captures += 1
            result.append((event, 0))
        return result

    # ------------------------------------------------------------------
    # deaths, respawns, departures
    # ------------------------------------------------------------------

    def _objective_death(self, match: PVPMatch, victim: Session,
                         killer_entry, victim_entry) -> None:
        """A participant died: drop any flag and schedule the respawn."""

        objective = getattr(match, "objective", None)
        position = CharacterService._session_world_position(victim)
        events = []
        with match.lock:
            if (isinstance(objective, CaptureTheFlagState)
                    and position is not None):
                carried = objective.flag_carried_by(victim_entry.character_id)
                if (carried is not None and killer_entry is not None
                        and killer_entry.team != victim_entry.team):
                    killer_entry.flag_carrier_kills += 1
                events.extend((event, 0) for event in objective.drop(
                    victim_entry.character_id, victim_entry.team, position,
                    now=time.perf_counter(),
                ))
            settings = match.settings
            delay = (
                settings.respawn_delay_seconds(
                    victim_entry.deaths, match.elapsed_round_seconds(),
                )
                if settings is not None else 0.0
            )
            victim_entry.respawn_due = time.perf_counter() + delay
            running = match.phase == MatchPhase.RUNNING
        CharacterService._publish_objective_events(self, match, events)
        if not running:
            return
        # The PvP death dialog offers the Respawn button (a RespawnCommand,
        # see _objective_respawn_request). Its number is event progress,
        # which this server does not track for battlegrounds, so it reads 0.
        self._send_pvp(
            victim,
            encode_show_pvp_death_dialog(ShowPVPDeathDialog(
                event_progress=0, actor_id=LOCAL_PLAYER_ACTOR_ID,
            )),
            label="pvp-death-dialog",
        )
        CharacterService._arm_match_timer(
            self, match, f"respawn-{victim_entry.character_id}", delay,
            partial(
                CharacterService._respawn_objective_participant,
                character_id=victim_entry.character_id,
            ),
        )

    @staticmethod
    def _respawn_objective_participant(server, match: PVPMatch,
                                       generation: int, *,
                                       character_id: int) -> bool:
        """Bring a dead participant back at the team's respawn entry."""

        with match.lock:
            if match.phase != MatchPhase.RUNNING:
                return False
            entry = match.participant(character_id)
            if entry is None or entry.session is None:
                return False
            if not match.revive(character_id):
                return False
            session = entry.session
            team = entry.team
        entries = CharacterService._battleground_team_entries(
            server, match.map_id
        )
        CharacterService._move_to_arena_position(server, session, entries, team)
        CharacterService._clear_battleground_round_effects(server, session)
        CharacterService._restore_battleground_health(server, session)
        CharacterService._send_objective_respawn_effects(server, session)
        server._broadcast_match_info(match)
        print(
            f"[{GAMEPLAY_SERVICE_NAME}] character {character_id} respawned "
            f"in PvP match {match.match_id} (team {team})"
        )
        return True

    def _objective_respawn_request(self, s: Session) -> bool:
        """The PvP death dialog's Respawn button (a ``RespawnCommand``).

        A dead objective-mode participant whose authored respawn wait has
        passed is respawned at once. An earlier press is ignored: the death
        dialog also shows ``_Globals.InstantRespawnCost`` (120), which suggests
        retail sold an early respawn; charging for it is not reproduced.
        Elimination modes ignore the request -- their dead wait for the next
        round, as before.
        """

        match = getattr(s, "gameplay_match", None)
        if not CharacterService._is_objective_match(match):
            return False
        character_id = CharacterService._match_character_id(s)
        with match.lock:
            entry = match.participant(character_id) if character_id is not None else None
            if (entry is None or entry.session is not s
                    or match.phase != MatchPhase.RUNNING
                    or entry.character_id not in match.round_eliminated
                    or time.perf_counter() < entry.respawn_due):
                return False
            timer = match.timers.pop(f"respawn-{entry.character_id}", None)
        if timer is not None:
            timer.cancel()
        return CharacterService._respawn_objective_participant(
            self, match, match.info_generation, character_id=entry.character_id,
        )

    @staticmethod
    def _send_objective_respawn_effects(server, s: Session) -> None:
        definitions = _respawn_effects(str(server.static_database_path))
        tick = CharacterService._session_tick(s)
        entries = []
        with s.flow_lock:
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
                        entry,
                        time.perf_counter() + duration / SERVER_TICKS_PER_SECOND,
                    )
        if not entries:
            return
        value = StatusEffect(False, tuple(entries))
        server._send_pvp(s, encode_status_effect_batch(
            value, actor_id=LOCAL_PLAYER_ACTOR_ID,
        ), label="pvp-respawn-effects")
        if s.gameplay_presence_callback is not None:
            s.gameplay_presence_callback(s, ("status_effects", (value,)))

    def _objective_departed(self, match: PVPMatch, character_id: int,
                            session: Session | None) -> None:
        """A participant left: a carried flag falls where they stood."""

        objective = getattr(match, "objective", None)
        if not isinstance(objective, CaptureTheFlagState):
            return
        position = (
            CharacterService._session_world_position(session)
            if session is not None else None
        )
        events = []
        with match.lock:
            entry = match.participant(character_id)
            flag = objective.flag_carried_by(character_id)
            if flag is not None and entry is not None:
                if position is None:
                    position = flag.position
                events.extend((event, 0) for event in objective.drop(
                    character_id, entry.team, position,
                    now=time.perf_counter(),
                ))
        CharacterService._publish_objective_events(self, match, events)

    @staticmethod
    def _arm_flag_reset(server, match: PVPMatch, flag_team: int,
                        dropped_at: float, delay: float) -> None:
        CharacterService._arm_match_timer(
            server, match, f"flag-reset-{int(flag_team)}", max(0.0, delay),
            partial(
                CharacterService._reset_dropped_flag,
                flag_team=int(flag_team), dropped_at=float(dropped_at),
            ),
        )

    @staticmethod
    def _reset_dropped_flag(server, match: PVPMatch, generation: int, *,
                            flag_team: int, dropped_at: float) -> None:
        """Return a flag still lying where it was dropped at ``dropped_at``."""

        objective = getattr(match, "objective", None)
        if not isinstance(objective, CaptureTheFlagState):
            return
        with match.lock:
            flag = objective.flags.get(int(flag_team))
            if (match.phase != MatchPhase.RUNNING or flag is None
                    or flag.status != FLAG_DROPPED
                    or flag.dropped_at != float(dropped_at)):
                return
            item_id = flag.item_id
            events = [(event, item_id) for event in objective.reset(flag_team)]
        CharacterService._publish_objective_events(server, match, events)

    @staticmethod
    def _rehost_objective_timers(server, match: PVPMatch) -> None:
        """Re-arm a running objective round's timers on a remaining session.

        Match timers live on one participant's scheduler, and retiring that
        session cancels them (docs/PVP_MATCHMAKING.md, "Match timers are
        hosted on a participant session"). An elimination round survives that
        because the next kill or round restarts its timers; a 15-minute
        objective round with pending respawns would not, so after a departure
        the round deadline, the scoreboard repeat and every pending respawn
        are armed again on a session that is still connected.
        """

        with match.lock:
            if (match.phase != MatchPhase.RUNNING
                    or not CharacterService._is_objective_match(match)):
                return
            now = time.perf_counter()
            pending = tuple(
                (entry.character_id, max(0.0, entry.respawn_due - now))
                for entry in match.participants
                if entry.in_match
                and entry.character_id in match.round_eliminated
            )
            deadline = match.round_deadline_seconds()
            objective = match.objective
            dropped = tuple(
                (flag.team, flag.dropped_at)
                for flag in getattr(objective, "flags", {}).values()
                if flag.status == FLAG_DROPPED
            )
        CharacterService._arm_match_timer(
            server, match, "round", deadline,
            CharacterService._expire_match_round,
        )
        for flag_team, dropped_at in dropped:
            CharacterService._arm_flag_reset(
                server, match, flag_team, dropped_at,
                dropped_at + FLAG_RESET_SECONDS - now,
            )
        CharacterService._arm_match_timer(
            server, match, "info", MATCH_INFO_INTERVAL_SECONDS,
            CharacterService._repeat_match_info,
        )
        for character_id, delay in pending:
            CharacterService._arm_match_timer(
                server, match, f"respawn-{character_id}", delay,
                partial(
                    CharacterService._respawn_objective_participant,
                    character_id=character_id,
                ),
            )

    # ------------------------------------------------------------------
    # resolution and cleanup
    # ------------------------------------------------------------------

    @staticmethod
    def _objective_timeout_winner(match: PVPMatch):
        """The team leading the objective, or ``None`` when level."""

        objective = getattr(match, "objective", None)
        if isinstance(objective, CaptureTheFlagState):
            return objective.leading_team()
        if isinstance(objective, FortressState):
            # The fortress held until time ran out: the defenders win.
            return objective.layout.defender_team
        return None

    @staticmethod
    def _objective_team_score(match: PVPMatch, team: int) -> int:
        objective = getattr(match, "objective", None)
        if isinstance(objective, CaptureTheFlagState):
            return int(objective.captures[int(team)])
        if isinstance(objective, FortressState):
            # Structures destroyed; only the attackers score (local choice).
            return (
                objective.destroyed_count
                if int(team) == objective.layout.attacker_team else 0
            )
        return 0

    @staticmethod
    def _objective_player_score(match: PVPMatch, entry) -> int:
        """A scoreboard row's Score column for the match's mode."""

        objective = getattr(match, "objective", None)
        if isinstance(objective, CaptureTheFlagState):
            return int(entry.flag_captures)
        if isinstance(objective, FortressState):
            return int(entry.objective_points)
        return int(entry.damage_dealt)

    @staticmethod
    def _objective_arena_entry(s: Session):
        """A mode-specific arena position for ``s``'s team, or ``None``."""

        match = getattr(s, "gameplay_match", None)
        team = getattr(s, "gameplay_match_team", None)
        if match is None or team is None:
            return None
        return CharacterService._fortress_respawn(match, int(team))

    @staticmethod
    def _cleanup_match_objectives(server, match: PVPMatch) -> None:
        """Take flags off the ground and banners off carriers."""

        objective = getattr(match, "objective", None)
        if isinstance(objective, FortressState):
            # The match is no longer running, so this clears the HUD line.
            CharacterService._broadcast_objective_state(server, match)
            return
        if not isinstance(objective, CaptureTheFlagState):
            return
        with match.lock:
            item_ids = tuple(
                flag.item_id for flag in objective.flags.values()
                if flag.item_id
            )
            for flag in objective.flags.values():
                flag.item_id = 0
        for item_id in item_ids:
            CharacterService._remove_flag_item(server, match, item_id)
        for entry in match.participants:
            if entry.session is not None:
                CharacterService._clear_flag_carrier_effects(server, entry.session)
        # The match is no longer running, so this clears the HUD line.
        CharacterService._broadcast_objective_state(server, match)

    # ------------------------------------------------------------------
    # publication
    # ------------------------------------------------------------------

    def _publish_objective_events(self, match: PVPMatch, events) -> None:
        """Make the world match the objective after ``events``.

        ``events`` pairs each :class:`ObjectiveEvent` with the world item it
        consumed (or 0). Flags that left the ground are discarded for every
        participant, flags that landed get a new world item, the carrier
        banner follows the flag, and each event is announced.
        """

        if not events:
            return
        objective = getattr(match, "objective", None)
        if not isinstance(objective, CaptureTheFlagState):
            return
        for event, consumed_item in events:
            if consumed_item:
                CharacterService._remove_flag_item(self, match, consumed_item)
            with match.lock:
                flag = objective.flags.get(event.flag_team)
                carrier_entry = (
                    match.participant(event.character_id)
                    if event.character_id is not None else None
                )
            if flag is None:
                continue
            carrier = (
                carrier_entry.session if carrier_entry is not None else None
            )
            if event.kind == CTF_FLAG_TAKEN:
                CharacterService._set_flag_carrier_effect(
                    self, carrier, flag, carrying=True,
                )
            elif event.kind in (CTF_FLAG_DROPPED, CTF_FLAG_CAPTURED):
                if carrier is not None:
                    CharacterService._drop_carrier_effect_entry(
                        self, carrier, flag.team,
                    )
            if flag.on_ground and not flag.item_id:
                CharacterService._place_flag_item(self, match, flag)
            if event.kind == CTF_FLAG_DROPPED:
                with match.lock:
                    dropped_at = flag.dropped_at
                CharacterService._arm_flag_reset(
                    self, match, flag.team, dropped_at, FLAG_RESET_SECONDS,
                )
            CharacterService._announce_objective_event(self, match, event)
            print(
                f"[{GAMEPLAY_SERVICE_NAME}] PvP match {match.match_id}: "
                f"{event.kind} (flag of team {event.flag_team}, by "
                f"character {event.character_id} of team {event.actor_team}); "
                f"captures {objective.captures}"
            )
        winner = None
        with match.lock:
            winner = objective.winner()
        CharacterService._broadcast_objective_state(self, match)
        if winner is not None:
            CharacterService._finish_objective_match(self, match, winner)
        else:
            self._broadcast_match_info(match)

    def _finish_objective_match(self, match: PVPMatch, winner) -> None:
        """Close the single objective round for ``winner`` (``None`` = draw)."""

        with match.lock:
            if match.phase != MatchPhase.RUNNING:
                return
            CharacterService._cancel_match_timers(match)
            if winner is None:
                decided = match.declare_draw()
                outcome = (None, True) if decided else None
            else:
                over = match.award_round(int(winner))
                outcome = (int(winner), over)
        if outcome is None:
            return
        CharacterService._publish_match_round_result(self, match, outcome)

    @staticmethod
    def _match_viewer_actor_id(match: PVPMatch, viewer: Session,
                         character_id: int) -> int:
        """``character_id``'s actor as ``viewer``'s client knows it.

        The same mapping as the scoreboard rows (``_match_info_for``): the
        viewer's own actor is 1, peers use their presence actor ids. The
        client resolves message names and carriers in that roster.
        """

        if CharacterService._match_character_id(viewer) == int(character_id):
            return LOCAL_PLAYER_ACTOR_ID
        entry = match.participant(character_id)
        if entry is None:
            return CharacterService._remote_actor_id(int(character_id))
        return int(
            getattr(entry.session, "gameplay_remote_actor_id", 0)
            or entry.actor_id
            or CharacterService._remote_actor_id(entry.character_id)
        )

    @staticmethod
    def _objective_state_for(match: PVPMatch, viewer: Session) -> PVPState:
        """The HUD objective line (``PVPStateCommand``) for one viewer.

        States 1..7 name which flags are at base, carried or dropped
        (0x1402f595c); ``actors`` lists the carriers. One flag carried while
        the other is dropped has no state of its own; the carried flag is
        reported, being the one a player can still act on (local choice).
        """

        objective = getattr(match, "objective", None)
        if match.phase != MatchPhase.RUNNING:
            return PVPState(PVP_STATE_NONE, ())
        if isinstance(objective, FortressState):
            return CharacterService._fortress_state_for(match)
        if not isinstance(objective, CaptureTheFlagState):
            return PVPState(PVP_STATE_NONE, ())
        with match.lock:
            blue = objective.flags[BLUE_TEAM]
            red = objective.flags[RED_TEAM]
            carriers = tuple(
                flag.carrier for flag in (blue, red)
                if flag.status == FLAG_CARRIED and flag.carrier is not None
            )
            statuses = (blue.status, red.status)
        if statuses == (FLAG_AT_BASE, FLAG_AT_BASE):
            state = PVP_STATE_FLAGS_AT_BASE
        elif statuses == (FLAG_CARRIED, FLAG_CARRIED):
            state = PVP_STATE_BOTH_FLAGS_CARRIED
        elif red.status == FLAG_CARRIED:
            state = PVP_STATE_RED_FLAG_CARRIED
        elif blue.status == FLAG_CARRIED:
            state = PVP_STATE_BLUE_FLAG_CARRIED
        elif statuses == (FLAG_DROPPED, FLAG_DROPPED):
            state = PVP_STATE_BOTH_FLAGS_DROPPED
        elif red.status == FLAG_DROPPED:
            state = PVP_STATE_RED_FLAG_DROPPED
        else:
            state = PVP_STATE_BLUE_FLAG_DROPPED
        return PVPState(state, tuple(
            CharacterService._match_viewer_actor_id(match, viewer, carrier)
            for carrier in carriers
        ))

    @staticmethod
    def _send_objective_state(server, s: Session, match: PVPMatch) -> None:
        if not CharacterService._is_objective_match(match):
            return
        server._send_pvp(
            s,
            encode_pvp_state(CharacterService._objective_state_for(match, s)),
            label="pvp-objective-state",
        )

    @staticmethod
    def _broadcast_objective_state(server, match: PVPMatch) -> None:
        """Send the objective's HUD state to every participant."""

        for session in match.live_sessions():
            CharacterService._send_objective_state(server, session, match)
        if (isinstance(getattr(match, "objective", None), FortressState)
                and match.phase == MatchPhase.RUNNING):
            CharacterService._send_fortress_stage_info(server, match)

    @staticmethod
    def _objective_message_type(event, viewer_team: int,
                                viewer_is_actor: bool) -> int | None:
        """The ``MessageCommand`` type announcing ``event`` to one viewer.

        From the viewer's side (0x1401d0b48, gui.window.messages): a flag
        taken by the viewer's team is 12 for the taker and 13 for teammates,
        14 for the flag's owners; a capture is 15 for the scoring team
        (``ctfTeamScoredPoint``) and 16 for the other (``ctfEnemyScoredPoint``);
        a return is 19 for the flag's team (``ctfTeamRecapturedFlag``) and 20
        for the other; an automatic return is 21/22. For a dropped flag the
        client only plays ``ctf_team_lost_flag`` (18) or
        ``ctf_enemy_lost_flag`` (17); reading "team" as the carrier's side is
        a local interpretation.
        """

        ours = int(viewer_team) == int(event.actor_team)
        own_flag = int(viewer_team) == int(event.flag_team)
        if event.kind == CTF_FLAG_TAKEN:
            if own_flag:
                return CTF_MESSAGE_ENEMY_TOOK_FLAG
            return (CTF_MESSAGE_WE_TOOK_FLAG_SELF if viewer_is_actor
                    else CTF_MESSAGE_WE_TOOK_FLAG_TEAMMATE)
        if event.kind == CTF_FLAG_CAPTURED:
            return CTF_MESSAGE_TEAM_SCORED if ours else CTF_MESSAGE_ENEMY_SCORED
        if event.kind == CTF_FLAG_DROPPED:
            return (CTF_MESSAGE_TEAM_LOST_FLAG if ours
                    else CTF_MESSAGE_ENEMY_LOST_FLAG)
        if event.kind == CTF_FLAG_RETURNED:
            return (CTF_MESSAGE_TEAM_RECAPTURED if own_flag
                    else CTF_MESSAGE_ENEMY_RECAPTURED)
        if event.kind == CTF_FLAG_RESET:
            return (CTF_MESSAGE_TEAM_FLAG_RESET if own_flag
                    else CTF_MESSAGE_ENEMY_FLAG_RESET)
        return None

    @staticmethod
    def _announce_objective_event(server, match: PVPMatch, event) -> None:
        """Announce one objective transition to every participant."""

        for entry in match.participants:
            session = entry.session
            if session is None or entry.left:
                continue
            message_type = CharacterService._objective_message_type(
                event, entry.team, entry.character_id == event.character_id,
            )
            if message_type is None:
                continue
            # The argument names the acting player; an automatic reset has
            # none, so it names the viewer, whom every roster resolves.
            actor_id = CharacterService._match_viewer_actor_id(
                match, session,
                event.character_id if event.character_id is not None
                else entry.character_id,
            )
            packet = encode_server_command_batch((ServerCommand(
                MESSAGE_COMMAND,
                Message(message_type, (
                    MessageArgument(MESSAGE_ARGUMENT_ACTOR, actor_id),
                )),
                0,
            ),))
            server._send_pvp(session, packet, label="pvp-objective-message")
