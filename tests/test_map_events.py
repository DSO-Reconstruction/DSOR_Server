"""Map-local scripted events: signals -> events -> gated content.

The Balor arena is the worked example from docs/GAME_EVENTS.md: walking into
``BOSS_start_trigger`` starts ``boss_spawn`` (the boss spawn point), killing
the boss starts ``boss_death`` (88 reward chests and every exit).
"""

from __future__ import annotations

from dataclasses import replace
import threading
import sqlite3
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from dso_server.gameplay.local_combat_state import LocalCombatState
from dso_server.gameplay.local_map_state import (
    _enabled_event_set_ids,
    local_npc_actor_id,
    local_prop_actor_base,
    load_local_npc_definitions,
    load_local_map_exits,
    load_map_coordinate_frame,
)
from dso_server.gameplay.local_monster_state import (
    load_local_player_only_encounter,
)
from dso_server.gameplay.map_event_state import (
    prop_index,
    MapEventRuntime,
    SignalContext,
    load_map_event_content,
    load_map_signal_catalog,
)
from dso_server.paths import DEFAULT_STATIC_DB_PATH
from dso_server.protocol.gameplay_protocol import (
    PROP_INFO_COMMAND,
    Move,
    decode_prop_info_batch,
)
from dso_server.protocol.player_init_protocol import (
    EVENT_UPDATE_COMMAND,
    EXIT_INFO_COMMAND,
    decode_event_update,
)
from dso_server.protocol.prop_protocol import (
    PROP_STATE_ACTIVE,
    PROP_STATE_INACTIVE,
    PROP_STATE_USED,
    PROP_UPDATE_COMMAND,
    NewProp,
    PropInteraction,
    PropUpdate,
    QuestTriggerSignal,
    decode_discard_prop_batch,
    decode_new_prop_batch,
    decode_prop_interaction,
    decode_prop_update_batch,
    decode_quest_trigger_signal,
    encode_discard_prop_batch,
    encode_new_prop_batch,
    encode_prop_interaction,
    encode_prop_update_batch,
    encode_quest_trigger_signal,
)
from dso_server.runtime.transport import Session
from dso_server.services.character_server import CharacterServer
from dso_server.tests.test_pvp_matchmaking import _FakeSocket, _Recorder, _quiet_server


BALOR = "e_bos_02_balor_01"
BALOR_TEMPLATE = "g0012_boss_07_balor"


class PropProtocolTests(unittest.TestCase):
    def test_prop_interaction_is_id_then_operation_without_actor(self):
        payload, bits = encode_prop_interaction(PropInteraction(0x10000, "Interact"))
        self.assertEqual(
            payload.hex(),
            "8b3900" "00000100" "0800" + b"Interact".hex(),
        )
        self.assertEqual(bits, len(payload) * 8)
        self.assertEqual(
            decode_prop_interaction(payload, bits),
            PropInteraction(0x10000, "Interact"),
        )

    def test_quest_trigger_signal_is_one_atom(self):
        payload, bits = encode_quest_trigger_signal(QuestTriggerSignal("BOSS_start_trigger"))
        self.assertEqual(payload[:3].hex(), "8b2501")
        self.assertEqual(
            decode_quest_trigger_signal(payload, bits),
            QuestTriggerSignal("BOSS_start_trigger"),
        )

    def test_new_prop_round_trip_and_state_range(self):
        prop = NewProp(
            0x20000, "state_chest_loot_01", bytes(16), (1.5, 2.0, -3.25), 0.75,
            operation="Interact",
        )
        payload, bits = encode_new_prop_batch((prop,), actor_id=1)
        self.assertEqual(payload[:3].hex(), "853400")
        self.assertEqual(decode_new_prop_batch(payload, bits), ((prop, 1),))
        with self.assertRaises(ValueError):
            encode_new_prop_batch((NewProp(1, "x", bytes(16), (0, 0, 0), 0.0, state=4),), actor_id=1)

    def test_prop_update_and_discard_round_trips(self):
        update = PropUpdate(0x10000, PROP_STATE_INACTIVE, actor_id=7, operation="Interact")
        payload, bits = encode_prop_update_batch((update,), actor_id=1)
        self.assertEqual(decode_prop_update_batch(payload, bits), ((update, 1),))
        payload, bits = encode_discard_prop_batch((5, 6), actor_id=1)
        self.assertEqual(decode_discard_prop_batch(payload, bits), ((5, 1), (6, 1)))


class InitialEventSetTests(unittest.TestCase):
    def test_loaders_accept_alternative_true_flags_and_honor_false_flags(self):
        with sqlite3.connect(":memory:") as db:
            db.execute("CREATE TABLE _Instance_EventSets (Id INTEGER, EventFlags TEXT)")
            db.executemany("INSERT INTO _Instance_EventSets VALUES (?, ?)", [
                (1, "later=False;"), (2, "first=True;second=True;later=False;"),
                (3, "first=True;later=False;"),
            ])
            self.assertEqual(_enabled_event_set_ids(db, frozenset()), {1})
            self.assertEqual(_enabled_event_set_ids(db, frozenset({"first"})), {1, 2, 3})
            self.assertEqual(_enabled_event_set_ids(db, frozenset({"first", "second"})), {1, 2, 3})
            self.assertEqual(_enabled_event_set_ids(db, frozenset({"first", "second", "later"})), set())

    def test_runtime_and_initial_npc_loader_agree_on_the_hub_phase(self):
        map_id = "a0002_start_hub"
        content = load_map_event_content(DEFAULT_STATIC_DB_PATH, map_id)
        catalog = load_map_signal_catalog(DEFAULT_STATIC_DB_PATH)
        for flags, rows in ((frozenset(), {11}),
                            (frozenset({"a0002_start_hub_02"}), {13}),
                            (frozenset({"a0002_start_hub_04"}), {15})):
            with self.subTest(flags=flags):
                runtime = MapEventRuntime(content, catalog, instance_key=(map_id,), initial_flags=flags)
                npcs = load_local_npc_definitions(
                    DEFAULT_STATIC_DB_PATH, map_id,
                    include_inactive_event_sets=False, active_event_flags=flags,
                )
                self.assertEqual({n.event_set_row for n in npcs}, rows)
                self.assertTrue(rows <= runtime.enabled_event_sets)


class SignalCatalogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = load_map_signal_catalog(DEFAULT_STATIC_DB_PATH)
        cls.content = load_map_event_content(DEFAULT_STATIC_DB_PATH, BALOR)

    def _context(self, params, **kwargs):
        defaults = dict(map_id=BALOR, running_events=frozenset())
        defaults.update(kwargs)
        return SignalContext(params=frozenset(params), **defaults)

    def test_map_discovery_conditions_require_all_positive_and_no_negative_maps(self):
        rule = self.catalog.rule("a0002_start_hub_04")
        fortress = "a0005_start_fortress_dun"
        garden = "a0003_start_garden_dun"
        self.assertEqual(rule.maps_unlocked, (fortress,))
        base = dict(map_id="a0002_start_hub", running_events=frozenset(),
                    player_level=1, class_name="ranger", player_count=1)
        self.assertFalse(rule.accepts(**base))
        self.assertFalse(rule.accepts(**base, unlocked_maps=frozenset({garden})))
        self.assertTrue(rule.accepts(
            **base, unlocked_maps=frozenset({"A0005_START_FORTRESS_DUN_diff_2"}),
        ))
        guarded = replace(rule, maps_unlocked=(garden, fortress),
                          maps_not_unlocked=("a0100_adventurershub",))
        self.assertFalse(guarded.accepts(**base, unlocked_maps=frozenset({fortress})))
        self.assertTrue(guarded.accepts(**base, unlocked_maps=frozenset({garden, fortress})))
        self.assertFalse(guarded.accepts(
            **base, unlocked_maps=frozenset({garden, fortress, "a0100_adventurershub"}),
        ))

    def test_balor_trigger_starts_the_boss_spawn_event(self):
        rules = self.catalog.evaluate(
            "PlayerHitTrigger", self._context({"boss_start_trigger"})
        )
        self.assertIn("BOSS_start_start_signal", [rule.signal_id for rule in rules])
        started = self.catalog.events_by_start_signal["boss_start_start_signal"]
        self.assertIn("boss_spawn", started)
        runtime = MapEventRuntime(
            self.content, self.catalog, instance_key=(BALOR,), initial_flags=frozenset(),
        )
        transition = runtime.begin_event(self.catalog.event("boss_spawn"))
        rows = self.content.spawn_rows_for_sets(transition.enabled_sets)
        self.assertEqual([row.template_id for row in rows], [BALOR_TEMPLATE])
        self.assertTrue(transition.announce)
        self.assertIsNone(runtime.begin_event(self.catalog.event("boss_spawn")))

    def test_balor_kill_reveals_chests_and_exits(self):
        rules = self.catalog.evaluate(
            "PlayerKilledMonster",
            self._context({BALOR_TEMPLATE}, present_templates=frozenset({BALOR_TEMPLATE})),
        )
        self.assertIn("BOSS_death_start_signal", [rule.signal_id for rule in rules])
        runtime = MapEventRuntime(
            self.content, self.catalog, instance_key=(BALOR,), initial_flags=frozenset(),
        )
        transition = runtime.begin_event(self.catalog.event("boss_death"))
        props = self.content.props_for_sets(transition.enabled_sets)
        self.assertEqual(len(props), 88)
        self.assertTrue(all(prop.instance_guid != bytes(16) for prop in props))
        self.assertTrue(all(prop.one_shot for prop in props))
        exits = load_local_map_exits(
            DEFAULT_STATIC_DB_PATH, BALOR, active_event_flags=runtime.active_flags(),
        )
        self.assertEqual(len(exits), 12)
        self.assertIn("exit_general_01", {exit.exit_id for exit in exits})
        self.assertEqual(
            load_local_map_exits(DEFAULT_STATIC_DB_PATH, BALOR), ()
        )

    def test_monsters_eliminated_waits_for_every_present_template(self):
        template = "boss_gorga_add_invisible_sharkman_spawner"
        context = SignalContext(
            map_id="pw027_gorga_01_boss", params=frozenset({template}),
            running_events=frozenset(),
            present_templates=frozenset({template}),
            alive_templates=frozenset({template}),
        )
        self.assertEqual(self.catalog.evaluate("MonstersEliminated", context), ())
        cleared = SignalContext(
            map_id="pw027_gorga_01_boss", params=frozenset({template}),
            running_events=frozenset(),
            present_templates=frozenset({template}),
        )
        self.assertEqual(
            [rule.signal_id for rule in self.catalog.evaluate("MonstersEliminated", cleared)],
            ["boss_pw_gorga_sharkman_spawn_start_signal"],
        )
        self.assertEqual(
            self.catalog.event("boss_pw_gorga_sharkman_spawn").start_delay, 2.0
        )

    def test_percentage_and_quest_gated_rules(self):
        rule = self.catalog.rule("all_unlock_chests_start_signal_01")
        self.assertAlmostEqual(rule.fraction, 0.05)
        low = SignalContext(map_id="pw004_hagastove_grotto_01_dun", params=frozenset(),
                            running_events=frozenset(), dead_fraction=0.01)
        self.assertNotIn(rule, self.catalog.evaluate("MonstersEliminatedPercentage", low))
        high = SignalContext(map_id="pw004_hagastove_grotto_01_dun", params=frozenset(),
                             running_events=frozenset(), dead_fraction=0.06)
        self.assertIn(rule, self.catalog.evaluate("MonstersEliminatedPercentage", high))
        # "Quest not accepted" is trivially true without a quest system, so
        # the tutorial's dwarf trigger still fires; a signal that needs a
        # quest to be accepted or solved never does.
        not_accepted = self.catalog.rule("a0001_enter_map_quest_signal_dwarf")
        self.assertTrue(not_accepted.accepts(
            map_id="a0001_start_tutorial_dun", running_events=frozenset(),
            player_level=1, class_name="dwarf", player_count=1,
        ))
        self.assertFalse(not_accepted.accepts(
            map_id="a0001_start_tutorial_dun", running_events=frozenset(),
            player_level=1, class_name="mage", player_count=1,
        ))
        quest_gated = next(
            rule for rule in self.catalog.rules_by_signal_id.values()
            if rule.requires_quest_state and rule.enabled
        )
        self.assertFalse(quest_gated.accepts(
            map_id=next(iter(quest_gated.maps), "any"), running_events=frozenset(),
            player_level=50, class_name="dwarf", player_count=1,
        ))
        self.assertFalse(any(
            # Prop interactions are client signals the server resolves from
            # its own prop table (map_event_state.server_evaluated).
            rule.signal_type == "clientsignal" and rule.event_type not in {
                "playerenteredmap", "playerlevelup", "playerinteractedprop",
            }
            for rules in self.catalog.rules_by_event_type.values()
            for rule in rules
        ))

    def test_trigger_volume_containment(self):
        volume = next(
            volume for volume in self.content.trigger_volumes
            if volume.trigger_id == "BOSS_start_trigger"
        )
        matrix = volume.matrix
        center = (matrix[12], matrix[13], matrix[14])
        self.assertTrue(volume.contains(center))
        self.assertTrue(volume.contains((center[0] + 20.0, center[1], center[2] + 20.0)))
        self.assertFalse(volume.contains((center[0] + 500.0, center[1], center[2])))


class MapEventServiceTests(unittest.TestCase):
    def setUp(self):
        self.server = _quiet_server(self)
        self.server.sessions = {}
        self.server.gameplay_world_lock = threading.RLock()
        patcher = patch.object(CharacterServer, "_accept_gameplay_state_command", lambda *a, **k: 0)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.encounter = load_local_player_only_encounter(DEFAULT_STATIC_DB_PATH, BALOR)
        self.frame = load_map_coordinate_frame(DEFAULT_STATIC_DB_PATH, BALOR)
        self.session = self._session(50001, 1)
        self.server.sessions[self.session.addr] = self.session
        self.runtime = self.server._attach_map_event_runtime(
            self.session, map_id=BALOR, initial_flags=frozenset(),
            monster_level=None, scaling_difficulty=0, fresh_instance=True,
        )

    def _session(self, port, character_id):
        session = Session(("127.0.0.1", port), _FakeSocket())
        session.connected = True
        session.gameplay_mode = True
        session.gameplay_map = BALOR
        session.gameplay_map_access_group_id = BALOR
        session.gameplay_initialization_sent = True
        session.gameplay_presence_ready = True
        session.gameplay_character = SimpleNamespace(
            character_id=character_id, name=f"P{character_id}", level=100, class_id=1,
        )
        session.gameplay_remote_actor_id = 0x10000 + character_id
        session.gameplay_coordinate_frame = self.frame
        session.gameplay_monster_encounter = self.encounter
        session.gameplay_combat_state = LocalCombatState(
            self.encounter, player_actor_id=session.gameplay_remote_actor_id,
            player_health=1000, initially_active=False,
        )
        # Stand where the authored boss spawn point is, so the spawn is near.
        session.gameplay_last_move = Move.from_values(
            self.frame.world_to_wire((-7.8, 0.0, 24.5)), 0, 1.0, 1.0, 100, 0,
        )
        recorder = _Recorder()
        session.send_reliable_ordered = recorder
        session.send_reliable_ordered_split = (
            lambda payload, **kwargs: recorder(payload, label=kwargs.get("label_prefix", "split"))
        )
        session.recorder = recorder
        return session

    def _trigger_boss_spawn(self):
        self.server._fire_map_signal(self.session, "PlayerHitTrigger", ("BOSS_start_trigger",))

    def test_trigger_spawns_the_boss_into_the_shared_combat_state(self):
        combat = self.session.gameplay_combat_state
        self.assertEqual(combat.monsters, {})
        self._trigger_boss_spawn()
        self.assertIn("boss_spawn", self.runtime.running)
        bosses = [m for m in combat.monsters.values() if m.template_id == BALOR_TEMPLATE]
        self.assertEqual(len(bosses), 1)
        self.assertEqual(len(combat.runtime_groups), 1)
        self.assertIs(combat.group_for_actor(bosses[0].actor_id), combat.runtime_groups[0])
        self.assertIn(bosses[0].actor_id, combat.alive_actor_ids)
        started = {
            entry.event_name.casefold(): (entry.event_id, entry.flags)
            for data, bits, _label in self.session.recorder.messages
            if int.from_bytes(data[1:3], "little") == EVENT_UPDATE_COMMAND
            for entry in decode_event_update(data, bits).events
        }
        # Retail shape: one entry per event, id 0, map-local flag set.
        self.assertEqual(started.get("boss_spawn"), (0, (True, True, False, False)))
        self.assertEqual(started.get("boss_start"), (0, (True, True, False, False)))
        # The viewer stands on the spawn point, so the group was announced.
        self.assertIn(bosses[0].actor_id, self.session.gameplay_announced_monster_actor_ids)
        # Firing the trigger again is idempotent.
        self._trigger_boss_spawn()
        self.assertEqual(
            len([m for m in combat.monsters.values() if m.template_id == BALOR_TEMPLATE]), 1,
        )

    def test_kill_reveals_chests_exits_and_prop_interaction_flips_state(self):
        self._trigger_boss_spawn()
        combat = self.session.gameplay_combat_state
        boss = next(m for m in combat.monsters.values() if m.template_id == BALOR_TEMPLATE)
        with combat.lock:
            combat.monster_health[boss.actor_id] = 0
        self.session.recorder.messages.clear()
        self.server._on_monsters_killed(self.session, (boss,))
        self.assertIn("boss_death", self.runtime.running)
        self.assertEqual(len(self.runtime.props), 88)
        ids = self.session.recorder.ids()
        self.assertIn(PROP_INFO_COMMAND, ids)
        self.assertIn(EXIT_INFO_COMMAND, ids)
        # Mid-map props arrive as PropInfo records, the path retail uses;
        # every event started by the kill (BOSS_death_dtu included) is sent.
        self.assertIn(
            "boss_death_dtu",
            {
                entry.event_name.casefold()
                for data, bits, _label in self.session.recorder.messages
                if int.from_bytes(data[1:3], "little") == EVENT_UPDATE_COMMAND
                for entry in decode_event_update(data, bits).events
            },
        )
        revealed = [
            entry
            for data, bits, _label in self.session.recorder.messages
            if int.from_bytes(data[1:3], "little") == PROP_INFO_COMMAND
            for entry in decode_prop_info_batch(data, bits)[0].entries
        ]
        self.assertEqual(len(revealed), 88)
        self.assertEqual(
            {prop.template_id for prop in revealed},
            {"e_bos_02_golden_chest_01", "e_bos_02_reward_chest_01"},
        )
        self.assertEqual({prop.field_48 for prop in revealed}, {""})
        # The map repeats Balor's entry once per access tier at the same
        # spot; only the instance's tier is kept at each spot (difficulty 0
        # here), beside the general exit. The spot's diff_05/07/09/10 tiers
        # have no access row (the client labels them "report_bug").
        self.assertEqual(
            sorted(
                exit.exit_id.removeprefix(
                    "i0002_andermagic_big_dun_event_balor_"
                )
                for exit in self.session.gameplay_map_exits
            ),
            ["diff_00_entry", "exit_general_01"],
        )
        # PropInfo for a late joiner carries the same props.
        info = CharacterServer._map_event_prop_info(self.session)
        self.assertEqual(len(info.entries), 88)
        self.assertEqual({entry.field_24 for entry in info.entries}, {PROP_STATE_ACTIVE})

        chest = revealed[0]
        self.session.recorder.messages.clear()
        self.session.gameplay_chase_server_tick = 333
        payload, bits = encode_prop_interaction(PropInteraction(chest.prop_id, ""))
        self.session.current_message_bit_length = bits
        self.server._handle_prop_interaction(self.session, payload)
        update = self.session.recorder.find(PROP_UPDATE_COMMAND)
        self.assertIsNotNone(update)
        (decoded, trailing_actor), = decode_prop_update_batch(*update)
        # Retail's click reply: state 3, game tick, every flag false,
        # trailing actor 0, then a PropInfo refresh of the same prop.
        self.assertEqual(trailing_actor, 0)
        self.assertEqual(decoded.prop_id, chest.prop_id)
        self.assertEqual(decoded.state, PROP_STATE_USED)
        self.assertEqual(decoded.counter, 333)
        self.assertFalse(decoded.state_changed)
        self.assertFalse(decoded.keep_visual_state)
        self.assertEqual(decoded.task_index, 0)
        refresh = [
            entry
            for data, bits, _label in self.session.recorder.messages
            if int.from_bytes(data[1:3], "little") == PROP_INFO_COMMAND
            for entry in decode_prop_info_batch(data, bits)[0].entries
        ]
        self.assertEqual([(e.prop_id, e.field_24, e.field_28) for e in refresh],
                         [(chest.prop_id, PROP_STATE_USED, 333)])
        # The chest is opened per player (individual_usage): the clicker sees
        # it used, the map's shared state stays active for everyone else.
        self.assertTrue(self.runtime.props[chest.prop_id].definition.individual_usage)
        self.assertEqual(self.runtime.props[chest.prop_id].state, PROP_STATE_ACTIVE)
        usage = set(self.runtime.individual_prop_usage)
        self.assertEqual(len(usage), 1)
        # A second click on the used chest changes nothing.
        self.session.recorder.messages.clear()
        self.server._handle_prop_interaction(self.session, payload)
        self.assertEqual(self.runtime.individual_prop_usage, usage)
        self.assertEqual(self.runtime.props[chest.prop_id].state, PROP_STATE_ACTIVE)

    def test_late_joiner_gets_running_events_and_props(self):
        self._trigger_boss_spawn()
        joiner = self._session(50002, 2)
        self.server.sessions[joiner.addr] = joiner
        joined = self.server._attach_map_event_runtime(
            joiner, map_id=BALOR, initial_flags=frozenset(),
            monster_level=None, scaling_difficulty=0, fresh_instance=False,
        )
        self.assertIs(joined, self.runtime)
        entries = CharacterServer._running_map_event_entries(joiner)
        by_name = {entry.event_name.casefold(): entry for entry in entries}
        self.assertIn("boss_spawn", by_name)
        self.assertEqual(by_name["boss_spawn"].flags, (True, True, False, False))
        self.assertEqual(by_name["boss_spawn"].event_id, 0)
        self.assertEqual(len(CharacterServer._map_event_prop_info(joiner).entries), 0)

    def test_movement_into_the_trigger_volume_raises_the_signal_once(self):
        events = []
        self.session.gameplay_presence_callback = lambda s, event: events.append(event)
        inside = self.frame.world_to_wire((-16.7, 6.0, 29.7))
        CharacterServer._check_trigger_volumes(self.session, inside)
        CharacterServer._check_trigger_volumes(self.session, inside)
        # The arena has two overlapping boxes; each entry is reported once.
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0][:2], ("map_signal", "PlayerHitTrigger"))
        self.assertIn("BOSS_start_trigger", events[0][2])
        outside = self.frame.world_to_wire((900.0, 6.0, 900.0))
        CharacterServer._check_trigger_volumes(self.session, outside)
        self.assertEqual(self.session.gameplay_trigger_volumes_inside, set())

    def test_quest_trigger_signal_command_reaches_the_evaluator(self):
        payload, bits = encode_quest_trigger_signal(QuestTriggerSignal("BOSS_start_trigger"))
        self.session.current_message_bit_length = bits
        self.server._handle_quest_trigger_signal(self.session, payload)
        self.assertIn("boss_spawn", self.runtime.running)


if __name__ == "__main__":
    unittest.main()


class PropIdAllocationTests(unittest.TestCase):
    """The client selects a prop slot by the low 16 bits of the prop id.

    ``ClientPropManager::RegisterPropEntity`` and the world's prop lookup both
    truncate the id to a word, so a spawn-point prop and a scene state object
    that share a rowid must not share a low word.  In kingscrypt01 rowid 4 is
    the ``a0200_UndeadRising_03_altar`` progress prop and, in the spawn-point
    table, a ``scaling_free_chest_duria``; the old 0x40000 base put both in
    slot 3 and the chest replaced the altar once its event set was published.
    """

    def _assert_unique_low_words(self, map_id):
        content = load_map_event_content(DEFAULT_STATIC_DB_PATH, map_id)
        indices = [prop_index(prop.prop_id) for prop in content.props]
        self.assertEqual(len(indices), len(set(indices)), map_id)
        # Props share the client's actor-id space with NPCs: the scene rows
        # start after the map's highest NPC slot, event-gated NPCs included.
        base = local_prop_actor_base(DEFAULT_STATIC_DB_PATH, map_id)
        npc_slots = {
            local_npc_actor_id(definition) & 0xFFFF
            for definition in load_local_npc_definitions(
                DEFAULT_STATIC_DB_PATH, map_id, include_inactive_event_sets=True,
            )
        }
        for prop in content.props:
            if prop.kind == "scene":
                self.assertEqual(prop.prop_id, base + prop.row_id - 1)
            self.assertLess(prop_index(prop.prop_id), 0x10000)
            self.assertNotIn(prop_index(prop.prop_id), npc_slots)
        return content

    def test_kingscrypt_spawn_chests_do_not_shadow_progress_props(self):
        content = self._assert_unique_low_words("a0202_kingscrypt01")
        by_row = {}
        for prop in content.props:
            by_row.setdefault(prop.row_id, {})[prop.kind] = prop
        altar = by_row[4]["scene"]
        chest = by_row[4]["spawn"]
        self.assertEqual(altar.instance_id, "a0200_UndeadRising_03_altar")
        self.assertEqual(chest.template_id, "scaling_free_chest_duria")
        self.assertNotEqual(prop_index(altar.prop_id), prop_index(chest.prop_id))

    def test_balor_props_keep_unique_slots(self):
        self._assert_unique_low_words(BALOR)


class DifficultyAndLocationGatingTests(unittest.TestCase):
    """Map data authors every difficulty variant on top of each other.

    Bloody sludges sit in the spawn-point table of every dungeon without an
    EventSet but belong to Bloodshed (tier 8); a chest location of the
    ``all_unlock_chests`` set holds seventeen overlapping spawn points (one
    normal free chest, ten scaling free chests, three rarities and their
    goblin twins); and a boss EventSet keeps one row per phase or difficulty.
    """

    @classmethod
    def setUpClass(cls):
        cls.catalog = load_map_signal_catalog(DEFAULT_STATIC_DB_PATH)
        cls.crypt = load_map_event_content(DEFAULT_STATIC_DB_PATH, "a0202_kingscrypt01")

    def _runtime(self, content, tier, key=("test", 1)):
        return MapEventRuntime(
            content, self.catalog, instance_key=key,
            initial_flags=frozenset(), scaling_difficulty=tier,
        )

    def test_bloody_sludges_spawn_only_on_bloodshed(self):
        from dso_server.gameplay.local_monster_state import load_local_monster_encounter

        def templates(tier):
            encounter = load_local_monster_encounter(
                DEFAULT_STATIC_DB_PATH, "a0202_kingscrypt01",
                scaling_difficulty=tier, include_spawn_areas=False,
            )
            return [
                getattr(c.value, "template_id", None)
                for c in encounter.spawn_batch.commands
            ]

        self.assertNotIn("all_guardian_bloodshed_thing_blood_01", templates(0))
        self.assertNotIn("all_guardian_bloodshed_thing_blood_01", templates(3))
        self.assertIn("all_guardian_bloodshed_thing_blood_01", templates(8))

    def test_one_chest_per_location_matching_the_tier(self):
        raw = self.crypt.props_for_sets({140})
        self.assertEqual(len(raw), 255)
        normal = self._runtime(self.crypt, 0).select_props(raw)
        self.assertEqual(len(normal), 15)
        self.assertTrue(all(p.template_id == "normal_free_chest_duria" for p in normal))
        scaling = self._runtime(self.crypt, 8).select_props(raw)
        self.assertEqual(len(scaling), 15)
        self.assertTrue(all(p.template_id.startswith("scaling_") for p in scaling))
        self.assertTrue(any(p.template_id != "scaling_free_chest_duria" for p in scaling))
        # Stable per instance: a second joiner sees the same chests.
        again = self._runtime(self.crypt, 8).select_props(raw)
        self.assertEqual([p.prop_id for p in scaling], [p.prop_id for p in again])
        # Scene props are never collapsed.
        scene = tuple(p for p in self.crypt.props if p.kind == "scene")
        self.assertEqual(self._runtime(self.crypt, 8).select_props(scene), scene)

    def test_boss_sets_are_left_to_the_event_runtime_at_load(self):
        from dso_server.gameplay.local_monster_state import load_local_monster_encounter

        balor = load_map_event_content(DEFAULT_STATIC_DB_PATH, BALOR)
        self.assertIn(154, balor.server_driven_event_sets(self.catalog))
        self.assertIsNone(load_local_monster_encounter(
            DEFAULT_STATIC_DB_PATH, BALOR, include_spawn_areas=False,
        ))
        heredur = load_map_event_content(DEFAULT_STATIC_DB_PATH, "a0204_boss")
        rows = self._runtime(heredur, 0).gated_spawn_rows({154})
        self.assertEqual(len(rows), 11)
        from dso_server.gameplay.boss_encounter_policy import select_boss_spawn_rows
        selected = select_boss_spawn_rows(
            DEFAULT_STATIC_DB_PATH, "a0204_boss",
            tuple((r.row_id, r.position, r.template_id, r.event_set_row) for r in rows),
            scaling_difficulty=0,
        )
        # One phase survives: the selector keeps a single concrete boss per
        # set instead of one per difficulty variant.
        self.assertEqual(len(selected), 1)
        scaling = select_boss_spawn_rows(
            DEFAULT_STATIC_DB_PATH, "a0204_boss",
            tuple((r.row_id, r.position, r.template_id, r.event_set_row) for r in rows),
            scaling_difficulty=3,
        )
        self.assertEqual([r[2] for r in scaling], ["boss_heredur_scaling"])
