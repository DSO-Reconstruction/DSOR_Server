"""Combat never waits on the filesystem for the static database revision.

Casts and monster ticks used to ``stat()`` static.db4 themselves. One such
call took three seconds on a volume spanning two hard disks, and the cast and
the map's simulation, both waiting on it, froze combat for that long. Only the
first call for a path may touch the file; later checks run in the background,
and a replaced database is parsed before callers switch to it.
"""

from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from dso_server.gameplay import player_combat_stats as stats


class CombatDatabaseRevisionTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "static.db4"
        self.path.write_bytes(b"one")
        self.key = str(self.path.resolve())
        self.loaded: list[tuple[int, int]] = []

        def load(key, modified_ns, file_size):
            self.loaded.append((modified_ns, file_size))
            return ("database", file_size)

        loader = patch.object(stats, "_load_static_database", side_effect=load)
        loader.start()
        self.addCleanup(loader.stop)
        self.addCleanup(self._forget)
        self.assertEqual(
            stats.prime_player_combat_database(self.path), ("database", 3)
        )

    def _forget(self) -> None:
        with stats._combat_database_revision_lock:
            stats._combat_database_revisions.pop(self.key, None)
            stats._combat_database_checked_at.pop(self.key, None)
            stats._combat_database_rechecks.discard(self.key)

    def _wait_for_recheck(self) -> None:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            with stats._combat_database_revision_lock:
                if self.key not in stats._combat_database_rechecks:
                    return
            time.sleep(0.01)
        self.fail("the background check did not finish")

    def test_callers_after_the_first_never_wait_on_the_file(self):
        real_revision = stats._combat_database_revision
        release = threading.Event()

        def slow_revision(path):
            release.wait(5.0)
            return real_revision(path)

        with patch.object(
            stats, "_combat_database_revision", side_effect=slow_revision
        ) as revision:
            # Within the interval nothing touches the file at all.
            stats.prime_player_combat_database(self.path)
            self.assertEqual(revision.call_count, 0)

            with patch.object(stats, "_COMBAT_DATABASE_RECHECK_SECONDS", 0.0):
                started = time.perf_counter()
                first = stats.prime_player_combat_database(self.path)
                second = stats.prime_player_combat_database(self.path)
                elapsed = time.perf_counter() - started
            release.set()
            self._wait_for_recheck()

        self.assertEqual((first, second), (("database", 3), ("database", 3)))
        self.assertLess(elapsed, 1.0)
        self.assertEqual(revision.call_count, 1)

    def test_replaced_database_is_loaded_before_callers_switch(self):
        self.path.write_bytes(b"replaced")
        with patch.object(stats, "_COMBAT_DATABASE_RECHECK_SECONDS", 0.0):
            self.assertEqual(
                stats.prime_player_combat_database(self.path), ("database", 3)
            )
            self._wait_for_recheck()
            self.assertIn(8, [size for _modified, size in self.loaded])
            self.assertEqual(
                stats.prime_player_combat_database(self.path), ("database", 8)
            )
            self._wait_for_recheck()

    def test_failed_check_keeps_serving_the_loaded_revision(self):
        with patch.object(
            stats, "_combat_database_revision", side_effect=OSError("busy")
        ), patch.object(
            stats, "_COMBAT_DATABASE_RECHECK_SECONDS", 0.0
        ), patch("builtins.print") as printed:
            self.assertEqual(
                stats.prime_player_combat_database(self.path), ("database", 3)
            )
            self._wait_for_recheck()

        self.assertIn("could not re-check", printed.call_args.args[0])
        self.assertEqual(
            stats.prime_player_combat_database(self.path), ("database", 3)
        )


if __name__ == "__main__":
    unittest.main()
