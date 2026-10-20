"""Monsters summoning monsters (services/monster_summons.py): a landed summon
spawns its monsters around the caster, wakes them, counts them against the
caster's limit and announces them to the players in that combat."""

from types import SimpleNamespace
from unittest.mock import Mock

from dso_server.gameplay.local_combat_state import LocalCombatState, MonsterSummonRequest
from dso_server.gameplay.local_monster_state import load_local_monster_encounter
from dso_server.paths import DEFAULT_STATIC_DB_PATH
from dso_server.services.monster_summons import MonsterSummonService


def test_a_landed_summon_spawns_wakes_and_counts_its_monsters():
    encounter = load_local_monster_encounter(
        DEFAULT_STATIC_DB_PATH, "a0204_boss", include_spawn_areas=False,
        active_event_flags=frozenset({"BOSS_spawn"}),
    )
    state = LocalCombatState(encounter, player_actor_id=1, player_health=1000)
    caster = encounter.actor_ids[0]
    session = SimpleNamespace(gameplay_combat_state=state)
    server = SimpleNamespace(
        _active_gameplay_sessions=lambda: (session,),
        _announce_runtime_group=Mock(),
    )
    request = MonsterSummonRequest(
        caster_actor_id=caster, template_id="a0203_normal_add_skeleton_warrior",
        amount=2, limit=3, position=state.monster_positions[caster], level=17,
    )
    MonsterSummonService._spawn_monster_summons(server, session, (request,))
    summoned = state.monster_summoned_actor_ids[caster]
    assert len(summoned) == 2
    assert summoned <= set(state.living_actor_ids)
    assert summoned <= state.active_monster_actor_ids
    assert {state.monsters[a].template_id for a in summoned} == {"a0203_normal_add_skeleton_warrior"}
    server._announce_runtime_group.assert_called_once()
    # The limit (3) leaves room for one more.
    MonsterSummonService._spawn_monster_summons(server, session, (request,))
    assert len(state.monster_summoned_actor_ids[caster]) == 3


def test_heredur_summons_skeletons_after_its_initial_cooldown():
    encounter = load_local_monster_encounter(
        DEFAULT_STATIC_DB_PATH, "a0204_boss", include_spawn_areas=False,
        active_event_flags=frozenset({"BOSS_spawn"}),
    )
    state = LocalCombatState(encounter, player_actor_id=1, player_health=100000)
    caster = encounter.actor_ids[0]
    position = state.monsters[caster].position
    target = encounter.coordinate_frame.world_to_wire((position[0] + 1.0, position[1], position[2]))
    summons = []
    for ordinal in range(15):
        now = 1000.0 + ordinal * 2.0
        state.pending_monster_attacks.clear()
        state.last_monster_attack_at[caster] = 0.0
        for attack in state.maybe_start_monster_attacks(target, server_tick=1000 + ordinal * 50, now=now):
            state.resolve_due_monster_attacks(target, now=attack.due_at + 0.01, player_actor_id=1)
            if attack.definition.summon_template:
                summons.append(now)
                assert attack.command_id == 0x004A  # ShiftedSkillCommand
        assert all(r.template_id == "a0203_normal_add_skeleton_warrior" and r.amount == 2
                   for r in state.pending_monster_summons)
    # InitialCoolDown 3-5 s (then the next free moment: another skill's
    # animation may still hold it), then every 20 s.
    assert summons and 1003.0 <= summons[0] <= 1010.0
    assert all(b - a >= 20.0 for a, b in zip(summons, summons[1:]))
    assert len(state.pending_monster_summons) == len(summons)
