"""A boss-death exit does not open because its destination is known.

Live report 2026-10-28: the Winter Summit's Kingshill and Bear's Den exits
stood open before Big Paws was dead. Both sit behind BOSS_death=True, and
``_quest_exit_reveal_flags`` took the discovered destination (every character
knows Kingshill) as proof the route was passed, so map entry started
BOSS_death and the client created the exits.
"""

from dso_server.tests import test_quests

WINTER_SUMMIT = "e_b1205_dun_chr_2012_02"


class BossExitDiscoveryTests(test_quests.QuestServiceTests):
    # Only this module's cases; the inherited quest suite runs on its own.
    locals().update({
        name: None for name in dir(test_quests.QuestServiceTests) if name.startswith("test_")
    })

    def _flags(self, map_id, discovered, port):
        s = self._session(map_id, port, class_id=2)
        s.gameplay_character.level = 60
        s.gameplay_unlocked_maps.update(discovered)
        return self.server._quest_exit_reveal_flags(s, map_id)

    def test_known_kingshill_does_not_restore_big_paws_death(self):
        flags = self._flags(
            WINTER_SUMMIT,
            {"a0200_kingscity", "e_b1205_dun_chr_2025_01_diff_00"},
            51811,
        )
        self.assertNotIn("boss_death", {flag.casefold() for flag in flags})

    def test_known_hub_does_not_open_mortis_return_gate(self):
        flags = self._flags("pw102_mortis_01_boss", {"pw000_hub"}, 51812)
        self.assertNotIn("pw102_spawn_exit_to_pw000", {flag.casefold() for flag in flags})

    def test_known_heredur_does_not_open_kingscrypt_portal(self):
        # PR #54: pw008_spawn_exit opens on this run's key-and-lock task, so
        # an earlier run's discovered boss map is no proof for this instance.
        flags = self._flags(
            "pw008_kingscrypt_01_dun", {"pw009_05_heredur_01_boss"}, 51813,
        )
        self.assertNotIn("pw008_spawn_exit", flags)
