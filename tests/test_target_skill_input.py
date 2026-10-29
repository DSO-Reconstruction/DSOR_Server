"""Target loss is a normal client input, not a malformed combat packet."""

import struct
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from dso_server.protocol.character_selection import BitWriter
from dso_server.protocol.combat_protocol import TARGET_SKILL_COMMAND, decode_target_skill
from dso_server.runtime.transport import Session
from dso_server.services.character_server import CharacterServer
from dso_server.services import player_combat
from dso_server.tests import test_combat_entities as combat_fixtures


def target_packet(actor_id, derived_flag=None, skill_index=1831):
    writer = BitWriter()
    writer.write_bytes(b"\x8b" + struct.pack("<HHHfII", TARGET_SKILL_COMMAND, 0, skill_index, 0.0, 200, 123))
    if derived_flag is not None:
        writer.write_bool(derived_flag)
    writer.write_uint32_le(actor_id)
    return writer.finish()


class TargetSkillInputTests(unittest.TestCase):
    def test_decoder_preserves_zero_and_event_actor_targets_in_both_layouts(self):
        for actor_id in (0, 196629):
            for flag in (None, False, True):
                with self.subTest(actor_id=actor_id, flag=flag):
                    payload, bits = target_packet(actor_id, flag)
                    request = decode_target_skill(payload, bits)
                    self.assertEqual(request.target_actor_id, actor_id)
                    self.assertEqual(request.derived_flag, flag)
                    self.assertEqual(request.skill_index, 1831)

    def test_zero_target_still_requires_a_well_formed_packet(self):
        payload, bits = target_packet(0)
        with self.assertRaises(ValueError):
            decode_target_skill(payload + b"\x00", bits + 8)
        with self.assertRaises((ValueError, EOFError)):
            decode_target_skill(payload[:-1], bits - 8)

    def test_zero_target_does_not_spend_resources_or_start_a_cast(self):
        session = Session(("127.0.0.1", 41982), object())
        payload, bits = target_packet(0)
        session.current_message_bit_length = bits
        with (
            patch.object(CharacterServer, "_player_victim_feature_locked", return_value=False),
            patch.object(CharacterServer, "_cancel_mount_cast") as cancel_mount,
            patch.object(CharacterServer, "_remove_mount_ride") as dismount,
            patch.object(CharacterServer, "_apply_player_skill_resource") as resource,
            patch.object(CharacterServer, "_consume_active_essence") as essence,
            patch.object(player_combat, "calculate_player_combat_stats") as stats,
        ):
            CharacterServer._handle_spatial_skill(session, payload)
        for operation in (cancel_mount, dismount, resource, essence, stats):
            operation.assert_not_called()
        self.assertFalse(session.gameplay_projectile_timers)
        self.assertEqual(session.gameplay_client_command_counts[TARGET_SKILL_COMMAND], 1)
        self.assertEqual(session.gameplay_client_command_state[TARGET_SKILL_COMMAND].target_actor_id, 0)

    def test_valid_target_after_zero_still_schedules_and_deals_damage(self):
        fixture = combat_fixtures.MixedEntityCastTests()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        session, combat, monster = fixture.source, fixture.combat, fixture.monster
        session.gameplay_skill_book_ids = {1831}
        session.gameplay_character.class_id = 0
        session.gameplay_presence_callback = None
        combat.monster_positions[monster] = (25.0, 0.0, 51.0)
        session.gameplay_delivered_monster_positions[monster] = combat.monster_positions[monster]
        stats = SimpleNamespace(
            minimum_damage=10.0, maximum_damage=10.0, critical_hit_chance=0.0,
            critical_damage_multiplier=2.0, attack_speed=1.0, skill_modifier_sources=(),
        )
        with (
            patch.object(player_combat, "calculate_player_combat_stats", return_value=stats),
            patch.object(CharacterServer, "_apply_player_skill_resource", return_value=True) as resource,
            patch.object(CharacterServer, "_consume_active_essence"),
            patch.object(CharacterServer, "_start_monster_chase"),
            patch.object(CharacterServer, "_record_monster_targeting_result"),
        ):
            payload, session.current_message_bit_length = target_packet(0)
            CharacterServer._handle_spatial_skill(session, payload)
            resource.assert_not_called()
            self.assertFalse(combat.pending_runtime_player_hits)
            payload, session.current_message_bit_length = target_packet(monster)
            CharacterServer._handle_spatial_skill(session, payload)
            resource.assert_called_once()
        hits = tuple(combat.pending_runtime_player_hits.values())
        self.assertTrue(hits)
        self.assertEqual({hit.target_actor_id for hit in hits}, {monster})
        health_before = combat.monster_health[monster]
        for hit in hits:
            resolution = combat.resolve_runtime_player_hit(
                hit.source_actor_id, hit.cast_id, hit.impact_index, now=hit.due_at,
            )
            self.assertGreater(resolution.damage, 0)
        self.assertLess(combat.monster_health[monster], health_before)

    def test_deadly_blow_zero_target_packet_hits_forward_monsters(self):
        fixture = combat_fixtures.MixedEntityCastTests()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        session, combat, monster = fixture.source, fixture.combat, fixture.monster
        skill_index = 1894  # Ranger's mightystab / Deadly Blow
        session.gameplay_skill_book_ids = {skill_index}
        session.gameplay_presence_callback = None
        caster = (25.0, 0.0, 50.0)
        # Aimed straight ahead (-Z in world space)
        target = (25.0, 0.0, 48.0)
        fixture.move(session, caster)
        combat.monster_positions[monster] = target
        session.gameplay_delivered_monster_positions[monster] = target
        combat.monster_health[monster] = 10000
        stats = SimpleNamespace(
            minimum_damage=10.0, maximum_damage=10.0, critical_hit_chance=0.0,
            critical_damage_multiplier=2.0, attack_speed=1.0, skill_modifier_sources=(),
        )
        with (
            patch.object(player_combat, "calculate_player_combat_stats", return_value=stats),
            patch.object(CharacterServer, "_apply_player_skill_resource", return_value=True),
            patch.object(CharacterServer, "_consume_active_essence"),
            patch.object(CharacterServer, "_start_monster_chase"),
            patch.object(CharacterServer, "_record_monster_targeting_result"),
        ):
            payload, session.current_message_bit_length = target_packet(
                0, skill_index=skill_index,
            )
            CharacterServer._handle_spatial_skill(session, payload)

        hits = tuple(combat.pending_runtime_player_hits.values())
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].target_actor_id, monster)
        health_before = combat.monster_health[monster]
        resolution = combat.resolve_runtime_player_hit(
            hits[0].source_actor_id, hits[0].cast_id, hits[0].impact_index,
            now=hits[0].due_at,
        )
        self.assertIsNotNone(resolution)
        self.assertGreater(resolution.damage, 0)
        self.assertLess(combat.monster_health[monster], health_before)

    def test_deadly_blow_hits_multiple_monsters_in_front(self):
        fixture = combat_fixtures.MixedEntityCastTests()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        session, combat, monster1 = fixture.source, fixture.combat, fixture.monster
        monster2 = monster1 + 1
        combat.monsters[monster2] = combat.monsters[monster1]
        session.gameplay_spawned_monster_actor_ids.add(monster2)

        skill_index = 1894
        session.gameplay_skill_book_ids = {skill_index}
        session.gameplay_presence_callback = None
        caster = (25.0, 0.0, 50.0)
        pos1 = (25.0, 0.0, 48.0)
        pos2 = (26.0, 0.0, 48.0)
        fixture.move(session, caster)
        combat.monster_positions[monster1] = pos1
        session.gameplay_delivered_monster_positions[monster1] = pos1
        combat.monster_health[monster1] = 10000

        combat.monster_positions[monster2] = pos2
        session.gameplay_delivered_monster_positions[monster2] = pos2
        combat.monster_health[monster2] = 10000

        stats = SimpleNamespace(
            minimum_damage=10.0, maximum_damage=10.0, critical_hit_chance=0.0,
            critical_damage_multiplier=2.0, attack_speed=1.0, skill_modifier_sources=(),
        )
        with (
            patch.object(player_combat, "calculate_player_combat_stats", return_value=stats),
            patch.object(CharacterServer, "_apply_player_skill_resource", return_value=True),
            patch.object(CharacterServer, "_consume_active_essence"),
            patch.object(CharacterServer, "_start_monster_chase"),
            patch.object(CharacterServer, "_record_monster_targeting_result"),
        ):
            payload, session.current_message_bit_length = target_packet(
                0, skill_index=skill_index,
            )
            CharacterServer._handle_spatial_skill(session, payload)

        hits = tuple(combat.pending_runtime_player_hits.values())
        self.assertEqual(len(hits), 2)
        hit_targets = {h.target_actor_id for h in hits}
        self.assertEqual(hit_targets, {monster1, monster2})
