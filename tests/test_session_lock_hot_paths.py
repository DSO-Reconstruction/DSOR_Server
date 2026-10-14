"""Guards for the per-packet work that ran on a shared session lock.

Every gameplay thread contends for a player's ``flow_lock``: the map's single
25 Hz simulation worker reads each viewer's clock and spawned set through it,
the projectile deadline workers commit impacts through it, and the ingress
workers apply movement through it. Anything expensive holding that lock is
therefore not one player's problem -- it delays hit registration and monster
targeting for **everyone on the map**, and it gets worse as the map fills up.

Three regressions live here:

- the account snapshot deep-copied the entire account inside the critical
  section, once per cast (6.5 ms p50 at 251 inventory items);
- combat raised that snapshot for every skill-resource spend, several times a
  second per player, even though resource is regenerating state;
- the ActorFeature victim gates re-parsed raw ``StartModifiers`` text per
  active effect, twice per MoveCommand, twice per cast, and once per candidate
  viewer per monster tick.
"""

from __future__ import annotations

import copy
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from dso_server.paths import (
    DEFAULT_STATIC_DB_PATH,
)
from dso_server.services.character_server import CharacterServer
from dso_server.tests.fixtures.characters import character_server
from dso_server.runtime.context import (
    COALESCED_ACCOUNT_SAVE_INTERVAL_SECONDS,
    COALESCED_ACCOUNT_SAVE_REASONS,
)
from dso_server.services.base import CharacterService
from dso_server.runtime.transport import Session
from dso_server.gameplay.local_combat_state import parse_disabled_actor_features


class FakeSocket:
    def __init__(self) -> None:
        self.sent: list[tuple[bytes, tuple[str, int]]] = []

    def sendto(self, data: bytes, address: tuple[str, int]) -> None:
        self.sent.append((data, address))


class _ObservableLock:
    """Records the wall time each holder kept the lock."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._depth = 0
        self._acquired_at = 0.0
        self.holds: list[float] = []

    def acquire(self, *args, **kwargs):
        acquired = self._lock.acquire(*args, **kwargs)
        if acquired:
            if not self._depth:
                self._acquired_at = time.perf_counter()
            self._depth += 1
        return acquired

    def release(self) -> None:
        self._depth -= 1
        if not self._depth:
            self.holds.append(time.perf_counter() - self._acquired_at)
        self._lock.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *_args) -> None:
        self.release()


class AccountSnapshotLockTests(unittest.TestCase):
    """``_capture_account_state`` must not deep-copy under ``flow_lock``."""

    def setUp(self) -> None:
        self.sock = FakeSocket()
        self.server = character_server(self)
        self.server.sock = self.sock
        self.session = Session(("127.0.0.1", 40000), self.sock)
        self.session.gameplay_mode = True
        self.session.gameplay_map = "a0200_kingscity"
        self.session.gameplay_character = (
            self.server.character_selection.active_character
        )
        self.session.gameplay_template_row_index = (
            self.server.player_template_rows[
                self.session.gameplay_character.class_id
            ]
        )
        self.server._send_local_player_initialization(self.session)
        self.sock.sent.clear()

    def test_no_deepcopy_runs_while_the_session_lock_is_held(self):
        held = threading.local()
        held.depth = 0
        violations = []
        original_deepcopy = copy.deepcopy
        original_lock = self.session.flow_lock

        class TrackingLock:
            def acquire(self, *args, **kwargs):
                acquired = original_lock.acquire(*args, **kwargs)
                if acquired:
                    held.depth = getattr(held, "depth", 0) + 1
                return acquired

            def release(self):
                held.depth = getattr(held, "depth", 0) - 1
                original_lock.release()

            def __enter__(self):
                self.acquire()
                return self

            def __exit__(self, *_args):
                self.release()

        def tracking_deepcopy(value, *args, **kwargs):
            if getattr(held, "depth", 0) > 0:
                violations.append(type(value).__name__)
            return original_deepcopy(value, *args, **kwargs)

        self.session.flow_lock = TrackingLock()
        try:
            with patch("copy.deepcopy", side_effect=tracking_deepcopy):
                state = self.server._capture_account_state(self.session)
        finally:
            self.session.flow_lock = original_lock

        self.assertIsNotNone(state)
        self.assertEqual(
            violations,
            [],
            "deep copies ran inside the session critical section: "
            f"{sorted(set(violations))}",
        )

    def test_the_snapshot_is_still_fully_detached(self):
        """Cheapness must not come from sharing live gameplay objects."""

        state = self.server._capture_account_state(self.session)
        self.assertIsNotNone(state)
        live = self.session.gameplay_inventory

        self.assertIsNot(state.inventory, live)
        self.assertIsNot(state.inventory.items, live.items)
        self.assertIsNot(
            state.inventory.equipment_slots, live.equipment_slots
        )
        for snapshot_item, live_item in zip(state.inventory.items, live.items):
            self.assertIsNot(snapshot_item, live_item)
            self.assertIsNot(
                snapshot_item.enchantments, live_item.enchantments
            )
            self.assertIsNot(
                snapshot_item.socketed_templates,
                live_item.socketed_templates,
            )

        # A later gameplay mutation must not reach the detached snapshot.
        before = copy.deepcopy(state.inventory)
        live.items[0].amount += 7
        live.items[0].socketed_templates.append("gem_ruby_i")
        live.equipment_slots.clear()
        live.storage_58.clear()
        self.assertEqual(state.inventory, before)

    def test_the_lock_is_held_for_a_small_fraction_of_the_snapshot(self):
        """The expensive half of the snapshot must be outside the lock.

        Deliberately a ratio rather than an absolute bound: absolute timings
        are host dependent, but "most of this work happens without the lock"
        is the property that keeps one player's persistence from stalling the
        map. The previous implementation held the lock for essentially the
        whole call.
        """

        observable = _ObservableLock()
        original_lock = self.session.flow_lock
        self.session.flow_lock = observable
        try:
            # Warm content/database caches so the measurement is the copy.
            self.server._capture_account_state(self.session)
            observable.holds.clear()
            started = time.perf_counter()
            self.server._capture_account_state(self.session)
            total = time.perf_counter() - started
        finally:
            self.session.flow_lock = original_lock

        self.assertTrue(observable.holds)
        locked = sum(observable.holds)
        self.assertLess(
            locked,
            total * 0.75,
            "the session lock covered "
            f"{locked / max(total, 1e-9):.0%} of the snapshot",
        )


class CoalescedAccountSaveTests(unittest.TestCase):
    """Regenerating state must not raise a full snapshot per cast."""

    def setUp(self) -> None:
        self.calls = []
        self.session = SimpleNamespace(
            flow_lock=threading.RLock(),
            gameplay_last_coalesced_save_at=0.0,
            gameplay_coalesced_saves_skipped=0,
            gameplay_persist_callback=(
                lambda session, reason, flush: self.calls.append(
                    (reason, flush)
                )
                or True
            ),
        )

    def test_skill_resource_is_the_coalesced_reason(self):
        self.assertIn(
            "skill resource changed", COALESCED_ACCOUNT_SAVE_REASONS
        )
        self.assertGreater(COALESCED_ACCOUNT_SAVE_INTERVAL_SECONDS, 0.0)

    def test_a_burst_of_casts_produces_one_snapshot(self):
        accepted = [
            CharacterService._request_account_save(
                self.session, "skill resource changed"
            )
            for _ in range(20)
        ]

        self.assertEqual(len(self.calls), 1)
        self.assertEqual(accepted.count(True), 1)
        self.assertEqual(
            self.session.gameplay_coalesced_saves_skipped, 19
        )

    def test_the_interval_reopens_the_window(self):
        CharacterService._request_account_save(
            self.session, "skill resource changed"
        )
        self.session.gameplay_last_coalesced_save_at = (
            time.perf_counter()
            - COALESCED_ACCOUNT_SAVE_INTERVAL_SECONDS
            - 0.01
        )
        CharacterService._request_account_save(
            self.session, "skill resource changed"
        )

        self.assertEqual(len(self.calls), 2)

    def test_durable_reasons_are_never_coalesced(self):
        for _ in range(5):
            CharacterService._request_account_save(
                self.session, "equipment changed", flush=True
            )
            CharacterService._request_account_save(
                self.session, "world loot pickup"
            )

        self.assertEqual(len(self.calls), 10)

    def test_an_explicit_flush_bypasses_coalescing(self):
        for _ in range(4):
            CharacterService._request_account_save(
                self.session, "skill resource changed", flush=True
            )

        self.assertEqual(len(self.calls), 4)


class DisabledActorFeatureTests(unittest.TestCase):
    """The victim gates must read a memo, not re-parse authored text."""

    def test_the_parser_matches_the_authored_content_forms(self):
        self.assertEqual(
            parse_disabled_actor_features(
                "ActorFeature:off,Movement,Skills,Damage"
            ),
            frozenset({"movement", "skills", "damage"}),
        )
        self.assertEqual(
            parse_disabled_actor_features(
                "ActorFeature:off,Damage,Targetable"
            ),
            frozenset({"damage", "targetable"}),
        )
        # An "on" program grants a feature; it must not read as a lock.
        self.assertEqual(
            parse_disabled_actor_features("ActorFeature:on,Movement"),
            frozenset(),
        )
        self.assertEqual(parse_disabled_actor_features(""), frozenset())
        self.assertEqual(
            parse_disabled_actor_features("Speed:-1.0,x,Movement"),
            frozenset(),
        )

    def test_a_repeated_gate_query_does_not_reparse(self):
        combat = _CountingCombat()
        session = _GateSession(combat, effect_ids=(11, 22))

        for _ in range(50):
            CharacterService._player_victim_feature_locked(
                session, "movement"
            )
            # ``_player_is_untargetable`` asks two more feature questions.
            CharacterService._player_is_untargetable(session)

        # One derivation for the whole run: two effect ids, looked up once.
        self.assertEqual(
            combat.lookups,
            2,
            "the per-player disabled-feature set was rebuilt per query",
        )
        self.assertEqual(
            combat.program_reads,
            2,
            "the ActorFeature program was re-parsed per query",
        )

    def test_adding_an_effect_invalidates_the_cache(self):
        combat = _CountingCombat()
        session = _GateSession(combat, effect_ids=(11,))

        self.assertFalse(
            CharacterService._player_victim_feature_locked(session, "damage")
        )
        combat.programs[33] = "ActorFeature:off,Damage"
        session.effect_ids = (11, 33)
        self.assertTrue(
            CharacterService._player_victim_feature_locked(session, "damage")
        )
        session.effect_ids = (11,)
        self.assertFalse(
            CharacterService._player_victim_feature_locked(session, "damage")
        )

    def test_the_gate_path_does_not_build_modifier_sources(self):
        """Effect ids are all the gate needs.

        Building the full modifier tuple also materialized item-set modifiers
        and parsed every socket cast rule -- none of which carry an
        ``effect_id``. The map's simulation thread did that per viewer, 25
        times a second.
        """

        combat = _CountingCombat()
        session = _GateSession(combat, effect_ids=(11,))

        with patch.object(
            CharacterService,
            "_active_skill_modifier_sources",
            staticmethod(
                lambda _session: (_ for _ in ()).throw(
                    AssertionError(
                        "the ActorFeature gate built modifier sources"
                    )
                )
            ),
        ):
            CharacterService._player_victim_feature_locked(
                session, "movement"
            )


class _CountingCombat:
    """Minimal map exposing the memoized ActorFeature lookup."""

    def __init__(self) -> None:
        self.programs = {
            11: "StatusEffectImmunityGroup:Debuff",
            22: "ActorFeature:off,Movement,RegularSkills",
        }
        # ``lookups`` counts calls, which the per-player cache must suppress.
        # ``program_reads`` counts actual parses, which the per-effect memo
        # must suppress. The two failures have different fixes.
        self.lookups = 0
        self.program_reads = 0
        self._cache: dict[int, frozenset[str]] = {}

    def status_effect_disabled_features(self, effect_id: int):
        effect_id = int(effect_id)
        self.lookups += 1
        cached = self._cache.get(effect_id)
        if cached is not None:
            return cached
        self.program_reads += 1
        result = parse_disabled_actor_features(
            self.programs.get(effect_id, "")
        )
        self._cache[effect_id] = result
        return result


class _GateSession:
    def __init__(self, combat, *, effect_ids) -> None:
        self.flow_lock = threading.RLock()
        self.gameplay_combat_state = combat
        self.effect_ids = tuple(effect_ids)
        self.gameplay_disabled_feature_cache = None
        self.gameplay_victim_status_effect_instances = {}
        self.gameplay_runtime_status_effects = {}
        self.gameplay_item_set_effects = {}
        self.gameplay_socket_effects = {}
        self.gameplay_item_set_evaluation = None
        self.gameplay_socket_effect_evaluation = None

    @property
    def gameplay_persistent_status_effects(self):
        return {
            effect_id: SimpleNamespace(effect_id=effect_id)
            for effect_id in self.effect_ids
        }


if __name__ == "__main__":
    unittest.main()
