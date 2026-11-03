"""CMS equipment editor: enchantments, enchantment tiers and base-stat rolls."""

from itertools import islice
import json
from urllib.parse import quote

import pytest

from dso_server.gameplay.item_random import (
    base_enchantment_seed, deterministic_item_rolls, leading_item_rolls,
)
from dso_server.gameplay.player_combat_stats import (
    _item_instructions, _resolved_range, prime_player_combat_database,
)
from dso_server.paths import DEFAULT_STATIC_DB_PATH
from dso_server.persistence.account_state import AccountStateStore
from dso_server.protocol.inventory_codec import INVENTORY_INFO_COMMAND
from dso_server.protocol.inventory_economy_protocol import refresh_item_economy_values
from dso_server.protocol.inventory_codec import clone_item_info
from dso_server.services import admin_item_editor
from dso_server.services.admin_item_editor import ItemEditor
from dso_server.tests.test_admin_cms import cms, equipment_booking, login, request
from dso_server.tests.test_admin_live import commands, online


STAFF = {"item_level": 100, "rarity": 3, "enchantment_tier": 3, "roll_quality": 50}


def staff(cms):
    equipment_booking(cms, STAFF)
    cms.service.delivery.deliver(42)
    return cms.store.get(42).inventory.items[-1]


def edit(cms, item, changes, reason="Support ticket 9"):
    return cms.service.post(f"/api/characters/42/inventory/{item.item_id}/edit",
                            {"changes": changes, "reason": reason}, cms.admin, "")


def draft(document, **changes):
    return {"base_enchantment_tier": document["base"]["tier"], "base_seed": document["base"]["seed"],
            "enchantments": [{key: e[key] for key in ("template_id", "value", "tier", "kind", "origin_item_level")}
                             for e in document["enchantments"]], **changes}


@pytest.mark.parametrize("seed", [0, 1, 6172, 0x7FFF, 0xFFFFFFFF])
def test_leading_rolls_match_the_client_stream(seed):
    for count in (0, 1, 2, 3, 8, 147, 148, 149):
        assert leading_item_rolls(seed, count) == tuple(islice(deterministic_item_rolls(seed), count))


def test_preview_values_are_the_combat_values(cms):
    item = staff(cms)
    document = ItemEditor(DEFAULT_STATIC_DB_PATH, item, "mage").document()
    assert document["editable"] and len(document["enchantments"]) == 3
    assert [stat["roll"] is not None for stat in document["base"]["stats"]] == [True, True]
    combat = prime_player_combat_database(DEFAULT_STATIC_DB_PATH)
    skip = len(combat.items[item.template_id].modifiers)
    expected = [_resolved_range(instruction, {})[1] for instruction in _item_instructions(combat, item)[skip:]]
    shown = [stat["value"] for stat in document["base"]["stats"]] + [
        line["value"] for enchantment in document["enchantments"] for line in enchantment["lines"]]
    assert shown == pytest.approx(expected)
    for stat in document["base"]["stats"]:
        assert stat["minimum"] <= stat["value"] <= stat["maximum"]


def test_edit_is_saved_audited_and_survives_reload(cms):
    item = staff(cms)
    document = ItemEditor(DEFAULT_STATIC_DB_PATH, item, "mage").document()
    current = {e["template_id"] for e in document["enchantments"]}
    other = next(o["template_id"] for o in document["options"]["enchantments"] if o["template_id"] not in current)
    changes = draft(document, base_enchantment_tier=16, base_seed=1234)
    changes["enchantments"][0] = {"template_id": other, "value": 1.0, "tier": 16, "kind": 4, "origin_item_level": 100}
    changes["enchantments"].pop()
    result = edit(cms, item, changes)
    assert result == {"ok": True, "item_id": item.item_id, "template_id": item.template_id,
                      "online": False, "equipped": False}
    reloaded = AccountStateStore(cms.store.path)
    try:
        saved = next(i for i in reloaded.get(42).inventory.items if i.item_id == item.item_id)
    finally:
        reloaded.close()
    assert (saved.base_enchantment_tier, saved.field_18) == (16, 1234)
    assert [(e.template_id, e.value, e.tier, e.kind) for e in saved.enchantments] == [
        (other, 1.0, 16, 4), *[(e.template_id, e.value, e.tier, e.kind) for e in item.enchantments[1:2]]]
    assert saved.stack_vc_value == item.stack_vc_value and saved.socket_count == item.socket_count
    assert saved.glyph_value == refresh_item_economy_values(
        DEFAULT_STATIC_DB_PATH, clone_item_info(saved), force_melt_value=True).glyph_value
    after = ItemEditor(DEFAULT_STATIC_DB_PATH, saved, "mage").document()
    assert all(stat["quality"] == 1.0 for stat in after["base"]["stats"] if stat["tier_factor"])
    audit = next(a for a in cms.service.repository.history("admin_audit")["rows"]
                 if a["action"] == "character.inventory.item.edit")
    assert audit["details"]["before"]["base_seed"] == item.field_18
    assert audit["details"]["after"]["base_enchantment_tier"] == 16
    assert audit["details"]["reason"] == "Support ticket 9"
    with pytest.raises(ValueError, match="no changes"):
        edit(cms, saved, draft(after))


@pytest.mark.parametrize("change, message", [
    ({"enchantments": [{"template_id": "item_resistance_fire_amulet", "value": 1}]}, "cannot be placed"),
    ({"enchantments": [{"template_id": "item_damage_rh_staff_damage", "value": 1.5}]}, "roll"),
    ({"enchantments": [{"template_id": "item_damage_rh_staff_damage", "value": True}]}, "roll"),
    ({"enchantments": [{"template_id": "item_damage_rh_staff_damage", "value": 1, "tier": 999}]}, "tier"),
    ({"enchantments": [{"template_id": "item_damage_rh_staff_damage", "value": 1, "kind": 8}]}, "rarity"),
    ({"enchantments": [{"template_id": "item_damage_rh_staff_damage", "value": 1}] * 9}, "at most"),
    ({"base_seed": 0x8000}, "seed"),
    ({"base_enchantment_tier": 999}, "base enchantment tier"),
    ({"base_target": 1.0}, "preview"),
    ({"rarity": 4}, "unknown item change"),
])
def test_invalid_edits_are_rejected(cms, change, message):
    item = staff(cms)
    with pytest.raises(ValueError, match=message):
        edit(cms, item, change)
    assert cms.store.get(42).inventory.items[-1] == item


def test_base_target_picks_the_closest_roll(cms, monkeypatch):
    # A slice of the 16,384-seed table keeps the test fast and the search exhaustive.
    table = tuple(leading_item_rolls(seed, admin_item_editor.TABLE_ROLLS) for seed in range(512))
    monkeypatch.setattr(admin_item_editor, "_roll_table", table)
    item = staff(cms)
    editor = ItemEditor(DEFAULT_STATIC_DB_PATH, item, "mage")

    def gaps(seed_idx, target):
        qualities = [s["quality"] for s in editor.document(
            editor.apply({"base_seed": seed_idx}))["base"]["stats"] if s["quality"] is not None]
        return max(abs(q - target) for q in qualities), sum(abs(q - target) for q in qualities)

    for target in (0.0, 0.5, 1.0):
        chosen = editor.apply({"base_target": target}).field_18
        assert base_enchantment_seed(chosen) < 512 or chosen == item.field_18
        best = min(gaps(2 * seed + 1, target) for seed in range(512))
        assert gaps(chosen, target) == pytest.approx(min(best, gaps(item.field_18, target)))
    assert gaps(editor.apply({"base_target": 1.0}).field_18, 1.0)[0] < 0.15
    # Tier 16 fixes every tier-scaled base roll at 100%, so the seed stays.
    assert editor.apply({"base_enchantment_tier": 16, "base_target": 1.0}).field_18 == item.field_18


def test_editor_route_previews_without_saving(cms):
    item = staff(cms)
    cookie, _ = login(cms)
    path = f"/api/characters/42/inventory/{item.item_id}/editor"
    status, current, _ = request(cms, "GET", path, cookie=cookie)
    assert status == 200 and current["editable"] and current["online"] is False
    changes = draft(current, base_enchantment_tier=16)
    changes["enchantments"][0]["value"] = 1.0
    status, preview, _ = request(cms, "GET", f"{path}?draft={quote(json.dumps(changes))}", cookie=cookie)
    assert status == 200
    assert preview["base"]["tier"] == 16 and preview["enchantments"][0]["value"] == 1.0
    assert preview["enchantments"][0]["lines"][0]["value"] > current["enchantments"][0]["lines"][0]["value"]
    assert cms.store.get(42).inventory.items[-1] == item
    status, error, _ = request(cms, "GET", f"{path}?draft={quote(json.dumps({'base_seed': 0}))}", cookie=cookie)
    assert status == 400 and "seed" in error["error"]


def test_live_edit_replaces_the_session_item_and_sends_inventory(cms, online):
    _, s = online
    equipment_booking(cms, STAFF)
    item = s.gameplay_inventory.items[-1]
    document = ItemEditor(DEFAULT_STATIC_DB_PATH, item, "mage").document()
    changes = draft(document)
    changes["enchantments"][1]["value"] = 1.0
    previous = s.gameplay_inventory
    assert edit(cms, item, changes)["online"] is True
    assert s.gameplay_inventory is not previous
    live = next(i for i in s.gameplay_inventory.items if i.item_id == item.item_id)
    assert live.enchantments[1].value == 1.0 and live.enchantments[0] == item.enchantments[0]
    sent = commands(s)
    assert [c.command_id for c in sent] == [INVENTORY_INFO_COMMAND]
    packet = sent[0].value
    assert packet.changed_item_ids == [] and packet.field_1e0 is False
    assert next(i for i in packet.items if i.item_id == item.item_id).enchantments == live.enchantments
    assert cms.store.get(42).inventory.items[-1].enchantments == live.enchantments
