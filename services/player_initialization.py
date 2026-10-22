"""Local-player initialization, assembled from its phase services.

``_send_local_player_initialization`` is the single entry point.  Each phase
lives in a sibling module so the sequence stays readable:

* :mod:`.player_init_state` derives and commits the session snapshot.
* :mod:`.player_init_world` loads the map and builds/sends the 0x85 batch.
* :mod:`.player_persistence` captures and stores the account state.
"""

from __future__ import annotations

from dso_server.runtime.context import *
from dso_server.runtime.transport import Session
from dso_server.services.player_init_state import PlayerInitializationStateService
from dso_server.services.player_init_world import PlayerInitializationWorldService
from dso_server.services.player_persistence import PlayerPersistenceService


class PlayerInitializationService(
        PlayerInitializationStateService,
        PlayerInitializationWorldService,
        PlayerPersistenceService):
    """Drive the phases that bring one session up to PlayerReady."""

    def _begin_local_player_initialization(
            self, s: Session, *, scheduled: bool) -> bool:
        """Install the session callbacks and claim the one-shot handoff."""

        if s.gameplay_persist_callback is None:
            s.gameplay_persist_callback = (
                lambda session, reason, flush=False:
                CharacterService._persist_gameplay_session(
                    self, session, reason=reason, flush=flush
                )
            )
        if s.gameplay_presence_callback is None:
            s.gameplay_presence_callback = (
                lambda session, event:
                CharacterService._handle_presence_event(
                    self, session, event
                )
            )
        if s.gameplay_match_callback is None:
            # Combat resolution runs without a server handle, so battleground
            # scoring is reported through a closure the same way presence and
            # persistence are.
            s.gameplay_match_callback = (
                lambda session, event:
                CharacterService._handle_match_event(self, session, event)
            )
        if s.gameplay_world_loot_allocator is None:
            s.gameplay_world_loot_allocator = (
                self._generate_world_loot_for_session
            )
        timer_to_cancel = None
        active = not scheduled or self.is_session_current(s)
        with s.flow_lock:
            if not active:
                s.gameplay_initialization_timer = None
                if NPC_TRACE:
                    print(
                        f"[NPC-TRACE] init skipped: session not current "
                        f"(scheduled={scheduled}, map={s.gameplay_map!r})"
                    )
                return False
            if s.gameplay_initialization_sent:
                if NPC_TRACE:
                    print(
                        f"[NPC-TRACE] init skipped: already sent "
                        f"(map={s.gameplay_map!r}, access_group="
                        f"{getattr(s, 'gameplay_map_access_group_id', None)!r})"
                    )
                return False
            if not scheduled:
                timer_to_cancel = s.gameplay_initialization_timer
            s.gameplay_initialization_sent = True
            # A Session can be reused by a map-transfer path after its
            # initialization claim is reset. Keep it hidden until this map's
            # complete NewPlayer/follow-up stream has been queued as well.
            s.gameplay_presence_ready = False
            s.gameplay_initialization_timer = None

        # A delayed fallback is installed after the client's 0x8d.  Some runs
        # send gameplay cmd 0x006f early enough to use it as an acceleration
        # signal; the retail trace also shows that 0x006f can follow NewPlayer,
        # so the bounded fallback remains necessary.  Cancel it whenever the
        # explicit path wins to avoid duplicate initialization.
        if timer_to_cancel is not None:
            timer_to_cancel.cancel()
        if NPC_TRACE:
            print(
                f"[NPC-TRACE] init claimed for map={s.gameplay_map!r} "
                f"scheduled={scheduled}"
            )
        return True

    def _send_local_player_initialization(
            self, s: Session, *, scheduled: bool = False):
        if not CharacterService._begin_local_player_initialization(
            self, s, scheduled=scheduled
        ):
            return

        state = CharacterService._resolve_initialization_character(self, s)
        # Establish the concrete world key before rekeying inventory items.
        # World drops and player inventory IDs share the same per-instance
        # allocator, so a joining character must reserve IDs against drops
        # that already exist in this map instance.
        s.gameplay_map = state.gameplay_map
        s.gameplay_map_access_id = state.map_access.request_map_id
        s.gameplay_map_access_group_id = state.map_access.access_group_id
        s.gameplay_map_difficulty = state.map_access.map_difficulty
        s.gameplay_scaling_difficulty = state.map_access.scaling_difficulty
        CharacterService._select_dungeon_instance(self, s, state.map_access)
        CharacterService._resolve_initialization_inventory(self, state, s)
        # Persistent effects use NewPlayer's actor-variable payload while
        # deriving their concrete values. Re-encode after vitality resolution
        # so the outgoing snapshot still observes hub restoration.
        CharacterService._encode_initialization_new_player(self, state)
        CharacterService._resolve_initialization_talents(self, state)
        CharacterService._resolve_initialization_persistent_effects(
            self, s, state
        )
        CharacterService._resolve_initialization_vitals(self, state)
        CharacterService._encode_initialization_new_player(self, state)
        CharacterService._apply_initialization_state(self, s, state)
        CharacterService._initialize_respawn_state(
            self, s, state.persisted_state, map_id=state.gameplay_map
        )

        # Retail puts SetBattlegroundId at the head of the arena's
        # initialization, before NewPlayer -- the client's battleground
        # manager has to exist before any of the PvP UI it drives is built,
        # or the countdown and the result panel arrive with nobody listening.
        CharacterService._announce_battleground(self, s)

        world = CharacterService._load_initialization_world(self, s, state)
        commands = CharacterService._build_initialization_commands(
            self, s, state, world
        )
        initialization = CharacterService._send_initialization_commands(
            self, s, commands
        )
        CharacterService._send_initialization_followups(self, s, state)
        CharacterService._restore_consumable_timers(self, s)
        CharacterService._log_initialization_summary(
            s, state, world, commands, initialization
        )

        CharacterService._persist_gameplay_session(
            self, s, reason='map initialization', flush=True
        )
        # Publish the session only after NewPlayer, PlayerInfo and every
        # initialization follow-up have been queued.  The claim flag above is
        # deliberately set much earlier and cannot safely be used as a
        # presence-readiness signal.
        with s.flow_lock:
            s.gameplay_presence_ready = True
        # A trigger may have started a map-local event during the world-load
        # phase, before this session could receive its scripted actor. Replay
        # the active gated spawns now that its complete PlayerReady stream is
        # queued; the event helper deduplicates actors already materialized.
        runtime = getattr(s, "gameplay_map_event_runtime", None)
        if runtime is not None and s.gameplay_combat_state is not None:
            with runtime.lock:
                running_event_sets = frozenset(runtime.enabled_event_sets)
            CharacterService._spawn_event_monsters(
                self, runtime, (s,), running_event_sets
            )
        CharacterService._quest_start_active_holdgrounds(self, s)
        CharacterService._refresh_premium_bags(s)
        CharacterService._announce_remote_presence(self, s)
        CharacterService._on_map_entered(self, s)
        # A battleground map load is the second half of a match handoff: the
        # arriving socket is a different object from the one that accepted the
        # invitation, so the match roster is re-bound here rather than being
        # carried across the transfer.
        CharacterService._bind_battleground_session(self, s)
        maintenance = getattr(self, "maintenance", None)
        if maintenance is not None:
            maintenance.notify_session(s)
