"""Authored battleground rules and the objective-mode state machines.

Pure domain state, like ``pvp_match_state.py``: no sockets and no packet
encoding. ``services/battleground.py`` publishes what these objects decide.

Everything numeric here is read from content:

* ``_Template_BattlegroundSettings`` (static.db4) authors each battleground:
  team size, ``WinningCondition`` (rounds/captures to win), ``RoundDuration``
  (seconds), ``PvpRespawnTimeout`` and ``TimeoutIncreasePerDeath`` (seconds),
  the matchmaking search ranges and the map.
* ``_Globals`` names the two flag items (``CTFFlagItemRedTeam`` /
  ``CTFFlagItemBlueTeam``).
* The arena map database authors the team entries (``_Instance_Entry``,
  ``OwnerTeam`` + ``EntryType``) and, for capture the flag, one
  ``_Instance_TriggerVolume`` per side with ``TriggeredEventCategory``
  ``CTFTeamBase`` and ``TriggeredEventId`` ``RedTeamBase``/``BlueTeamBase``.

Team numbering follows ``load_local_team_entries``: team names sorted, so the
two-sided arenas use 0 = ``BlueTeam`` and 1 = ``RedTeam``, which is also the
order of ``_Template_TeamAttributes`` (``PvPTeam1``/``PvPTeam2``).
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from dso_server.persistence.readonly_db import connect


# ---------------------------------------------------------------------------
# _Template_BattlegroundSettings
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BattlegroundSettings:
    """One ``_Template_BattlegroundSettings`` row."""

    battleground_id: str
    map_name: str
    ruleset: str
    team_size: int
    winning_condition: int
    round_duration_seconds: float
    respawn_timeout_seconds: float
    timeout_increase_per_death: float
    timeout_increase_per_time: float
    level_search_range: int
    pvp_level_search_range: int
    rating_search_range: int
    rating_max_wait_seconds: float
    allowed_missing_players: int
    prepare_seconds: float
    wait_for_players_seconds: float
    is_live: bool
    visible: bool

    def respawn_delay_seconds(self, deaths: int, elapsed_seconds: float = 0.0) -> float:
        """Respawn wait for a player's ``deaths``-th death.

        ``PvpRespawnTimeout`` is the base wait. ``TimeoutIncreasePerDeath``
        (3.0 for Assault the Fortress, 0 elsewhere) and
        ``TimeoutIncreasePerTime`` (0 for every live row) are read as additive
        seconds per earlier death and per elapsed match minute. The client
        uses neither column, so the combination is a local interpretation of
        the column names.
        """

        earlier = max(0, int(deaths) - 1)
        return max(0.0, (
            float(self.respawn_timeout_seconds)
            + float(self.timeout_increase_per_death) * earlier
            + float(self.timeout_increase_per_time)
            * max(0.0, float(elapsed_seconds)) / 60.0
        ))


@lru_cache(maxsize=64)
def _settings_rows(database_path: str) -> dict[str, BattlegroundSettings]:
    database = connect(database_path)
    cursor = database.execute("SELECT * FROM _Template_BattlegroundSettings")
    columns = [description[0] for description in cursor.description]
    result = {}
    for values in cursor.fetchall():
        row = dict(zip(columns, values))

        def number(name, default=0.0):
            value = row.get(name)
            try:
                return float(value) if value is not None else float(default)
            except (TypeError, ValueError):
                return float(default)

        settings = BattlegroundSettings(
            battleground_id=str(row.get("Id") or ""),
            map_name=str(row.get("MapName") or ""),
            ruleset=str(row.get("Ruleset") or ""),
            team_size=max(1, int(number("TeamSize", 1))),
            winning_condition=int(number("WinningCondition")),
            round_duration_seconds=number("RoundDuration"),
            respawn_timeout_seconds=number("PvpRespawnTimeout"),
            timeout_increase_per_death=number("TimeoutIncreasePerDeath"),
            timeout_increase_per_time=number("TimeoutIncreasePerTime"),
            level_search_range=int(number("PlayerXpLevelSearchRange")),
            pvp_level_search_range=int(number("PlayerPvPLevelSearchRange")),
            rating_search_range=int(number("PlayerRatingSearchRange")),
            rating_max_wait_seconds=number("PlayerRatingMaxWaitTime"),
            allowed_missing_players=int(number("AllowedMissingPlayers")),
            prepare_seconds=number("PrepareDuration"),
            wait_for_players_seconds=number("WaitForPlayersDuration"),
            is_live=bool(int(number("IsLive"))),
            visible=bool(int(number("Visible"))),
        )
        result[settings.battleground_id.casefold()] = settings
    return result


def load_battleground_settings(
    static_database_path: str | Path, battleground_id: str,
) -> BattlegroundSettings | None:
    """The authored settings for ``battleground_id`` (case-insensitive)."""

    return _settings_rows(str(static_database_path)).get(
        str(battleground_id).casefold()
    )


def authored_battleground_maps(static_database_path: str | Path) -> frozenset[str]:
    """Every ``MapName`` a battleground row names."""

    return frozenset(
        settings.map_name for settings in
        _settings_rows(str(static_database_path)).values()
        if settings.map_name
    )


@lru_cache(maxsize=16)
def load_globals_value(static_database_path: str, column: str) -> str:
    database = connect(str(static_database_path))
    columns = {
        row[1] for row in database.execute("PRAGMA table_info(_Globals)")
    }
    if column not in columns:
        return ""
    row = database.execute(f'SELECT "{column}" FROM _Globals').fetchone()
    return str(row[0] or "") if row is not None else ""


# ---------------------------------------------------------------------------
# Map geometry helpers
# ---------------------------------------------------------------------------

TEAM_NAMES_SORTED = ("BlueTeam", "RedTeam")


def _matrix(value: bytes) -> tuple[float, ...]:
    if value is None or len(value) != 64:
        raise ValueError("map transform must be 64 bytes")
    return struct.unpack("<16f", bytes(value))


def _map_database(static_database_path: str | Path, map_id: str) -> Path:
    return Path(static_database_path).parent.parent / "maps" / f"{map_id}.db4"


@dataclass(frozen=True)
class BaseVolume:
    """A side's ``CTFTeamBase`` trigger box (unit cube under ``matrix``)."""

    team: int
    trigger_id: str
    matrix: tuple[float, ...]

    @property
    def center(self) -> tuple[float, float, float]:
        return (self.matrix[12], self.matrix[13], self.matrix[14])

    def contains(self, position) -> bool:
        m = self.matrix
        relative = (
            float(position[0]) - m[12],
            float(position[1]) - m[13],
            float(position[2]) - m[14],
        )
        basis = ((m[0], m[1], m[2]), (m[4], m[5], m[6]), (m[8], m[9], m[10]))
        local = []
        for axis in basis:
            length_sq = sum(component * component for component in axis)
            if length_sq <= 0.0:
                return False
            # The authored bases are axis-aligned scaled boxes, so projecting
            # onto each (orthogonal) basis vector recovers the local
            # coordinate.
            local.append(sum(r * a for r, a in zip(relative, axis)) / length_sq)
        return all(abs(value) <= 0.5 + 1e-6 for value in local)


def horizontal_distance(a, b) -> float:
    return math.hypot(float(a[0]) - float(b[0]), float(a[2]) - float(b[2]))


# ---------------------------------------------------------------------------
# Capture the flag
# ---------------------------------------------------------------------------

CTF_BASE_CATEGORY = "CTFTeamBase"

# How close a player has to come to a flag on the ground to touch it. The
# flag items are AutoPickUp 'All' world items. The client never picks up by
# proximity -- it sends PickupItem only on a click -- and has no reader for
# ``_Globals.ItemAutoPickUpRadius`` (2.25), so automatic pickup is the
# server's: the flag uses that authored radius. An explicit PickupItem
# request is accepted up to 4.0, the server's existing PickupItem range
# (services/world.py).
FLAG_TOUCH_RADIUS_DEFAULT = 2.25
FLAG_PICKUP_REQUEST_RADIUS = 4.0

# A dropped flag nobody touches goes back to its base after this long. The
# client has jingles for it (ctf_team_flag_reset / ctf_enemy_flag_reset,
# MessageCommand 21/22), but no table authors the delay; 30 seconds is a
# local choice.
FLAG_RESET_SECONDS = 30.0


def flag_touch_radius(static_database_path: str | Path) -> float:
    try:
        value = float(load_globals_value(
            str(static_database_path), "ItemAutoPickUpRadius",
        ) or 0.0)
    except ValueError:
        value = 0.0
    return value if value > 0.0 else FLAG_TOUCH_RADIUS_DEFAULT

FLAG_AT_BASE = "base"
FLAG_CARRIED = "carried"
FLAG_DROPPED = "dropped"

# Objective transitions the service publishes. The wire event type each maps
# to lives in ``protocol/pvp_match_protocol.py``.
CTF_FLAG_TAKEN = "flag_taken"
CTF_FLAG_DROPPED = "flag_dropped"
CTF_FLAG_RETURNED = "flag_returned"
CTF_FLAG_CAPTURED = "flag_captured"
CTF_FLAG_RESET = "flag_reset"


@dataclass(frozen=True)
class CaptureTheFlagLayout:
    """A capture-the-flag map: one base box and one flag item per team.

    ``bases[team]`` is the side's ``CTFTeamBase`` trigger volume, whose centre
    is where that side's flag stands; ``flag_items[team]`` is the item
    template of *that side's* flag (``_Globals.CTFFlagItemBlueTeam`` for team
    0, ``CTFFlagItemRedTeam`` for team 1).
    """

    map_id: str
    bases: tuple[BaseVolume, ...]
    flag_items: tuple[str, ...]

    @property
    def team_count(self) -> int:
        return len(self.bases)

    def home(self, team: int) -> tuple[float, float, float]:
        return self.bases[int(team)].center


@lru_cache(maxsize=16)
def _load_ctf_layout(static_database_path: str, map_id: str):
    path = _map_database(static_database_path, map_id)
    if not path.exists():
        return None
    database = connect(str(path))
    try:
        rows = database.execute(
            "SELECT _ID, TriggeredEventId, Transform "
            "FROM _Instance_TriggerVolume "
            "WHERE TriggeredEventCategory = ? ORDER BY rowid",
            (CTF_BASE_CATEGORY,),
        ).fetchall()
    except Exception:
        return None
    bases: dict[int, BaseVolume] = {}
    for trigger_id, event_id, transform in rows:
        team_name = str(event_id or "")
        if team_name.endswith("Base"):
            team_name = team_name[:-len("Base")]
        if team_name not in TEAM_NAMES_SORTED:
            continue
        team = TEAM_NAMES_SORTED.index(team_name)
        try:
            bases.setdefault(
                team, BaseVolume(team, str(trigger_id), _matrix(transform))
            )
        except ValueError:
            continue
    if sorted(bases) != list(range(len(TEAM_NAMES_SORTED))):
        return None
    items = (
        load_globals_value(static_database_path, "CTFFlagItemBlueTeam"),
        load_globals_value(static_database_path, "CTFFlagItemRedTeam"),
    )
    if not all(items):
        return None
    return CaptureTheFlagLayout(
        str(map_id),
        tuple(bases[team] for team in range(len(TEAM_NAMES_SORTED))),
        items,
    )


def load_capture_the_flag_layout(
    static_database_path: str | Path, map_id: str,
) -> CaptureTheFlagLayout | None:
    """The authored CTF layout of ``map_id``, or ``None`` if incomplete."""

    return _load_ctf_layout(str(static_database_path), str(map_id))


@dataclass
class FlagState:
    """One side's flag: at its base, carried by an enemy, or on the ground."""

    team: int
    home: tuple[float, float, float]
    item_template: str
    status: str = FLAG_AT_BASE
    carrier: int | None = None
    position: tuple[float, float, float] = (0.0, 0.0, 0.0)
    # World item id while the flag is on the ground (at base or dropped);
    # allocated by the service, zero while carried.
    item_id: int = 0
    dropped_at: float = 0.0

    @property
    def on_ground(self) -> bool:
        return self.status in (FLAG_AT_BASE, FLAG_DROPPED)


@dataclass(frozen=True)
class ObjectiveEvent:
    """One objective transition for the service to publish."""

    kind: str
    flag_team: int
    actor_team: int
    character_id: int | None
    position: tuple[float, float, float]


class CaptureTheFlagState:
    """Flag lifecycle and capture score of one capture-the-flag match.

    The rules, all enforced here so a replayed or duplicate request cannot
    score twice:

    * touching the *enemy* flag (at its base or dropped) takes it, unless the
      player already carries a flag;
    * touching *your own* dropped flag returns it to your base;
    * a carrier who dies, leaves, or is removed drops the flag where they
      were;
    * a carrier standing in their own base while their own flag is at home
      captures: their team scores and the captured flag goes back to its
      base. Requiring the home flag follows the common capture-the-flag rule;
      the client does not encode it, so it is a local choice.
    """

    def __init__(self, layout: CaptureTheFlagLayout, captures_to_win: int) -> None:
        self.layout = layout
        self.captures_to_win = max(1, int(captures_to_win))
        self.captures = [0] * layout.team_count
        self.flags = {
            team: FlagState(
                team, layout.home(team), layout.flag_items[team],
                position=layout.home(team),
            )
            for team in range(layout.team_count)
        }

    # -- queries --------------------------------------------------------

    def flag_carried_by(self, character_id: int) -> FlagState | None:
        for flag in self.flags.values():
            if flag.status == FLAG_CARRIED and flag.carrier == int(character_id):
                return flag
        return None

    def flag_for_item(self, item_id: int) -> FlagState | None:
        for flag in self.flags.values():
            if flag.on_ground and flag.item_id and flag.item_id == int(item_id):
                return flag
        return None

    def winner(self) -> int | None:
        for team, captures in enumerate(self.captures):
            if captures >= self.captures_to_win:
                return team
        return None

    def leading_team(self) -> int | None:
        """The team with strictly most captures, or ``None`` on a tie."""

        best = max(self.captures)
        leaders = [
            team for team, value in enumerate(self.captures) if value == best
        ]
        return leaders[0] if len(leaders) == 1 else None

    # -- transitions ----------------------------------------------------

    def touch(self, character_id: int, team: int, flag_team: int,
              position) -> tuple[ObjectiveEvent, ...]:
        """A player of ``team`` reached ``flag_team``'s flag on the ground."""

        flag = self.flags.get(int(flag_team))
        if flag is None or not flag.on_ground or self.winner() is not None:
            return ()
        character_id = int(character_id)
        team = int(team)
        where = tuple(float(value) for value in position)
        if flag.team == team:
            if flag.status != FLAG_DROPPED:
                return ()
            self._home(flag)
            return (ObjectiveEvent(
                CTF_FLAG_RETURNED, flag.team, team, character_id, flag.home,
            ),)
        if self.flag_carried_by(character_id) is not None:
            return ()
        flag.status = FLAG_CARRIED
        flag.carrier = character_id
        flag.position = where
        flag.item_id = 0
        return (ObjectiveEvent(
            CTF_FLAG_TAKEN, flag.team, team, character_id, where,
        ),)

    def drop(self, character_id: int, team: int, position, *,
             now: float = 0.0) -> tuple[ObjectiveEvent, ...]:
        """A carrier died or left: their flag falls where they were."""

        flag = self.flag_carried_by(character_id)
        if flag is None:
            return ()
        where = tuple(float(value) for value in position)
        flag.status = FLAG_DROPPED
        flag.carrier = None
        flag.position = where
        flag.dropped_at = float(now)
        return (ObjectiveEvent(
            CTF_FLAG_DROPPED, flag.team, int(team), int(character_id), where,
        ),)

    def reach_base(self, character_id: int, team: int,
                   position) -> tuple[ObjectiveEvent, ...]:
        """A player of ``team`` is at ``position``; capture if they may."""

        team = int(team)
        flag = self.flag_carried_by(character_id)
        if flag is None or flag.team == team or self.winner() is not None:
            return ()
        if not self.layout.bases[team].contains(position):
            return ()
        if self.flags[team].status != FLAG_AT_BASE:
            return ()
        self.captures[team] += 1
        self._home(flag)
        return (ObjectiveEvent(
            CTF_FLAG_CAPTURED, flag.team, team, int(character_id),
            tuple(float(value) for value in position),
        ),)

    def reset(self, flag_team: int) -> tuple[ObjectiveEvent, ...]:
        """Put a dropped or carried flag back at its base (no scoring)."""

        flag = self.flags.get(int(flag_team))
        if flag is None or flag.status == FLAG_AT_BASE:
            return ()
        self._home(flag)
        return (ObjectiveEvent(
            CTF_FLAG_RESET, flag.team, flag.team, None, flag.home,
        ),)

    @staticmethod
    def _home(flag: FlagState) -> None:
        flag.status = FLAG_AT_BASE
        flag.carrier = None
        flag.position = flag.home
        flag.item_id = 0
        flag.dropped_at = 0.0


# ---------------------------------------------------------------------------
# Assault the fortress (Ruleset "CastleRush")
# ---------------------------------------------------------------------------

# The map's stage objectives are the SpawnPoint rows named
# ``a0209_atf_stageNN_target`` (the drawbridge switch, then the gun control);
# the minimap's ``PvPObjectiveEntity`` markers x1000_def_marker_stage_0/1
# stand on exactly these two positions.
CASTLE_RUSH_STAGE_TARGET_PREFIX = "a0209_atf_stage"
CASTLE_RUSH_INITIAL_EVENT = "pvp_castlerush_initial"
CASTLE_RUSH_STAGE_EVENT = "pvp_castlerush_stage_{stage}"
CASTLE_RUSH_STAGE_FLAG = "pvp_stage_{stage}"
INVINCIBLE_SUFFIX = "_invincible"
# ``pvp_castlerush_bridge_mechanism`` starts when the core tower
# (pvp_defense_tower_t5) is eliminated; the drawbridge switch ends it.
STAGE_TARGET_GATES = {0: "pvp_castlerush_bridge_mechanism"}

FORTRESS_OBJECTIVE_DESTROYED = "objective_destroyed"
FORTRESS_STAGE_COMPLETED = "stage_completed"
FORTRESS_ATTACKERS_WON = "attackers_won"


def _parse_event_flags(value: str) -> tuple[tuple[str, bool], ...]:
    flags = []
    for token in str(value or "").split(";"):
        name, separator, state = token.partition("=")
        if separator and name.strip():
            flags.append((name.strip(), state.strip().casefold() == "true"))
    return tuple(flags)


@dataclass(frozen=True)
class FortressObjective:
    """One authored team-owned monster spawn of the fortress map."""

    row_id: int
    spawn_id: str
    template_id: str
    owner_team: int
    position: tuple[float, float, float]
    event_flags: tuple[tuple[str, bool], ...]

    @property
    def invincible(self) -> bool:
        return self.template_id.casefold().endswith(INVINCIBLE_SUFFIX)

    def present(self, active: frozenset[str]) -> bool:
        return all((name in active) == state for name, state in self.event_flags)


@dataclass(frozen=True)
class FortressControlPoint:
    """An attacker control point (``_Instance_StateObject``).

    Its ``OperationId`` names a ``_Template_PropOperation`` whose
    ``ModuleClass`` is ``TeamPvPCapturingModule`` (CaptureTime 10 s), and its
    ``MechanismId`` a ``_Template_PropMechanism`` (``TeamPvPMonsterSpawner
    Module``) whose ``MonsterId`` -- a rammer or bomb spawner -- appears for
    the capturing team. Present only while its EventSet is enabled.
    """

    row_id: int
    template_id: str
    owner_team: int
    position: tuple[float, float, float]
    rotation: float
    picking_range: float
    capture_seconds: float
    monster_id: str
    path_name: str
    event_flags: tuple[tuple[str, bool], ...]

    def present(self, active: frozenset[str]) -> bool:
        return all((name in active) == state for name, state in self.event_flags)


@dataclass(frozen=True)
class FortressClearEffect:
    """A destroying ``_Instance_LocationEffect`` aura of the fortress map.

    ``pvp_destroy_towers_aura`` (``CurrHealthPoints:-250,relative`` on the
    stage-0 lane towers) and ``pvp_destroy_towers_aura_01`` (``-1000`` on
    the rammers, bombs and their spawners) stand in EventSet 1830
    (``pvp_castlerush_kill_stage_01_tower=True``): when the drawbridge falls
    they wipe the first stage's towers and siege units inside their box.
    """

    volume: BaseVolume
    templates: frozenset[str]
    event_flags: tuple[tuple[str, bool], ...]

    def present(self, active: frozenset[str]) -> bool:
        return bool(self.event_flags) and all(
            (name in active) == state for name, state in self.event_flags
        )


@dataclass(frozen=True)
class FortressLayout:
    """The authored CastleRush objective graph of one map.

    * ``objectives``: every ``_Instance_SpawnPoint`` with an ``OwnerTeam``
      (defense towers, the ``_invincible`` stand-ins that map events swap
      out, the stage-0 drawbridge switch and the stage-1 gun control);
    * ``signals``: this map's ``_Template_Signal`` ``MonstersEliminated``
      rows, template set -> signal id;
    * ``events_by_signal``: map-local ``_Template_Events`` started by each
      signal (any one start signal starts an event, as map_event_state reads
      ``StartSignalId``);
    * ``stage_targets``: stage index -> stage target spawn ids;
    * ``respawns``: per team, ``(event flags, position, heading)`` rows.
    """

    map_id: str
    defender_team: int
    attacker_team: int
    objectives: tuple[FortressObjective, ...]
    signals: tuple[tuple[frozenset[str], str], ...]
    events_by_signal: dict
    stage_targets: dict
    respawns: dict
    control_points: tuple[FortressControlPoint, ...] = ()
    # Monsters the attackers bring: the control points' spawners and what
    # their summon skills create (pvp_rammer, pvp_bomb).
    attacker_templates: tuple[str, ...] = ()
    clear_effects: tuple[FortressClearEffect, ...] = ()

    def objective_for_template(self, template_id: str):
        key = str(template_id).casefold()
        for objective in self.objectives:
            if objective.template_id.casefold() == key:
                return objective
        return None

    def team_templates(self) -> dict[str, int]:
        """Template id -> owning team, for the combat world."""

        teams = {o.template_id: o.owner_team for o in self.objectives}
        teams.update({t: self.attacker_team for t in self.attacker_templates})
        return teams


@lru_cache(maxsize=8)
def _load_fortress_layout(static_database_path: str, map_id: str):
    path = _map_database(static_database_path, map_id)
    if not path.exists():
        return None
    database = connect(str(path))
    try:
        event_sets = {
            str(row[0]): _parse_event_flags(row[1])
            for row in database.execute(
                "SELECT Id, EventFlags FROM _Instance_EventSets"
            )
        }
        spawn_rows = database.execute(
            "SELECT rowid, _ID, NpcId, OwnerTeam, Transform, EventSetRow "
            "FROM _Instance_SpawnPoint WHERE COALESCE(OwnerTeam, '') <> '' "
            "ORDER BY rowid"
        ).fetchall()
        entry_rows = database.execute(
            "SELECT OwnerTeam, Transform, EntryType, EventSetRow "
            "FROM _Instance_Entry WHERE COALESCE(OwnerTeam, '') <> '' "
            "ORDER BY rowid"
        ).fetchall()
        control_rows = database.execute(
            "SELECT rowid, _ID, OwnerTeam, Transform, EventSetRow, "
            "COALESCE(OperationId, ''), COALESCE(MechanismId, ''), "
            "COALESCE(PickingRange, 0), COALESCE(PathName, '') "
            "FROM _Instance_StateObject WHERE COALESCE(OwnerTeam, '') <> '' "
            "ORDER BY rowid"
        ).fetchall()
        control_owners = {str(row[2]) for row in control_rows}
        effect_rows = database.execute(
            "SELECT rowid, _ID, StatusEffectId, EventSetRow, Transform "
            "FROM _Instance_LocationEffect ORDER BY rowid"
        ).fetchall()
    except Exception:
        return None
    objectives = []
    owners = set()
    for row_id, spawn_id, template_id, owner, transform, event_row in spawn_rows:
        if str(owner) not in TEAM_NAMES_SORTED:
            continue
        try:
            matrix = _matrix(transform)
        except ValueError:
            continue
        owners.add(str(owner))
        objectives.append(FortressObjective(
            int(row_id), str(spawn_id), str(template_id),
            TEAM_NAMES_SORTED.index(str(owner)),
            (matrix[12], matrix[13], matrix[14]),
            event_sets.get(str(event_row), ()),
        ))
    if len(owners) != 1 or not objectives:
        return None
    defender = TEAM_NAMES_SORTED.index(owners.pop())
    attackers = {
        TEAM_NAMES_SORTED.index(name) for name in control_owners
        if name in TEAM_NAMES_SORTED
    } - {defender}
    attacker = attackers.pop() if len(attackers) == 1 else 1 - defender
    static = connect(str(static_database_path))
    signals = []
    for event_param, signal_id in static.execute(
        "SELECT EventParam, SignalId FROM _Template_Signal "
        "WHERE Map = ? AND EventType = 'MonstersEliminated' "
        "AND COALESCE(Enabled, 1) = 1",
        (str(map_id),),
    ):
        templates = frozenset(
            token.strip() for token in str(event_param or "").split(";")
            if token.strip()
        )
        if templates and signal_id:
            signals.append((templates, str(signal_id)))
    events_by_signal: dict[str, list[str]] = {}
    for event_id, start_signals in static.execute(
        "SELECT Id, StartSignalId FROM _Template_Events "
        "WHERE MapLocal = 1 AND Map = ?",
        (str(map_id),),
    ):
        for signal in str(start_signals or "").split(";"):
            if signal.strip():
                events_by_signal.setdefault(signal.strip(), []).append(
                    str(event_id)
                )
    stage_targets: dict[int, tuple[str, ...]] = {}
    for objective in objectives:
        name = objective.spawn_id
        if not name.startswith(CASTLE_RUSH_STAGE_TARGET_PREFIX):
            continue
        digits = "".join(
            ch for ch in name[len(CASTLE_RUSH_STAGE_TARGET_PREFIX):]
            if ch.isdigit()
        )
        if digits:
            stage = int(digits) - 1
            stage_targets[stage] = stage_targets.get(stage, ()) + (name,)
    respawns: dict[int, list] = {}
    for owner, transform, entry_type, event_row in entry_rows:
        if str(owner) not in TEAM_NAMES_SORTED:
            continue
        if str(entry_type or "").casefold() != "respawn":
            continue
        try:
            matrix = _matrix(transform)
        except ValueError:
            continue
        respawns.setdefault(TEAM_NAMES_SORTED.index(str(owner)), []).append((
            event_sets.get(str(event_row), ()),
            (matrix[12], matrix[13], matrix[14]),
            math.atan2(matrix[8], matrix[0]),
        ))
    if (not stage_targets
            or sorted(stage_targets) != list(range(len(stage_targets)))):
        return None
    control_points = []
    attacker_templates: list[str] = []
    for (row_id, template_id, owner, transform, event_row, operation,
         mechanism, picking_range, path_name) in control_rows:
        if str(owner) not in TEAM_NAMES_SORTED:
            continue
        operation_row = static.execute(
            "SELECT COALESCE(ModuleClass, ''), COALESCE(CaptureTime, 0) "
            "FROM _Template_PropOperation WHERE Id = ?", (str(operation),),
        ).fetchone()
        mechanism_row = static.execute(
            "SELECT COALESCE(MonsterId, '') FROM _Template_PropMechanism "
            "WHERE Id = ?", (str(mechanism),),
        ).fetchone()
        if (operation_row is None or mechanism_row is None
                or str(operation_row[0]) != "TeamPvPCapturingModule"
                or not str(mechanism_row[0])):
            continue
        try:
            matrix = _matrix(transform)
        except ValueError:
            continue
        monster_id = str(mechanism_row[0])
        control_points.append(FortressControlPoint(
            int(row_id), str(template_id),
            TEAM_NAMES_SORTED.index(str(owner)),
            (matrix[12], matrix[13], matrix[14]),
            math.atan2(matrix[8], matrix[0]),
            float(picking_range or 0.0), float(operation_row[1] or 0.0),
            monster_id, str(path_name or ""),
            event_sets.get(str(event_row), ()),
        ))
        if monster_id not in attacker_templates:
            attacker_templates.append(monster_id)
        skills = static.execute(
            "SELECT COALESCE(Skills, '') FROM _Template_Monster WHERE Id = ?",
            (monster_id,),
        ).fetchone()
        for skill in str(skills[0] if skills else "").replace(";", ",").split(","):
            skill_id = skill.partition(":")[0].strip()
            if not skill_id:
                continue
            summon = static.execute(
                "SELECT COALESCE(SummonMonsterId, '') FROM _Template_Skill "
                "WHERE Id = ?", (skill_id,),
            ).fetchone()
            if summon and str(summon[0]) and str(summon[0]) not in attacker_templates:
                attacker_templates.append(str(summon[0]))
    clear_effects = []
    for row_id, effect_name, effect_id, event_row, transform in effect_rows:
        aura = static.execute(
            "SELECT COALESCE(AuraEffectAffectedTemplateId, ''), "
            "COALESCE(AuraEffectId, '') FROM _Template_StatusEffect "
            "WHERE Id = ?", (str(effect_id),),
        ).fetchone()
        if aura is None or not str(aura[0]).strip():
            continue
        child = static.execute(
            "SELECT COALESCE(StartModifiers, '') FROM _Template_StatusEffect "
            "WHERE Id = ?", (str(aura[1]),),
        ).fetchone()
        if child is None or "currhealthpoints:-" not in str(child[0]).casefold():
            continue
        try:
            volume = BaseVolume(-1, str(effect_name), _matrix(transform))
        except ValueError:
            continue
        clear_effects.append(FortressClearEffect(
            volume,
            frozenset(
                token.strip().casefold() for token in str(aura[0]).split(";")
                if token.strip()
            ),
            event_sets.get(str(event_row), ()),
        ))
    return FortressLayout(
        str(map_id), defender, attacker, tuple(objectives), tuple(signals),
        {key: tuple(value) for key, value in events_by_signal.items()},
        stage_targets,
        {key: tuple(value) for key, value in respawns.items()},
        tuple(control_points), tuple(attacker_templates),
        tuple(clear_effects),
    )


def load_fortress_layout(static_database_path: str | Path, map_id: str):
    """The authored CastleRush layout of ``map_id``, or ``None``."""

    return _load_fortress_layout(str(static_database_path), str(map_id))


class FortressState:
    """Attack/defend progress of one CastleRush round.

    The defenders own every authored tower; the attackers own the control
    points. Destroying monsters raises the authored ``MonstersEliminated``
    signals, which start the map-local events that swap ``_invincible``
    towers for vulnerable ones -- lane by lane, then the core tower, then the
    drawbridge switch. Destroying a stage's target completes the stage: the
    next ``pvp_castlerush_stage_N`` event replaces ``pvp_castlerush_initial``
    (which moves both sides' respawns to the next authored entry, as the
    entries' EventSets say). Destroying the last stage's target wins the
    round for the attackers; the defenders win when the round times out.
    Retiring ``pvp_castlerush_initial`` at the stage change is a local
    reading: otherwise both stages' respawn entries would be live at once.
    """

    def __init__(self, layout: FortressLayout) -> None:
        self.layout = layout
        self.stage = 0
        self.destroyed: set[int] = set()
        self.fired_signals: set[str] = set()
        self.active_events: set[str] = {
            CASTLE_RUSH_INITIAL_EVENT, CASTLE_RUSH_STAGE_FLAG.format(stage=0),
        }
        self.attackers_won = False
        self.applied_clear_effects: set[int] = set()

    @property
    def stage_count(self) -> int:
        return len(self.layout.stage_targets)

    def active(self) -> frozenset[str]:
        return frozenset(self.active_events)

    def live_objectives(self) -> tuple[FortressObjective, ...]:
        active = self.active()
        return tuple(
            objective for objective in self.layout.objectives
            if objective.row_id not in self.destroyed
            and objective.present(active)
        )

    @property
    def destroyed_count(self) -> int:
        return len(self.destroyed)

    def stage_target_templates(self) -> tuple[str, ...]:
        """The current stage's target monster templates (StageInfo keys)."""

        names = self.layout.stage_targets.get(self.stage, ())
        return tuple(
            objective.template_id for objective in self.layout.objectives
            if objective.spawn_id in names
        )

    def destroy_template(self, template_id: str, *, attacker_team: int):
        """``destroy`` for the live objective with this monster template."""

        objective = self.layout.objective_for_template(template_id)
        if objective is None:
            return ()
        return self.destroy(objective.row_id, attacker_team=attacker_team)

    def control_point_present(self, point: FortressControlPoint) -> bool:
        return point.present(self.active())

    def due_clear_effects(self) -> tuple[tuple[int, FortressClearEffect], ...]:
        """Destroying auras whose EventSet just became active."""

        active = self.active()
        return tuple(
            (index, effect)
            for index, effect in enumerate(self.layout.clear_effects)
            if index not in self.applied_clear_effects and effect.present(active)
        )

    def apply_clear_effect(self, index: int) -> tuple[FortressObjective, ...]:
        """Mark one aura applied; destroy the structures it covers.

        Covered towers and their ``_invincible`` stand-ins count as
        destroyed without raising further signals: everything the chain
        would have revealed inside the box dies to the same aura.
        """

        if index in self.applied_clear_effects:
            return ()
        self.applied_clear_effects.add(index)
        effect = self.layout.clear_effects[index]
        covered = []
        for objective in self.layout.objectives:
            template = objective.template_id.casefold()
            base = (template[:-len(INVINCIBLE_SUFFIX)]
                    if template.endswith(INVINCIBLE_SUFFIX) else template)
            if (base in effect.templates
                    and effect.volume.contains(objective.position)
                    and objective.row_id not in self.destroyed):
                self.destroyed.add(objective.row_id)
                covered.append(objective)
        return tuple(covered)

    def vulnerable(self, row_id: int) -> bool:
        for objective in self.live_objectives():
            if objective.row_id != int(row_id) or objective.invincible:
                continue
            stage = self._target_stage(objective)
            if stage is None:
                return True
            # A stage target only counts in its own stage (its minimap
            # marker's EventSet is pvp_stage_N), and the stage-0 drawbridge
            # switch only once the core tower's fall started the bridge
            # mechanism -- the switch stands behind the raised bridge. The
            # map enforces both physically; the server enforces them here.
            if stage != self.stage:
                return False
            gate = STAGE_TARGET_GATES.get(stage)
            return gate is None or gate in self.active_events
        return False

    def _target_stage(self, objective: FortressObjective) -> int | None:
        for stage, names in self.layout.stage_targets.items():
            if objective.spawn_id in names:
                return stage
        return None

    def respawn(self, team: int):
        """The team's respawn entry for the current event state."""

        active = self.active()
        rows = self.layout.respawns.get(int(team), ())
        for flags, position, heading in rows:
            if flags and all((name in active) == state for name, state in flags):
                return position, heading
        return (rows[0][1], rows[0][2]) if rows else None

    def destroy(self, row_id: int, *, attacker_team: int):
        """An attacker destroyed objective ``row_id``. Return the events."""

        if (self.attackers_won
                or int(attacker_team) != self.layout.attacker_team
                or not self.vulnerable(row_id)):
            return ()
        objective = next(
            o for o in self.layout.objectives if o.row_id == int(row_id)
        )
        self.destroyed.add(objective.row_id)
        events = [(FORTRESS_OBJECTIVE_DESTROYED, objective.spawn_id, self.stage)]
        self._raise_signals()
        if objective.spawn_id in self.layout.stage_targets.get(self.stage, ()):
            events.append((FORTRESS_STAGE_COMPLETED, objective.spawn_id, self.stage))
            if self.stage + 1 >= self.stage_count:
                self.attackers_won = True
                events.append((FORTRESS_ATTACKERS_WON, objective.spawn_id, self.stage))
            else:
                self.active_events.discard(CASTLE_RUSH_INITIAL_EVENT)
                self.active_events.discard(
                    CASTLE_RUSH_STAGE_FLAG.format(stage=self.stage)
                )
                self.stage += 1
                self.active_events.add(
                    CASTLE_RUSH_STAGE_EVENT.format(stage=self.stage)
                )
                self.active_events.add(
                    CASTLE_RUSH_STAGE_FLAG.format(stage=self.stage)
                )
        return tuple(events)

    def _raise_signals(self) -> None:
        # A MonstersEliminated signal fires once every authored spawn of its
        # templates is destroyed; the events it starts may expose more.
        changed = True
        while changed:
            changed = False
            for templates, signal in self.layout.signals:
                if signal in self.fired_signals:
                    continue
                rows = [
                    o for o in self.layout.objectives
                    if o.template_id in templates
                ]
                if rows and all(o.row_id in self.destroyed for o in rows):
                    self.fired_signals.add(signal)
                    for event in self.layout.events_by_signal.get(signal, ()):
                        if event not in self.active_events:
                            self.active_events.add(event)
                            changed = True
