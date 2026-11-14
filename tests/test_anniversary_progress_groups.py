"""Sequential Anniversary pages use their own collectible and wire group 0."""

import json
from pathlib import Path

from dso_server.gameplay.event_system import load_event_catalog


ROOT = Path(__file__).resolve().parents[1]


def test_anniversary_sequential_pages_and_login(tmp_path):
    schedule = tmp_path / "events.json"
    schedule.write_text(json.dumps({"events": [{
        "event_id": 65549, "template_id": "anniversary_event", "state": "active",
    }]}), encoding="utf-8")
    catalog = load_event_catalog(ROOT / "data/db/static.db4", schedule)
    event = catalog.events[0]
    assert len(event.template.progress_bars) == 1
    bar = event.template.progress_bars[0]
    collectibles = sorted(bar.collectibles, key=lambda value: value.tier)
    assert len(collectibles) == 17
    progress = {}
    empty = catalog.progress_info(progress, player_level=100)
    assert len(empty.events) == 1
    assert empty.events[0].values == (0, 0xFFFFFFFF, 0xFFFFFFFF, 0)

    def advance(item, amount):
        return catalog.advance(progress, collectible_id=item, amount=amount,
                               player_level=100, class_name="Mage")

    for page, collectible in enumerate(collectibles):
        if page < 16:
            assert advance(collectibles[page + 1].item_id, 5) is None
        if page:
            assert advance(collectibles[page - 1].item_id, 5) is None
        start = bar.page_end(page - 1) if page else 0
        partial = advance(collectible.item_id, 5)
        assert partial is not None
        assert partial.progress["amount"] == start + 5
        progress[str(event.event_id)] = partial.progress
        snapshot = catalog.progress_info(progress, player_level=100)
        assert len(snapshot.events) == 1
        assert snapshot.events[0].values[0] == start + 5
        assert snapshot.events[0].values[3] == 0

        completed = advance(collectible.item_id, 100000)
        assert completed is not None
        assert completed.progress["amount"] == bar.page_end(page)
        assert completed.progress["group"] == 0
        assert "groups" not in completed.progress
        assert all(reward.tier == page for reward in completed.item_rewards)
        assert all(reward.tier == page for reward in completed.currency_rewards)
        assert not set(partial.progress["claimed"]) & {
            reward.key for reward in (*completed.item_rewards, *completed.currency_rewards)
        }
        progress[str(event.event_id)] = completed.progress
        assert advance(collectible.item_id, 1) is None


def test_anniversary_migration_keeps_only_unlocked_progress(tmp_path):
    schedule = tmp_path / "events.json"
    schedule.write_text(json.dumps({"events": [{
        "event_id": 65549, "template_id": "anniversary_event", "state": "active",
    }]}), encoding="utf-8")
    catalog = load_event_catalog(ROOT / "data/db/static.db4", schedule)
    old = {"65549": {"amount": 100, "group": 9, "groups": {
        "0": {"amount": 50}, "9": {"amount": 100},
    }}}
    snapshot = catalog.progress_info(old, player_level=100)
    assert snapshot.events[0].values[0] == 50
    assert snapshot.events[0].values[3] == 0
    assert catalog.advance(old, collectible_id="e_anniversary_main_progress_halloween",
                           amount=5, player_level=100, class_name="Mage") is None
