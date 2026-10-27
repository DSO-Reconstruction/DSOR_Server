"""The native Singularity creator aura reduces authoritative cooldowns in range."""

from dataclasses import replace
import sqlite3
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from dso_server.gameplay.local_combat_state import LocalCombatState
from dso_server.gameplay.local_monster_state import load_local_player_only_encounter
from dso_server.gameplay.player_skill_timing import PlayerSkillTimingState
from dso_server.gameplay import player_skill_timing
from dso_server.paths import DEFAULT_STATIC_DB_PATH
from dso_server.runtime.transport import Session
from dso_server.services import combat_resolution, player_owned_resolution_dispatch
from dso_server.services.character_server import CharacterServer


class RecordedTimer:
    def __init__(self, interval, callback, args=()):
        self.interval = interval
        self.callback = callback
        self.args = args
        self.started = False
        self.cancelled = False

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True

    def fire(self):
        # Already-dequeued callbacks can arrive even after cancellation.
        self.callback(*self.args)


@pytest.fixture
def aura_case(monkeypatch):
    clock = SimpleNamespace(now=100.0)
    clock.perf_counter = lambda: clock.now
    monkeypatch.setattr(combat_resolution, "time", clock)
    monkeypatch.setattr(player_owned_resolution_dispatch, "time", clock)
    monkeypatch.setattr(player_skill_timing, "time", clock)
    monkeypatch.setattr(CharacterServer, "_refresh_player_combat_vitals", Mock())
    session = Session(("127.0.0.1", 47981), Mock())
    session.connected = True
    session.gameplay_character = SimpleNamespace(character_id=17, class_id=1, level=100)
    session.gameplay_remote_actor_id = 0x10011
    session.gameplay_chase_generation = 11
    session.send_reliable_ordered = Mock()
    encounter = load_local_player_only_encounter(DEFAULT_STATIC_DB_PATH, "a0200_kingscity")
    session.gameplay_combat_state = LocalCombatState(
        encounter, player_actor_id=session.gameplay_remote_actor_id,
        player_health=1000, initially_active=False,
    )
    session.gameplay_player_skill_timing = PlayerSkillTimingState(
        {"skill10": 160.0, "skill02": 145.0, "unrelated": 150.0},
        cast_ready_at=103.0, cast_category="skill10",
    )
    timers = []

    def timer(interval, callback, args=()):
        result = RecordedTimer(interval, callback, args)
        timers.append(result)
        return result

    session.gameplay_deadline_timer = timer

    def enter(amount=-0.25, *, duration_ticks=None):
        with sqlite3.connect(DEFAULT_STATIC_DB_PATH.resolve().as_uri() + "?mode=ro", uri=True) as db:
            definition, = LocalCombatState._load_aura_owner_effects(
                db, f"mage_singularity_buff_cdreduction_aura,$0:{amount}",
            )
        assert definition.resource_id == "mage_singularity_buff_cdreduction_aura_effect"
        assert definition.duration_ticks == 200  # The zero-duration child inherits the eight-second aura.
        if duration_ticks is not None:
            definition = replace(definition, duration_ticks=duration_ticks)
        CharacterServer._send_player_skill_status_effects(
            session, (definition,), server_tick=2500, label="test-managed-cooldown-aura",
            force_fresh_effect_ids={definition.effect_id},
        )
        instance_id, = session.gameplay_runtime_status_effects
        return definition, instance_id, session.gameplay_projectile_timers[("player-status-tick", instance_id)]

    return SimpleNamespace(session=session, clock=clock, timers=timers, enter=enter)


@pytest.mark.parametrize("amount", (-0.1, -0.25))
def test_singularity_ticks_at_authored_interval_and_preserves_cast_recovery(aura_case, amount):
    case = aura_case
    state = case.session.gameplay_player_skill_timing
    before = dict(state.cooldowns)
    definition, instance_id, timer = case.enter(amount)
    assert timer.started
    assert timer.interval == pytest.approx(0.5)
    assert state.cooldowns == before  # TickModifiers must not execute as StartModifiers.
    assert definition.values[0] == amount
    assert case.session.gameplay_runtime_status_effects[instance_id][1] == pytest.approx(108.0)

    for now in (100.5, 101.0):
        case.clock.now = now
        timer.fire()
        for category in ("skill10", "skill02"):
            before[category] = now + (before[category] - now) * (1.0 + amount)
            assert state.cooldowns[category] == pytest.approx(before[category])
        assert state.cooldowns["unrelated"] == 150.0
        assert state.cast_ready_at == 103.0
        assert state.cast_category == "skill10"
        timer = case.session.gameplay_projectile_timers[("player-status-tick", instance_id)]
        assert timer.started
        assert timer.interval == pytest.approx(0.5)


@pytest.mark.parametrize("invalidated", ("removed", "expired", "generation", "disconnected"))
def test_late_cooldown_tick_does_nothing_after_aura_is_invalidated(aura_case, invalidated):
    case = aura_case
    _, instance_id, timer = case.enter()
    case.clock.now = 100.5
    if invalidated == "removed":
        case.session.gameplay_runtime_status_effects.pop(instance_id)
    elif invalidated == "expired":
        case.clock.now = 108.1
    elif invalidated == "generation":
        case.session.gameplay_chase_generation += 1
    else:
        case.session.connected = False
    before = dict(case.session.gameplay_player_skill_timing.cooldowns)
    count = len(case.timers)
    timer.fire()
    assert case.session.gameplay_player_skill_timing.cooldowns == before
    assert case.session.gameplay_player_skill_timing.cast_ready_at == 103.0
    assert ("player-status-tick", instance_id) not in case.session.gameplay_projectile_timers
    assert len(case.timers) == count


def test_managed_aura_exit_cancels_ticks_and_reentry_uses_remaining_lifetime(aura_case):
    case = aura_case
    definition, old_id, old_timer = case.enter()
    case.clock.now = 100.5
    old_timer.fire()
    pending = case.session.gameplay_projectile_timers[("player-status-tick", old_id)]
    before = dict(case.session.gameplay_player_skill_timing.cooldowns)

    case.clock.now = 101.0
    CharacterServer._remove_managed_player_aura_effects(case.session, (definition,))
    assert pending.cancelled
    assert old_id not in case.session.gameplay_runtime_status_effects
    assert ("player-status-tick", old_id) not in case.session.gameplay_projectile_timers
    pending.fire()
    assert case.session.gameplay_player_skill_timing.cooldowns == before

    case.clock.now = 102.0
    _, new_id, new_timer = case.enter(duration_ticks=150)
    assert new_id != old_id
    assert case.session.gameplay_runtime_status_effects[new_id][1] == pytest.approx(108.0)
    pending.fire()  # A stale callback cannot affect the new aura instance.
    assert case.session.gameplay_player_skill_timing.cooldowns == before
    case.clock.now = 102.5
    new_timer.fire()
    assert case.session.gameplay_player_skill_timing.cooldowns["skill10"] == pytest.approx(
        102.5 + (before["skill10"] - 102.5) * 0.75,
    )
    assert case.session.gameplay_player_skill_timing.cast_ready_at == 103.0
