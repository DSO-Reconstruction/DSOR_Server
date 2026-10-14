"""Client team identity, persistent corpses, and complete arena spell teardown."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock
import unittest

from dso_server.paths import (
    DEFAULT_STATIC_DB_PATH,
)
from dso_server.protocol.combat_protocol import KillServerCommand, decode_combat_batch
from dso_server.protocol.gameplay_protocol import (
    decode_discard_location_effect_batch,
    decode_discard_monster_batch,
)
from dso_server.gameplay.local_combat_state import DamageResolution, LocalCombatState
from dso_server.gameplay.local_monster_state import load_local_player_only_encounter
from dso_server.protocol.new_player_codec import (
    decode_new_player_prefix,
    encode_new_player_wire,
)
from dso_server.protocol.pvp_match_protocol import decode_match_over
from dso_server.gameplay.pvp_match_state import MatchPhase
from dso_server.services.base import CharacterService
from dso_server.tests import test_pvp_matchmaking as fixtures


class BattlegroundPolishTests(unittest.TestCase):
    setUp = fixtures.BattlegroundTests.setUp
    _arrive = fixtures.BattlegroundTests._arrive

    def _initial_player(self, session, map_id="b1102_dm"):
        state = SimpleNamespace(
            character=session.gameplay_character, persisted_state=None,
            template_row_index=0, spawn_position=(0.0, 0.0, 0.0),
            travel_map_unlocks=(0, 0, 0, 0), talent_category_points=(0, 0, 0, 0),
            inventory=SimpleNamespace(equipment_slots={}), player_xp_state={},
            gameplay_map=map_id,
        )
        CharacterService._encode_initialization_new_player(self.server, state)
        packet = encode_new_player_wire(state.new_player)
        decoded, consumed = decode_new_player_prefix(*packet)
        self.assertEqual(consumed, packet[1])
        return decoded

    def test_personal_result_uses_newplayer_team_for_both_possible_winners(self):
        # Client 0x14020fe74 copies NewPlayer +0x388 to GamePlayer +0x18.
        # Personal result 0x1402e28b5 compares that field against C1's winner.
        players = [self._initial_player(s) for s in (self.home, self.away)]
        self.assertEqual([p.field_388 for p in players], [0, 1])
        self._arrive(self.home)
        self._arrive(self.away)
        for winner in (0, 1):
            self.match.phase = MatchPhase.OVER
            self.match.winning_team = winner
            for s in (self.home, self.away):
                s.recorder.messages.clear()
            CharacterService._finish_match(self.server, self.match, self.match.info_generation)
            results = [decode_match_over(*s.recorder.find(0xC1))
                       for s in (self.home, self.away)]
            self.assertEqual([p.field_388 == result.winning_team
                              for p, result in zip(players, results)],
                             [winner == 0, winner == 1])

    def test_town_initialization_does_not_inherit_pending_arena_team(self):
        self.assertEqual(self._initial_player(self.home, "a0200_kingscity").field_388,
                         0xFFFFFFFF)

    def _combat(self):
        combat = LocalCombatState(
            load_local_player_only_encounter(DEFAULT_STATIC_DB_PATH, "b1102_dm"),
            player_actor_id=self.home.gameplay_remote_actor_id, player_health=1000,
        )
        combat.register_player(self.away.gameplay_remote_actor_id, 1000)
        for s in (self.home, self.away):
            s.gameplay_combat_state = combat
            s.gameplay_runtime_status_effects.clear()
        return combat

    def test_player_corpses_use_retail_lifetime_for_local_and_remote_viewers(self):
        self._arrive(self.home)
        self._arrive(self.away)
        self._combat()
        resolution = DamageResolution(
            target_actor_id=self.away.gameplay_remote_actor_id,
            attacker_actor_id=self.home.gameplay_remote_actor_id,
            current_health=0, maximum_health=1000, damage=1000, killed=True,
            position=(25.49, 0.0, 53.57), server_tick=1531,
            hit_tags=(0,), skill_index=0,
        )
        for session, victim, killer in (
                (self.home, self.away.gameplay_remote_actor_id, 1),
                (self.away, 1, self.home.gameplay_remote_actor_id)):
            session.recorder.messages.clear()
            CharacterService._send_combat_resolution(session, replace(
                resolution, target_actor_id=victim, attacker_actor_id=killer,
            ), label="test-player-death")
            batch = decode_combat_batch(*session.recorder.find(0x6B))
            kill = next(c for c in batch.commands if isinstance(c, KillServerCommand))
            self.assertEqual(kill.actor_id, victim)
            self.assertEqual(kill.value.animation_time, 2_160_000)
            self.assertFalse(kill.value.field_78)
            self.assertNotIn(0x1C, session.recorder.ids())
        self.assertTrue(self.away.gameplay_death_pending)
        # Monster death keeps the established shorter lifetime.
        self.home.recorder.messages.clear()
        CharacterService._send_combat_resolution(
            self.home, replace(resolution, target_actor_id=900001), label="test-monster-death")
        batch = decode_combat_batch(*self.home.recorder.find(0x6B))
        self.assertEqual(next(c.value.animation_time for c in batch.commands
                              if isinstance(c, KillServerCommand)), 300)

    def test_new_round_discards_summons_traps_ground_effects_and_pending_hits(self):
        self._arrive(self.home)
        self._arrive(self.away)
        combat = self._combat()
        home, away = self.home.gameplay_remote_actor_id, self.away.gameplay_remote_actor_id
        for actor, owner, client_ids in ((7001, home, (7001, 7002)),
                                         (7003, away, (7003,)), (7004, 999, (7004,))):
            combat.player_summons[actor] = SimpleNamespace(
                actor_id=actor, owner_actor_id=owner, client_actor_ids=client_ids,
                definition=SimpleNamespace(map_companion=False))
            combat.pending_player_summon_attacks[actor] = object()
        for instance, owner in ((8001, home), (8002, away), (8003, 999)):
            combat.player_location_visuals[instance] = SimpleNamespace(
                instance_id=instance, owner_actor_id=owner, expires_at=1e20)
        combat.player_location_aura_members = {(8001, home), (8002, away), (8003, home)}
        for trap, owner in ((9001, home), (9002, away), (9003, 999)):
            combat.player_traps[trap] = SimpleNamespace(trap_id=trap, owner_actor_id=owner)
        for pending in (combat.pending_player_hits, combat.pending_runtime_player_hits):
            pending.update({1: SimpleNamespace(source_actor_id=home, target_actor_id=7003),
                            2: SimpleNamespace(source_actor_id=away, target_actor_id=home),
                            3: SimpleNamespace(source_actor_id=999, target_actor_id=7004)})
        events = []
        def relay(source, event):
            events.append(event)
            for viewer in (self.home, self.away):
                if event[0] == "player_summon_despawn":
                    CharacterService._send_player_summon_despawn_to(viewer, event[1])
                elif event[0] == "player_location_visuals":
                    CharacterService._send_player_location_visuals_to(viewer, event[1])
                elif event[0] == "player_trap_discard":
                    CharacterService._send_player_trap_discard_to(viewer, event[1])
        timers = []
        for s in (self.home, self.away):
            s.gameplay_summon_actor_ids = {7001, 7002, 7003, 7004, 7999}
            s.gameplay_location_effect_ids = {8001, 8002, 8003}
            s.gameplay_presence_callback = relay
            for key in (("player-summon", 1), ("player-location", 1),
                        ("player-trap", 9001), ("player-summon-discard", 7999), (home, 1, 0)):
                timer = Mock()
                timers.append(timer)
                s.gameplay_projectile_timers[key] = timer
            s.recorder.messages.clear()
        CharacterService._begin_match_round(self.server, self.match)
        self.assertEqual(set(combat.player_summons), {7004})
        self.assertEqual(set(combat.pending_player_summon_attacks), {7004})
        self.assertEqual(set(combat.player_location_visuals), {8003})
        self.assertEqual(combat.player_location_aura_members, {(8003, home)})
        self.assertEqual(set(combat.player_traps), {9003})
        self.assertEqual(set(combat.pending_player_hits), {3})
        self.assertEqual(set(combat.pending_runtime_player_hits), {3})
        self.assertEqual({e[1] for e in events if e[0] == "player_trap_discard"}, {9001, 9002})
        for timer in timers:
            timer.cancel.assert_called_once()
        for s in (self.home, self.away):
            self.assertEqual(s.gameplay_summon_actor_ids, {7004})
            self.assertEqual(s.gameplay_location_effect_ids, {8003})
            self.assertFalse(s.gameplay_projectile_timers)
            packet = next((p, bits) for p, bits, label in s.recorder.messages
                          if label == "player-summon-despawn")
            self.assertEqual(set(decode_discard_monster_batch(*packet)), {7001, 7002, 7003, 7999})
            packet = next((p, bits) for p, bits, label in s.recorder.messages
                          if label == "player-location-effect-discard")
            self.assertEqual(set(decode_discard_location_effect_batch(*packet)[0]), {8001, 8002})

    def test_queued_old_round_summons_and_ground_effects_cannot_reappear(self):
        self._arrive(self.home)
        self._arrive(self.away)
        combat = self._combat()
        self.match.phase = MatchPhase.RUNNING
        token = CharacterService._pvp_round_token(self.home)
        self.match.round_index += 1
        # Already-dequeued callbacks must return before touching definitions.
        CharacterService._resolve_player_summon(
            self.home, self.home.gameplay_chase_generation, ("player-summon", 1),
            None, self.home.gameplay_remote_actor_id, (0, 0, 0), 0, 100, 100,
            (1, 1), (), round_token=token)
        CharacterService._resolve_player_location_visuals(
            self.home, self.home.gameplay_chase_generation, ("player-location", 1),
            combat, (None,), position=(0, 0, 0), yaw=0, server_tick=100,
            owner_actor_id=self.home.gameplay_remote_actor_id, cast_id=1,
            round_token=token)
        self.assertFalse(combat.player_summons)
        self.assertFalse(combat.player_location_visuals)
