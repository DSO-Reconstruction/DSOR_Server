"""Authored defense waves require combat and reset on authoritative failure."""

from dataclasses import replace
from types import SimpleNamespace
import threading
from unittest.mock import Mock

import pytest

from dso_server.gameplay.holdground import UnsupportedHoldGround, load_holdground_plan
from dso_server.gameplay.local_combat_state import LocalCombatState
from dso_server.gameplay.local_monster_state import load_local_player_only_encounter
from dso_server.gameplay.quest_content import HoldGroundDefinition
from dso_server.paths import DEFAULT_STATIC_DB_PATH
from dso_server.protocol.inventory_codec import InventoryInfo
from dso_server.services import quest_holdgrounds as defense
from dso_server.services.base import CharacterService
from dso_server.tests.test_pvp_matchmaking import _session


BINDING = HoldGroundDefinition(
    "c2200-RewriteHistory-01-n.s_RewriteHistory-01-Holdground1HoldGround",
    "c2303_quest_column_01", ((0, "c2303_holdground_RewriteHistory-01a"),
                              (1, "c2303_holdground_RewriteHistory-01a_scale")),
)


class Timer:
    def __init__(self, delay, callback, args=(), kwargs=None):
        self.delay, self.callback, self.args, self.kwargs = delay, callback, args, kwargs or {}
        self.cancelled = False
    def start(self):
        pass
    def cancel(self):
        self.cancelled = True


@pytest.fixture
def arena(monkeypatch):
    s, peer = _session(52510, 510, "Defender"), _session(52511, 511, "Peer")
    combat = LocalCombatState(load_local_player_only_encounter(DEFAULT_STATIC_DB_PATH, "c2303_dun"),
                             player_actor_id=s.gameplay_remote_actor_id, player_health=1000)
    timers = []
    def timer(*args, **kwargs):
        value = Timer(*args, **kwargs)
        timers.append(value)
        return value
    runtime = SimpleNamespace(lock=threading.Lock(), map_id="c2303_dun",
                              scaling_difficulty=0, monster_level=None,
                              scheduler=SimpleNamespace(timer=timer),
                              active_flags=lambda: frozenset(),
                              catalog=SimpleNamespace(event=lambda value: None))
    for session in (s, peer):
        session.gameplay_map = "c2303_dun"
        session.gameplay_combat_state = combat
        session.gameplay_map_event_runtime = runtime
        session.gameplay_inventory = InventoryInfo()
        session.gameplay_deadline_timer = timer
        session.gameplay_chase_server_tick = 200
        session.gameplay_presence_callback = None
        session.send_reliable_ordered = Mock()
        session.send_reliable_ordered_split = Mock()
        session.gameplay_announced_npc_actor_ids = {65536}
    def announce(viewer, _combat, group, **kwargs):
        viewer.gameplay_spawned_monster_actor_ids.update(group.actor_ids)
        viewer.gameplay_announced_monster_actor_ids.update(group.actor_ids)
    server = SimpleNamespace(static_database_path=DEFAULT_STATIC_DB_PATH,
                             _map_event_sessions=lambda r: (s, peer),
                             _announce_runtime_group=Mock(side_effect=announce),
                             _quest_feed=Mock(), _quest_refresh_npc_infos=Mock())
    clock = [100.0]
    monkeypatch.setattr(defense.time, "perf_counter", lambda: clock[0])
    monkeypatch.setattr(CharacterService, "_start_monster_chase", Mock())
    yield SimpleNamespace(server=server, s=s, peer=peer, combat=combat,
                          runtime=runtime, timers=timers, clock=clock)
    defense.cancel_holdgrounds(server, s)


def start(arena, binding=BINDING):
    assert defense.start_holdground(arena.server, arena.s, "c2200-RewriteHistory-01-n", binding)
    return next(iter(arena.runtime.holdgrounds.values()))


def test_current_rewrite_history_waves_preserve_duplicate_spawns():
    plan = load_holdground_plan(DEFAULT_STATIC_DB_PATH, "c2303_dun",
                               BINDING.configurations[0][1], BINDING.actor)
    assert [len(w.spawns) for w in plan.waves] == [5, 6, 6, 9, 11]
    assert [w.duration_seconds for w in plan.waves] == [30, 40, 50, 60, 12]
    assert plan.waves[0].spawns[-1] == plan.waves[0].spawns[-2]
    assert plan.defender_template_id == "c2303_guard_column_01"


def test_rejects_missing_markers_and_wrong_npc_identity():
    with pytest.raises(UnsupportedHoldGround, match="matching placements"):
        load_holdground_plan(DEFAULT_STATIC_DB_PATH, "c2303_dun",
                             BINDING.configurations[0][1], BINDING.actor, npc_actor_id=123)
    with pytest.raises(UnsupportedHoldGround, match="unresolved defense spawn"):
        load_holdground_plan(DEFAULT_STATIC_DB_PATH, "g0005_temple_ice_02_dun", "g0005_TempleOfIce_01",
                             "g0005_quest_child_02")


def test_read_only_validation_does_not_spawn_or_credit(arena):
    plan = defense.resolve_holdground(arena.server, arena.s, BINDING)
    assert len(plan.waves) == 5
    assert not arena.combat.player_summons
    assert not arena.combat.monsters
    assert not hasattr(arena.runtime, "holdgrounds")
    arena.server._quest_feed.assert_not_called()


def test_starts_actual_defender_and_distinct_first_wave_once(arena):
    run = start(arena)
    assert len(run.owned_actor_ids) == 5
    assert len(arena.combat.monsters) == 5
    assert set(arena.combat.holdground_targets) == run.owned_actor_ids
    assert set(arena.combat.holdground_targets.values()) == {run.defender_actor_id}
    defender = arena.combat.player_summons[run.defender_actor_id]
    assert defender.definition.movement_locked and not defender.definition.stationary
    assert not defender.definition.follows_owner
    assert arena.combat.player_summon_health[run.defender_actor_id][0] > 1000
    assert not defense.start_holdground(arena.server, arena.s, run.quest_id, BINDING)
    assert len(arena.combat.monsters) == 5
    arena.server._quest_feed.assert_not_called()
    assert run.defender_actor_id in arena.s.gameplay_summon_actor_ids
    assert run.defender_actor_id in arena.peer.gameplay_summon_actor_ids


def test_waves_wait_authored_durations_and_success_requires_every_owned_attacker(arena):
    run = start(arena)
    arena.clock[0] += 29.9
    defense.advance_holdgrounds(arena.server, arena.s)
    assert run.wave_index == 0
    for wave_index in range(1, len(run.plan.waves)):
        arena.clock[0] = run.wave_deadline
        defense.advance_holdgrounds(arena.server, arena.s)
        assert run.wave_index == wave_index
    assert len(run.owned_actor_ids) == 37
    arena.clock[0] = run.wave_deadline
    defense.advance_holdgrounds(arena.server, arena.s)
    arena.server._quest_feed.assert_not_called()
    alive = next(iter(run.owned_actor_ids))
    for actor_id in run.owned_actor_ids - {alive}:
        arena.combat.monster_health[actor_id] = 0
    defense.advance_holdgrounds(arena.server, arena.peer, killed_actor_ids=(999999,))
    arena.server._quest_feed.assert_not_called()
    arena.combat.monster_health[alive] = 0
    defense.advance_holdgrounds(arena.server, arena.peer, killed_actor_ids=(alive,))
    defense.advance_holdgrounds(arena.server, arena.s)
    assert arena.server._quest_feed.call_count == 1
    assert arena.server._quest_feed.call_args.args[1:] == ("holdgroundsucceeded", BINDING.holdground_id)
    assert not arena.runtime.holdgrounds
    assert not arena.combat.holdground_targets


@pytest.mark.parametrize("cause", ["defender", "owner", "transfer", "cancel"])
def test_failure_or_abandonment_cleans_all_actors_and_stale_timer_cannot_complete(arena, cause):
    run = start(arena)
    timer = run.timer
    if cause == "defender":
        arena.combat.player_summon_health[run.defender_actor_id][0] = 0
    elif cause == "owner":
        arena.s.gameplay_death_pending = True
    elif cause == "transfer":
        arena.s.gameplay_chase_generation += 1
    else:
        defense.cancel_holdgrounds(arena.server, arena.s, run.quest_id)
    timer.callback(*timer.args, **timer.kwargs)
    timer.callback(*timer.args, **timer.kwargs)
    assert run.stopped
    assert not arena.runtime.holdgrounds
    assert not arena.combat.holdground_targets
    assert run.defender_actor_id not in arena.combat.player_summons
    assert all(arena.combat.monster_health.get(actor_id, 0) <= 0 for actor_id in run.owned_actor_ids)
    assert arena.server._quest_feed.call_count == (1 if cause in {"defender", "owner"} else 0)
    if cause in {"defender", "owner"}:
        assert arena.server._quest_feed.call_args.args[1] == "holdgroundfailed"


def test_scaled_defender_health_uses_map_level_and_difficulty(arena):
    arena.runtime.scaling_difficulty = 1
    arena.runtime.monster_level = 100
    run = start(arena)
    assert run.plan.defender_template_id.endswith("_scale")
    assert arena.combat.player_summon_health[run.defender_actor_id][0] > 100_000


def test_peer_publication_failure_does_not_prevent_polling_or_cleanup(arena):
    arena.peer.send_reliable_ordered.side_effect = OSError("peer disconnected")
    run = start(arena)
    assert run.timer is not None
    arena.combat.player_summon_health[run.defender_actor_id][0] = 0
    run.timer.callback(*run.timer.args)
    assert run.stopped
    assert arena.server._quest_feed.call_args.args[1] == "holdgroundfailed"


def test_superseded_poll_callback_does_not_duplicate_poll_schedule(arena):
    run = start(arena)
    previous = run.timer
    previous.callback(*previous.args)
    current = run.timer
    count = len(arena.timers)
    previous.callback(*previous.args)
    assert run.timer is current
    assert len(arena.timers) == count


def test_departed_session_character_teardown_does_not_break_cleanup(arena):
    run = start(arena)
    timer = run.timer
    arena.s.gameplay_chase_generation += 1
    arena.s.gameplay_character = None
    timer.callback(*timer.args)
    assert run.stopped
    assert not arena.runtime.holdgrounds


def test_only_the_authored_current_difficulty_is_validated(arena):
    binding = HoldGroundDefinition("test-heart", "f4202_quest_fox", (
        (0, "f4202_holdground_DestroyWorldsHeart-06"),
        (1, "f4202_holdground_DestroyWorldsHeart-06_scale"),
    ))
    arena.s.gameplay_map = "f4202_dun"
    with pytest.raises(UnsupportedHoldGround, match="unresolved defense spawn"):
        defense.resolve_holdground(arena.server, arena.s, binding)
    arena.runtime.scaling_difficulty = 1
    assert defense.resolve_holdground(arena.server, arena.s, binding).config_id.endswith("_scale")
    arena.runtime.scaling_difficulty = 8
    with pytest.raises(UnsupportedHoldGround, match="no defense config"):
        defense.resolve_holdground(arena.server, arena.s, binding)
    arena.server._quest_feed.assert_not_called()
