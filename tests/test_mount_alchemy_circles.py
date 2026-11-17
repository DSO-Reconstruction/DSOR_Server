import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from dso_server.paths import DEFAULT_STATIC_DB_PATH
from dso_server.protocol.gameplay_protocol import StatusEffect
from dso_server.runtime.context import LOCAL_PLAYER_ACTOR_ID
from dso_server.gameplay.local_mount_state import (
    begin_mount_cast,
    begin_mount_ride,
    find_mount,
)
from dso_server.gameplay.stickerbook_state import (
    load_sticker_definition,
    sticker_status_entry,
)
from dso_server.services.customization import CustomizationService
from dso_server.services.presence_visuals import PresenceVisualService
from dso_server.services.base import CharacterService
import app


class FakeSocket:
    def getsockname(self):
        return ("127.0.0.1", 2192)

    def close(self):
        pass


class MountAlchemyCirclesTests(unittest.TestCase):
    def setUp(self):
        self.flow_lock = threading.RLock()
        self.sent_messages = []

        self.session = SimpleNamespace(
            flow_lock=self.flow_lock,
            connected=True,
            gameplay_mode=True,
            gameplay_mount_ride=None,
            gameplay_mount_cast=None,
            gameplay_mount_cast_timer=None,
            gameplay_mount_generation=1,
            gameplay_next_status_effect_instance_id=100,
            gameplay_persistent_status_effects={},
            gameplay_timed_status_effect_expiries={},
            gameplay_active_decorations={},
            gameplay_active_costume="",
            gameplay_active_pet="",
            gameplay_active_costume_effect_instance_id=None,
            gameplay_active_pet_effect_instance_id=None,
            gameplay_character=SimpleNamespace(level=100, character_id=1, class_id=1),
            gameplay_inventory=SimpleNamespace(items=()),
            gameplay_quick_slots=None,
            gameplay_exit_transition_started=False,
            gameplay_last_move=None,
            gameplay_combat_state=None,
            gameplay_presence_callback=None,
            gameplay_persist_callback=None,
            gameplay_stickerbook_entries={},
            gameplay_game_currencies={},
            gameplay_client_command_state={},
            send_reliable_ordered=lambda payload, track=True, label="": self.sent_messages.append((payload, label)),
        )

        self.service = CustomizationService()
        self.service.static_database_path = DEFAULT_STATIC_DB_PATH
        self.service.is_session_current = lambda s: True

    def _equip_circle(self, resource_id="vanity_groundmarker_01"):
        definition = load_sticker_definition(DEFAULT_STATIC_DB_PATH, resource_id)
        instance_id = self.session.gameplay_next_status_effect_instance_id
        self.session.gameplay_next_status_effect_instance_id += 1
        effect = sticker_status_entry(
            definition,
            instance_id=instance_id,
            start_tick=100,
            actor_id=LOCAL_PLAYER_ACTOR_ID,
            active=True,
        )
        self.session.gameplay_persistent_status_effects[instance_id] = effect
        self.session.gameplay_active_decorations["vanitygroundmarker"] = (
            resource_id, instance_id
        )
        return definition, effect

    def test_mounting_suppresses_alchemy_circle(self):
        _, circle_effect = self._equip_circle()

        defn = find_mount("epicmount_steambike_01", DEFAULT_STATIC_DB_PATH)
        cast = begin_mount_cast(defn, instance_id=10, start_tick=1)
        self.session.gameplay_mount_cast = cast

        self.service._complete_mount_cast(self.session, generation=1)

        # Ride is active
        self.assertIsNotNone(self.session.gameplay_mount_ride)
        # Groundmarker is suppressed: not in persistent status effects
        self.assertNotIn(
            circle_effect.instance_id,
            self.session.gameplay_persistent_status_effects,
        )
        # But decoration is still remembered with None instance
        self.assertEqual(
            self.session.gameplay_active_decorations["vanitygroundmarker"],
            ("vanity_groundmarker_01", None),
        )

    def test_dismounting_restores_alchemy_circle(self):
        self._equip_circle()

        defn = find_mount("epicmount_steambike_01", DEFAULT_STATIC_DB_PATH)
        cast = begin_mount_cast(defn, instance_id=10, start_tick=1)
        self.session.gameplay_mount_cast = cast
        self.service._complete_mount_cast(self.session, generation=1)

        self.assertIsNone(
            self.session.gameplay_active_decorations["vanitygroundmarker"][1]
        )

        # Dismount
        unmounted = CharacterService._remove_mount_ride(
            self.session, reason="test dismount"
        )
        self.assertTrue(unmounted)
        self.assertIsNone(self.session.gameplay_mount_ride)

        # Circle is restored
        res_id, new_inst = self.session.gameplay_active_decorations["vanitygroundmarker"]
        self.assertEqual(res_id, "vanity_groundmarker_01")
        self.assertIsNotNone(new_inst)
        self.assertIn(new_inst, self.session.gameplay_persistent_status_effects)
        restored_entry = self.session.gameplay_persistent_status_effects[new_inst]
        self.assertTrue(restored_entry.has_details)
        self.assertTrue(any(label == "groundmarker-restore" for _, label in self.sent_messages))

    def test_toggle_alchemy_circle_while_mounted(self):
        defn = find_mount("epicmount_steambike_01", DEFAULT_STATIC_DB_PATH)
        cast = begin_mount_cast(defn, instance_id=10, start_tick=1)
        ride = begin_mount_ride(cast, instance_id=11)
        self.session.gameplay_mount_ride = ride
        self.session.gameplay_persistent_status_effects[11] = ride

        # Equip circle while mounted
        circle_defn = load_sticker_definition(DEFAULT_STATIC_DB_PATH, "vanity_groundmarker_02")
        self.service._toggle_switchable_sticker(self.session, circle_defn)

        # Stays equipped with None instance
        self.assertEqual(
            self.session.gameplay_active_decorations["vanitygroundmarker"],
            ("vanity_groundmarker_02", None),
        )

        # Dismount restores the newly equipped circle
        CharacterService._remove_mount_ride(self.session, reason="dismount")
        res_id, new_inst = self.session.gameplay_active_decorations["vanitygroundmarker"]
        self.assertEqual(res_id, "vanity_groundmarker_02")
        self.assertIsNotNone(new_inst)
        self.assertIn(new_inst, self.session.gameplay_persistent_status_effects)

    def test_presence_visuals_hides_circle_while_mounted(self):
        _, circle_effect = self._equip_circle()

        # Before mounting: visual effects include circle
        visuals = PresenceVisualService._remote_status_snapshot(self.session, 1)
        self.assertTrue(any(e.effect_id == circle_effect.effect_id for e in visuals.entries))

        # Mount up
        defn = find_mount("epicmount_steambike_01", DEFAULT_STATIC_DB_PATH)
        cast = begin_mount_cast(defn, instance_id=10, start_tick=1)
        ride = begin_mount_ride(cast, instance_id=11)
        self.session.gameplay_mount_ride = ride

        # While mounted: visual effects do NOT include circle
        visuals_mounted = PresenceVisualService._remote_status_snapshot(self.session, 1)
        self.assertFalse(any(e.effect_id == circle_effect.effect_id for e in visuals_mounted.entries))


if __name__ == "__main__":
    unittest.main()
