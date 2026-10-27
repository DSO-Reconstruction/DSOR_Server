"""Content revisions are observed off the gameplay hot path.

A warm Kingscity entry used to stat quests.xml, events.json and static.db4
about 2,000 times under the session's mutation gate (2026-10-27 ingress stall
on map entry). Lookups now reuse the last observed revision.
"""

import os
import time
from unittest.mock import patch

import pytest

from dso_server.gameplay import quest_content
from dso_server.paths import DEFAULT_QUESTS_PATH, DEFAULT_STATIC_DB_PATH
from dso_server.runtime import file_revisions


@pytest.fixture
def counted_stats():
    seen = []
    real_stat = os.stat

    def counting(path, *args, **kwargs):
        seen.append(str(path))
        return real_stat(path, *args, **kwargs)

    with patch.object(file_revisions.os, "stat", counting):
        yield seen


def test_first_lookup_stats_then_reuses_the_revision(tmp_path, counted_stats):
    path = tmp_path / "content.xml"
    path.write_text("one", encoding="utf-8")
    first = file_revisions.observed_revision(path)
    for _ in range(100):
        assert file_revisions.observed_revision(path) == first
    assert counted_stats.count(str(path)) == 1


def test_background_recheck_picks_up_an_edit(tmp_path, monkeypatch):
    monkeypatch.setattr(file_revisions, "RECHECK_SECONDS", 0.0)
    path = tmp_path / "content.xml"
    path.write_text("one", encoding="utf-8")
    first = file_revisions.observed_revision(path)
    path.write_text("a longer second revision", encoding="utf-8")
    deadline = time.monotonic() + 5.0
    while file_revisions.observed_revision(path) == first:
        assert time.monotonic() < deadline, "edit was never observed"
        time.sleep(0.01)


def test_invalidate_makes_the_next_lookup_current(tmp_path):
    path = tmp_path / "content.xml"
    path.write_text("one", encoding="utf-8")
    first = file_revisions.observed_revision(path)
    path.write_text("a longer second revision", encoding="utf-8")
    file_revisions.invalidate(path)
    assert file_revisions.observed_revision(path) != first


def test_missing_file_is_remembered_until_it_appears(tmp_path, counted_stats):
    path = tmp_path / "later.xml"
    assert file_revisions.observed_revision(path) is None
    assert file_revisions.observed_revision(path) is None
    assert counted_stats.count(str(path)) == 1
    path.write_text("now", encoding="utf-8")
    file_revisions.invalidate(path)
    assert file_revisions.observed_revision(path) is not None


def test_hot_catalog_lookups_do_not_stat_content_files(counted_stats):
    from dso_server.gameplay.map_event_state import load_map_signal_catalog

    quest_content.load_quest_catalog(DEFAULT_QUESTS_PATH)
    load_map_signal_catalog(DEFAULT_STATIC_DB_PATH)
    counted_stats.clear()
    for _ in range(500):
        quest_content.load_quest_catalog(DEFAULT_QUESTS_PATH)
        load_map_signal_catalog(DEFAULT_STATIC_DB_PATH)
    # At most one background re-check per file and second.
    assert len(counted_stats) <= 4, counted_stats


def test_recheck_started_before_a_write_cannot_restore_the_old_revision(tmp_path, monkeypatch):
    path = tmp_path / "content.xml"
    path.write_text("one", encoding="utf-8")
    old = file_revisions.observed_revision(path)
    key = str(path)
    with file_revisions._lock:
        generation = file_revisions._generations.get(key, 0)
    # The writer replaces the file and invalidates while a re-check that
    # already read the old revision is still finishing.
    path.write_text("a longer second revision", encoding="utf-8")
    file_revisions.invalidate(path)
    new = file_revisions.observed_revision(path)
    monkeypatch.setattr(file_revisions, "_stat_revision", lambda _key: old)
    file_revisions._recheck(key, generation)
    assert new != old
    assert file_revisions.observed_revision(path) == new
