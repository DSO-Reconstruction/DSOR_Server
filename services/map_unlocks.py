"""Persistent map grants shared by quests, travel, chat and the CMS."""

from dso_server.gameplay.map_unlocks import (
    map_unlocking_quests, quest_map_unlocks, unlocked_travel_map_ids,
)
from dso_server.gameplay.quest_content import QUEST_STATE_CLOSED
from dso_server.services.base import CharacterService


class MapUnlockService:
    def _unlock_all_maps(self, s):
        with s.flow_lock:
            if (not s.connected or not s.gameplay_mode or s.gameplay_character is None
                    or not s.gameplay_initialization_sent or not s.gameplay_presence_ready
                    or s.gameplay_presence_departed or s.gameplay_exit_transition_started
                    or getattr(s, "retired", False)):
                raise ValueError("the character is not ready; try again after entering the map")
            s.gameplay_admin_all_maps = True
        CharacterService._persist_gameplay_session(
            self, s, reason="admin unlocked all maps", flush=True)

    def _quest_unlock_maps(self, s, quest_id):
        grants = quest_map_unlocks(self.static_database_path, (quest_id,))
        with s.flow_lock:
            s.gameplay_unlocked_maps.update(grants)
        # The quest turn-in saves these grants with the closed quest and sends
        # its native QuestUpdate, which refreshes the client's quest access UI.

    def _map_quest_accessible(self, s, map_id):
        with s.flow_lock:
            if s.gameplay_admin_all_maps:
                return True
            closed = {q.casefold() for q, p in getattr(s, "gameplay_quest_log", {}).items()
                      if p.state == QUEST_STATE_CLOSED}
        requirements = map_unlocking_quests(self.static_database_path, map_id)
        return not requirements or any(q.casefold() in closed for q in requirements)

    def _travel_map_unlocked(self, s, map_id):
        from dso_server.gameplay.map_difficulty_state import resolve_map_access
        access = resolve_map_access(self.static_database_path, map_id,
                                    player_level=max(1, s.gameplay_character.level))
        with s.flow_lock:
            known, admin_all_maps = tuple(s.gameplay_unlocked_maps), s.gameplay_admin_all_maps
        available = unlocked_travel_map_ids(
            self.static_database_path, known, admin_all_maps=admin_all_maps)
        return (access.access_group_id in available or access.physical_map_id in available)
