"""A ride saved under another static.db4 revision keeps its mount."""
from dataclasses import replace

from dso_server.gameplay.local_mount_state import (
    BLACK_BEAR_MOUNT, begin_mount_cast, begin_mount_ride, find_mount,
)
from dso_server.persistence.account_state import _state_from_document, _state_to_document
from dso_server.tests import test_achievements as fixtures


def test_a_ride_saved_under_another_database_takes_the_current_ride_id():
    mount = find_mount(BLACK_BEAR_MOUNT.stickerbook_resource_id)
    ride = begin_mount_ride(begin_mount_cast(mount, instance_id=6, start_tick=0), instance_id=7)
    document = _state_to_document(replace(fixtures._account_state(5), mount_ride=ride))
    # Another build's row for the same ride.
    document["mount_ride"]["effect_id"] = mount.ride_effect_id - 13
    restored = _state_from_document(document)
    assert restored.mount_ride.definition.stickerbook_resource_id == mount.stickerbook_resource_id
    assert restored.mount_ride.effect_id == mount.ride_effect_id
