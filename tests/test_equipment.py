"""What an item is, and what wearing it does.

Both tables come from the client, and the measurement they are checked against is the
torso the operator picked up on the live service during ``officiel4``: item level 125,
tier 5, two rolled statistics named ``item_block_torso`` at 0.8079 and
``item_armor_torso`` at 0.8210.
"""

import pytest

from dsor import equipment as eq
from dsor import inventory
from dsor.world import World

TORSO = "warrior_base_torso_armor_block_speedMovement"


def test_both_tables_are_there():
    assert eq.loaded()
    assert len(eq.pieces()) == 15645
    assert len(eq.enchantments()) == 4108


def test_the_template_says_slot_class_and_what_it_always_carries():
    piece = eq.piece(TORSO)
    assert piece is not None
    assert piece.slot == "TorsoSlot" and piece.wearable
    assert piece.character_class == "warrior"
    assert piece.category == "Armor"
    assert piece.base_enchantments == (
        "item_base_armor_torso",
        "item_base_block_torso",
        "item_base_speed_movement_torso",
    )
    assert piece.droppable_at(1) and piece.droppable_at(200)
    assert not piece.droppable_at(201)


def test_the_modifier_grammar_holds_over_every_row():
    """``Attribute:value[|max],mode[,qualifier...]``, and only 24 attributes exist."""
    shapes = {}
    attributes = set()
    for enchantment in eq.enchantments().values():
        for modifier in enchantment.modifiers:
            attributes.add(modifier.attribute)
            shapes[modifier.mode] = shapes.get(modifier.mode, 0) + 1
    assert len(attributes) == 24, sorted(attributes)
    assert {"absolute", "relative"} <= set(shapes)
    assert "ItemArmor" in attributes and "MaxHealthPoints" in attributes
    # Two modes, and eight rows out of 4,108 that hang a condition off one with an
    # ampersand. Rare enough for a spot check to miss and common enough to break a
    # parser that compares the whole field to "relative".
    assert set(shapes) == {
        "absolute",
        "relative",
        "relative&emperorgold",
        "relative&rarityunique",
    }
    conditional = [
        modifier
        for enchantment in eq.enchantments().values()
        for modifier in enchantment.modifiers
        if modifier.condition
    ]
    assert len(conditional) == 8
    assert all(modifier.relative for modifier in conditional)
    assert {modifier.condition for modifier in conditional} == {
        "emperorgold",
        "rarityunique",
    }


@pytest.mark.parametrize(
    "text,attribute,low,high,mode,qualifiers",
    [
        ("MaxHealthPoints:29.0|85.0,absolute", "MaxHealthPoints", 29.0, 85.0,
         "absolute", ()),
        ("ItemArmor:833.0,absolute", "ItemArmor", 833.0, None, "absolute", ()),
        ("ItemCritical:10.13|36.93,absolute,Rating", "ItemCritical", 10.13, 36.93,
         "absolute", ("Rating",)),
        ("ItemResistance:0.076,relative,DarkMagic", "ItemResistance", 0.076, None,
         "relative", ("DarkMagic",)),
        ("ItemResistance:39999.6|99999,absolute,Fire,Ice,Lightning,Poison,DarkMagic",
         "ItemResistance", 39999.6, 99999.0, "absolute",
         ("Fire", "Ice", "Lightning", "Poison", "DarkMagic")),
    ],
)
def test_a_modifier_reads_back(text, attribute, low, high, mode, qualifiers):
    only, = eq.parse_modifiers(text)
    assert only.attribute == attribute
    assert only.low == pytest.approx(low)
    assert (only.high is None) == (high is None)
    if high is not None:
        assert only.high == pytest.approx(high)
    assert only.mode == mode
    assert only.qualifiers == qualifiers


def test_a_roll_interpolates_a_range_and_refuses_a_single_value():
    """The line this module will not cross.

    ``ItemArmor:0.599|1.498,absolute`` at a roll of 0.821 is 1.337. A modifier stating
    one value does not say what the roll does to it, and there are two readings, so
    asking raises rather than picking one.
    """
    ranged, = eq.parse_modifiers("ItemArmor:0.599|1.498,absolute")
    assert ranged.ranged
    assert ranged.at(0.0) == pytest.approx(0.599)
    assert ranged.at(1.0) == pytest.approx(1.498)
    assert ranged.at(0.8210) == pytest.approx(1.33708, abs=1e-4)
    # Out of range is clamped rather than extrapolated.
    assert ranged.at(-1.0) == pytest.approx(0.599)
    assert ranged.at(2.0) == pytest.approx(1.498)

    single, = eq.parse_modifiers("ItemArmor:0.204,relative")
    assert not single.ranged
    with pytest.raises(ValueError):
        single.at(0.5)


def test_the_torso_can_roll_exactly_what_the_capture_showed():
    """Its two live statistics are in the candidate list, and the base ones are not."""
    candidates = {e.id for e in eq.rolled_for(TORSO, 125)}
    assert "item_armor_torso" in candidates
    assert "item_block_torso" in candidates
    assert "item_base_armor_torso" not in candidates, "base ones do not travel"
    # And nothing belonging to another slot.
    assert not any(name.endswith("_gloves") for name in candidates)


def test_an_item_contributes_named_attributes():
    got = eq.attributes_of(TORSO, {"item_armor_torso": 0.8210})
    assert set(got) == {"ItemArmor", "ItemBlockValue", "ItemSpeed"}
    # The base armour enchantment at its midpoint plus the rolled one's flat value.
    assert got["ItemArmor"] == pytest.approx(1.0485 + 0.204, abs=1e-3)


def test_an_unknown_template_contributes_nothing():
    assert eq.attributes_of("no_such_item") == {}
    assert eq.piece("no_such_item") is None
    assert eq.slot_of("no_such_item") is None
    assert eq.rolled_for("no_such_item") == []


def test_what_can_drop_is_filtered_by_class_slot_and_level():
    warrior = eq.droppable(100, "warrior")
    assert warrior, "a level 100 warrior has things to find"
    for piece in warrior:
        assert piece.wearable and piece.droppable_at(100)
        assert piece.character_class in ("", "warrior")
    rings = eq.droppable(100, "warrior", slot="RingSlot")
    assert rings and all(p.slot == "RingSlot" for p in rings)
    assert len(rings) < len(warrior)


def test_a_kill_leaves_a_real_item_and_the_bag_agrees():
    """The whole point, end to end.

    Every kill used to leave the recording's mace, and the bag used to show the other
    recording's sword. Now the ground and the bag name the same real item of the
    player's own class and level, with statistics its template can actually carry.
    """
    world = World()
    world.rules.mobs = 3
    world._ready()
    world.rules.enforce = False
    here = ("1.2.3.4", 5)
    player = world.player(here)
    player.in_world, player.level, player.health = True, 100, 450_000.0

    world.smite_all(here)
    assert len(world.dropped) == 3
    assert len(world.loot) == 3

    for actor, rolled in world.loot.items():
        piece = eq.piece(rolled.template)
        assert piece is not None, rolled.template
        assert piece.character_class in ("", "warrior")
        assert piece.droppable_at(100)
        assert rolled.level == 100
        assert len(rolled.statistics) == 2
        for line in rolled.statistics:
            assert eq.enchantment(line.name) is not None
            assert 0.0 <= line.value <= 1.0
            assert line.fourth == 100
            assert line.name in {e.id for e in eq.rolled_for(rolled.template, 100)}

    actor = next(iter(world.dropped))
    expected = world.loot[actor]
    out = world.pick_up(here, actor)
    got, _ends = inventory.decode(out[0][1])
    record = got.items[0]
    assert record.template == expected.template
    assert record.level == 100
    assert [line.name for line in record.statistics] == [
        line.name for line in expected.statistics
    ]


def test_the_same_kill_leaves_the_same_thing_twice():
    """Seeded by the drop count, so a drop is reproducible."""
    def once():
        world = World()
        world.rules.mobs = 2
        world._ready()
        world.rules.enforce = False
        here = ("1.2.3.4", 5)
        world.player(here).in_world = True
        world.player(here).level = 100
        world.smite_all(here)
        return [(rolled.template, [round(s.value, 6) for s in rolled.statistics])
                for rolled in world.loot.values()]
    assert once() == once()


def test_the_switch_falls_back_to_the_recorded_blueprint():
    world = World()
    world.rules.mobs = 1
    world._ready()
    world.rules.enforce = False
    world.rules.real_loot = False
    here = ("1.2.3.4", 5)
    world.player(here).in_world = True
    world.smite_all(here)
    assert world.loot == {}
    assert set(world.templates.values()) == {
        "warrior_base_rh_mace_speedAttack_damage"
    }
