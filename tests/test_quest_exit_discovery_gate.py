"""A discovered destination does not open a quest exit mid-run.

Live report 2026-10-27: during I Hear Voices (pw000-introduction-01-n) the
Chamber of Panoltiaco exit to The Splintered City of Cardhun opened before the
final boss died. The character had already discovered pw000_hub (admin
all-maps travel), and ``_quest_exit_reveal_flags`` treated that discovery as
proof the gate was passed although the exit's authored entry restore names an
unfinished task of the open quest.
"""

from dso_server.gameplay.local_combat_state import LocalCombatState
from dso_server.gameplay.local_monster_state import load_local_monster_encounter
from dso_server.gameplay.quest_content import (
    QUEST_STATE_CLOSED, QUEST_STATE_FINISHED, QuestProgress,
)
from dso_server.paths import DEFAULT_STATIC_DB_PATH
from dso_server.tests import test_quests

MAP = "g0017_templecity_md_04"
QUEST = "pw000-introduction-01-n"
EXIT = "g0017_activate_exit"
TASK_TOKENS = (
    (0, "pw000-introduction-01-explore-01_fin"),
    (1, "pw000-introduction-01-clear-01_fin"),
    (2, "pw000-introduction-01-kill-01_fin"),
    (3, "pw000-introduction-01-kill-02_fin"),
)


class QuestExitDiscoveryGateTests(test_quests.QuestServiceTests):
    # Only this module's cases; the inherited quest suite runs on its own.
    locals().update({
        name: None for name in dir(test_quests.QuestServiceTests) if name.startswith("test_")
    })

    def _enter(self, *, discovered_hub, done_tasks=1, state=None, port=51801):
        s = self._session(MAP, port, class_id=2)
        s.gameplay_character.level = 60
        progress = QuestProgress(QUEST)
        for index, token in TASK_TOKENS:
            if index < done_tasks:
                progress.tokens[token] = 1
                progress.finished_mask |= 1 << index
        progress.tokens["pw000-introduction-01-explore-01_g0017expl01Explored"] = 1
        if state is not None:
            progress.state = state
        s.gameplay_quest_log[QUEST] = progress
        if discovered_hub:
            s.gameplay_unlocked_maps.add("pw000_hub")
        encounter = load_local_monster_encounter(DEFAULT_STATIC_DB_PATH, MAP)
        s.gameplay_monster_encounter = encounter
        s.gameplay_combat_state = LocalCombatState(
            encounter, player_actor_id=s.gameplay_remote_actor_id, player_health=1000)
        self.server.sessions[s.addr] = s
        runtime = self.server._attach_map_event_runtime(
            s, map_id=MAP, initial_flags=frozenset(self.server._quest_reveal_flags(s, MAP)),
            monster_level=None, scaling_difficulty=0, fresh_instance=True)
        self.server._quest_prepare_map(s)
        self.server._on_map_entered(s)
        return s, runtime

    def _exit_open(self, s, runtime):
        with runtime.lock:
            in_runtime = EXIT in runtime.active_flags()
        # The client's EventUpdate carries the per-character reveal flags.
        revealed = EXIT in self.server._quest_reveal_flags(s, MAP)
        return in_runtime, revealed

    def _kill(self, s, template):
        combat = s.gameplay_combat_state
        with combat.lock:
            victims = [monster for actor_id, monster in combat.monsters.items()
                       if monster.template_id.casefold() == template
                       and combat.monster_health.get(actor_id, 0) > 0]
            for monster in victims:
                combat.monster_health[monster.actor_id] = 0
        self.assertTrue(victims, template)
        for monster in victims:
            self.server._on_monsters_killed(s, (monster,))

    def _run_waves_and_check(self, s, runtime):
        self.assertEqual(self._exit_open(s, runtime), (False, False))
        for template in ("g0017_normal_anderworld_creature_01",
                         "g0017_guardian_anderworld_creature_01",
                         "g0017_sentinel_anderworld_creature_01"):
            self._kill(s, template)
            self.assertEqual(self._exit_open(s, runtime), (False, False), template)
        self._kill(s, "g0017_boss_anderworld_creature_01")
        self.assertEqual(self._exit_open(s, runtime), (True, True))

    def test_discovered_destination_waits_for_the_final_boss(self):
        self._run_waves_and_check(*self._enter(discovered_hub=True))

    def test_first_visit_waits_for_the_final_boss(self):
        self._run_waves_and_check(*self._enter(discovered_hub=False, port=51802))

    def test_finished_gate_task_reopens_on_entry(self):
        s, runtime = self._enter(discovered_hub=True, done_tasks=4,
                                 state=QUEST_STATE_FINISHED, port=51803)
        self.assertEqual(self._exit_open(s, runtime), (True, True))

    def test_closed_quest_reopens_on_entry(self):
        s, runtime = self._enter(discovered_hub=True, done_tasks=4,
                                 state=QUEST_STATE_CLOSED, port=51804)
        self.assertEqual(self._exit_open(s, runtime), (True, True))

    def test_parallel_world_exit_ignores_previous_boss_discovery(self):
        # Discovering Heredur on an earlier run must not bypass Kingscrypt's
        # current map-instance completion condition.
        quest_id, map_id = "pw007-pw008-pw009-n-re", "pw008_kingscrypt_01_dun"
        s = self._session(map_id, 51805, class_id=2)
        s.gameplay_character.level = 60
        s.gameplay_quest_log[quest_id] = QuestProgress(quest_id)
        s.gameplay_unlocked_maps.add("pw009_05_heredur_01_boss")
        self.assertNotIn("pw008_spawn_exit", self.server._quest_exit_reveal_flags(s, map_id))
