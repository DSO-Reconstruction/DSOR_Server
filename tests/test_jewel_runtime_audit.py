"""Behavior checks for authored jewel stacking across all five ranks."""
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch
import time
import sqlite3
import pytest
from dso_server.paths import DEFAULT_STATIC_DB_PATH
from dso_server.protocol.socket_effect_protocol import SocketEffectDatabase
from dso_server.services.base import CharacterService
from dso_server.services.player_projectiles import PlayerProjectileService
from dso_server.tests.test_item_set_events import wearer
from dso_server.tests.test_monster_location_auras import _state as location_state
from dso_server.tests.test_pvp_matchmaking import _session

@pytest.mark.parametrize("rank", ["common", "uncommon", "magic", "rare", "epic"])
def test_blocked_energy_accumulates_two_real_occurrences(wearer, rank):
    db = SocketEffectDatabase(DEFAULT_STATIC_DB_PATH)
    program = db.programs["jewel_buff_blocked_energy_" + rank]
    rule = next(r for r in program.rules if r.trigger == "hitblocked")
    rule = replace(rule, chance=1.0, definition=replace(rule.definition, chance=1.0))
    wearer.gameplay_socket_effect_evaluation = SimpleNamespace(
        rules_for_event=lambda event: (rule,) if event == "hitblocked" else (),
        cast_rules=(),
    )
    now = [100.0]
    with patch(
        "dso_server.services.socket_effects.time.perf_counter",
        side_effect=lambda: now[0],
    ):
        for tick in (200, 201):
            CharacterService._dispatch_socket_event(
                wearer, "hitblocked", server_tick=tick
            )
            now[0] += 2.01
    entries = [entry for entry, expiry in wearer.gameplay_runtime_status_effects.values()
               if entry.effect_id == rule.definition.effect_id]
    assert len(entries) == 1
    assert entries[0].field_18 == 1


def test_frozen_heart_does_not_proc_when_wearer_hits_frozen_target():
    pending = SimpleNamespace(
        cast_id=10, projectile_index=0, impact_index=0, skill_index=3,
        player_minimum_damage=10, player_maximum_damage=20,
    )
    resolution = SimpleNamespace(
        server_tick=200, position=(1.0, 2.0, 3.0), critical_hit=False,
        killed=False,
    )
    events = PlayerProjectileService._player_impact_socket_events(
        pending, resolution, source_actor_id=1, low_health=False,
        low_health_casts=set(),
    )
    assert [event[0] for event in events] == ["hit"]


def test_frozen_heart_checks_freeze_on_the_wearer(wearer):
    wearer.gameplay_combat_state.status_sources_have_group = (
        lambda sources, groups: any(
            source.effect_id == 123 for source in sources
        ) and "freeze" in groups
    )
    source = SimpleNamespace(effect_id=123)
    from unittest.mock import patch
    with patch.object(
        CharacterService, "_active_skill_modifier_sources",
        return_value=(source,),
    ):
        assert CharacterService._player_is_frozen(wearer)


def test_triggered_healing_jewel_buff_has_two_second_proc_cooldown(wearer):
    db = SocketEffectDatabase(DEFAULT_STATIC_DB_PATH)
    program = db.programs["jewel_buff_heal_onHitFrost_common"]
    rule = next(r for r in program.rules if r.trigger == "hitfrost")
    wearer.gameplay_socket_effect_evaluation = SimpleNamespace(
        rules_for_event=lambda event: (rule,) if event == "hitfrost" else (),
        cast_rules=(),
    )
    now = [100.0]
    with patch(
        "dso_server.services.socket_effects.time.perf_counter",
        side_effect=lambda: now[0],
    ):
        first = CharacterService._dispatch_socket_event(
            wearer, "hitfrost", server_tick=200
        )
        now[0] = 101.999
        blocked = CharacterService._dispatch_socket_event(
            wearer, "hitfrost", server_tick=201
        )
        now[0] = 102.001
        second = CharacterService._dispatch_socket_event(
            wearer, "hitfrost", server_tick=202
        )

    assert first == (rule,)
    assert blocked == ()
    assert second == (rule,)
    assert CharacterService._socket_rule_cooldown_seconds(rule) == 2.0
    assert CharacterService._socket_rule_cooldown_seconds(
        replace(rule, target="location")
    ) == 2.0


def test_eternal_scorn_hit_event_reaches_location_damage_resolution():
    session = _session(52261, 261, "Eternal Scorn wearer")
    combat = location_state()
    session.gameplay_combat_state = combat
    session.gameplay_remote_actor_id = 1
    session.gameplay_last_move = SimpleNamespace(
        relative_position=(0.0, 0.0, 0.0), yaw=0.0
    )
    session.send_reliable_ordered = lambda *args, **kwargs: None
    program = SocketEffectDatabase(
        DEFAULT_STATIC_DB_PATH
    ).programs["jewel_buff_xmas_event_mage"]
    rule = next(r for r in program.rules if r.trigger == "hit")
    rule = replace(
        rule, chance=1.0, definition=replace(rule.definition, chance=1.0)
    )
    session.gameplay_socket_effect_evaluation = SimpleNamespace(
        rules_for_event=lambda event: (rule,) if event == "hit" else (),
        cast_rules=(),
    )
    pending = SimpleNamespace(
        cast_id=10, projectile_index=0, impact_index=0,
        skill_index=1857, player_minimum_damage=100.0,
        player_maximum_damage=100.0,
    )
    impact = SimpleNamespace(
        server_tick=200, position=(0.0, 0.0, 0.0),
        critical_hit=False, killed=False,
    )
    events = PlayerProjectileService._player_impact_socket_events(
        pending, impact, source_actor_id=1,
        low_health=False, low_health_casts=set(),
    )
    CharacterService._dispatch_socket_event_batch(session, events)
    resolutions, _applications = combat.apply_player_location_auras(
        now=time.perf_counter() + 0.01, server_tick=201,
    )

    assert [value.damage for value in resolutions] == [150]


def test_ranger_eternal_scorn_is_cooldown_limited_and_uses_hit_position(wearer):
    combat = location_state()
    target_position = (8.0, 1.5, -3.0)
    combat.monster_positions[100] = target_position
    wearer.gameplay_combat_state = combat
    wearer.gameplay_remote_actor_id = 1
    wearer.gameplay_last_move = SimpleNamespace(
        relative_position=(0.0, 0.0, 0.0), yaw=0.0
    )
    wearer.send_reliable_ordered = lambda *args, **kwargs: None
    program = SocketEffectDatabase(
        DEFAULT_STATIC_DB_PATH
    ).programs["jewel_buff_xmas_event_ranger"]
    rule = next(r for r in program.rules if r.target == "location")
    assert rule.trigger == "hitcritical"
    assert rule.chance == 0.05
    rule = replace(
        rule, chance=1.0, definition=replace(rule.definition, chance=1.0)
    )
    wearer.gameplay_socket_effect_evaluation = SimpleNamespace(
        rules_for_event=lambda event: (rule,) if event == "hitcritical" else (),
        cast_rules=(),
    )
    with sqlite3.connect(DEFAULT_STATIC_DB_PATH) as database:
        (multishot_index,) = database.execute(
            "SELECT rowid - 1 FROM _Template_Skill WHERE Id=?",
            ("multishot",),
        ).fetchone()
    now = [100.0]
    with patch(
        "dso_server.services.socket_effects.time.perf_counter",
        side_effect=lambda: now[0],
    ):
        for cast_id in (10, 11):
            pending = SimpleNamespace(
                cast_id=cast_id, projectile_index=0, impact_index=0,
                skill_index=multishot_index,
                player_minimum_damage=100.0,
                player_maximum_damage=100.0,
            )
            impact = SimpleNamespace(
                server_tick=200 + cast_id, position=target_position,
                critical_hit=True, killed=False,
            )
            events = PlayerProjectileService._player_impact_socket_events(
                pending, impact, source_actor_id=1,
                low_health=False, low_health_casts=set(),
            )
            CharacterService._dispatch_socket_event_batch(wearer, events)
            now[0] += 0.1

    visuals = tuple(combat.player_location_visuals.values())
    assert len(visuals) == 1
    assert visuals[0].position == target_position
    assert CharacterService._socket_rule_cooldown_seconds(rule) == 2.0
