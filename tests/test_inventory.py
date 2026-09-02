"""The inventory command, against the live service's own.

Two real pickup replies were captured on 2026-09-01 from a level 100 character in
``officiel4`` -- 15,851 and 15,971 bytes -- and they are what this codec is measured
against. The measure is severe and it is the point: a BitStream carries no lengths, so a
layout that is one bit out reads plausible-looking strings and drifts. Only landing
exactly on the 32-bit actor and the ``0xFF`` that end every command proves the walk,
and only re-encoding to the same bytes proves it reproduces what it does not
understand.
"""

import pathlib
import struct

import pytest

from dsor import inventory
from dsor.items import item_taken

DATA = pathlib.Path(__file__).resolve().parent.parent / "dsor/data"

#: The live service's replies, and where its walk has to end.
OFFICIAL = (
    ("inventory_official_first.bin", 126_808, 126_768, 0x0001015E, "gem_ruby_e"),
    (
        "inventory_official_second.bin",
        127_768,
        127_728,
        0x00010165,
        "warrior_base_torso_armor_block_speedMovement",
    ),
)


def recorded(name: str) -> bytes:
    return (DATA / name).read_bytes()


@pytest.mark.parametrize("name,bits,ends,item,template", OFFICIAL)
def test_the_walk_lands_on_the_terminator(name, bits, ends, item, template):
    """The whole proof of the grammar, per message.

    The 40 bits left over are the command's actor and its ``0xFF``. A layout that is
    wrong anywhere does not arrive here -- it dies inside a string with an absurd
    length, which is what every earlier reading of this message did.
    """
    raw = recorded(name)
    assert len(raw) * 8 == bits
    got, at = inventory.decode(raw)
    assert at == ends
    assert bits - at == 40, "the actor and the terminator, and nothing else"
    assert got.items, "the merged records"


@pytest.mark.parametrize("name,bits,ends,item,template", OFFICIAL)
def test_it_re_encodes_to_the_same_bytes(name, bits, ends, item, template):
    raw = recorded(name)
    got, _at = inventory.decode(raw)
    assert bytes(inventory.rebuild(raw, got)) == raw


@pytest.mark.parametrize("name,bits,ends,item,template", OFFICIAL)
def test_the_picked_up_item_is_the_one_the_client_asked_for(
    name, bits, ends, item, template
):
    """``PickupItemCommand`` carries four bytes and the reply names them back.

    The client sent ``5e 01 01 00`` and ``65 01 01 00`` for these two, and both the
    leading ``DiscardItemCommand`` and the record inside the inventory name the same
    actor.
    """
    raw = recorded(name)
    assert inventory.discarded(raw) == item
    got, _at = inventory.decode(raw)
    assert item in [record.id for record in got.items]
    only = next(record for record in got.items if record.id == item)
    assert only.template == template


def test_the_stamp_is_the_moment_the_item_was_acquired():
    """Six uint32 at the end of a record: year, month, day, hour, minute, second.

    The gem was picked up during the capture and carries 2026-09-01 22:34:08. The other
    record in the same message -- ammunition the character already had -- carries
    2025-10-12 11:40:51, nearly a year earlier. Nothing but a date behaves like that.
    """
    got, _at = inventory.decode(recorded("inventory_official_first.bin"))
    stamps = {record.template: record.stamped for record in got.items}
    assert stamps["gem_ruby_e"] == (2026, 9, 1, 22, 34, 8)
    assert stamps["ammunition_damage_common"] == (2025, 10, 12, 11, 40, 51)


def test_the_record_carries_where_the_item_lay():
    """And only for the one that was on the ground."""
    got, _at = inventory.decode(recorded("inventory_official_first.bin"))
    lying = next(r for r in got.items if r.template == "gem_ruby_e")
    held = next(r for r in got.items if r.template == "ammunition_damage_common")
    assert [round(value, 2) for value in lying.position] == [-20.71, -6.0, 194.63]
    assert held.position == (0.0, 0.0, 0.0)


def test_the_live_character_wears_fourteen_things():
    """Which is what named the dictionary at ``+0xa8``.

    A character has fourteen equipment slots and this dictionary has fourteen entries,
    with values 0 to 14, in both replies. The one at ``+0x58`` has a hundred and more,
    with values past 140 -- the bag.
    """
    for name, *_rest in OFFICIAL:
        got, _at = inventory.decode(recorded(name))
        assert len(got.equipment) == 14
        assert sorted(slot for _item, slot in got.equipment) == [
            0, 1, 2, 3, 4, 5, 7, 8, 9, 10, 11, 12, 13, 14,
        ]
        assert len(got.placements) > 100
        assert max(slot for _item, slot in got.placements) > 100


def test_the_health_scalar_sits_between_the_two_stats_messages():
    """The measurement that explains the collapsing health bar.

    ``ActorStatsUpdateCommand`` for actor ``0x00010054`` carries 2,616,196 just before
    the second pickup and 2,639,085 just after; the reply carries 2,634,612.5. The
    first pickup agrees the same way against 2,698,962. So the scalar is the player's
    current health, and the recorded reply this server replays carries 236.13 -- a
    level 1 character's.
    """

    def health(name: str) -> float:
        scalars = inventory.decode(recorded(name))[0].scalars
        return struct.unpack(
            "<f", scalars[inventory.HEALTH].to_bytes(4, "little")
        )[0]

    first = health("inventory_official_first.bin")
    second = health("inventory_official_second.bin")
    assert 2_698_962 < first < 2_800_000
    assert 2_616_196 < second < 2_639_085
    ours = inventory.decode(item_taken())[0].scalars
    assert round(
        struct.unpack("<f", ours[inventory.HEALTH].to_bytes(4, "little"))[0], 2
    ) == 236.13


def test_our_own_recorded_reply_round_trips_too():
    """Bit for bit, trailing StatusEffectCommand and byte padding included."""
    raw = item_taken()
    got, at = inventory.decode(raw)
    assert at == 66909
    assert len(got.items) == 1
    assert bytes(inventory.rebuild(raw, got)) == raw


def test_rewriting_one_field_leaves_every_other_byte_alone():
    """The reason to decode rather than splice.

    Everything this server does not understand -- the storage shapes, the equipment
    map, the 247 strings, the trailing status effect -- has to survive being rewritten,
    and re-encoding the collection is what makes that automatic. The splice this
    replaces wrote its own array 352 bits before the array it meant, and shifted the
    rest of the message.
    """
    raw = item_taken()
    before, _at = inventory.decode(raw)
    reply = inventory.picked_up(raw, 0x000104D2, template="gem_ruby_e")
    after, _at = inventory.decode(reply)
    assert after.slots == before.slots
    assert after.equipment == before.equipment
    assert after.names == before.names
    assert after.allocations == before.allocations
    assert after.three_hundred_eighty_fourth == before.three_hundred_eighty_fourth
    assert after.items[0].statistics == before.items[0].statistics
    # And the tail of the message, past the command, is untouched.
    _, ends = inventory.decode(reply)
    assert reply[-90:] == raw[-90:]


def test_a_count_past_the_client_s_own_ceiling_is_refused():
    """``cmp ecx, 0xf4240`` -- the client refuses a million and one, so this does."""
    got = inventory.Inventory(
        names=["x"], scalars=(0,) * 8
    )
    got.two_hundred_forty_eighth = [0] * 3
    assert inventory.encode(got)
    with pytest.raises(ValueError):
        inventory.encode(
            inventory.Inventory(
                names=[], scalars=(0,) * 7
            )
        )


def test_an_item_carries_at_most_a_byte_of_statistics():
    """The count is eight bits wide, so 256 lines cannot be written at all."""
    item = inventory.Item(id=1, template="x")
    item.statistics = [inventory.Statistic("a", 1.0, 0, 0, 0)] * 256
    with pytest.raises(ValueError):
        inventory.encode(inventory.Inventory(items=[item], scalars=(0,) * 8))


def test_the_reply_places_only_the_item_that_was_picked_up():
    """Three bugs the operator reported in one message, and one cause each.

    * "il se met au 3eme slot de mon inventaire alors que le 1er etait vide" -- cells
      below fourteen are the equipment slots, so cell 2 was never a bag cell.
    * "l'epee se desequippe" -- the recorded reply places an item of its own session in
      cell 0, and carrying that across moves whatever the player has there.
    * "a chaque fois que je drop un item meme si les slots sont libres il me fait +1" --
      the cell was a counter that only went up.
    """
    from dsor.world import World

    recorded = item_taken()
    before, _ends = inventory.decode(recorded)
    assert before.placements == [(0x00010001, 0), (0x00010004, 1)]

    reply = inventory.picked_up(recorded, 0x000104D2, slot=2, into="placements")
    after, _ends = inventory.decode(reply)
    assert after.placements == [(0x000104D2, 2)]
    assert after.slots == [], "and the other collections are cleared"

    # One collection at a time, and only ``slots`` states a list of cells -- the one
    # the client searches first, whose first entry it takes as the primary cell.
    for name in inventory.LAYOUTS:
        only = inventory.decode(
            inventory.picked_up(recorded, 0x000104D2, slot=14, into=name)
        )[0]
        written = {
            layout: getattr(only, layout)
            for layout in inventory.LAYOUTS
            if getattr(only, layout)
        }
        wanted = [14] if name == "slots" else 14
        assert written == {name: [(0x000104D2, wanted)]}, written

    world = World()
    world.rules.enforce = False
    here = ("1.2.3.4", 5)
    # The bag's first cell, and the bag is where the operator saw the item land:
    # "il se met au 3eme slot de mon inventaire alors que le 1er etait vide" -- cell 2
    # of the bag with cell 0 free. The equipment slot numbers are a different space
    # entirely; see dsor.equipment.WORN.
    assert world.rules.first_slot == 0
    assert world.rules.bag_layout == "placements"
    assert world.free_cell() == 0

    handed = []
    for index in range(4):
        actor = bytes([0x50 + index, 0x00, 0x01, 0x00])
        world.dropped[actor] = (0.0, 0.0, 0.0)
        out = world.pick_up(here, actor)
        got, _ends = inventory.decode(out[0][1])
        written = getattr(got, world.rules.bag_layout)
        assert len(written) == 1, written
        cell = written[0][1]
        handed.append(cell[0] if isinstance(cell, list) else cell)
    assert handed == [0, 1, 2, 3]

    # A cell that comes free is handed out again, lowest first, which is what the live
    # service does: cell 70 for one pickup and 147 -- its lowest free cell -- for the
    # next.
    world.cells.discard(1)
    assert world.free_cell() == 1
