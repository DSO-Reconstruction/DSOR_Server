"""Death dialog, retail request flags, respawn payments and map boundaries."""

from dataclasses import replace
from threading import RLock
from contextlib import contextmanager
from types import SimpleNamespace
import unittest
import json
from pathlib import Path
from unittest.mock import Mock, patch

from dso_server.paths import (
    DEFAULT_STATIC_DB_PATH,
)
from dso_server.persistence.static_game_database import StaticGameDatabase
from dso_server.protocol.gameplay_protocol import Move, decode_force_transform_batch
from dso_server.protocol.respawn_protocol import RespawnRequest
from dso_server.protocol.player_init_protocol import decode_server_command_batch
from dso_server.protocol.combat_protocol import decode_combat_batch, KillServerCommand
from dso_server.gameplay.local_combat_state import LocalCombatState
from dso_server.gameplay.local_monster_state import load_local_monster_encounter
from dso_server.gameplay.local_map_state import load_map_coordinate_frame, load_local_map_exits
from dso_server.protocol.player_init_protocol import CurrencyChanged
from dso_server.services.character_server import CharacterServer
from dso_server.protocol.respawn_protocol import (
    decode_respawn,
    encode_revive,
    encode_death_dialog,
)
from dso_server.services.player_respawn import respawn_map_allowed, _nearest_exit_transform
from dso_server.tests.test_pvp_matchmaking import _session


class RespawnProtocolTests(unittest.TestCase):
    def test_captured_nearest_exit_request(self):
        request = decode_respawn(bytes.fromhex("8b620080"), 26)
        self.assertTrue(request.nearest_exit)
        self.assertFalse(request.same_spot)

    def test_town_and_same_spot_are_independent_flags(self):
        self.assertEqual(decode_respawn(bytes.fromhex("8b620000")), RespawnRequest())
        self.assertTrue(decode_respawn(bytes.fromhex("8b620040")).same_spot)

    def test_malformed_or_ambiguous_requests_are_rejected(self):
        for payload, bits in [("8b6200c0", 26), ("8b620081", 26),
                              ("8b6200", 24), ("8b62008000", 34),
                              ("8b630080", 26), ("8b620080", 25)]:
            with self.subTest(payload=payload, bits=bits), self.assertRaises(ValueError):
                decode_respawn(bytes.fromhex(payload), bits)

    def test_retail_death_and_paid_revival_batches(self):
        from dso_server.protocol import player_init_protocol as init
        from dso_server.protocol import gameplay_protocol as gameplay
        from dso_server.protocol.character_selection import BitReader
        capture = json.loads((Path(__file__).parent / "fixtures/player_respawn_retail.json").read_text(encoding="utf-8"))
        incoming = [m for m in capture["messages"] if m["direction"] == "IN"]
        death = decode_combat_batch(
            bytes.fromhex(incoming[0]["payload"]), incoming[0]["bits"],
            legacy_hit_tag_boundary_bit=True,
        )
        kill = next(c for c in death.commands if isinstance(c, KillServerCommand))
        self.assertEqual(kill.value.animation_time, 2160000)
        self.assertEqual(death.commands[0].value.current_health, 0)
        for message, cost, animation_ticks, protection_ticks in zip(
            incoming[1:], (120, 40), (107, 50), (250, 375)
        ):
            reader = BitReader(bytes.fromhex(message["payload"]), message["bits"])
            self.assertEqual(reader.read_uint8(), 0x85)
            commands = []
            while reader.remaining:
                command = int.from_bytes(reader.read_bytes(2), "little")
                if command == 0x71:
                    value = gameplay.ForceTransform(
                        tuple(reader.read_float() for _ in range(3)), reader.read_float()
                    )
                elif command == 0x7B:
                    value = gameplay._read_actor_stats_body(reader)
                elif command == 0x6D:
                    value = reader.read_uint32_le()
                else:
                    value = init._BODY_READERS[command](reader)
                actor = reader.read_uint32_le()
                commands.append((command, value, actor))
                self.assertEqual(reader.read_uint8(), 0xFF)
            self.assertEqual([c[0] for c in commands],
                             [0x13A, 0x80, 0x71, 0x4F, 0x4F, 0x7B, 0x6D, 0x4F, 0x4F, 0x7B])
            self.assertEqual(commands[0][1].amount, cost)
            self.assertEqual(commands[6][1], 0)
            self.assertEqual(commands[7][1].entries[0].field_10, animation_ticks)
            self.assertEqual(commands[8][1].entries[0].field_10, protection_ticks)
            self.assertEqual(commands[5][1].health_points, 113663)
            self.assertEqual(commands[5][1].resource_points, 100)

    def test_captured_revive_and_dialog_bytes(self):
        self.assertEqual(encode_revive(actor_id=65930),
                         (bytes.fromhex("856d00000000008a010100ff"), 96))
        self.assertEqual(encode_death_dialog(),
                         (bytes.fromhex("85810000000000ff"), 64))


class RespawnServiceTests(unittest.TestCase):
    def setUp(self):
        self.server = CharacterServer.__new__(CharacterServer)
        self.server.static_database_path = DEFAULT_STATIC_DB_PATH
        self.server.static_database = StaticGameDatabase(DEFAULT_STATIC_DB_PATH)
        self.session = s = _session(50001, 1, "Respawner")
        s.gameplay_map = "b1201_pve"
        s.gameplay_presence_ready = True
        s.gameplay_initialization_sent = True
        s.gameplay_respawn_allowed = True
        s.gameplay_death_pending = True
        s.current_message_bit_length = 26
        s.gameplay_coordinate_frame = load_map_coordinate_frame(DEFAULT_STATIC_DB_PATH, s.gameplay_map)
        s.gameplay_map_exits = load_local_map_exits(DEFAULT_STATIC_DB_PATH, s.gameplay_map)
        s.gameplay_last_move = Move.from_values(
            s.gameplay_coordinate_frame.world_to_wire((20.0, 0.0, 30.0)), 0, 1.0, 1.0, 100, 0
        )
        s.gameplay_inventory = SimpleNamespace(health_points=0, secondary_resource=17)
        s.gameplay_currency_state = CurrencyChanged(1000, 25, (0, 0, 0), 0, True)
        s.gameplay_membership_active = False
        s.gameplay_combat_state = SimpleNamespace(
            lock=RLock(), player_actor_id=1, player_current_health=0,
            player_health={1: [0, 4200], 65537: [0, 4200]},
            player_resource={1: [17.0, 100.0], 65537: [17.0, 100.0]},
            encounter=SimpleNamespace(coordinate_frame=s.gameplay_coordinate_frame),
        )
        self.timers = []

        def timer_factory(interval, function, args=None, kwargs=None):
            timer = SimpleNamespace(
                interval=interval, start=Mock(), cancel=Mock(),
                fire=lambda: function(*(args or ()), **(kwargs or {})),
            )
            self.timers.append(timer)
            return timer

        s.gameplay_deadline_timer = timer_factory
        self.mocks = {}
        for name in ("_send_player_skill_status_effects", "_dispatch_socket_event",
                     "_update_low_health_socket_state", "_persist_gameplay_session",
                     "_accept_gameplay_state_command", "_begin_map_transfer",
                     "_refresh_player_combat_vitals"):
            patcher = patch.object(CharacterServer, name, return_value=True)
            self.mocks[name] = patcher.start()
            self.addCleanup(patcher.stop)

    def request(self, flag=0x80):
        self.server._on_char_op(self.session, b"\x8b\x62\x00" + bytes((flag,)))

    def test_nearest_exit_charges_once_restores_health_and_moves_authority(self):
        s = self.session
        old_position = s.gameplay_last_move.relative_position
        self.request()
        self.assertEqual(s.gameplay_currency_state.currency_type, 960)
        self.assertEqual(s.gameplay_combat_state.player_health[1][0], 4200)
        self.assertEqual(s.gameplay_inventory.health_points, 4200)
        self.assertFalse(s.gameplay_death_pending)
        self.assertNotEqual(s.gameplay_last_move.relative_position, old_position)
        self.assertEqual(s.recorder.ids(), [0x13A, 0x71, 0x7B, 0x6D])
        payment = decode_server_command_batch(*s.recorder.find(0x13A))
        self.assertEqual([c.command_id for c in payment], [0x13A, 0x80])
        self.assertEqual(payment[0].value.amount, 40)
        self.assertEqual(payment[1].value.currency_type, 960)
        transform, actor = decode_force_transform_batch(*s.recorder.find(0x71))
        self.assertEqual(actor, 1)
        self.assertEqual(transform.position, s.gameplay_spawn_position)
        self.request()
        self.assertEqual(s.gameplay_currency_state.currency_type, 960)
        self.assertEqual(s.recorder.ids().count(0x6D), 1)
        self.mocks["_persist_gameplay_session"].assert_called_once()

    def test_same_spot_costs_120_and_keeps_position(self):
        s = self.session
        before = s.gameplay_last_move.relative_position
        self.request(0x40)
        self.assertEqual(s.gameplay_last_move.relative_position, before)
        self.assertEqual(s.gameplay_currency_state.currency_type, 880)
        self.assertEqual(self.mocks["_dispatch_socket_event"].call_args.args[1], "revive120")

    def test_premium_and_deluxe_are_free_for_both_paid_options(self):
        for membership in (1, 2):
            for flag in (0x40, 0x80):
                with self.subTest(membership=membership, flag=flag):
                    s = self.session
                    s.gameplay_combat_state.player_health[1][0] = 0
                    s.gameplay_membership_type = membership
                    s.gameplay_membership_active = True
                    s.gameplay_membership_expires_at = "2099-01-01T00:00:00+00:00"
                    s.gameplay_currency_state = replace(s.gameplay_currency_state, currency_type=0)
                    self.request(flag)
                    self.assertEqual(s.gameplay_currency_state.currency_type, 0)
                    self.assertEqual(s.gameplay_combat_state.player_health[1][0], 4200)
                    definitions = self.mocks["_send_player_skill_status_effects"].call_args.args[1]
                    self.assertEqual([(d.resource_id, d.duration_ticks) for d in definitions],
                                     [("resurrect_self", 108), ("InvulnerablePremiumRespawn", 250)]
                                     if flag == 0x40 else [("map_respawn", 50), ("Invulnerable", 375)])

    def test_paid_revives_get_destination_specific_protection_and_animation(self):
        import time
        for flag, expected in (
            (0x40, [("resurrect_self", 108), ("InvulnerablePremiumRespawn", 250)]),
            (0x80, [("map_respawn", 50), ("Invulnerable", 375)]),
        ):
            with self.subTest(flag=flag):
                s = self.session
                s.gameplay_combat_state.player_health[1][0] = 0
                before = time.perf_counter()
                self.request(flag)
                effects = self.mocks["_send_player_skill_status_effects"].call_args.args[1]
                self.assertEqual([(e.resource_id, e.duration_ticks) for e in effects], expected)
                duration = expected[0][1] / 25
                self.assertGreaterEqual(s.gameplay_revive_movement_until, before + duration)
                self.assertLessEqual(s.gameplay_revive_movement_until, time.perf_counter() + duration)

    def test_stonekeep_fallback_matches_captured_portal_transform(self):
        position, heading = _nearest_exit_transform(
            DEFAULT_STATIC_DB_PATH, "pw007_stonekeep_01_dun", (), (-25.97, 1.0, -13.1)
        )
        self.assertEqual(position, (-78.64935302734375, 2.250765085220337, -19.364229202270508))
        self.assertAlmostEqual(heading, -1.5378528833389282, places=5)

    def test_three_monster_deaths_and_captured_respawn_choices(self):
        s = self.session
        s.gameplay_map = "pw007_stonekeep_01_dun"
        encounter = load_local_monster_encounter(
            DEFAULT_STATIC_DB_PATH, s.gameplay_map, include_spawn_areas=False
        )
        actor = s.gameplay_remote_actor_id
        combat = s.gameplay_combat_state = LocalCombatState(
            encounter, player_actor_id=actor, player_health=4200
        )
        s.gameplay_presence_callback = lambda *_args: None
        s.gameplay_coordinate_frame = frame = encounter.coordinate_frame
        s.gameplay_map_exits = ()
        s.gameplay_last_visited_town = "pw000_hub"
        combat.register_player(actor, 4200, regeneration_fraction=0.25)
        monster_id = next(a for a in encounter.actor_ids if combat.monsters[a].attack is not None)
        monster_position = combat.monster_positions[monster_id]
        s.gameplay_last_move = Move.from_values(frame.world_to_wire(monster_position), 0, 0, 0, 100, 0)
        # Use a real authored monster cast and due-impact resolver, not a
        # hand-built killed flag; that was the gap in the old respawn tests.
        attacks = combat.maybe_start_monster_attacks(
            s.gameplay_last_move.relative_position, server_tick=100,
            now=max(combat.last_monster_attack_at.values()) + 10,
            target_actor_id=actor, eligible_actor_ids={monster_id},
        )
        self.assertEqual(len(attacks), 1)
        attack = attacks[0]
        capture = json.loads((Path(__file__).parent / "fixtures/player_respawn_retail.json").read_text(encoding="utf-8"))
        requests = [m for m in capture["messages"] if m["direction"] == "OUT"]
        self.assertEqual(len(requests), 3)
        for index, request in enumerate(requests):
            s.gameplay_death_pending = False
            s.gameplay_inventory.health_points = 4200
            combat.player_health[actor][0] = 10
            combat.player_current_health = 10
            position = frame.wire_to_world(s.gameplay_last_move.relative_position)
            combat.monster_positions[monster_id] = position
            combat.pending_monster_attacks[monster_id] = replace(
                attack, target_position=position, projectile_origin=position,
                # A target spell resolves at the victim even if the chosen
                # monster also has straight-projectile variants.
                motion_type="Target",
            )
            results = combat.resolve_due_monster_attacks(
                s.gameplay_last_move.relative_position, now=attack.due_at,
                player_actor_id=actor, incoming_damage_multiplier=100000,
            )
            self.assertEqual(len(results), 1)
            self.assertTrue(results[0].killed)
            self.assertEqual(combat.player_health[actor][0], 0)
            s.recorder.messages.clear()
            self.server._send_monster_activity_to(s, combat, (), (), (), results)
            self.assertTrue(s.gameplay_death_pending)
            self.assertEqual(s.gameplay_inventory.health_points, 0)
            self.assertNotIn(0x81, s.recorder.ids())
            self.timers[-1].fire()
            self.assertEqual(s.recorder.ids()[-1], 0x81)
            batch = decode_combat_batch(*s.recorder.find(0x6B))
            kill = next(c for c in batch.commands if isinstance(c, KillServerCommand))
            self.assertEqual(kill.actor_id, 1)
            self.assertEqual(kill.value.animation_time, 2160000)
            peer = _session(50002, 2, "Observer")
            peer.gameplay_combat_state = combat
            self.server._send_monster_activity_to(peer, combat, (), (), (), results)
            remote_batch = decode_combat_batch(*peer.recorder.find(0x6B))
            remote_kill = next(c for c in remote_batch.commands if isinstance(c, KillServerCommand))
            self.assertEqual(remote_kill.actor_id, actor)
            self.assertEqual(remote_kill.value.animation_time, 2160000)
            self.assertNotIn(0x81, peer.recorder.ids())
            self.assertTrue(self.server._player_victim_feature_locked(s, "movement", "skills"))
            # Actual users wait on the death screen. Regeneration used to
            # heal the corpse here, silently rejecting every subsequent choice.
            now = combat.last_player_health_regeneration_at[actor] + 30
            self.assertEqual(combat.regenerate_players(now=now), ())
            self.assertEqual(combat.player_health[actor][0], 0)
            before = s.gameplay_last_move.relative_position
            s.current_message_bit_length = request["bits"]
            self.server._on_char_op(s, bytes.fromhex(request["payload"]))
            self.assertEqual(s.gameplay_currency_state.currency_type, (880, 840, 840)[index])
            if index < 2:
                self.assertEqual(combat.player_health[actor][0], 4200)
                self.assertFalse(s.gameplay_death_pending)
                self.assertEqual(s.recorder.ids().count(0x6D), 1)
                if index == 0:
                    self.assertEqual(s.gameplay_last_move.relative_position, before)
                else:
                    self.assertNotEqual(s.gameplay_last_move.relative_position, before)
            else:
                self.assertEqual(self.mocks["_begin_map_transfer"].call_args.args[1], "pw000_hub")
                self.assertEqual(combat.player_health[actor][0], 0)

    def test_expired_membership_is_charged(self):
        s = self.session
        s.gameplay_membership_type = 1
        s.gameplay_membership_active = True
        s.gameplay_membership_expires_at = "2000-01-01T00:00:00Z"
        self.request()
        self.assertEqual(s.gameplay_currency_state.currency_type, 960)

    def test_insufficient_funds_preserve_death_and_balance(self):
        s = self.session
        s.gameplay_currency_state = replace(s.gameplay_currency_state, currency_type=39)
        self.request()
        self.assertEqual(s.gameplay_combat_state.player_health[1][0], 0)
        self.assertTrue(s.gameplay_death_pending)
        self.assertEqual(s.gameplay_currency_state.currency_type, 39)
        self.assertEqual(s.recorder.ids(), [0x81])
        self.mocks["_persist_gameplay_session"].assert_not_called()

    def test_town_uses_last_visited_hub_and_is_free(self):
        s = self.session
        s.gameplay_last_visited_town = "a0200_kingscity"
        self.request(0)
        self.assertEqual(self.mocks["_begin_map_transfer"].call_args.args[1], "a0200_kingscity")
        self.assertEqual(s.gameplay_currency_state.currency_type, 1000)
        self.assertEqual(s.recorder.ids(), [])

    def test_failed_destination_lookup_does_not_charge_or_revive(self):
        with patch("dso_server.services.player_respawn._nearest_exit_transform", side_effect=ValueError("missing entry")):
            self.request()
        self.assertEqual(self.session.gameplay_currency_state.currency_type, 1000)
        self.assertEqual(self.session.gameplay_combat_state.player_health[1][0], 0)
        self.assertFalse(self.session.gameplay_respawn_in_progress)

    def test_arena_or_match_membership_rejects_all_options(self):
        for map_id, match in [("b1102_dm", None), ("a0206_tdm", None),
                              ("b1201_pve", object())]:
            for flag in (0, 0x40, 0x80):
                self.session.gameplay_map = map_id
                self.session.gameplay_match = match
                self.request(flag)
        self.assertEqual(self.session.recorder.ids(), [])
        self.mocks["_begin_map_transfer"].assert_not_called()

    def test_royal_arena_keeps_the_ordinary_respawn_flow(self):
        # a0201_basicarena is OpenPVE with ordinary duels, not a match map.
        from dso_server.services.player_respawn import respawn_map_allowed
        self.assertTrue(respawn_map_allowed(str(DEFAULT_STATIC_DB_PATH), "a0201_basicarena"))
        for match_map in ("b1102_dm", "a0206_tdm"):
            self.assertFalse(respawn_map_allowed(str(DEFAULT_STATIC_DB_PATH), match_map))

    def test_transfer_or_concurrent_request_is_rejected(self):
        for name in ("gameplay_exit_transition_started", "gameplay_respawn_in_progress",
                     "gameplay_presence_departed"):
            setattr(self.session, name, True)
            self.request()
            setattr(self.session, name, False)
        self.assertEqual(self.session.recorder.ids(), [])

    def test_payment_encoding_failure_cannot_charge_or_revive(self):
        s = self.session
        s.gameplay_currency_state = replace(s.gameplay_currency_state, values=())
        self.request()
        self.assertEqual(s.gameplay_currency_state.currency_type, 1000)
        self.assertEqual(s.gameplay_combat_state.player_health[1][0], 0)
        self.assertEqual(s.recorder.ids(), [])

    def test_aborted_revive_never_charges_and_restores_dead_pool(self):
        s = self.session
        combat = s.gameplay_combat_state
        original_lock = combat.lock
        acquisitions = 0

        @contextmanager
        def interrupt_after_restore():
            nonlocal acquisitions
            acquisitions += 1
            with original_lock:
                yield
            if acquisitions == 2:
                self.assertEqual(s.gameplay_currency_state.currency_type, 1000)
                # A map handoff wins after the map-owned HP write but before
                # the session publishes the new position and inventory.
                s.gameplay_exit_transition_started = True
                # Preserve an unrelated currency booking during the handoff.
                s.gameplay_currency_state = replace(
                    s.gameplay_currency_state,
                    currency_type=s.gameplay_currency_state.currency_type + 50,
                )

        class InterruptedLock:
            def __enter__(self):
                self.context = interrupt_after_restore()
                return self.context.__enter__()

            def __exit__(self, *args):
                return self.context.__exit__(*args)

        combat.lock = InterruptedLock()
        self.request()
        self.assertEqual(combat.player_health[1][0], 0)
        self.assertEqual(combat.player_current_health, 0)
        self.assertEqual(s.gameplay_inventory.health_points, 0)
        self.assertEqual(s.gameplay_currency_state.currency_type, 1050)
        self.assertTrue(s.gameplay_death_pending)
        self.assertFalse(s.gameplay_respawn_in_progress)
        self.assertEqual(s.recorder.ids(), [])
        self.mocks["_persist_gameplay_session"].assert_not_called()

    def test_removed_corpse_does_not_charge_for_a_revive(self):
        combat = self.session.gameplay_combat_state
        original_lock = combat.lock
        acquisitions = 0

        @contextmanager
        def remove_before_restore():
            nonlocal acquisitions
            acquisitions += 1
            with original_lock:
                if acquisitions == 2:
                    combat.player_health.pop(1)
                yield

        class RemovedCorpseLock:
            def __enter__(self):
                self.context = remove_before_restore()
                return self.context.__enter__()

            def __exit__(self, *args):
                return self.context.__exit__(*args)

        combat.lock = RemovedCorpseLock()
        self.request()
        self.assertEqual(self.session.gameplay_currency_state.currency_type, 1000)
        self.assertEqual(self.session.recorder.ids(), [])

    def test_send_failure_still_persists_the_committed_respawn(self):
        with patch.object(self.session, "send_reliable_ordered", side_effect=OSError("closed")):
            self.request()
        self.assertFalse(self.session.gameplay_death_pending)
        self.assertFalse(self.session.gameplay_respawn_in_progress)
        self.mocks["_persist_gameplay_session"].assert_called_once()

    def test_a_new_death_after_revive_is_not_cleared_by_request_cleanup(self):
        def die_again(*args, **kwargs):
            self.session.gameplay_combat_state.player_health[1][0] = 0
            self.session.gameplay_death_pending = True
        self.mocks["_update_low_health_socket_state"].side_effect = die_again
        self.request()
        self.assertTrue(self.session.gameplay_death_pending)
        self.assertEqual(self.session.gameplay_combat_state.player_health[1][0], 0)

    def test_known_remote_player_receives_transform_stats_and_revive(self):
        peer = _session(50002, 2, "Observer")
        with patch.object(CharacterServer, "_active_gameplay_sessions", return_value=(peer,)):
            self.server._handle_presence_event(
                self.session, ("player_revived", (1.0, 2.0, 3.0), 0.0, 4200, 17.0)
            )
        self.assertEqual(peer.recorder.ids(), [0x71, 0x7B, 0x6D])
        transform, actor = decode_force_transform_batch(*peer.recorder.find(0x71))
        self.assertEqual(actor, self.session.gameplay_remote_actor_id)
        self.assertEqual(transform.position, (1.0, 2.0, 3.0))

    def test_shared_world_uses_character_actor_and_publishes_to_peers(self):
        events = []
        s = self.session
        s.gameplay_presence_callback = lambda session, event: events.append(event)
        self.request()
        self.assertEqual(s.gameplay_combat_state.player_health[65537][0], 4200)
        self.assertEqual(s.gameplay_combat_state.player_health[1][0], 0)
        self.assertEqual(events[0][0], "player_revived")

    def test_open_world_pvp_lethality_and_death_dialog(self):
        source = _session(50002, 2, "Attacker")
        target = self.session
        combat = target.gameplay_combat_state
        combat.note_player_attacked = Mock()
        source.gameplay_combat_state = combat
        source.gameplay_pvp_enabled = target.gameplay_pvp_enabled = True
        observed = []
        source.gameplay_presence_callback = lambda s, event: observed.append(event)
        target.gameplay_death_pending = False
        pending = SimpleNamespace(
            target_session=target, combat=combat, target_actor_id=1, source_actor_id=65538,
            damage=100, blocked=False, position=(20.0, 0.0, 30.0), server_tick=100,
            damage_type="Physical", skill_index=1, victim_status_effects=(),
            critical_hit=False, cast_id=1, frost_hit=False, user_status_effects=(),
        )
        for allowed, expected in ((False, 1), (True, 0)):
            target.gameplay_respawn_allowed = allowed
            combat.player_health[1][0] = 30
            source.gameplay_pending_pvp_hits["test"] = pending
            with patch.object(CharacterServer, "_pvp_target_is_hostile", return_value=True), \
                    patch.object(CharacterServer, "_player_victim_feature_locked", return_value=False), \
                    patch.object(CharacterServer, "_player_is_low_health", return_value=False), \
                    patch.object(CharacterServer, "_schedule_pvp_status_damage"):
                self.server._resolve_pvp_skill_hit(source, source.gameplay_chase_generation, "test")
            self.assertEqual(combat.player_health[1][0], expected)
            self.assertEqual(combat.note_player_attacked.call_args.args, (1,))
            resolution = next(event[1] for event in reversed(observed)
                              if event[0] == "pvp_combat_resolution")
            self.assertEqual(resolution.killed, allowed)
        self.server._send_combat_resolution(target, resolution, label="test-player-death")
        self.assertEqual(target.recorder.ids(), [0x6B])
        self.timers[-1].fire()
        self.assertEqual(target.recorder.ids(), [0x6B, 0x81])
        self.assertTrue(target.gameplay_death_pending)

    def test_death_dialog_waits_for_animation_and_locks_player_immediately(self):
        s = self.session
        s.gameplay_death_pending = False
        self.server._notify_player_death(s)
        self.server._notify_player_death(s)
        self.assertEqual(s.recorder.ids(), [])
        self.assertEqual(len(self.timers), 1)
        self.assertAlmostEqual(self.timers[0].interval, 2.32)
        self.timers[0].start.assert_called_once()
        self.assertTrue(self.server._player_victim_feature_locked(s, "movement", "skills"))
        self.timers[0].fire()
        self.timers[0].fire()
        self.assertEqual(s.recorder.ids(), [0x81])
        self.assertTrue(self.server._player_victim_feature_locked(s, "movement", "skills"))

    def test_death_dialog_deadline_reaches_real_transport_without_state_lock(self):
        from dso_server.runtime.transport import Session
        from dso_server.runtime.gameplay_scheduler import GameplayDeadlineScheduler
        s = self.session
        scheduler = GameplayDeadlineScheduler(name="death-dialog-test", worker_count=1)
        self.addCleanup(scheduler.shutdown)
        s.gameplay_deadline_scheduler = scheduler
        s.gameplay_deadline_timer = Session.gameplay_deadline_timer.__get__(s)
        s.send_reliable_ordered = Session.send_reliable_ordered.__get__(s)
        s.gameplay_death_pending = False
        with patch("dso_server.services.player_respawn._DEATH_DIALOG_DELAY_SECONDS", 0.02):
            self.server._notify_player_death(s)
        timer = next(iter(s.gameplay_projectile_timers.values()))
        self.assertTrue(timer.finished.wait(2), "death-dialog deadline did not finish")
        # Keep the actual scheduler, instrumented flow lock, RakNet framing
        # and transport assertion. Only the UDP socket is an in-memory sink.
        self.assertEqual(len(s.sock.sent), 1)
        packet, address = s.sock.sent[0]
        self.assertEqual(address, s.addr)
        self.assertTrue(packet.endswith(encode_death_dialog()[0]))
        self.assertTrue(any(entry["label"] == "player-death-dialog"
                            for entry in s.pending_reliable.values()))

    def test_delayed_dialog_is_discarded_after_revive_or_departure(self):
        for change in ("revive", "disconnect", "map-generation", "transfer"):
            with self.subTest(change=change):
                s = self.session
                s.connected = True
                s.gameplay_exit_transition_started = False
                s.gameplay_death_pending = False
                s.gameplay_combat_state.player_health[1][0] = 0
                self.server._notify_player_death(s)
                timer = self.timers[-1]
                if change == "revive":
                    self.request(0x40)
                    timer.cancel.assert_called_once()
                elif change == "disconnect":
                    s.connected = False
                elif change == "map-generation":
                    s.gameplay_chase_generation += 1
                else:
                    s.gameplay_exit_transition_started = True
                s.recorder.messages.clear()
                timer.fire()
                self.assertEqual(s.recorder.ids(), [])

    def test_previous_death_timer_cannot_display_or_cancel_new_death_dialog(self):
        s = self.session
        s.gameplay_death_pending = False
        self.server._notify_player_death(s)
        old = self.timers[-1]
        self.request(0x40)
        s.gameplay_combat_state.player_health[1][0] = 0
        self.server._notify_player_death(s)
        current = self.timers[-1]
        s.recorder.messages.clear()
        old.fire()
        self.assertEqual(s.recorder.ids(), [])
        current.fire()
        self.assertEqual(s.recorder.ids(), [0x81])

    def test_dead_resource_regeneration_preserves_zero_health_on_wire(self):
        from dso_server.protocol.gameplay_protocol import decode_actor_stats_update_batch
        s = self.session
        # Inventory may still hold the pre-hit snapshot when Kill arrives.
        s.gameplay_inventory.health_points = 4200
        self.server._apply_base_resource_regeneration({1: s}, ((1, 25, 100, 8),))
        update, actor = decode_actor_stats_update_batch(*s.recorder.find(0x7B))
        self.assertEqual(actor, 1)
        self.assertEqual(update.health_points, 0)
        self.assertEqual(update.resource_points, 25)

    def test_pre_death_health_regeneration_cannot_publish_after_kill(self):
        s = self.session
        self.server._apply_base_health_regeneration({1: s}, ((1, 10, 20, 4200, 10),))
        self.assertEqual(s.gameplay_inventory.health_points, 0)
        self.assertEqual(s.recorder.ids(), [])

    def test_regeneration_publishes_the_combat_pools_not_the_inventory_mirror(self):
        # FAILURE: "la barre de vie ... le client la met a 100% alors que je
        #   prends des degats" / "idem pour le mana": monster blows leave
        #   inventory.health_points at its old value, and each resource tick
        #   republished it.
        from dso_server.protocol.gameplay_protocol import decode_actor_stats_update_batch
        s = self.session
        s.gameplay_death_pending = False
        combat = s.gameplay_combat_state
        combat.player_health[1] = [300, 4200]
        combat.player_resource[1] = [10.0, 100.0]
        s.gameplay_inventory.health_points = 4200
        s.gameplay_inventory.secondary_resource = 100.0
        self.server._apply_base_resource_regeneration({1: s}, ((1, 25, 100, 8),))
        update, _ = decode_actor_stats_update_batch(*s.recorder.find(0x7B))
        self.assertEqual((update.health_points, update.resource_points), (300, 25))
        s.recorder.messages.clear()
        s.gameplay_inventory.secondary_resource = 100.0
        combat.player_resource[1][0] = 25.0
        self.server._apply_base_health_regeneration({1: s}, ((1, 300, 310, 4200, 10),))
        update, _ = decode_actor_stats_update_batch(*s.recorder.find(0x7B))
        self.assertEqual((update.health_points, update.resource_points), (310, 25))

    def test_a_blow_on_us_updates_the_health_inventory_info_carries(self):
        # FAILURE: "je suis full hp le client me dit que je suis mid hp":
        #   InventoryInfo republished the health from before the blows.
        from dso_server.gameplay.local_combat_state import DamageResolution
        from dso_server.protocol.gameplay_commands import LOCAL_PLAYER_ACTOR_ID
        s = self.session
        s.gameplay_death_pending = False
        s.gameplay_inventory.health_points = 4200
        self.server._send_combat_resolution(s, DamageResolution(
            target_actor_id=LOCAL_PLAYER_ACTOR_ID, attacker_actor_id=0x20001,
            current_health=1234, maximum_health=4200, damage=2966, killed=False,
            position=(0.0, 0.0, 0.0), server_tick=1, hit_tags=(0,), skill_index=0,
        ), label="test")
        self.assertEqual(s.gameplay_inventory.health_points, 1234)

    def test_arena_death_has_no_normal_death_dialog(self):
        s = self.session
        s.gameplay_death_pending = False
        s.gameplay_respawn_allowed = False
        self.server._notify_player_death(s)
        self.assertEqual(s.recorder.ids(), [])

    def test_death_clears_temporary_buffs_and_debuffs_but_keeps_authored_survivors(self):
        from dso_server.services.player_respawn import _effects_removed_on_death
        from dso_server.persistence.readonly_db import connect
        removed = _effects_removed_on_death(str(DEFAULT_STATIC_DB_PATH))
        with connect(DEFAULT_STATIC_DB_PATH) as db:
            kept = db.execute(
                "SELECT rowid - 1 FROM _Template_StatusEffect WHERE KeepOnDeath=1 LIMIT 1"
            ).fetchone()[0]
        lost = next(iter(removed))
        s = self.session
        s.gameplay_runtime_status_effects = {
            10: (SimpleNamespace(effect_id=lost), 123),
            11: (SimpleNamespace(effect_id=kept), 123),
        }
        s.gameplay_victim_status_effect_instances = {
            (1, lost): (SimpleNamespace(effect_id=lost), 123),
            (99, lost): (SimpleNamespace(effect_id=lost), 123),
        }
        self.server._clear_player_death_effects(s)
        self.assertEqual(set(s.gameplay_runtime_status_effects), {11})
        self.assertEqual(set(s.gameplay_victim_status_effect_instances), {(99, lost)})
        self.mocks["_refresh_player_combat_vitals"].assert_called_once()

    def test_dead_inventory_snapshot_stays_at_zero(self):
        self.session.gameplay_inventory.health_points = 5
        self.server._sync_inventory_live_health(self.session)
        self.assertEqual(self.session.gameplay_inventory.health_points, 0)

    def test_real_paid_revive_effects_enter_runtime_and_expire(self):
        import time
        from dso_server.services.combat_resolution import CombatResolutionService
        s = self.session
        encounter = load_local_monster_encounter(
            DEFAULT_STATIC_DB_PATH, s.gameplay_map, include_spawn_areas=False
        )
        s.gameplay_combat_state = combat = LocalCombatState(
            encounter, player_actor_id=1, player_health=4200
        )
        for flag in (0x40, 0x80):
            with self.subTest(flag=flag):
                combat.player_health[1][0] = 0
                s.gameplay_death_pending = True
                with patch.object(CharacterServer, "_send_player_skill_status_effects",
                                  CombatResolutionService._send_player_skill_status_effects):
                    self.request(flag)
                self.assertEqual(len(s.gameplay_runtime_status_effects), 2)
                self.assertTrue(self.server._player_victim_feature_locked(s, "movement"))
                self.assertTrue(self.server._player_victim_feature_locked(s, "damage"))
                s.gameplay_revive_movement_until = time.perf_counter() - 1
                # Advance just the animation effect; protection must remain.
                instance, (entry, _) = min(
                    s.gameplay_runtime_status_effects.items(), key=lambda item: item[1][0].field_10
                )
                s.gameplay_runtime_status_effects[instance] = (entry, time.perf_counter() - 1)
                self.assertFalse(self.server._player_victim_feature_locked(s, "movement"))
                self.assertTrue(self.server._player_victim_feature_locked(s, "damage"))
                for instance, (entry, _) in tuple(s.gameplay_runtime_status_effects.items()):
                    s.gameplay_runtime_status_effects[instance] = (entry, time.perf_counter() - 1)
                self.assertFalse(self.server._player_victim_feature_locked(s, "damage"))
                self.assertEqual(s.gameplay_runtime_status_effects, {})

    def test_last_town_survives_combat_map_initialization(self):
        s = self.session
        self.server._initialize_respawn_state(s, SimpleNamespace(last_visited_town="a0200_kingscity"))
        self.assertEqual(s.gameplay_last_visited_town, "a0200_kingscity")
        s.gameplay_map = "b1200_hub"
        self.server._initialize_respawn_state(s, SimpleNamespace(last_visited_town="a0200_kingscity"))
        self.assertEqual(s.gameplay_last_visited_town, "b1200_hub")
        self.assertFalse(s.gameplay_respawn_allowed)


if __name__ == "__main__":
    unittest.main()
