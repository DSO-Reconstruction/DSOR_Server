"""Monster summons with an authored ``_Template_Monster.LifeTime`` expire.

Khalys's bullet portals (``boss_khalys_bullet_spawn_target``, 3.9 s) and her
illusions (``boss_khalys_illusion_stationary``, 24 s) are summoned like any
add (services/monster_summons.py) but leave on their own: every viewer gets a
``DiscardMonster``, nobody earns a kill, and neither a late joiner nor a
leash/vicinity reset can bring the actor back. Their caster's death or reset
retires them early. ``LifeTime`` 0 summons (Heredur's skeletons) stay.
"""

import threading
import time
from types import SimpleNamespace

import pytest

from dso_server.gameplay.local_combat_state import (
    LocalCombatState,
    MonsterSummonRequest,
)
from dso_server.gameplay.local_monster_state import load_local_monster_encounter
from dso_server.paths import DEFAULT_STATIC_DB_PATH
from dso_server.protocol.gameplay_protocol import decode_discard_monster_batch
from dso_server.services.monster_summons import (
    MonsterSummonService,
    cleanup_monster_summons,
)

PORTAL = "boss_khalys_bullet_spawn_target"      # LifeTime 3.9
ILLUSION = "boss_khalys_illusion_stationary"    # LifeTime 24.0
SKELETON = "a0203_normal_add_skeleton_warrior"  # LifeTime 0


class ManualScheduler:
    """Deadline scheduler stand-in: records timers, never runs them."""

    def __init__(self):
        self.timers = []

    def timer(self, delay, callback, args=None, kwargs=None):
        handle = SimpleNamespace(
            delay=delay, callback=callback, started=False, cancelled=False,
        )
        handle.start = lambda: setattr(handle, "started", True)
        handle.cancel = lambda: setattr(handle, "cancelled", True)
        self.timers.append(handle)
        return handle


class Viewer:
    def __init__(self, combat, *, failures=0):
        self.gameplay_combat_state = combat
        self.connected = True
        self.gameplay_presence_departed = False
        self.gameplay_map_event_runtime = None
        self.gameplay_deadline_scheduler = None
        self.gameplay_mutation_lock = threading.RLock()
        self.flow_lock = threading.RLock()
        self.gameplay_spawned_monster_actor_ids = set()
        self.gameplay_announced_monster_actor_ids = set()
        self.gameplay_new_monster_sent_actor_ids = set()
        self.gameplay_requested_actor_ids = set()
        self.gameplay_delivered_monster_positions = {}
        self.failures = failures
        self.sent = []

    def send_reliable_ordered(self, packet, *, track=False, label=""):
        if self.failures:
            self.failures -= 1
            raise OSError("socket closed")
        self.sent.append((packet, label))

    def discarded(self):
        return [
            actor_id
            for packet, label in self.sent
            if label == "monster-summon-expired"
            for actor_id in decode_discard_monster_batch(*packet)
        ]


class Arena:
    def __init__(self, viewer_count=2, failures=()):
        encounter = load_local_monster_encounter(
            DEFAULT_STATIC_DB_PATH, "a0204_boss", include_spawn_areas=False,
            active_event_flags=frozenset({"BOSS_spawn"}),
        )
        self.combat = LocalCombatState(
            encounter, player_actor_id=1, player_health=1000
        )
        self.caster = encounter.actor_ids[0]
        self.scheduler = ManualScheduler()
        self.viewers = [
            Viewer(self.combat, failures=(failures[i] if i < len(failures) else 0))
            for i in range(viewer_count)
        ]
        self.viewers[0].gameplay_deadline_scheduler = self.scheduler
        self.kills = []
        arena = self

        class Server(MonsterSummonService):
            def _active_gameplay_sessions(self):
                return tuple(arena.viewers)

            def _announce_runtime_group(self, viewer, combat, group, *, force_actor_ids=()):
                # The vicinity spawned the fresh actors for this viewer.
                viewer.gameplay_spawned_monster_actor_ids.update(force_actor_ids)

            def _on_monsters_killed(self, session, monsters):  # pragma: no cover
                arena.kills.append(monsters)

        self.server = Server()

    def summon(self, template, amount=1):
        before = set(self.combat.monsters)
        request = MonsterSummonRequest(
            caster_actor_id=self.caster, template_id=template, amount=amount,
            limit=0, position=self.combat.monster_positions[self.caster],
            level=40,
        )
        self.server._spawn_monster_summons(self.viewers[0], (request,))
        return tuple(sorted(set(self.combat.monsters) - before))

    @property
    def lifecycle(self):
        return self.combat.monster_summon_lifecycle


def test_authored_lifetime_is_pinned_at_creation_and_schedules_expiry():
    arena = Arena()
    started = time.perf_counter()
    (portal,) = arena.summon(PORTAL)
    (illusion,) = arena.summon(ILLUSION)
    records = arena.combat.temporary_monster_summons
    assert records[portal].expires_at - started == pytest.approx(3.9, abs=0.5)
    assert records[illusion].expires_at - started == pytest.approx(24.0, abs=0.5)
    assert {portal, illusion} <= set(arena.combat.living_actor_ids)
    # One map-owned watcher, armed with a bounded poll.
    assert arena.lifecycle is not None
    assert arena.scheduler.timers and arena.scheduler.timers[-1].started
    assert 0.0 < arena.scheduler.timers[-1].delay <= 0.25


def test_expiry_discards_for_every_viewer_without_a_kill():
    arena = Arena()
    (portal,) = arena.summon(PORTAL)
    (illusion,) = arena.summon(ILLUSION)
    now = arena.combat.temporary_monster_summons[portal].expires_at

    arena.lifecycle.advance(now=now - 0.5)
    assert all(viewer.discarded() == [] for viewer in arena.viewers)

    arena.lifecycle.advance(now=now + 0.01)
    for viewer in arena.viewers:
        assert viewer.discarded() == [portal]
        assert portal not in viewer.gameplay_spawned_monster_actor_ids
        # Only DiscardMonster: no Kill packet, no loot.
        assert {label for _packet, label in viewer.sent} == {"monster-summon-expired"}
    assert arena.combat.monster_health[portal] == 0
    assert portal not in arena.combat.temporary_monster_summons
    assert portal not in arena.combat.monster_summoned_actor_ids[arena.caster]
    assert illusion in arena.combat.living_actor_ids
    assert arena.kills == []


def test_expired_actor_never_returns_to_late_joiners_or_resets():
    arena = Arena()
    (portal,) = arena.summon(PORTAL)
    arena.lifecycle.advance(
        now=arena.combat.temporary_monster_summons[portal].expires_at + 1.0
    )
    combat = arena.combat
    assert portal not in combat.alive_actor_ids
    combat.activate_monsters((portal,))
    assert portal not in combat.active_monster_actor_ids
    combat.retire_monsters((portal,))
    combat._reset_monster_to_spawn(portal, 100)
    assert combat.monster_health[portal] == 0
    assert portal not in combat.alive_actor_ids


def test_caster_death_retires_children_through_the_kill_callback():
    arena = Arena()
    (illusion,) = arena.summon(ILLUSION)
    (skeleton,) = arena.summon(SKELETON)
    caster = arena.combat.monsters[arena.caster]
    arena.combat.monster_health[arena.caster] = 0
    # Scripted notifications carry only a template id: ignored safely.
    cleanup_monster_summons(arena.server, arena.viewers[0], (
        SimpleNamespace(template_id="x"), caster,
    ))
    assert arena.combat.monster_health[illusion] == 0
    for viewer in arena.viewers:
        assert viewer.discarded() == [illusion]
    # Unlimited-lifetime adds keep their existing behaviour.
    assert arena.combat.monster_health[skeleton] > 0
    assert skeleton in arena.combat.monster_summoned_actor_ids[arena.caster]


def test_leash_reset_retires_children_and_cancels_queued_casts():
    arena = Arena()
    (illusion,) = arena.summon(ILLUSION)
    combat = arena.combat
    stale = MonsterSummonRequest(
        caster_actor_id=arena.caster, template_id=PORTAL, amount=1, limit=0,
        position=combat.monster_positions[arena.caster], level=40,
        caster_generation=combat._monster_summon_generations.get(arena.caster, 0),
    )
    combat._delayed_monster_summons.append((0.0, stale))
    combat._reset_monster_to_spawn(arena.caster, 100)
    assert combat.monster_health[illusion] == 0
    assert combat._delayed_monster_summons == []
    # A request captured before the reset is no longer current.
    assert not combat.monster_summon_request_is_current(stale)
    before = set(combat.monsters)
    arena.server._spawn_monster_summons(arena.viewers[0], (stale,))
    assert set(combat.monsters) == before
    arena.lifecycle.advance()
    for viewer in arena.viewers:
        assert viewer.discarded() == [illusion]


def test_a_failed_viewer_send_is_retried_without_reviving_the_actor():
    arena = Arena(failures=(0, 1))
    (portal,) = arena.summon(PORTAL)
    due = arena.combat.temporary_monster_summons[portal].expires_at + 0.01
    arena.lifecycle.advance(now=due)
    healthy, flaky = arena.viewers
    assert healthy.discarded() == [portal]
    assert flaky.discarded() == []
    assert arena.combat.monster_health[portal] == 0
    # The watcher stays armed for the pending delivery and retries it.
    assert arena.scheduler.timers[-1].started
    arena.lifecycle.advance(now=due + 0.25)
    assert flaky.discarded() == [portal]
    assert healthy.discarded() == [portal]


def test_map_teardown_retires_every_temporary_summon():
    arena = Arena()
    (illusion,) = arena.summon(ILLUSION)
    arena.viewers.clear()
    arena.lifecycle.advance(now=0.0)
    assert arena.combat.monster_health[illusion] == 0
    assert arena.combat.temporary_monster_summons == {}


def test_status_minions_with_a_lifetime_are_queued():
    arena = Arena()
    combat = arena.combat
    effect = SimpleNamespace(chance=1.0, delay_ticks=0)
    specs = ((ILLUSION, 1, (-4.0, 0.0, 2.5, 0.0)), (PORTAL, 1, (8.0, 0.0, 0.0, 0.0)))
    combat.player_status_minion_specs = lambda _effect: specs
    attack = SimpleNamespace(user_status_effects=(effect,))
    combat._queue_monster_status_minions(arena.caster, attack, (0.0, 0.0, 0.0), 0.0, 0.0)
    combat.release_due_monster_summons(now=0.1)
    assert sorted(r.template_id for r in combat.pending_monster_summons) == sorted((ILLUSION, PORTAL))
