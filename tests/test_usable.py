"""Using an item, and the mount that comes of it.

Measured against the ``officiel4`` capture, where the operator rode a manticore. There
is no mount command: there is an item used by name, and two status effects in answer.
"""

import pytest

from dsor import effects, usable
from dsor.world import World

MANTICORE = "mythical_mount_manticore_01"


def test_the_client_sends_the_item_s_name_and_nothing_else():
    """A 16-bit length and that many bytes, exactly as captured."""
    body = bytes([0x1B, 0x00]) + MANTICORE.encode()
    assert usable.name_in(body) == MANTICORE
    assert usable.name_in(b"") is None
    assert usable.name_in(bytes([0x00, 0x00])) is None
    assert usable.name_in(bytes([0xFF, 0x00]) + b"short") is None


def test_the_item_names_its_own_effect():
    """``_Template_Item.StatusEffectId``, so nothing is mangled out of the name."""
    item = usable.by_template(MANTICORE)
    assert item is not None
    assert item.effect == "cast_mount_manticore_01"
    assert item.is_mount
    assert item.ride == "ride_mount_manticore_01"
    assert len(usable.loaded()) > 2000, "most usable items are not mounts"


def test_the_duration_comes_from_the_item_and_not_the_effect():
    """3.5 seconds on the item, 6.0 on the effect, and 87 ticks on the wire.

    87 / 25 = 3.48, so the item wins. Getting this from the effect table would have
    made the summon nearly twice as long as the animation it plays.
    """
    item = usable.by_template(MANTICORE)
    assert item.duration == 3.5
    assert effects.by_id("cast_mount_manticore_01").duration == 6.0
    assert round(item.duration * 25) == 88, "one tick off what the service sent"


def test_using_it_starts_the_summon_and_the_ride():
    """In that order, with the ride's own ten hours -- meaning until you get off."""
    entries = usable.entries_for(MANTICORE)
    assert [entry.effect for entry in entries] == [
        "cast_mount_manticore_01",
        "ride_mount_manticore_01",
    ]
    assert entries[0].duration == 3.5
    assert entries[1].duration == 36000.0


def test_an_item_that_applies_nothing_is_answered_with_nothing():
    assert usable.entries_for("mount_selection_chest_1") == []
    assert usable.entries_for("no_such_item_at_all") == []


def test_the_world_puts_both_on_the_player_and_on_the_wire():
    world = World()
    world.rules.mobs = 0
    world._ready()
    here = ("1.2.3.4", 5)
    player = world.player(here)
    player.in_world = True

    assert world.use_item(here, "no_such_item_at_all") == []
    assert player.running == []

    world.use_item(here, MANTICORE)
    running = {effects.by_wire(wire).id: seconds for wire, _s, _t, seconds, _c in player.running}
    assert running == {
        "cast_mount_manticore_01": 3.5,
        "ride_mount_manticore_01": 36000.0,
    }

    from dsor import statuseffect

    message = world.effect_message(player, 0)
    assert message is not None
    got = statuseffect.decode(message)
    assert [effects.by_wire(e.index).id for e in got.elements] == [
        "cast_mount_manticore_01",
        "ride_mount_manticore_01",
    ]
    # What the live service put on the wire for the same two: 87 and 900,000.
    assert [element.duration for element in got.elements] == [88, 900000]


def test_the_switch_refuses():
    world = World()
    world.rules.mobs = 0
    world._ready()
    world.rules.usable_items = False
    here = ("1.2.3.4", 5)
    world.player(here).in_world = True
    assert world.use_item(here, MANTICORE) == []
    assert world.player(here).running == []


@pytest.mark.parametrize(
    "template,effect",
    [
        ("mount_common_horse_01", "cast_mount_horse_vc_01"),
        ("mythical_mount_skeleton_dragon_01", "cast_mount_skeleton_dragon_01"),
    ],
)
def test_other_mounts_resolve_the_same_way(template, effect):
    item = usable.by_template(template)
    assert item is not None and item.effect == effect and item.is_mount
