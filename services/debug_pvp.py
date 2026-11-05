"""``!debug_pvp``: battleground matches and an open arena filled with bots.

Two ways to fight bots:

* ``!debug_pvp <mode>`` (``1v1``, ``tdm``, ``ctf``, ``fortress``, ``ffa``)
  puts the admin and enough bots for a full match into that mode's real
  matchmaking queue. The server forms the match, invites everyone (the admin
  accepts in the normal dialog), loads the arena and runs its countdown,
  rounds, objectives and result screen exactly as it does for players.
* ``!debug_pvp open [bots] [mapId]`` sends the admin to the Royal Arena
  (``a0201_basicarena``) and spawns bots there for open-world PvP.

A bot is an ordinary gameplay ``Session`` with nobody on the other end of the
wire. It plays by feeding the server the commands a game client sends --
JoinMatchQueue, JoinBattlegroundResponse, MoveCommand, TargetSkillCommand /
BulletSkillCommand, ActorRequest, PropInteraction, RespawnCommand, LeavePVP --
through the normal ingress path, so every rule the server applies to players
applies to bots too. Whatever the server sends a bot is discarded, and bots
are never saved.

The one thing a bot cannot do by itself is reconnect after a map transfer.
``_begin_map_transfer`` hands a bot's transfer to :func:`transfer_bot`; the
bot's controller then does what a client would: a fresh session for the same
character, initialized on the destination map. A bot sent anywhere other than
its battleground (the hub, after a match) is removed instead.

Behaviour is deliberately simple. Every bot uses its class's basic attack,
walks the navmesh, stops at that attack's range and keeps walking while a
wall blocks its shot. In a match bots fight every hostile player, bots
included; capture-the-flag bots run for the enemy flag, carry it home and
return their own; fortress attackers capture control points and shoot the
structures, defenders guard them. Bots leave the arena a few seconds after
the result screen, or at once on ``!debug_pvp stop``.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import itertools
import math
import random
import sqlite3
import struct
import threading
import time
import traceback

from dso_server.gameplay.pvp_match_state import MatchPhase
from dso_server.gameplay.pvp_objectives import (
    FLAG_CARRIED,
    FLAG_DROPPED,
    CaptureTheFlagState,
    FortressState,
    horizontal_distance,
)
from dso_server.protocol.character_selection import (
    CLASS_IDS,
    CLASS_NAMES,
    LOCAL_CHARACTER_ID_BASE,
    CharacterRecord,
)
from dso_server.protocol.combat_protocol import (
    BULLET_SKILL_COMMAND,
    TARGET_SKILL_COMMAND,
)
from dso_server.protocol.gameplay_protocol import ACTOR_REQUEST_COMMAND, Move
from dso_server.protocol.prop_protocol import (
    PropInteraction,
    encode_prop_interaction,
)
from dso_server.protocol.pvp_match_protocol import (
    BATTLEGROUND_MAPS,
    MATCH_MODE_ASSAULT_THE_FORTRESS_5V5,
    MATCH_MODE_BEST_OF_3_1V1,
    MATCH_MODE_BEST_OF_5_5V5,
    MATCH_MODE_CAPTURE_THE_FLAG_5V5,
    MATCH_MODE_LAST_MAN_STANDING_8FFA,
    MATCH_TEAM_SIZES,
    OBJECTIVE_MATCH_MODES,
    JoinBattlegroundResponse,
    JoinMatchQueue,
    LeavePVP,
    client_queue_for_mode,
    encode_join_battleground_response,
    encode_join_match_queue,
    encode_leave_pvp,
)
from dso_server.protocol.respawn_protocol import RESPAWN_COMMAND
from dso_server.runtime.transport import Session
from dso_server.services.base import CharacterService
from dso_server.services.player_respawn import (
    _is_open_pve_map,
    respawn_map_allowed,
)


# The Royal Arena: an OpenPVE map built for duels, so PvP there is lethal,
# the death dialog's respawn works and every visitor shares one instance. Its
# only monsters are passive damage dolls. The battleground maps only work
# inside a match (``!debug_pvp <mode>``).
DEFAULT_ARENA_MAP = "a0201_basicarena"
DEFAULT_BOT_COUNT = 3
MAX_BOT_COUNT = 8

MATCH_MODES = {
    "1v1": MATCH_MODE_BEST_OF_3_1V1,
    "duel": MATCH_MODE_BEST_OF_3_1V1,
    "tdm": MATCH_MODE_BEST_OF_5_5V5,
    "5v5": MATCH_MODE_BEST_OF_5_5V5,
    "ctf": MATCH_MODE_CAPTURE_THE_FLAG_5V5,
    "fortress": MATCH_MODE_ASSAULT_THE_FORTRESS_5V5,
    "atf": MATCH_MODE_ASSAULT_THE_FORTRESS_5V5,
    "ffa": MATCH_MODE_LAST_MAN_STANDING_8FFA,
    "lms": MATCH_MODE_LAST_MAN_STANDING_8FFA,
}
MODE_LABELS = {
    MATCH_MODE_BEST_OF_3_1V1: "1v1 best of 3",
    MATCH_MODE_BEST_OF_5_5V5: "5v5 team deathmatch",
    MATCH_MODE_CAPTURE_THE_FLAG_5V5: "5v5 capture the flag",
    MATCH_MODE_ASSAULT_THE_FORTRESS_5V5: "5v5 assault the fortress",
    MATCH_MODE_LAST_MAN_STANDING_8FFA: "8-player last man standing",
}

USAGE = (
    "usage: !debug_pvp 1v1|tdm|ctf|fortress|ffa | "
    "!debug_pvp open [bots 1-8] [mapId] | !debug_pvp stop"
)

BOT_TICK_SECONDS = 0.2
SERVER_TICKS_PER_SECOND = 25
# Viewers dead-reckon a remote player at (speed byte / 64) x 5 m/s, the base
# RunSpeed of every class, so a scalar of 1.0 is an ordinary run.
BOT_RUN_SPEED = 5.0
# The client stamps every MoveCommand it sends with this horizon.
BOT_MOVE_DURATION_TICKS = 20
BOT_ATTACK_INTERVAL_SECONDS = 1.2
BOT_RESPAWN_DELAY_SECONDS = 5.0
ARRIVAL_TIMEOUT_SECONDS = 90.0
ARRIVAL_POLL_SECONDS = 0.25
SPAWN_RADIUS = 8.0
# Ranged bots hold this distance rather than walking into melee.
RANGED_ENGAGE_DISTANCE = 9.0
# Same-spot respawns cost gold; bots carry plenty (in memory only).
BOT_RESPAWN_GOLD = 1_000_000

# Matchmaking: how long the queue may take, how quickly a bot "clicks"
# accept, and how long bots look at the result screen before leaving.
QUEUE_TIMEOUT_SECONDS = 60.0
BOT_ACCEPT_DELAY_SECONDS = 0.5
BOT_LEAVE_DELAY_SECONDS = 5.0
OBJECTIVE_RESPAWN_RETRY_SECONDS = 1.0
# Capture the flag: fight only what comes this close; otherwise go for flags.
CTF_ENGAGE_DISTANCE = 10.0
# Assault the fortress.
FORTRESS_ENGAGE_DISTANCE = 12.0
FORTRESS_STRUCTURE_REACH = 30.0
FORTRESS_GUARD_DISTANCE = 6.0
CONTROL_POINT_RETRY_SECONDS = 11.0

# Warrior, mage, ranger, dwarf: each bot uses its class's first quickbar
# skill, the basic attack. ``target`` sends TargetSkillCommand at the victim
# (MeleeTarget skills); ``bullet`` sends BulletSkillCommand along the aim.
BOT_CLASS_ORDER = (
    CLASS_IDS["warrior"], CLASS_IDS["mage"],
    CLASS_IDS["ranger"], CLASS_IDS["dwarf"],
)
BOT_BASIC_ATTACKS = {
    CLASS_IDS["warrior"]: ("angrystrike", "target"),
    CLASS_IDS["mage"]: ("magicmissile", "bullet"),
    CLASS_IDS["ranger"]: ("markshot", "bullet"),
    CLASS_IDS["dwarf"]: ("SimpleShot", "bullet"),
}

# ``_remote_actor_id`` has room for 255 local character ordinals. Real
# characters are numbered upward from LOCAL_CHARACTER_ID_BASE, so bots take
# the top of the range, skipping any id a real character already uses.
BOT_CHARACTER_ID_TOP = LOCAL_CHARACTER_ID_BASE + 0xFE
BOT_CHARACTER_ID_FLOOR = BOT_CHARACTER_ID_TOP - 63

LOG_PREFIX = "[DebugPvP]"

_REGISTRY_LOCK = threading.Lock()
_BOT_PORTS = itertools.count(1)


class _NullSocket:
    """Stands in for the UDP socket: a bot's control packets go nowhere."""

    @staticmethod
    def sendto(data, address):
        return len(data)


class DebugBotSession(Session):
    """A gameplay session without a client: every datagram is discarded.

    Nothing is tracked for acknowledgement, so the retransmit loop has no
    work for it and never retires it for a missing ACK. The bot loop keeps
    ``last_received_at`` fresh for the idle timeout.
    """

    debug_bot = True

    def _send_locked(self, data, *, track, label, pace_after=0.0):
        self.last_send_at = time.perf_counter()
        return False, 0.0


def is_debug_bot(session) -> bool:
    return getattr(session, "debug_bot", False) is True


@dataclass
class DebugBot:
    session: DebugBotSession
    character: CharacterRecord
    skill_index: int = -1
    attack_kind: str = "target"
    attack_range: float = 2.0
    # Bullet launch offset; its height decides what wall a shot would hit.
    start_offset: tuple = (0.0, 0.0, 0.0, 0.0)
    static_collision: bool = False
    next_attack_at: float = 0.0
    died_at: float | None = None
    cast_id: int = 0x100
    # Matchmaking bookkeeping.
    answered_match: int = 0
    left_match: bool = False
    next_respawn_at: float = 0.0
    next_interaction_at: float = 0.0
    requested_actor_ids: set = field(default_factory=set)


# ----------------------------------------------------------------------
# client command encoding
# ----------------------------------------------------------------------

def encode_move_command(move: Move) -> bytes:
    """MoveCommand exactly as the client sends it (18 bytes)."""

    return b"\x8b" + (0x005F).to_bytes(2, "little") + move.encode_body()


def _skill_header(command_id, skill_index, aim_angle, server_tick, cast_id):
    # SkillCommand base: SkillCounter, SkillManager index, aim, tick, cast.
    # Counter 0 never trips the viewer's increasing-counter check. The
    # optional inheritance bit is omitted; the decoders accept both layouts.
    return b"\x8b" + int(command_id).to_bytes(2, "little") + struct.pack(
        "<HHfII", 0, int(skill_index), float(aim_angle),
        int(server_tick) & 0xFFFFFFFF, int(cast_id) & 0xFFFFFFFF,
    )


def encode_target_skill_command(
        skill_index, aim_angle, server_tick, cast_id, target_actor_id) -> bytes:
    return _skill_header(
        TARGET_SKILL_COMMAND, skill_index, aim_angle, server_tick, cast_id,
    ) + struct.pack("<I", int(target_actor_id))


def encode_bullet_skill_command(
        skill_index, aim_angle, server_tick, cast_id, direction) -> bytes:
    return _skill_header(
        BULLET_SKILL_COMMAND, skill_index, aim_angle, server_tick, cast_id,
    ) + struct.pack("<ffffB", *(float(value) for value in direction), 0)


def encode_respawn_command(*, nearest_exit=False, same_spot=False):
    """RespawnCommand: two flags in the top bits of the fourth byte."""

    flags = (0x80 if nearest_exit else 0) | (0x40 if same_spot else 0)
    return b"\x8b" + int(RESPAWN_COMMAND).to_bytes(2, "little") + bytes(
        (flags,)
    ), 26


def encode_actor_request(actor_id: int) -> bytes:
    """ActorRequest: the client asking for an actor it was told is nearby."""

    return b"\x8b" + int(ACTOR_REQUEST_COMMAND).to_bytes(2, "little") + (
        struct.pack("<I", int(actor_id))
    )


def _aim(dx: float, dz: float) -> float:
    # The client moves and aims along (-sin a, 0, -cos a).
    return math.atan2(-dx, -dz)


# ----------------------------------------------------------------------
# registries
# ----------------------------------------------------------------------

def _controllers(server) -> dict:
    """Admin character id -> that admin's running controller."""

    controllers = getattr(server, "_debug_pvp_arenas", None)
    if controllers is None:
        controllers = server._debug_pvp_arenas = {}
    return controllers


def _owners(server) -> dict:
    """Bot character id -> the controller driving it."""

    owners = getattr(server, "_debug_pvp_bot_owners", None)
    if owners is None:
        owners = server._debug_pvp_bot_owners = {}
    return owners


def active_arena(server, character_id: int):
    with _REGISTRY_LOCK:
        return _controllers(server).get(int(character_id))


def stop_arena(server, character_id: int, *, reason: str,
               wait: float = 10.0) -> bool:
    with _REGISTRY_LOCK:
        controller = _controllers(server).get(int(character_id))
    if controller is None:
        return False
    controller.stop(reason)
    controller.join(wait)
    return True


def transfer_bot(server, s, destination_map_id, *, spawn_position=None,
                 reason: str = "") -> bool:
    """``_begin_map_transfer`` for a bot, which has no client to reconnect.

    The bot's controller brings it in on the destination itself. Returns
    whether the transfer started, as ``_begin_map_transfer`` does.
    """

    character = s.gameplay_character
    with _REGISTRY_LOCK:
        controller = (
            _owners(server).get(int(character.character_id))
            if character is not None else None
        )
    if controller is None or controller.stopped:
        print(
            f"{LOG_PREFIX} {getattr(character, 'name', s.addr)!r} has no "
            f"controller; removing it instead of sending it to "
            f"{destination_map_id}"
        )
        _retire_session(server, s, "debug pvp bot without a controller")
        return False
    with s.flow_lock:
        if s.gameplay_exit_transition_started:
            return False
        # As for a real client: the source session takes no more input.
        s.gameplay_exit_transition_started = True
    controller.request_transfer(
        s, str(destination_map_id), spawn_position, str(reason),
    )
    return True


def _retire_session(server, session, reason: str) -> None:
    try:
        server._retire_session(session, reason=reason, remove=True)
    except Exception as exc:
        print(
            f"{LOG_PREFIX} could not retire bot {session.addr}: "
            f"{type(exc).__name__}: {exc}"
        )


# ----------------------------------------------------------------------
# admin command
# ----------------------------------------------------------------------

def handle_admin_command(server, admin: Session, arguments, *, notify=None) -> str:
    """Run ``!debug_pvp`` for ``admin`` and return the chat reply."""

    character = admin.gameplay_character
    if character is None:
        raise ValueError("your gameplay character is not online")
    character_id = int(character.character_id)
    arguments = tuple(str(value) for value in arguments)
    if not arguments:
        raise ValueError(USAGE)
    word = arguments[0].casefold()
    if word in {"stop", "off", "end"}:
        if len(arguments) != 1:
            raise ValueError(USAGE)
        if not stop_arena(server, character_id, reason="stopped by admin"):
            return "No PvP debug bots are running for you."
        return "PvP debug bots removed."
    if word == "open":
        return _start_open_arena(
            server, admin, character_id, arguments[1:], notify,
        )
    if word in MATCH_MODES:
        if len(arguments) != 1:
            raise ValueError(USAGE)
        return _start_match(
            server, admin, character_id, MATCH_MODES[word], notify,
        )
    raise ValueError(USAGE)


def _start_open_arena(server, admin, character_id, arguments, notify) -> str:
    if len(arguments) > 2:
        raise ValueError(USAGE)
    try:
        bot_count = int(arguments[0]) if arguments else DEFAULT_BOT_COUNT
    except ValueError:
        raise ValueError(f"{USAGE}; bots must be a whole number") from None
    if not 1 <= bot_count <= MAX_BOT_COUNT:
        raise ValueError(f"bots must be from 1 to {MAX_BOT_COUNT}")
    map_id = arguments[1] if len(arguments) == 2 else DEFAULT_ARENA_MAP
    database = str(server.static_database_path)
    try:
        usable = (
            respawn_map_allowed(database, map_id)
            and _is_open_pve_map(database, map_id)
        )
    except (OSError, sqlite3.Error) as exc:
        raise ValueError(f"cannot read map {map_id!r}: {exc}") from None
    if not usable:
        raise ValueError(
            f"{map_id!r} is not an open PvE map with respawn; open-arena "
            f"bots need one (default {DEFAULT_ARENA_MAP}). For battleground "
            "maps use !debug_pvp 1v1|tdm|ctf|fortress|ffa"
        )
    if getattr(admin, "gameplay_match", None) is not None:
        raise ValueError("leave your battleground first")

    stop_arena(server, character_id, reason="replaced by a new !debug_pvp")
    already_there = (
        str(admin.gameplay_map or "") == map_id
        and admin.gameplay_presence_ready
        and admin.gameplay_combat_state is not None
    )
    if not already_there:
        if not server._begin_map_transfer(
            admin, map_id, reason="admin-debug-pvp",
            gameplay_switch=True, scaling_difficulty=0,
        ):
            raise ValueError("a map transfer is already in progress")
    arena = DebugPvPArena(
        server, character_id, map_id, bot_count, notify=notify,
    )
    _register(server, character_id, arena)
    arena.start()
    if already_there:
        return (
            f"Spawning {bot_count} PvP bot(s) around you. "
            "!debug_pvp stop removes them."
        )
    return (
        f"Teleporting to {map_id}; {bot_count} PvP bot(s) join when you "
        "arrive. !debug_pvp stop removes them."
    )


def _start_match(server, admin, character_id, match_mode, notify) -> str:
    if getattr(admin, "gameplay_match", None) is not None:
        raise ValueError("you are already in a battleground")
    if not admin.gameplay_presence_ready:
        raise ValueError("wait until your map has finished loading")
    sizes = MATCH_TEAM_SIZES.get(int(match_mode))
    if not sizes:
        raise ValueError(f"mode {match_mode} has no team sizes")
    bot_count = sum(sizes) - 1
    stop_arena(server, character_id, reason="replaced by a new !debug_pvp")
    controller = DebugPvPMatch(
        server, character_id, match_mode, bot_count, notify=notify,
    )
    _register(server, character_id, controller)
    controller.start()
    return (
        f"Queueing you and {bot_count} bot(s) for "
        f"{MODE_LABELS[int(match_mode)]} on "
        f"{BATTLEGROUND_MAPS[int(match_mode)]}. Accept the match invitation "
        "when it appears; !debug_pvp stop removes the bots."
    )


def _register(server, character_id, controller) -> None:
    with _REGISTRY_LOCK:
        _controllers(server)[int(character_id)] = controller


# ----------------------------------------------------------------------
# shared bot machinery
# ----------------------------------------------------------------------

class _BotController:
    """One admin's bots: creation, the 5 Hz loop, movement, combat, removal."""

    def __init__(self, server, admin_character_id: int, *, notify=None,
                 rng=None):
        self.server = server
        self.admin_character_id = int(admin_character_id)
        self.notify = notify
        self.rng = rng or random.Random()
        self.bots: list[DebugBot] = []
        self.admin: Session | None = None
        self.stop_reason = None
        self._stop = threading.Event()
        self._thread = None
        self._clock_anchor = None
        self._transfer_lock = threading.Lock()
        self._transfers: list = []

    # lifecycle ---------------------------------------------------------

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, daemon=True,
            name=f"debug-pvp-{self.admin_character_id}",
        )
        self._thread.start()

    def stop(self, reason: str) -> None:
        if self.stop_reason is None:
            self.stop_reason = str(reason)
        self._stop.set()

    def join(self, timeout: float | None = None) -> None:
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    def _say(self, text: str) -> None:
        print(f"{LOG_PREFIX} {text}")
        if self.notify is not None:
            try:
                self.notify(text)
            except Exception as exc:
                print(f"{LOG_PREFIX} could not notify the admin: {exc}")

    def _run(self) -> None:
        try:
            self._main()
        except Exception as exc:
            traceback.print_exc()
            self._say(
                f"PvP debug bots stopped: {type(exc).__name__}: {exc}"
            )
        finally:
            self.teardown()
            with _REGISTRY_LOCK:
                controllers = _controllers(self.server)
                if controllers.get(self.admin_character_id) is self:
                    del controllers[self.admin_character_id]

    def _main(self) -> None:
        raise NotImplementedError

    def _admin_sessions(self):
        for session in tuple(self.server.sessions.values()):
            character = session.gameplay_character
            if (
                not is_debug_bot(session)
                and character is not None
                and int(character.character_id) == self.admin_character_id
                and session.connected
                and not session.retired
                and session.gameplay_mode
                and session.gameplay_presence_ready
                and not session.gameplay_presence_departed
                and not session.gameplay_exit_transition_started
            ):
                yield session

    # bots --------------------------------------------------------------

    def _free_character_ids(self, count: int) -> list[int]:
        taken = set()
        for session in tuple(self.server.sessions.values()):
            if session.gameplay_character is not None:
                taken.add(int(session.gameplay_character.character_id))
        selection = getattr(self.server, "character_selection", None)
        for character in getattr(selection, "characters", ()) or ():
            taken.add(int(character.character_id))
        with _REGISTRY_LOCK:
            taken.update(int(value) for value in _owners(self.server))
        store = getattr(self.server, "account_store", None)
        chosen = []
        for character_id in range(
                BOT_CHARACTER_ID_TOP, BOT_CHARACTER_ID_FLOOR - 1, -1):
            if len(chosen) == count:
                break
            if character_id in taken:
                continue
            if store is not None and store.get(character_id) is not None:
                continue
            chosen.append(character_id)
        if len(chosen) < count:
            raise ValueError("no free character ids left for bots")
        return chosen

    def _make_character(self, index, character_id, level, map_id):
        class_id = BOT_CLASS_ORDER[index % len(BOT_CLASS_ORDER)]
        level = max(1, min(100, int(level)))
        try:
            experience = int(
                self.server.static_database.player_xp_bounds(class_id, level)[0]
            )
        except Exception:
            experience = 0
        return CharacterRecord(
            name=f"Bot {index + 1} {CLASS_NAMES[class_id].title()}",
            map_id=str(map_id or DEFAULT_ARENA_MAP),
            class_id=class_id,
            gender=0,
            hair_style=0,
            beard_style=0,
            body_style=0,
            body_stature=0,
            character_id=int(character_id),
            unknown_u32_a=0,
            premium_currency=0,
            progression_u64=0,
            progression_u32=experience,
            level=level,
        )

    def _new_session(self, character) -> DebugBotSession:
        """A connected bot session in the server registry, not yet on a map."""

        server = self.server
        address = ("192.0.2.1", 20000 + next(_BOT_PORTS) % 40000)
        bot = DebugBotSession(
            address, _NullSocket(),
            getattr(server, "gameplay_deadline_scheduler", None),
            getattr(server, "gameplay_ingress_scheduler", None),
        )
        bot.connected = True
        bot.gameplay_mode = True
        bot.gameplay_character = character
        bot.gameplay_persist_callback = lambda *_args, **_kwargs: False
        bot.last_received_at = time.perf_counter()
        with _REGISTRY_LOCK:
            _owners(server)[int(character.character_id)] = self
        server.sessions[address] = bot
        return bot

    def _initialize(self, session, map_id, position, *, access_id=None,
                    scaling_difficulty=0) -> None:
        """Run the real map initialization for a bot session."""

        character = session.gameplay_character
        # Same access route and difficulty as the admin: the same instance.
        character.map_access_id = str(access_id or map_id)
        character.scaling_difficulty = int(scaling_difficulty or 0)
        session.gameplay_map = str(map_id)
        session.gameplay_route_spawn_position = (
            tuple(float(v) for v in position) if position is not None else None
        )
        session.last_received_at = time.perf_counter()
        self.server._send_local_player_initialization(session)
        if not session.gameplay_presence_ready:
            raise ValueError(f"{character.name} did not finish loading")
        if session.gameplay_combat_state is None:
            raise ValueError(f"{character.name} got no combat world on {map_id}")

    def _arm_bot(self, bot: DebugBot, index: int = 0) -> None:
        """Pick up the class attack and gold for the bot's current session."""

        s = bot.session
        combat = s.gameplay_combat_state
        class_id = int(bot.character.class_id)
        resource, kind = BOT_BASIC_ATTACKS[class_id]
        skill_index = self._skill_index(resource)
        if skill_index not in s.gameplay_skill_book_ids:
            raise ValueError(
                f"{resource} is not in the {CLASS_NAMES[class_id]} skill book"
            )
        geometry = combat._load_player_skill_geometry(skill_index)
        bot.skill_index = skill_index
        bot.attack_kind = kind
        bot.attack_range = float(
            getattr(geometry, "attack_range", 0.0)
            or (2.0 if kind == "target" else 20.0)
        )
        bot.start_offset = tuple(
            getattr(geometry, "start_offset", ()) or (0.0,) * 4
        )
        bot.static_collision = bool(getattr(geometry, "static_collision", False))
        bot.next_attack_at = time.perf_counter() + 1.0 + 0.2 * index
        bot.requested_actor_ids = set()
        bot.died_at = None
        with s.flow_lock:
            currency = s.gameplay_currency_state
            if currency is not None:
                s.gameplay_currency_state = replace(
                    currency, currency_type=max(
                        int(currency.currency_type), BOT_RESPAWN_GOLD,
                    ),
                )

    def _skill_index(self, resource: str) -> int:
        with sqlite3.connect(str(self.server.static_database_path)) as database:
            row = database.execute(
                'SELECT rowid - 1 FROM "_Template_Skill" '
                'WHERE "Id"=? COLLATE NOCASE LIMIT 1',
                (resource,),
            ).fetchone()
        if row is None:
            raise ValueError(f"skill {resource!r} is not in static.db4")
        return int(row[0])

    def _bot_for(self, session) -> DebugBot | None:
        return next((bot for bot in self.bots if bot.session is session), None)

    def _drop(self, bot: DebugBot, reason: str) -> None:
        """Remove one bot for good."""

        if bot in self.bots:
            self.bots.remove(bot)
        with _REGISTRY_LOCK:
            owners = _owners(self.server)
            if owners.get(int(bot.character.character_id)) is self:
                del owners[int(bot.character.character_id)]
        _retire_session(self.server, bot.session, reason)

    def request_transfer(self, session, destination, spawn_position, reason):
        with self._transfer_lock:
            self._transfers.append((session, destination, spawn_position, reason))

    def _process_transfers(self) -> None:
        with self._transfer_lock:
            transfers, self._transfers = self._transfers, []
        for session, destination, spawn_position, reason in transfers:
            try:
                self._transfer(session, destination, spawn_position, reason)
            except Exception as exc:
                traceback.print_exc()
                print(f"{LOG_PREFIX} transfer failed: {exc}")

    def _transfer(self, session, destination, spawn_position, reason) -> None:
        """By default a bot that is sent away is simply removed."""

        bot = self._bot_for(session)
        if bot is None:
            _retire_session(self.server, session, "debug pvp bot transferred")
            return
        self._drop(bot, f"debug pvp bot sent to {destination} ({reason})")

    def _keep_alive(self) -> None:
        now = time.perf_counter()
        for bot in tuple(self.bots):
            bot.session.last_received_at = now

    def teardown(self) -> None:
        bots = tuple(self.bots)
        for bot in bots:
            self._drop(bot, f"debug pvp ended: {self.stop_reason}")
        with _REGISTRY_LOCK:
            owners = _owners(self.server)
            for character_id in [k for k, v in owners.items() if v is self]:
                del owners[character_id]
        if bots:
            print(
                f"{LOG_PREFIX} removed {len(bots)} bot(s) for admin "
                f"{self.admin_character_id} ({self.stop_reason})"
            )

    # clock and positions -----------------------------------------------

    def _tick(self) -> int:
        """The bots' clock, kept in the admin client's tick base.

        A viewer refuses a remote Move stamped more than ten ticks ahead of
        its own clock, so bots follow the admin's latest tick (their client
        sends one every tick) and only run on wall time between updates.
        """

        now = time.perf_counter()
        admin_tick = int(
            getattr(self.admin, "gameplay_chase_server_tick", 0) or 0
        )
        anchor = self._clock_anchor
        if anchor is None:
            anchor = self._clock_anchor = (admin_tick, now)
        base_tick, base_time = anchor
        tick = base_tick + int((now - base_time) * SERVER_TICKS_PER_SECOND)
        # Follow the admin forward at once, and back down when their client
        # restarted its clock (an arena load); zero means no Move seen yet.
        if admin_tick > tick or (
                admin_tick > 0 and tick - admin_tick > 2 * SERVER_TICKS_PER_SECOND):
            self._clock_anchor = (admin_tick, now)
            tick = admin_tick
        return tick & 0xFFFFFFFF

    @staticmethod
    def _world_position(session, frame):
        with session.flow_lock:
            move = session.gameplay_last_move
            spawn = session.gameplay_spawn_position
        if move is not None:
            return frame.wire_to_world(move.relative_position)
        if spawn is None:
            raise ValueError("player has no position")
        return tuple(float(value) for value in spawn)

    @staticmethod
    def _ground_position(combat, point, reference):
        """Walkable ground near ``point`` that a player at ``reference`` reaches.

        A projected point can land on a disconnected navmesh piece (a ledge
        or the far side of a wall). A bot spawned there gets no combat world
        (the encounter is built for the spawn's component) and could never
        walk to its target, so such points fall back to closer rings.
        """

        navigation = getattr(combat.encounter, "navigation", None)
        if navigation is None:
            return tuple(point)
        try:
            home = navigation.component_for_position(reference)
        except Exception:
            return tuple(reference)
        dx, dz = point[0] - reference[0], point[2] - reference[2]
        for fraction in (1.0, 0.75, 0.5, 0.25):
            candidate = (
                reference[0] + dx * fraction, point[1],
                reference[2] + dz * fraction,
            )
            try:
                projected = tuple(navigation.project_position_relative(
                    candidate, reference, max_horizontal_distance=2.0,
                ))
                # Same ground piece, not merely linked to it: the map's
                # encounter is assembled for the spawn's own component.
                if navigation.component_for_position(projected) == home:
                    return projected
            except Exception:
                continue
        return tuple(reference)

    def _aim_toward(self, bot_session, target_session, combat) -> float:
        frame = combat.encounter.coordinate_frame
        try:
            mine = self._world_position(bot_session, frame)
            theirs = self._world_position(target_session, frame)
        except ValueError:
            return 0.0
        return _aim(theirs[0] - mine[0], theirs[2] - mine[2])

    @staticmethod
    def _alive(session, combat) -> bool:
        if session.gameplay_death_pending:
            return False
        actor_id = session.gameplay_remote_actor_id
        with combat.lock:
            health = combat.player_health.get(int(actor_id or 0))
        return health is not None and float(health[0]) > 0

    def _hostile_players(self, bot: DebugBot, combat, mine, *,
                         include_bots: bool):
        """Living hostile players in reach, nearest first."""

        s = bot.session
        frame = combat.encounter.coordinate_frame
        found = []
        for peer in self.server._active_gameplay_sessions(
                map_id=s.gameplay_map, excluding=s, instance_of=s):
            if (
                (not include_bots and is_debug_bot(peer))
                or peer.gameplay_combat_state is not combat
                or peer.gameplay_last_move is None
                or not self._alive(peer, combat)
                or CharacterService._player_is_untargetable(peer)
                or not CharacterService._pvp_target_is_hostile(s, peer)
            ):
                continue
            theirs = self._world_position(peer, frame)
            distance = math.hypot(theirs[0] - mine[0], theirs[2] - mine[2])
            found.append((distance, peer, theirs))
        found.sort(key=lambda value: value[0])
        return found

    # actions -----------------------------------------------------------

    def _engage(self, bot: DebugBot, combat, mine, theirs, target_actor_id,
                now, tick) -> None:
        """Close to attack range (with a clear shot) and attack."""

        frame = combat.encounter.coordinate_frame
        dx, dz = theirs[0] - mine[0], theirs[2] - mine[2]
        distance = math.hypot(dx, dz)
        with bot.session.flow_lock:
            last = bot.session.gameplay_last_move
        heading = float(last.yaw) if last is not None else 0.0
        aim = _aim(dx, dz) if distance > 1e-3 else heading
        if bot.attack_kind == "target":
            engage = max(0.8, bot.attack_range * 0.75)
        else:
            engage = min(RANGED_ENGAGE_DISTANCE, bot.attack_range * 0.8)
            if not self._clear_shot(combat, bot, mine, theirs):
                # The server stops bullets at walls like the client does.
                # Walk the navmesh route, which bends around the cover,
                # until the line opens.
                engage = 1.0
        if distance > engage:
            self._walk_to(bot, combat, mine, theirs, tick, stop_within=engage)
            return
        self._send_move(bot, frame.world_to_wire(mine), 0.0, aim, tick)
        if now >= bot.next_attack_at:
            self._attack(bot, target_actor_id, aim, dx, dz, distance, tick)
            bot.next_attack_at = now + BOT_ATTACK_INTERVAL_SECONDS + (
                self.rng.uniform(0.0, 0.4)
            )

    def _walk_to(self, bot: DebugBot, combat, mine, goal, tick, *,
                 stop_within: float = 0.5) -> None:
        frame = combat.encounter.coordinate_frame
        distance = math.hypot(goal[0] - mine[0], goal[2] - mine[2])
        with bot.session.flow_lock:
            last = bot.session.gameplay_last_move
        heading = float(last.yaw) if last is not None else 0.0
        if distance <= stop_within:
            self._send_move(bot, frame.world_to_wire(mine), 0.0, heading, tick)
            return
        step = min(
            distance - stop_within * 0.9, BOT_RUN_SPEED * BOT_TICK_SECONDS,
        )
        destination = self._advance(combat, mine, goal, step)
        moved_x, moved_z = destination[0] - mine[0], destination[2] - mine[2]
        aim = _aim(moved_x, moved_z) if math.hypot(moved_x, moved_z) > 1e-3 else heading
        self._send_move(
            bot, frame.world_to_wire(destination),
            min(1.0, step / (BOT_RUN_SPEED * BOT_TICK_SECONDS)), aim, tick,
        )

    @staticmethod
    def _clear_shot(combat, bot: DebugBot, start, target) -> bool:
        """Would this bot's bullet reach ``target`` before a wall stops it?"""

        if not bot.static_collision:
            return True
        try:
            reach = combat._bullet_static_reach(bot.start_offset)
            if reach is None:
                return True
            return reach(start, target) + 0.5 >= math.dist(
                tuple(start[:3]), tuple(target[:3])
            )
        except Exception:
            return True

    @staticmethod
    def _advance(combat, start, goal, step):
        navigation = getattr(combat.encounter, "navigation", None)
        if navigation is not None:
            try:
                return tuple(navigation.advance_toward(start, goal, step))
            except Exception:
                pass
        dx, dz = goal[0] - start[0], goal[2] - start[2]
        length = math.hypot(dx, dz)
        if length <= 1e-6:
            return tuple(start)
        ratio = min(1.0, step / length)
        return (start[0] + dx * ratio, start[1], start[2] + dz * ratio)

    def _stand(self, bot: DebugBot, combat, tick) -> None:
        frame = combat.encounter.coordinate_frame
        try:
            mine = self._world_position(bot.session, frame)
        except ValueError:
            return
        with bot.session.flow_lock:
            last = bot.session.gameplay_last_move
        heading = float(last.yaw) if last is not None else 0.0
        self._send_move(bot, frame.world_to_wire(mine), 0.0, heading, tick)

    def _send_move(self, bot: DebugBot, wire, scalar, yaw, tick) -> None:
        move = Move.from_values(
            tuple(float(value) for value in wire), float(scalar), float(yaw),
            0.0, int(tick), BOT_MOVE_DURATION_TICKS,
        )
        self._send_client_command(bot.session, encode_move_command(move))

    def _attack(self, bot, target_actor_id, aim, dx, dz, distance, tick) -> None:
        bot.cast_id = (bot.cast_id + 1) & 0xFFFFFFFF
        if bot.attack_kind == "target":
            payload = encode_target_skill_command(
                bot.skill_index, aim, tick, bot.cast_id, int(target_actor_id),
            )
        else:
            length = max(1e-6, distance)
            payload = encode_bullet_skill_command(
                bot.skill_index, aim, tick, bot.cast_id,
                (dx / length, 0.0, dz / length, 0.0),
            )
        self._send_client_command(bot.session, payload)

    def _respawn(self, bot: DebugBot) -> None:
        s = bot.session
        with s.flow_lock:
            currency = s.gameplay_currency_state
            if currency is not None and int(currency.currency_type) < BOT_RESPAWN_GOLD:
                s.gameplay_currency_state = replace(
                    currency, currency_type=BOT_RESPAWN_GOLD,
                )
        payload, bits = encode_respawn_command(same_spot=True)
        self._send_client_command(s, payload, bits)

    def _request_announced_actors(self, bot: DebugBot) -> None:
        """Ask for nearby monsters the way a client does after vicinity.

        Monsters only become valid targets for a session once its client
        has requested them. Structures in the fortress are monsters.
        """

        s = bot.session
        with s.flow_lock:
            pending = sorted(
                set(s.gameplay_announced_monster_actor_ids)
                - set(s.gameplay_spawned_monster_actor_ids)
                - bot.requested_actor_ids
            )[:8]
        for actor_id in pending:
            bot.requested_actor_ids.add(int(actor_id))
            self._send_client_command(s, encode_actor_request(actor_id))

    def _send_client_command(self, s, payload, bits=None) -> None:
        """Hand one client command to the server as if it had arrived."""

        if isinstance(payload, tuple):
            payload, bits = payload
        s.current_message_bit_length = (
            len(payload) * 8 if bits is None else int(bits)
        )
        s.current_message_received_at = time.perf_counter()
        if is_debug_bot(s):
            s.last_received_at = time.perf_counter()
        self.server._on_char_op(s, payload)


# ----------------------------------------------------------------------
# open arena
# ----------------------------------------------------------------------

class DebugPvPArena(_BotController):
    """Bots in an open PvP map: spawned on arrival, removed on departure."""

    def __init__(self, server, admin_character_id: int, map_id: str,
                 bot_count: int, *, notify=None, rng=None):
        super().__init__(server, admin_character_id, notify=notify, rng=rng)
        self.map_id = str(map_id)
        self.bot_count = int(bot_count)
        self.previous_pvp_enabled = None

    def _main(self) -> None:
        admin = self._wait_for_admin()
        if admin is None:
            if not self.stopped:
                self._say(
                    f"PvP debug: you did not arrive on {self.map_id} "
                    f"within {ARRIVAL_TIMEOUT_SECONDS:.0f} s; no bots "
                    "spawned."
                )
            return
        self.spawn(admin)
        while not self._stop.wait(BOT_TICK_SECONDS):
            self._process_transfers()
            if not self._admin_present():
                self.stop("you left the arena")
                break
            self.tick(time.perf_counter())

    def _wait_for_admin(self) -> Session | None:
        deadline = time.perf_counter() + ARRIVAL_TIMEOUT_SECONDS
        while not self.stopped and time.perf_counter() < deadline:
            admin = self._find_admin_session()
            if admin is not None:
                return admin
            self._stop.wait(ARRIVAL_POLL_SECONDS)
        return None

    def _find_admin_session(self) -> Session | None:
        return next((
            session for session in self._admin_sessions()
            if str(session.gameplay_map or "") == self.map_id
            and session.gameplay_combat_state is not None
        ), None)

    def _admin_present(self) -> bool:
        admin = self.admin
        if admin is None:
            return False
        is_current = getattr(self.server, "is_session_current", None)
        return (
            admin.connected
            and not admin.retired
            and not admin.gameplay_presence_departed
            and not admin.gameplay_exit_transition_started
            and str(admin.gameplay_map or "") == self.map_id
            and (is_current is None or bool(is_current(admin)))
        )

    def spawn(self, admin: Session) -> None:
        """Bring the bots in around ``admin`` and switch PvP on for both."""

        self.admin = admin
        combat = admin.gameplay_combat_state
        if combat is None:
            raise ValueError(f"{self.map_id} has no combat world")
        frame = combat.encounter.coordinate_frame
        center = self._world_position(admin, frame)
        self._clock_anchor = None
        with admin.flow_lock:
            self.previous_pvp_enabled = bool(admin.gameplay_pvp_enabled)
        if not self.previous_pvp_enabled:
            CharacterService._force_battleground_pvp(
                self.server, admin, enabled=True,
            )
        level = getattr(admin.gameplay_character, "level", 1)
        character_ids = self._free_character_ids(self.bot_count)
        for index, character_id in enumerate(character_ids):
            if self.stopped:
                return
            angle = math.tau * index / max(1, len(character_ids))
            position = self._ground_position(
                combat,
                (
                    center[0] + SPAWN_RADIUS * math.cos(angle),
                    center[1],
                    center[2] + SPAWN_RADIUS * math.sin(angle),
                ),
                center,
            )
            character = self._make_character(
                index, character_id, level, self.map_id,
            )
            self.bots.append(
                self._spawn_bot(admin, index, character, position)
            )
        names = ", ".join(bot.character.name for bot in self.bots)
        pvp_note = (
            "" if self.previous_pvp_enabled
            else " Your PvP flag is on until the bots leave."
        )
        self._say(
            f"PvP debug: {len(self.bots)} bot(s) joined on {self.map_id}: "
            f"{names}.{pvp_note} !debug_pvp stop removes them."
        )

    def _spawn_bot(self, admin, index, character, position) -> DebugBot:
        session = self._new_session(character)
        bot = DebugBot(session, character, cast_id=0x100 + (index << 20))
        try:
            self._initialize(
                session, self.map_id, position,
                access_id=getattr(admin, "gameplay_map_access_id", None),
                scaling_difficulty=getattr(admin, "gameplay_scaling_difficulty", 0),
            )
            combat = admin.gameplay_combat_state
            if session.gameplay_combat_state is not combat:
                raise ValueError(
                    f"{character.name} landed in another instance of "
                    f"{self.map_id} (bot "
                    f"{CharacterService._gameplay_instance_key(session)}, you "
                    f"{CharacterService._gameplay_instance_key(admin)})"
                )
            self._arm_bot(bot, index)
            CharacterService._force_battleground_pvp(
                self.server, session, enabled=True,
            )
            # Viewers only place, and PvP only targets, a player with a Move.
            self._send_move(bot, combat.encounter.coordinate_frame.world_to_wire(
                session.gameplay_spawn_position or position
            ), 0.0, self._aim_toward(session, admin, combat), self._tick())
        except Exception:
            self._drop(bot, "debug pvp bot failed to load")
            raise
        print(
            f"{LOG_PREFIX} {character.name} (character "
            f"{character.character_id}, actor "
            f"{session.gameplay_remote_actor_id}) joined {self.map_id} for "
            f"admin {self.admin_character_id}"
        )
        return bot

    def tick(self, now: float) -> None:
        for bot in tuple(self.bots):
            if self.stopped:
                return
            try:
                self._tick_bot(bot, now)
            except Exception as exc:
                print(
                    f"{LOG_PREFIX} {bot.character.name} tick failed: "
                    f"{type(exc).__name__}: {exc}"
                )

    def _tick_bot(self, bot: DebugBot, now: float) -> None:
        s = bot.session
        s.last_received_at = time.perf_counter()
        if not s.connected or s.retired:
            return
        combat = s.gameplay_combat_state
        if combat is None:
            return
        if not self._alive(s, combat):
            if bot.died_at is None:
                bot.died_at = now
            elif now - bot.died_at >= BOT_RESPAWN_DELAY_SECONDS:
                self._respawn(bot)
                # A refused respawn retries after another delay.
                bot.died_at = now
            return
        bot.died_at = None
        frame = combat.encounter.coordinate_frame
        mine = self._world_position(s, frame)
        tick = self._tick()
        # Open-arena bots hunt the real players only.
        targets = self._hostile_players(bot, combat, mine, include_bots=False)
        if not targets:
            self._stand(bot, combat, tick)
            return
        _distance, peer, theirs = targets[0]
        self._engage(
            bot, combat, mine, theirs, peer.gameplay_remote_actor_id,
            now, tick,
        )

    def teardown(self) -> None:
        had_bots = bool(self.bots)
        super().teardown()
        if self.previous_pvp_enabled is False:
            self.previous_pvp_enabled = None
            admin = next(self._admin_sessions(), None)
            if admin is not None and admin.gameplay_pvp_enabled:
                try:
                    CharacterService._force_battleground_pvp(
                        self.server, admin, enabled=False,
                    )
                except Exception as exc:
                    print(
                        f"{LOG_PREFIX} could not restore the admin PvP flag: "
                        f"{type(exc).__name__}: {exc}"
                    )
        if had_bots and self.stop_reason == "you left the arena":
            self._say("PvP debug bots removed: you left the arena.")


# ----------------------------------------------------------------------
# battleground matches
# ----------------------------------------------------------------------

class DebugPvPMatch(_BotController):
    """The admin and bots through one real matchmaking round trip.

    Queue -> match formed -> invitations accepted -> arena loaded -> rounds
    -> result screen -> everyone leaves. The server runs all of it; this
    class only plays the bots' side.
    """

    def __init__(self, server, admin_character_id: int, match_mode: int,
                 bot_count: int, *, notify=None, rng=None):
        super().__init__(server, admin_character_id, notify=notify, rng=rng)
        self.match_mode = int(match_mode)
        self.bot_count = int(bot_count)
        self.match = None
        self.formed_at = None
        self.over_at = None
        self.queue_deadline = None
        self.admin_queued = False

    @property
    def registry(self):
        return CharacterService._matchmaking_registry(self.server)

    # lifecycle ---------------------------------------------------------

    def _main(self) -> None:
        admin = next(self._admin_sessions(), None)
        if admin is None:
            raise ValueError("your gameplay character is not online")
        self.admin = admin
        self._create_lobby_bots(admin)
        # The admin first, so a stranger already waiting in this queue is
        # matched with the admin rather than with a handful of bots.
        self._queue(admin)
        self.admin_queued = True
        self._wait_until_queued(admin)
        for bot in tuple(self.bots):
            if self.stopped:
                return
            self._queue(bot.session)
        self.queue_deadline = time.perf_counter() + QUEUE_TIMEOUT_SECONDS
        while not self._stop.wait(BOT_TICK_SECONDS):
            self._keep_alive()
            self._process_transfers()
            if not self.step(time.perf_counter()):
                break

    def _create_lobby_bots(self, admin) -> None:
        level = getattr(admin.gameplay_character, "level", 1)
        honor = int(getattr(admin, "gameplay_honor_points", 0) or 0)
        for index, character_id in enumerate(
                self._free_character_ids(self.bot_count)):
            character = self._make_character(
                index, character_id, level, admin.gameplay_map,
            )
            session = self._new_session(character)
            # The queue's search windows compare levels and PvP ranks.
            session.gameplay_honor_points = honor
            self.bots.append(
                DebugBot(session, character, cast_id=0x100 + (index << 20))
            )

    def _queue(self, session) -> None:
        payload = encode_join_match_queue(JoinMatchQueue(
            server_tick=0, match_mode=self.match_mode, queue_kind=0,
        ))
        self._send_client_command(session, payload)

    def _wait_until_queued(self, admin, timeout: float = 3.0) -> None:
        deadline = time.perf_counter() + timeout
        while time.perf_counter() < deadline and not self.stopped:
            if self._admin_waiting() or self.registry.match_for_character(
                    self.admin_character_id) is not None:
                return
            self._stop.wait(0.05)

    def _admin_waiting(self) -> bool:
        return any(
            entry.character_id == self.admin_character_id
            for entry in self.registry.queued_for(self.match_mode)
        )

    def step(self, now: float) -> bool:
        """One look at the match. ``False`` once there is nothing left to do."""

        match = self.match
        if match is None:
            match = self.registry.match_for_character(self.admin_character_id)
            if match is None or match.match_mode != self.match_mode:
                if not self._admin_waiting():
                    self.stop("you left the queue")
                    return False
                if now >= self.queue_deadline:
                    self.stop(
                        f"no match formed within {QUEUE_TIMEOUT_SECONDS:.0f} s"
                    )
                    return False
                return True
            self._on_match_formed(match, now)
        with match.lock:
            phase = match.phase
            admin_entry = match.participant(self.admin_character_id)
        if phase == MatchPhase.CANCELLED:
            self.stop("the match was cancelled")
            return False
        if admin_entry is None or admin_entry.left:
            if phase != MatchPhase.OVER:
                self.stop("you left the match")
                return False
        elif admin_entry.session is not None:
            self.admin = admin_entry.session
        if phase == MatchPhase.INVITING:
            if now - self.formed_at >= BOT_ACCEPT_DELAY_SECONDS:
                self._accept_invitations(match)
            return True
        if phase == MatchPhase.OVER:
            if self.over_at is None:
                self.over_at = now
            elif now - self.over_at >= BOT_LEAVE_DELAY_SECONDS:
                self._leave(match)
            return bool(self.bots)
        for bot in tuple(self.bots):
            if self.stopped:
                return False
            try:
                self.tick_bot(bot, match, phase, now)
            except Exception as exc:
                print(
                    f"{LOG_PREFIX} {bot.character.name} tick failed: "
                    f"{type(exc).__name__}: {exc}"
                )
        return True

    def _on_match_formed(self, match, now) -> None:
        self.match = match
        self.formed_at = now
        ids = {entry.character_id for entry in match.participants}
        for bot in tuple(self.bots):
            if int(bot.character.character_id) not in ids:
                # Someone else from the queue took this seat.
                self.registry.dequeue(bot.character.character_id)
                self._drop(bot, "debug pvp bot not needed")
        admin_entry = match.participant(self.admin_character_id)
        team = admin_entry.team if admin_entry is not None else 0
        allies = sum(
            1 for entry in match.participants
            if entry.team == team and entry.character_id != self.admin_character_id
        )
        enemies = sum(1 for entry in match.participants if entry.team != team)
        side = ""
        objective = getattr(match, "objective", None)
        if isinstance(objective, FortressState):
            side = (
                " (attackers)" if team == objective.layout.attacker_team
                else " (defenders)"
            )
        self._say(
            f"PvP debug: {MODE_LABELS.get(match.match_mode, 'match')} formed "
            f"on {match.map_id}. You are team {team}{side} with {allies} "
            f"bot(s) against {enemies}. Accept the invitation."
        )

    def _accept_invitations(self, match) -> None:
        for bot in tuple(self.bots):
            if bot.answered_match == match.match_id:
                continue
            if getattr(bot.session, "gameplay_match", None) is not match:
                continue
            bot.answered_match = match.match_id
            self._send_client_command(
                bot.session,
                encode_join_battleground_response(JoinBattlegroundResponse(
                    server_tick=0, battleground_id=match.match_id,
                    accepted=True, field_2c=0,
                    # Clients answer with the solo/team queue id.
                    match_mode=client_queue_for_mode(match.match_mode),
                )),
            )

    def _leave(self, match) -> None:
        for bot in tuple(self.bots):
            if bot.left_match:
                continue
            bot.left_match = True
            if bot.session.gameplay_match is match:
                # The result screen's Leave button; the server sends the bot
                # to the hub, which removes it (see _transfer).
                self._send_client_command(
                    bot.session, encode_leave_pvp(LeavePVP(True)),
                )
            else:
                self._drop(bot, "debug pvp match over")

    def _transfer(self, session, destination, spawn_position, reason) -> None:
        bot = self._bot_for(session)
        match = self.match
        entering = (
            bot is not None and match is not None
            and destination == match.map_id
            and match.participant(bot.character.character_id) is not None
            and match.phase not in (MatchPhase.OVER, MatchPhase.CANCELLED)
        )
        if not entering:
            super()._transfer(session, destination, spawn_position, reason)
            return
        # The bot's "client" reconnects on the arena: a fresh session for
        # the same character, initialized there like any arriving player.
        index = self.bots.index(bot)
        arrival = self._new_session(bot.character)
        try:
            self._initialize(arrival, destination, spawn_position)
            if getattr(arrival, "gameplay_match", None) is not match:
                raise ValueError(
                    f"{bot.character.name} was not bound to match "
                    f"{match.match_id}"
                )
            bot.session = arrival
            self._arm_bot(bot, index)
            self._stand(bot, arrival.gameplay_combat_state, self._tick())
        except Exception as exc:
            traceback.print_exc()
            _retire_session(self.server, arrival, "debug pvp bot failed to load")
            self._drop(bot, "debug pvp bot failed to load")
            self._say(
                f"PvP debug: {bot.character.name} could not enter "
                f"{destination}: {type(exc).__name__}: {exc}"
            )
            return
        _retire_session(self.server, session, "debug pvp bot entered the arena")
        print(
            f"{LOG_PREFIX} {bot.character.name} (character "
            f"{bot.character.character_id}, actor "
            f"{arrival.gameplay_remote_actor_id}, team "
            f"{arrival.gameplay_match_team}) entered {destination} for match "
            f"{match.match_id}"
        )

    def teardown(self) -> None:
        match = self.match
        registry = self.registry
        bot_ids = [int(bot.character.character_id) for bot in self.bots]
        for bot_id in bot_ids:
            registry.dequeue(bot_id)
        if match is not None:
            with match.lock:
                phase = match.phase
            if phase in (MatchPhase.INVITING, MatchPhase.LOADING):
                # Nobody is fighting yet: call the whole match off.
                CharacterService._cancel_match(
                    self.server, match, reason="debug pvp bots removed",
                )
        super().teardown()
        # Cancelling puts accepted players back in the queue, and a stop
        # before any match leaves the admin queued alone: take them out.
        for bot_id in bot_ids:
            registry.dequeue(bot_id)
        if self.admin_queued and self._admin_waiting():
            admin = next(self._admin_sessions(), None)
            if admin is not None:
                try:
                    CharacterService._leave_match_queue(
                        self.server, admin, reason="debug pvp stopped",
                    )
                except Exception as exc:
                    print(f"{LOG_PREFIX} could not unqueue the admin: {exc}")
            else:
                registry.dequeue(self.admin_character_id)
        if self.stop_reason not in (None, "stopped by admin",
                                    "replaced by a new !debug_pvp"):
            self._say(f"PvP debug bots stopped: {self.stop_reason}.")

    # behaviour ---------------------------------------------------------

    def tick_bot(self, bot: DebugBot, match, phase, now: float) -> None:
        s = bot.session
        s.last_received_at = time.perf_counter()
        if (
            str(s.gameplay_map or "") != match.map_id
            or not s.gameplay_presence_ready
            or s.gameplay_combat_state is None
            or s.gameplay_exit_transition_started
        ):
            return
        combat = s.gameplay_combat_state
        tick = self._tick()
        if phase != MatchPhase.RUNNING:
            # Countdown and intermission lock movement; keep reporting in.
            self._stand(bot, combat, tick)
            return
        if not self._alive(s, combat):
            if (match.match_mode in OBJECTIVE_MATCH_MODES
                    and now >= bot.next_respawn_at):
                # The PvP death dialog's Respawn button; the server waits out
                # the authored delay. Elimination rounds revive everyone at
                # the next countdown instead.
                bot.next_respawn_at = now + OBJECTIVE_RESPAWN_RETRY_SECONDS
                self._respawn(bot)
            return
        frame = combat.encounter.coordinate_frame
        mine = self._world_position(s, frame)
        enemies = self._hostile_players(bot, combat, mine, include_bots=True)
        if match.match_mode == MATCH_MODE_CAPTURE_THE_FLAG_5V5:
            plan = self.ctf_plan(bot, match, mine, enemies)
        elif match.match_mode == MATCH_MODE_ASSAULT_THE_FORTRESS_5V5:
            self._request_announced_actors(bot)
            plan = self.fortress_plan(bot, match, combat, mine, enemies, now)
        else:
            plan = self._fight_nearest(enemies)
        self._execute(bot, combat, mine, plan, now, tick)

    @staticmethod
    def _fight_nearest(enemies):
        if not enemies:
            return None
        _distance, peer, theirs = enemies[0]
        return ("fight", int(peer.gameplay_remote_actor_id), theirs)

    def _execute(self, bot, combat, mine, plan, now, tick) -> None:
        if plan is None:
            self._stand(bot, combat, tick)
        elif plan[0] == "fight":
            _kind, actor_id, theirs = plan
            self._engage(bot, combat, mine, theirs, actor_id, now, tick)
        elif plan[0] == "walk":
            _kind, goal, stop_within = plan
            self._walk_to(bot, combat, mine, goal, tick, stop_within=stop_within)
        elif plan[0] == "interact":
            _kind, prop_id, operation = plan
            self._stand(bot, combat, tick)
            self._send_client_command(
                bot.session,
                encode_prop_interaction(PropInteraction(prop_id, operation)),
            )

    def ctf_plan(self, bot: DebugBot, match, mine, enemies):
        """Carry home, else fight what is close, else go for the flags."""

        state = getattr(match, "objective", None)
        team = getattr(bot.session, "gameplay_match_team", None)
        if not isinstance(state, CaptureTheFlagState) or team is None:
            return self._fight_nearest(enemies)
        team = int(team)
        with match.lock:
            carried = state.flag_carried_by(bot.character.character_id)
            own = state.flags.get(team)
            enemy = state.flags.get(1 - team)
            home = state.layout.home(team)
            own_status, own_position, own_carrier = (
                (own.status, own.position, own.carrier) if own else (None, None, None)
            )
            enemy_status, enemy_position = (
                (enemy.status, enemy.position) if enemy else (None, None)
            )
        if carried is not None:
            # Never stop to fight with the flag: run it home.
            return ("walk", tuple(home), 0.3)
        # Half of each side runs for the flags whatever is around them;
        # otherwise both teams meet in the middle and brawl all match (PvP
        # damage is low, so nobody dies and no flag ever moves).
        runner = int(bot.character.character_id) % 2 == 0
        if runner:
            if enemy_status is not None and enemy_status != FLAG_CARRIED:
                return ("walk", tuple(enemy_position), 0.3)
            if own_status == FLAG_DROPPED:
                return ("walk", tuple(own_position), 0.3)
        if own_status == FLAG_CARRIED:
            # Hunt whoever carries our flag: while it is away, nobody on
            # this side can score.
            for _distance, peer, theirs in enemies:
                character = peer.gameplay_character
                if character is not None and int(character.character_id) == own_carrier:
                    return ("fight", int(peer.gameplay_remote_actor_id), theirs)
        close = [value for value in enemies if value[0] <= CTF_ENGAGE_DISTANCE]
        if close:
            return self._fight_nearest(close)
        if own_status == FLAG_DROPPED:
            return ("walk", tuple(own_position), 0.3)
        if enemy_status is not None and enemy_status != FLAG_CARRIED:
            return ("walk", tuple(enemy_position), 0.3)
        if enemies:
            return self._fight_nearest(enemies)
        return ("walk", tuple(home), 2.0)

    def fortress_plan(self, bot: DebugBot, match, combat, mine, enemies, now):
        """Attackers take control points and shoot structures; defenders guard."""

        state = getattr(match, "objective", None)
        team = getattr(bot.session, "gameplay_match_team", None)
        if not isinstance(state, FortressState) or team is None:
            return self._fight_nearest(enemies)
        team = int(team)
        layout = state.layout
        attacker = team == int(layout.attacker_team)
        close = [value for value in enemies if value[0] <= FORTRESS_ENGAGE_DISTANCE]
        if close:
            return self._fight_nearest(close)
        structure = self._nearest_hostile_structure(bot, combat, mine)
        if attacker:
            point, holding = self._open_control_point(
                match, state, team, mine, int(bot.character.character_id),
            )
            if holding:
                # The capture only completes if the capturer is still inside
                # the point's radius when its 10 s run out.
                return None
            if structure is not None and structure[0] <= FORTRESS_STRUCTURE_REACH:
                return ("fight", structure[1], structure[2])
            if point is not None:
                distance = horizontal_distance(point.position, mine)
                if distance <= max(0.5, float(point.picking_range)):
                    if now >= bot.next_interaction_at:
                        prop = self._control_point_prop(bot, point)
                        if prop is not None:
                            bot.next_interaction_at = now + CONTROL_POINT_RETRY_SECONDS
                            return ("interact", *prop)
                    return None
                return (
                    "walk", tuple(point.position),
                    max(0.3, float(point.picking_range) * 0.5),
                )
            if structure is not None:
                return ("fight", structure[1], structure[2])
            return self._fight_nearest(enemies)
        # Defenders: break the siege units, then meet attackers, then guard.
        if structure is not None and structure[0] <= FORTRESS_STRUCTURE_REACH:
            return ("fight", structure[1], structure[2])
        if enemies and enemies[0][0] <= 2.5 * FORTRESS_ENGAGE_DISTANCE:
            return self._fight_nearest(enemies)
        guard = self._nearest_own_structure(match, state, team, mine)
        if guard is not None:
            return ("walk", tuple(guard), FORTRESS_GUARD_DISTANCE)
        return self._fight_nearest(enemies)

    @staticmethod
    def _nearest_hostile_structure(bot, combat, mine):
        """(distance, actor id, position) of the closest damageable foe monster."""

        actor = int(bot.session.gameplay_remote_actor_id or 0)
        best = None
        with combat.lock:
            for actor_id in tuple(combat.living_actor_ids):
                monster = combat.monsters.get(actor_id)
                if (
                    monster is None
                    or getattr(monster, "invulnerable", False)
                    or combat.monster_team(actor_id) is None
                    or not combat.monster_hostile_to_player(actor_id, actor)
                    or combat.monster_health.get(actor_id, 0) <= 0
                ):
                    continue
                position = combat.monster_positions.get(actor_id, monster.position)
                distance = horizontal_distance(position, mine)
                if best is None or distance < best[0]:
                    best = (distance, int(actor_id), tuple(position))
        return best

    @staticmethod
    def _open_control_point(match, state, team, mine, character_id):
        """(nearest capturable point, whether this bot is capturing one)."""

        with match.lock:
            captures = {
                row_id: dict(value)
                for row_id, value in (getattr(match, "fortress_captures", {}) or {}).items()
            }
            present = [
                point for point in state.layout.control_points
                if int(point.owner_team) == team
                and state.control_point_present(point)
            ]
        if any(
            captures.get(point.row_id, {}).get("capturer") == character_id
            for point in present
        ):
            return None, True
        points = [point for point in present if point.row_id not in captures]
        if not points:
            return None, False
        return min(
            points, key=lambda point: horizontal_distance(point.position, mine),
        ), False

    @staticmethod
    def _nearest_own_structure(match, state, team, mine):
        with match.lock:
            positions = [
                objective.position for objective in state.live_objectives()
                if int(objective.owner_team) == team
            ]
        if not positions:
            return None
        return min(positions, key=lambda position: horizontal_distance(position, mine))

    @staticmethod
    def _control_point_prop(bot, point):
        """(prop id, operation) of the map prop standing on a control point."""

        runtime = getattr(bot.session, "gameplay_map_event_runtime", None)
        if runtime is None:
            return None
        with runtime.lock:
            for prop_id, prop in runtime.props.items():
                definition = prop.definition
                if (
                    str(definition.template_id).casefold()
                    == str(point.template_id).casefold()
                    and horizontal_distance(definition.position, point.position) <= 1.0
                ):
                    return int(prop_id), str(definition.operation or "")
        return None
