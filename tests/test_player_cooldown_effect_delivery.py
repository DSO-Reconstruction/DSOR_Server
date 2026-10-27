"""Native cooldown programs execute through real player status delivery."""

from dataclasses import replace
import sqlite3
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from dso_server.gameplay.local_combat_state import LocalCombatState
from dso_server.gameplay.player_skill_timing import PlayerSkillTimingState
from dso_server.paths import DEFAULT_STATIC_DB_PATH
from dso_server.runtime.transport import Session
from dso_server.services.base import CharacterService


def effect(encoded):
    with sqlite3.connect(DEFAULT_STATIC_DB_PATH.resolve().as_uri() + "?mode=ro", uri=True) as db:
        return LocalCombatState._load_victim_status_effects(db, encoded)[0]


def player():
    session = Session(("127.0.0.1", 47801), Mock())
    session.send_reliable_ordered = Mock()
    now = time.perf_counter()
    session.gameplay_player_skill_timing = PlayerSkillTimingState(
        {"skill10": now + 60, "skill02": now + 60}, now + 1,
    )
    return session


@pytest.mark.parametrize("encoded", (
    "mage_arcanetorrent_cd_reduction,$0:-3",
    "pvp_cooldown_reset",
))
def test_zero_duration_cooldown_program_is_applied_and_sent_once(encoded):
    session = player()
    state = session.gameplay_player_skill_timing
    before, cadence = dict(state.cooldowns), state.cast_ready_at
    definition = effect(encoded)
    assert definition.duration_ticks == 0
    CharacterService._send_player_skill_status_effects(
        session, (definition,), server_tick=25, label="test-cooldown-start",
    )
    if encoded.startswith("mage_"):
        assert state.cooldowns["skill10"] == pytest.approx(before["skill10"] - 3)
        assert state.cooldowns["skill02"] == before["skill02"]
    else:
        assert state.cooldowns == {}
    assert state.cast_ready_at == cadence  # Resetting a cooldown does not cancel the cast animation.
    assert session.gameplay_runtime_status_effects == {}
    assert session.gameplay_projectile_timers == {}
    session.send_reliable_ordered.assert_called_once()


def test_denied_status_reapplication_does_not_reduce_cooldown_twice():
    session = player()
    definition = replace(
        effect("mage_arcanetorrent_cd_reduction,$0:-3"),
        duration_ticks=25, exclusivity_type="Deny",
    )
    for _ in range(2):
        CharacterService._send_player_skill_status_effects(
            session, (definition,), server_tick=25, label="test-cooldown-start",
        )
    session.send_reliable_ordered.assert_called_once()
    assert len(session.gameplay_runtime_status_effects) == 1
    # A third attempt is also a no-op while the original instance is alive.
    remaining = dict(session.gameplay_player_skill_timing.cooldowns)
    CharacterService._send_player_skill_status_effects(
        session, (definition,), server_tick=25, label="test-cooldown-start",
    )
    assert session.gameplay_player_skill_timing.cooldowns == remaining


@pytest.mark.parametrize("target_actor_id", [1, 40001])
def test_received_cooldown_increase_applies_once_to_its_local_wearer(target_actor_id):
    session = player()
    state = session.gameplay_player_skill_timing
    previous = dict(state.cooldowns)
    resolution = SimpleNamespace(
        target_actor_id=target_actor_id, source_actor_id=42, attacker_actor_id=42,
        victim_status_effects=(effect("monster_increase_cooldown_primary,$0:4"),),
        skill_index=0, server_tick=25, cast_id=123, killed=False, damage_immune=False,
    )
    commands, = CharacterService._victim_effect_command_transactions(
        session, (resolution,), include_item_set_procs=False,
    )
    assert commands  # Peer viewers still receive the native status update.
    assert state.cooldowns["skill10"] == pytest.approx(
        previous["skill10"] + (4 if target_actor_id == 1 else 0),
    )
    assert state.cooldowns["skill02"] == previous["skill02"]
    assert session.gameplay_victim_status_effect_instances == {}
    # Repeated impacts/publication of this cast cannot apply the increase twice.
    assert CharacterService._victim_effect_command_transactions(
        session, (resolution,), include_item_set_procs=False,
    ) == ((),)
