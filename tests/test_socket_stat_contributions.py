"""Real socket programs contribute each authored stat with item-local scope."""

from dataclasses import replace

import pytest

from dso_server.gameplay.player_combat_stats import (
    CombatModifier, _calculate_player_combat_stats, prime_player_combat_database,
)
from dso_server.paths import DEFAULT_STATIC_DB_PATH
from dso_server.protocol.inventory_codec import InventoryInfo, ItemInfo
from dso_server.protocol.socket_effect_protocol import SocketEffectDatabase


@pytest.fixture(scope="module")
def database():
    return prime_player_combat_database(DEFAULT_STATIC_DB_PATH)


def inventory(*socket_groups):
    return InventoryInfo(
        items=[ItemInfo(item_id=i + 1, item_level=100,
                        template_id="all_base_trinket_rune_01",
                        socketed_templates=list(group))
               for i, group in enumerate(socket_groups)],
        equipment_slots={i + 1: (14 + i,) for i in range(len(socket_groups))},
    )


def stats(database, inv, sources=()):
    return _calculate_player_combat_stats(
        database, class_name="mage", level=100, inventory=inv,
        modifier_sources=tuple(sources),
    )


@pytest.mark.parametrize("gem,expected", [
    ("gem_faster_run_q", 0.17),
    ("gem_critical_damage_fasterRun_q", 0.1275),
])
def test_movement_gems_increase_uncapped_speed(database, gem, expected):
    empty = stats(database, inventory(()))
    socketed = stats(database, inventory((gem,)))
    assert socketed.movement_speed - empty.movement_speed == pytest.approx(expected)
    assert socketed.movement_bonus_speed == 0


def test_combined_gem_contributes_damage_critical_and_movement(database):
    plain = stats(database, inventory(()))
    combined = stats(database, inventory(("gem_critical_damage_fasterRun_q",)))
    assert combined.minimum_damage - plain.minimum_damage == pytest.approx(525)
    assert combined.critical_hit_chance > plain.critical_hit_chance
    assert combined.movement_speed - plain.movement_speed == pytest.approx(0.1275)


def test_item_movement_multiplier_applies_only_to_its_own_sockets(database):
    template = database.items["all_base_trinket_rune_01"]
    local_template = replace(template, template_id="test_local_speed", scale_with_level=False,
        modifiers=(CombatModifier("ItemSpeed", ("0.5", "relative", "Movement")),))
    db = replace(database, items={**database.items, local_template.template_id: local_template})
    inv = inventory(("gem_faster_run_q",), ("gem_faster_run_q",))
    inv.items[0].template_id = local_template.template_id
    actor_speed = (CombatModifier("Speed", ("0.2", "relative", "Movement")),)
    actual = stats(db, inv, actor_speed)
    assert actual.movement_speed == pytest.approx((5 + 0.17 * 1.5 + 0.17) * 1.2)
    inv.items[0].socketed_templates = []
    assert stats(db, inv, actor_speed).movement_speed == pytest.approx((5 + 0.17) * 1.2)


def test_bag_sockets_do_not_increase_speed_and_two_slots_do_not_duplicate_item(database):
    inv = inventory(("gem_faster_run_q",), ("gem_faster_run_q",))
    inv.equipment_slots = {1: (5, 6)}
    assert stats(database, inv).movement_speed == pytest.approx(5.17)


def test_movement_rune_multiplies_item_movement_and_preserves_bonus_cap(database):
    inv = inventory(("gem_faster_run_q",), ("unique_gem_faster_run_04",))
    assert stats(database, inv).movement_speed == pytest.approx(5.17 * 1.065)
    capped = stats(database, inv, (
        CombatModifier("Speed", ("20", "absolute", "Movement")),
        CombatModifier("Speed", ("5", "absolute", "MovementBonus")),
    ))
    assert capped.movement_speed == 13
    assert capped.movement_bonus_speed == 5


def test_combined_resistance_damage_block_rune_contributes_all_three(database):
    sources = (CombatModifier("BlockValue", ("1000", "absolute")),)
    plain = stats(database, inventory(()), sources)
    combined = stats(database, inventory(("unique_gem_resistance_damage_block_04",)), sources)
    assert combined.minimum_damage == pytest.approx(plain.minimum_damage * 1.065)
    assert combined.block_chance == pytest.approx(plain.block_chance * 1.065)
    assert dict(combined.resistance_ratings) == pytest.approx({
        key: value * 1.065 for key, value in plain.resistance_ratings
    })


def test_combined_ice_critical_health_rune_contributes_all_three(database):
    sources = (CombatModifier("CriticalValue", ("1000", "absolute")),)
    plain = stats(database, inventory(()), sources)
    combined = stats(database, inventory(("unique_gem_resistance_ice_critical_hitpoints_04",)), sources)
    assert combined.critical_hit_chance == pytest.approx(plain.critical_hit_chance * 1.065)
    assert combined.maximum_health == round(plain.maximum_health * 1.065)
    assert dict(combined.resistance_ratings)["ice"] == pytest.approx(dict(plain.resistance_ratings)["ice"] * 1.065)
    assert dict(combined.resistance_ratings)["fire"] == dict(plain.resistance_ratings)["fire"]


def test_jewel_direct_damage_and_combined_rune_drop_and_damage_bonuses(database):
    plain = stats(database, inventory(()))
    pirate = stats(database, inventory(("jewel_buff_pirate_epic",)))
    assert pirate.minimum_damage == pytest.approx(plain.minimum_damage * 1.1)
    inv = inventory(("unique_gem_run_damage_berry_hal_04",))
    combined = stats(database, inv)
    assert combined.minimum_damage == pytest.approx(plain.minimum_damage * 1.065)
    assert combined.movement_speed == pytest.approx(plain.movement_speed * 1.065)
    sockets = SocketEffectDatabase(DEFAULT_STATIC_DB_PATH).evaluate_inventory(inv)
    assert sockets.drop_amount_bonus("money_rc") == pytest.approx((0, 0.05))
