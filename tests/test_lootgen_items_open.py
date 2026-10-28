"""Boxes, Lucky Spheres and Deluxe teasers the player could not use.

FAILURE: "y'a des items que je peux pas utiliser genre les lucky sphere le
deluxe le daily limited chest".
"""

from datetime import datetime, timedelta, timezone
import random

import pytest

from dso_server.paths import DEFAULT_STATIC_DB_PATH
from dso_server.protocol.chest_loot_protocol import roll_chest_contents
from dso_server.protocol.gameplay_protocol import InventoryItemAction, encode_inventory_item_action
from dso_server.protocol.inventory_codec import InventoryInfo, ItemInfo
from dso_server.protocol.membership_protocol import DELUXE_MEMBERSHIP, parse_membership_expiry
from dso_server.services.membership_items import extended_expiry, teaser_days
from dso_server.tests import test_achievements as achievement_fixtures


@pytest.mark.parametrize("box,expected", [
    # "Open to receive one of the following: 1x Winter Solstice Breeze ..."
    # (description only: chest_contents_descriptions.json).
    ("emilia_daily_andermant_cost", None),
    # A listed Lucky Sphere without odds: one of its items, evenly.
    ("ls_mount_warthog_c", None),
    # A category Lucky Sphere: a pet of the Free Spirits sticker book.
    ("ls_pets_stickerbook_free_spirits_p", "pet_"),
    # The daily challenges' chests: an equipment item of their rarity.
    ("lootbox_daily_challenge_rare", None),
])
def test_boxes_without_a_server_loot_list_open(box, expected):
    contents = roll_chest_contents(DEFAULT_STATIC_DB_PATH, box, player_level=60, class_name="warrior",
                                   rng=random.Random(7))
    assert contents.rewards
    if expected:
        assert all(r.definition.template_id.startswith(expected) for r in contents.rewards)


def test_deluxe_teaser_durations_and_extension():
    assert teaser_days("deluxe_teaser_one_day") == 1
    assert teaser_days("deluxe_teaser_three_days_consumable_7d") == 3
    assert teaser_days("deluxe_teaser_one_year") == 365
    assert teaser_days("ls_dyes_c") is None
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    later = (now + timedelta(days=10)).isoformat()
    # Extended from the current Deluxe expiry ...
    assert parse_membership_expiry(extended_expiry(DELUXE_MEMBERSHIP, True, later, 1, now)) == now + timedelta(days=11)
    # ... or from now when it has lapsed.
    past = (now - timedelta(days=3)).isoformat()
    assert parse_membership_expiry(extended_expiry(DELUXE_MEMBERSHIP, True, past, 7, now)) == now + timedelta(days=7)


def test_using_a_deluxe_teaser_consumes_it_and_extends_deluxe():
    case = achievement_fixtures.AchievementServiceTests()
    case.setUp()
    try:
        server, session = case.server, case.session
        session.gameplay_inventory = InventoryInfo(
            items=[ItemInfo(item_id=51, template_id="deluxe_teaser_one_week", amount=1)], storage_58={51: 0},
        )
        session.gameplay_next_item_id = 52
        before = parse_membership_expiry(session.gameplay_membership_expires_at)
        payload, _ = encode_inventory_item_action(InventoryItemAction(51, 0x13, 0, 0, 0))
        server._handle_inventory_item_action(session, payload)
        assert not any(item.item_id == 51 for item in session.gameplay_inventory.items)
        assert session.gameplay_membership_type == DELUXE_MEMBERSHIP
        assert parse_membership_expiry(session.gameplay_membership_expires_at) - before == timedelta(days=7)
    finally:
        case.doCleanups()
