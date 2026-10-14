"""Active-session discovery and persistent remote title selection."""

from __future__ import annotations

from dso_server.runtime.context import *
from dso_server.runtime.transport import Session


class PresenceIdentityService:
    """Small identity helpers shared by presence and social services."""

    def _map_session_candidates(self, map_id: str | None):
        """Sessions plausibly on ``map_id`` -- a superset, cheap to obtain.

        Presence events used to scan *every* session on the server even though
        only same-map sessions can matter. At 1000 sessions a scan returned 24
        relevant peers, so ~97% of the work was wasted, and it is paid per move
        packet, making the broadcast path O(N^2).

        Correctness rests on the invalidation token, not on remembering to
        update the index at every call site -- a first attempt wired
        ``gameplay_map`` assignments by hand and broke immediately, because not
        every service that assigns it inherits this mixin. ``Session`` now bumps
        a class-level counter on assignment instead, so the token below catches
        every map change from anywhere, tests included. Session count covers
        connects and disconnects.

        The index is still only a hint: the caller re-checks ``gameplay_map``
        in its predicate, so over-inclusion is harmless.
        """

        sessions = self.sessions
        if map_id is None:
            return tuple(sessions.values())
        token = (len(sessions), Session.map_generation)
        if getattr(self, "_map_index_token", None) != token:
            # Buckets are lists, not sets: callers depend on sessions arriving
            # in `self.sessions` insertion order (presence announcements are
            # asserted in that order), and a set would scramble it.
            index: dict = {}
            for session in tuple(sessions.values()):
                index.setdefault(session.gameplay_map, []).append(session)
            self._sessions_by_map = index
            self._map_index_token = token
        return tuple(self._sessions_by_map.get(map_id, ()))

    @staticmethod
    def _gameplay_instance_key(session: Session) -> tuple:
        physical_map_id = str(session.gameplay_map or '')
        access_group_id = str(
            getattr(
                session,
                'gameplay_map_access_group_id',
                None,
            )
            or getattr(session, 'gameplay_map_access_id', None)
            or physical_map_id
        ).casefold()
        return (
            physical_map_id,
            access_group_id,
            max(int(getattr(session, 'gameplay_map_difficulty', 0)),
                int(getattr(session, 'gameplay_scaling_difficulty', 0))),
            getattr(session, 'gameplay_instance_id', ''),
        )

    def _select_dungeon_instance(self, s, map_access):
        """Private dungeon runs; only a group member can supply an existing run."""
        from uuid import uuid4
        from dso_server.gameplay.boss_encounter_policy import load_map_encounter_policy
        policy = load_map_encounter_policy(self.static_database_path, s.gameplay_map)
        if policy.area_type.casefold() not in {'closedpve', 'boss'}:
            s.gameplay_instance_id = ''
            return
        store = getattr(self, 'social_store', None)
        group = store.group_for(s.gameplay_character.character_id) if store else None
        s.gameplay_instance_id = ''
        target = self._gameplay_instance_key(s)[:-1]
        if group:
            for peer in self._active_gameplay_sessions(map_id=s.gameplay_map, excluding=s):
                other = store.group_for(peer.gameplay_character.character_id)
                if (other and other.group_id == group.group_id
                        and self._gameplay_instance_key(peer)[:-1] == target):
                    s.gameplay_instance_id = getattr(peer, 'gameplay_instance_id', '')
                    return
        s.gameplay_instance_id = uuid4().hex

    def _active_gameplay_sessions(
            self, *, map_id: str | None = None,
            excluding: Session | None = None,
            instance_of: Session | None = None) -> tuple[Session, ...]:
        instance_key = (
            CharacterService._gameplay_instance_key(instance_of)
            if instance_of is not None
            else None
        )
        return tuple(
            session
            for session in self._map_session_candidates(map_id)
            if (
                session is not excluding
                and session.connected
                and session.gameplay_mode
                and session.gameplay_initialization_sent
                and session.gameplay_presence_ready
                and not session.gameplay_presence_departed
                and session.gameplay_character is not None
                and (map_id is None or session.gameplay_map == map_id)
                and (
                    instance_key is None
                    or CharacterService._gameplay_instance_key(session)
                    == instance_key
                )
            )
        )

    @staticmethod
    def _handle_update_show_player_overhead_icons(s: Session, payload: bytes) -> None:
        try:
            request = decode_update_show_player_overhead_icons(
                payload, s.current_message_bit_length
            )
        except ValueError as exc:
            print(f"[{GAMEPLAY_SERVICE_NAME}] rejected UpdateShowPlayerOverheadIcons: {exc}")
            return
        # A settings request can only change its authenticated local player.
        if request.actor_id != LOCAL_PLAYER_ACTOR_ID or s.gameplay_character is None:
            print(f"[{GAMEPLAY_SERVICE_NAME}] rejected UpdateShowPlayerOverheadIcons: invalid local actor")
            return
        with s.flow_lock:
            s.gameplay_client_command_state[UPDATE_SHOW_PLAYER_OVERHEAD_ICONS_COMMAND] = request
        CharacterService._accept_gameplay_state_command(
            s, UPDATE_SHOW_PLAYER_OVERHEAD_ICONS_COMMAND
        )
        CharacterService._send_player_overhead_icons_to(s, s, LOCAL_PLAYER_ACTOR_ID)
        if s.gameplay_presence_callback is not None:
            s.gameplay_presence_callback(s, ("overhead_icons", request.visible))

    @staticmethod
    def _send_player_overhead_icons_to(
        viewer: Session, source: Session, actor_id: int,
    ) -> None:
        with source.flow_lock:
            request = source.gameplay_client_command_state.get(
                UPDATE_SHOW_PLAYER_OVERHEAD_ICONS_COMMAND
            )
            # Preserve the PvP default until the client supplies its setting.
            if request is None and not source.gameplay_pvp_enabled:
                return
            visible = request.visible if request is not None else True
        viewer.send_reliable_ordered(
            encode_update_show_player_overhead_icons(visible, actor_id=actor_id),
            track=True,
            label=f"player-overhead-{actor_id}",
        )

    @staticmethod
    def _handle_set_title(s: Session, payload: bytes) -> None:
        request = decode_set_title_request(
            payload, s.current_message_bit_length
        )
        achievement_title = (
            request.achievement_title if request.title_type == 1 else ""
        )
        with s.flow_lock:
            s.gameplay_client_command_state[SET_TITLE_COMMAND] = request
            s.gameplay_title_type = int(request.title_type)
            s.gameplay_achievement_title = achievement_title
        CharacterService._accept_gameplay_state_command(s, SET_TITLE_COMMAND)
        if s.gameplay_presence_callback is not None:
            # RemotePlayerInfo safely refreshes already registered peers.
            s.gameplay_presence_callback(s, "appearance")
        callback = s.gameplay_persist_callback
        if callback is not None:
            callback(s, "title display changed", True)
        print(
            f"[{GAMEPLAY_SERVICE_NAME}] set title display mode "
            f"{request.title_type} for the active character"
        )
