from __future__ import annotations

from types import SimpleNamespace
import time
import unittest
from unittest.mock import patch

from dso_server.paths import (
    DEFAULT_STATIC_DB_PATH,
)
from dso_server.services.character_server import CharacterServer
from dso_server.services.player_owned_resolution_dispatch import (
    PlayerOwnedResolutionDispatchService,
)
from dso_server.services.player_progression import (
    load_player_progression_catalog,
)
from dso_server.runtime.transport import Session


class _FakeSocket:
    def __init__(self) -> None:
        self.sent = []

    def sendto(self, data: bytes, addr: tuple[str, int]) -> None:
        self.sent.append((data, addr))


def _status(effect_id: int, *, stack_index: int = 0, values=()):
    return SimpleNamespace(
        effect_id=effect_id,
        field_18=stack_index,
        values=tuple(values),
    )


class PlayerProgressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.catalog = load_player_progression_catalog(
            DEFAULT_STATIC_DB_PATH
        )

    def test_monster_reward_uses_level_rank_and_difficulty_tables(self):
        # 2.3125 coefficient * 40 L31 monster factor * normal rank = 92.5.
        self.assertEqual(
            self.catalog.monster_kill_xp(
                'b1201_s_gen_norsegnome',
                monster_level=31,
                rank_id='Rank_monster',
                map_difficulty=0,
            ),
            93,
        )
        # Infernal III has authored XPGain 4.0.
        self.assertEqual(
            self.catalog.monster_kill_xp(
                'b1201_s_gen_norsegnome',
                monster_level=31,
                rank_id='Rank_monster',
                map_difficulty=3,
            ),
            370,
        )
        # Herold additionally carries Rank_boss's 6x XP factor.
        self.assertEqual(
            self.catalog.monster_kill_xp(
                'a0604_normal_g_Herold',
                monster_level=30,
                rank_id='Rank_boss',
            ),
            3021,
        )

    def test_progression_crosses_levels_and_stops_at_authored_cap(self):
        levels = self.catalog.player_level_xp['mage']
        transition = self.catalog.advance_player(
            class_id=1,
            current_level=30,
            current_xp=levels[30],
            amount=levels[31] - levels[30] + 10,
        )
        self.assertIsNotNone(transition)
        self.assertEqual(transition.level, 31)
        self.assertEqual(transition.xp, levels[31] + 10)
        self.assertTrue(transition.level_up)

        capped = self.catalog.advance_player(
            class_id=1,
            current_level=109,
            current_xp=levels[109],
            amount=0xFFFFFFFF,
        )
        self.assertEqual(capped.level, 110)
        self.assertEqual(capped.xp, levels[110])
        self.assertEqual(capped.next_level_base_xp, levels[110] + 1)
        self.assertIsNone(self.catalog.advance_player(
            class_id=1,
            current_level=110,
            current_xp=levels[110],
            amount=1000,
        ))

    def test_runtime_award_applies_stacked_active_xp_gain(self):
        sock = _FakeSocket()
        session = Session(('127.0.0.1', 44000), sock)
        levels = self.catalog.player_level_xp['mage']
        session.connected = True
        session.gameplay_chase_generation = 7
        session.gameplay_character = SimpleNamespace(
            class_id=1,
            level=30,
            progression_u32=levels[30],
        )
        session.gameplay_talent_category_points = (
            levels[30], 0, 0, 0
        )
        # Reactivation +100%, Qaizah ten stacks +25%: total multiplier 2.25.
        session.gameplay_runtime_status_effects = {
            1: (_status(5990), time.perf_counter() + 60.0),
            2: (_status(2335, stack_index=9), time.perf_counter() + 60.0),
        }
        monster = SimpleNamespace(
            template_id='b1201_s_gen_norsegnome',
            level=31,
            rank_id='Rank_monster',
        )

        awarded = CharacterServer._award_monster_kill_xp(
            session,
            (monster,),
            database_path=DEFAULT_STATIC_DB_PATH,
        )

        self.assertEqual(awarded, 209)
        self.assertEqual(
            session.gameplay_character.progression_u32,
            levels[30] + 209,
        )
        self.assertEqual(
            session.gameplay_talent_category_points[0], levels[30] + 209
        )
        self.assertEqual(len(sock.sent), 1)

    def test_owned_lethal_results_delegate_to_shared_xp_award(self):
        order = []
        monster = SimpleNamespace(
            template_id='test_monster', level=30, rank_id='Rank_monster'
        )
        combat = SimpleNamespace(monsters={10: monster})
        owner = SimpleNamespace(
            gameplay_character=SimpleNamespace(class_id=1, level=30),
            gameplay_monster_encounter=SimpleNamespace(
                static_database_path=DEFAULT_STATIC_DB_PATH
            ),
            gameplay_world_loot_allocator=None,
            gameplay_presence_callback=lambda _session, event: order.append(
                event[0] if isinstance(event, tuple) else event
            ),
        )
        resolution = SimpleNamespace(
            source_actor_id=100,
            attacker_actor_id=100,
            target_actor_id=10,
            killed=True,
            position=(0.0, 0.0, 0.0),
        )
        with (
            patch.object(
                CharacterServer, '_apply_player_damage_healing',
                return_value=0,
            ),
            patch.object(
                CharacterServer, '_award_monster_kill_xp',
                side_effect=lambda *_args, **_kwargs: order.append('xp') or 1,
            ) as award,
        ):
            PlayerOwnedResolutionDispatchService._dispatch_player_owned_resolutions(
                combat, {100: owner}, (resolution,)
            )
        award.assert_called_once_with(
            owner,
            (monster,),
            database_path=DEFAULT_STATIC_DB_PATH,
        )
        # The map-event kill notification follows the XP award so signal
        # effects never run ahead of the kill's own resolution.
        self.assertEqual(
            order, ['player_combat_resolution', 'xp', 'monsters_killed']
        )


if __name__ == '__main__':
    unittest.main()
