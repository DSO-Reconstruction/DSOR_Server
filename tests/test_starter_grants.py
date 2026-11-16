"""Tests for audited starter grants configuration (membership, currencies, and items)."""

from __future__ import annotations

import json
from pathlib import Path
import pytest

from dso_server.gameplay.starter_config import (
    default_starter_config,
    normalize_starter_config,
    load_starter_config,
    calculate_starter_gold_units,
    calculate_membership_expiry_iso,
    DEFAULT_ANDERMANT,
    DEFAULT_GOLD,
    DEFAULT_MEMBERSHIP_TYPE,
)
from dso_server.protocol.membership_protocol import (
    NO_MEMBERSHIP,
    PREMIUM_MEMBERSHIP,
    DELUXE_MEMBERSHIP,
)
from dso_server.protocol.inventory_codec import (
    encode_bootstrap_inventory_info,
    decode_inventory_info,
)
from dso_server.services.admin_starter_grants import (
    StarterGrantsAdministration,
    StarterConfigConflict,
)


def test_starter_config_normalization():
    empty_norm = normalize_starter_config({})
    assert empty_norm["membership"]["type"] == DEFAULT_MEMBERSHIP_TYPE
    assert empty_norm["currencies"]["andermant"] == DEFAULT_ANDERMANT
    assert len(empty_norm["starter_items"]["shared"]) > 0

    custom = {
        "membership": {"type": PREMIUM_MEMBERSHIP, "duration_days": 30, "active": False},
        "currencies": {
            "andermant": 50000,
            "gold": 1234,
            "silver": 56,
            "copper": 78,
            "badge_of_honor": 9999,
            "draken": 888,
            "glyph_of_power": 777,
            "game_currencies": {"bone_coin": 12345},
        },
        "starter_items": {
            "shared": [
                {"template_id": "test_item_1", "name": "Test Item", "amount": 5, "item_level": 10, "rarity": 4, "slot": "inventory"}
            ],
            "class_equipment": {
                "warrior": [
                    {"template_id": "custom_sword", "slot": 5, "item_level": 20, "rarity": 3}
                ]
            }
        }
    }
    norm = normalize_starter_config(custom)
    assert norm["membership"]["type"] == PREMIUM_MEMBERSHIP
    assert norm["membership"]["duration_days"] == 30
    assert norm["membership"]["active"] is False
    assert norm["currencies"]["andermant"] == 50000
    assert norm["currencies"]["gold"] == 1234
    assert norm["currencies"]["silver"] == 56
    assert norm["currencies"]["copper"] == 78
    assert norm["currencies"]["game_currencies"]["bone_coin"] == 12345
    assert len(norm["starter_items"]["shared"]) == 1
    assert norm["starter_items"]["shared"][0]["template_id"] == "test_item_1"
    assert norm["starter_items"]["class_equipment"]["warrior"][0]["template_id"] == "custom_sword"

    gold_units = calculate_starter_gold_units(norm)
    assert gold_units == (1234 * 10000) + (56 * 100) + 78

    expiry = calculate_membership_expiry_iso(norm)
    assert isinstance(expiry, str) and len(expiry) > 10


def test_starter_grants_administration(tmp_path):
    cfg_file = tmp_path / "starter_config.json"
    admin = StarterGrantsAdministration(service=None, config_path=cfg_file)

    overview = admin.overview()
    rev = overview["revision"]
    assert "config" in overview
    assert "currency_definitions" in overview
    assert "membership_options" in overview
    assert "slots" in overview

    # Update
    updated_doc = overview["config"]
    updated_doc["membership"]["type"] = NO_MEMBERSHIP
    updated_doc["currencies"]["andermant"] = 999_999

    res = admin.update("admin_user", "Testing update", {"revision": rev, "config": updated_doc})
    assert res["ok"] is True
    assert res["config"]["membership"]["type"] == NO_MEMBERSHIP
    assert res["config"]["currencies"]["andermant"] == 999_999

    # Conflict check
    with pytest.raises(StarterConfigConflict):
        admin.update("admin_user", "Stale update", {"revision": "wrong_revision", "config": updated_doc})

    # Reset
    reset_res = admin.reset("admin_user", "Testing reset", res["revision"])
    assert reset_res["ok"] is True
    assert reset_res["config"]["membership"]["type"] == DEFAULT_MEMBERSHIP_TYPE
    assert reset_res["config"]["currencies"]["andermant"] == DEFAULT_ANDERMANT


def test_custom_starter_inventory_encoding():
    custom_cfg = {
        "starter_items": {
            "shared": [
                {"template_id": "test_ring", "name": "Custom Ring", "amount": 1, "item_level": 5, "rarity": 6, "slot": "ring1"},
                {"template_id": "test_potion", "name": "Custom Potion", "amount": 10, "item_level": 1, "rarity": 1, "slot": "inventory"},
            ],
            "class_equipment": {
                "mage": [
                    {"template_id": "mage_custom_wand", "slot": 5, "item_level": 15, "rarity": 4},
                ]
            }
        }
    }
    pkt, bits = encode_bootstrap_inventory_info(
        actor_id=1,
        class_name="mage",
        item_level=1,
        starter_config=custom_cfg,
    )
    inv = decode_inventory_info(pkt, bits)
    template_ids = [item.template_id for item in inv.items]
    assert "test_ring" in template_ids
    assert "test_potion" in template_ids
    assert "mage_custom_wand" in template_ids

    # Verify equipment slot 8 has test_ring
    ring_item = next(i for i in inv.items if i.template_id == "test_ring")
    assert inv.equipment_slots.get(ring_item.item_id) == (8,)

    # Verify storage slot for test_potion
    potion_item = next(i for i in inv.items if i.template_id == "test_potion")
    assert potion_item.item_id in inv.storage_slots
