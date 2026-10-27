"""Authoritative cast deadlines, authored modifiers and ingress rejection."""

from contextlib import ExitStack
from dataclasses import replace
import sqlite3
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from dso_server.gameplay.player_skill_timing import (
    PlayerSkillTimingState, apply_skill_cooldown_effect,
    reserve_player_skill_timing, rollback_player_skill_timing,
    restore_player_skill_timing, snapshot_player_skill_timing,
)
from dso_server.gameplay.status_modifier_runtime import parse_modifier
from dso_server.paths import DEFAULT_STATIC_DB_PATH
from dso_server.protocol import combat_protocol as protocol
from dso_server.runtime.transport import Session
from dso_server.services.base import CharacterService
from dso_server.services import player_combat
from dso_server.tests import test_combat_entities as combat_fixtures
from dso_server.tests import test_local_combat_state as local_fixtures


@pytest.fixture
def combat():
    fixture = local_fixtures.LocalCombatStateTests()
    fixture.setUp()
    return fixture.state


def skill(combat, resource):
    with sqlite3.connect(DEFAULT_STATIC_DB_PATH) as database:
        index = database.execute(
            "SELECT rowid-1 FROM _Template_Skill WHERE Id=?", (resource,),
        ).fetchone()[0]
    return index, combat._load_player_skill_geometry(index)


def effect(resource, *, stacks=1, values=()):
    with sqlite3.connect(DEFAULT_STATIC_DB_PATH) as database:
        index = database.execute(
            "SELECT rowid-1 FROM _Template_StatusEffect WHERE Id=?", (resource,),
        ).fetchone()[0]
    return SimpleNamespace(effect_id=index, field_18=stacks - 1, values=values)


def session():
    return Session(("127.0.0.1", 40997), object())


def test_authored_shared_category_and_other_skill_cooldowns_are_independent(combat):
    s = session()
    _, first = skill(combat, "earthquake")
    _, variant = skill(combat, "true_earthquake")
    _, basic = skill(combat, "angrystrike")
    assert first.cooldown_seconds == 10
    assert first.cooldown_category == variant.cooldown_category == "Skill15"
    assert reserve_player_skill_timing(s, first, now=100) is not None
    assert reserve_player_skill_timing(s, basic, now=101) is not None
    assert reserve_player_skill_timing(s, variant, now=109.9) is None
    assert reserve_player_skill_timing(s, variant, now=110) is not None
    # Shared map combat must never share one player's cooldowns with another.
    assert reserve_player_skill_timing(session(), variant, now=100) is not None


def test_cadence_scales_uses_shared_recovery_and_does_not_accumulate_grace(combat):
    s = session()
    _, basic = skill(combat, "magicmissile")
    _, other = skill(combat, "fireball")
    reservation = reserve_player_skill_timing(s, basic, speed=2, now=100)
    assert reservation.cast_ready_at == pytest.approx(100 + round(15 / 2) / 25)
    assert reserve_player_skill_timing(s, other, speed=2, now=100.1) is None
    assert reserve_player_skill_timing(s, basic, speed=2, now=100.28) is not None
    assert s.gameplay_player_skill_timing.cast_ready_at == pytest.approx(100.64)
    assert reserve_player_skill_timing(s, basic, speed=2, now=100.56) is None
    _, unscaled = skill(combat, "frostnova")
    r = reserve_player_skill_timing(session(), unscaled, speed=10, now=100)
    assert r.cast_ready_at == pytest.approx(100 + unscaled.skill_unblock_frame / 25)


def test_escape_can_interrupt_other_cast_but_cannot_bypass_its_own_cooldown(combat):
    s = session()
    _, channel = skill(combat, "arcanetorrent")
    _, teleport = skill(combat, "teleport")
    assert reserve_player_skill_timing(s, channel, now=100) is not None
    assert reserve_player_skill_timing(s, teleport, now=100.1) is not None
    assert s.gameplay_player_skill_timing.cast_ready_at == pytest.approx(100.26)
    assert reserve_player_skill_timing(s, teleport, now=101) is None
    # The authored zero-cooldown forced charge cannot interrupt itself.
    _, charge = skill(combat, "arcanecharge")
    assert reserve_player_skill_timing(s, charge, now=101) is not None
    assert reserve_player_skill_timing(s, charge, now=101) is None


def test_scoped_cooldown_modifiers_resolve_stacks_variables_and_additive_buckets(combat):
    _, frozen = skill(combat, "frozenorb")
    modified = combat._modified_player_skill_geometry(frozen, (
        effect("mage_talent_frozen_master_lvl", stacks=5),
        parse_modifier("CoolDown:-0.1,relative,player_skills"),
        parse_modifier("CoolDown:-0.2,relative,frozenorb"),
        parse_modifier("CoolDown:-99,absolute,teleport"),
    ))
    assert modified.cooldown_seconds == pytest.approx(2.5 * 0.7 - 1)
    _, shout = skill(combat, "warshout")
    modified = combat._modified_player_skill_geometry(shout, (
        effect("warrior_talent_skill_cat_04_cooldown", stacks=5),
        parse_modifier("ActorVariable:-1,absolute,talent_warrior_skill_cat_04_cooldown"),
    ))
    assert modified.cooldown_seconds == 20
    _, teleport = skill(combat, "teleport")
    assert combat._modified_player_skill_geometry(teleport, (
        effect("mage_mindbuff_caster"),
    )).cooldown_seconds == 0


def test_snapshot_restore_keeps_elapsed_time_and_recovery_category():
    original = PlayerSkillTimingState({"skill01": 110, "skill02": 99}, 100.5, "skill01")
    document = snapshot_player_skill_timing(original, now=100, now_epoch=1000)
    restored = restore_player_skill_timing(document, now=500, now_epoch=1000.2)
    assert restored.cooldowns == pytest.approx({"skill01": 509.8})
    assert restored.cast_ready_at == pytest.approx(500.3)
    assert restored.cast_category == "skill01"
    expired = restore_player_skill_timing(document, now=500, now_epoch=1011)
    assert expired.cooldowns == {}
    assert expired.cast_ready_at == 0
    assert restore_player_skill_timing({"cooldowns": {"bad": float("nan")}}).cooldowns == {}


def test_remaining_cooldown_start_tick_and_reset_preserve_cast_recovery():
    s = session()
    s.gameplay_player_skill_timing = PlayerSkillTimingState(
        {"skill13": 130, "skill01": 120, "skill14": 140}, 101,
    )
    assert apply_skill_cooldown_effect(s, effect(
        "skill_arcanetorrent_talent_cd_reduction", values=(-5,),
    ), DEFAULT_STATIC_DB_PATH, now=100)
    assert s.gameplay_player_skill_timing.cooldowns["skill13"] == 125
    aura = effect("mage_singularity_buff_cdreduction_aura_effect", values=(-0.25,))
    assert not apply_skill_cooldown_effect(s, aura, DEFAULT_STATIC_DB_PATH, now=100)
    assert apply_skill_cooldown_effect(s, aura, DEFAULT_STATIC_DB_PATH, now=100, tick=True)
    assert s.gameplay_player_skill_timing.cooldowns["skill01"] == 115
    assert apply_skill_cooldown_effect(s, effect("skill_mightystab_kill_remove_cooldown"),
                                       DEFAULT_STATIC_DB_PATH, now=100)
    assert "skill14" not in s.gameplay_player_skill_timing.cooldowns
    assert apply_skill_cooldown_effect(s, effect("pvp_cooldown_reset"), DEFAULT_STATIC_DB_PATH, now=100)
    assert s.gameplay_player_skill_timing.cooldowns == {}
    assert s.gameplay_player_skill_timing.cast_ready_at == 101


def test_failed_resource_payment_rolls_back_and_premature_cast_never_pays(combat):
    s = session()
    index, geometry = skill(combat, "frozenorb")
    with patch.object(CharacterService, "_apply_player_skill_resource", side_effect=[False, True]) as pay:
        args = dict(skill_index=index, modifier_sources=(), speed=1, now=100)
        assert not CharacterService._accept_player_skill_timing(s, combat, **args)
        assert s.gameplay_player_skill_timing.cooldowns == {}
        assert s.gameplay_player_skill_timing.cast_ready_at == 0
        assert CharacterService._accept_player_skill_timing(s, combat, **args)
        assert not CharacterService._accept_player_skill_timing(s, combat, **args)
        assert pay.call_count == 2


def funded_session(combat, current_resource):
    s = session()
    s.gameplay_character = SimpleNamespace(character_id=23, class_id=1, level=100)
    s.gameplay_remote_actor_id = 123
    s.gameplay_combat_state = combat
    s.gameplay_inventory = SimpleNamespace(
        health_points=1000, secondary_resource=float(current_resource),
    )
    combat.register_player(
        123, 1000, maximum_resource=100, current_resource=current_resource,
    )
    return s


def test_resource_pricing_exception_releases_unpaid_timing_reservation(combat):
    s = funded_session(combat, 100)
    index, _geometry = skill(combat, "frozenorb")
    with patch.object(combat, "player_skill_resource_fractions", side_effect=RuntimeError("pricing failed")), \
         patch.object(s, "send_reliable_ordered") as send:
        with pytest.raises(RuntimeError, match="pricing failed"):
            CharacterService._accept_player_skill_timing(
                s, combat, skill_index=index, modifier_sources=(), speed=1, now=100,
            )
    send.assert_not_called()
    assert combat.player_resource[123][0] == 100
    assert s.gameplay_player_skill_timing.cooldowns == {}
    assert s.gameplay_player_skill_timing.cast_ready_at == 0


@pytest.mark.parametrize("current_resource", [0, 100])
def test_resource_packet_failure_retains_timing_only_after_actual_debit(combat, current_resource):
    s = funded_session(combat, current_resource)
    index, geometry = skill(combat, "frozenorb")
    with patch.object(s, "send_reliable_ordered", side_effect=OSError("send failed")):
        with pytest.raises(OSError, match="send failed"):
            CharacterService._accept_player_skill_timing(
                s, combat, skill_index=index, modifier_sources=(), speed=1, now=100,
            )
    state = s.gameplay_player_skill_timing
    if current_resource:
        expected = current_resource - 100 * geometry.resource_cost_fraction
        assert combat.player_resource[123][0] == pytest.approx(expected)
        assert s.gameplay_inventory.secondary_resource == pytest.approx(expected)
        assert state.cooldowns[geometry.cooldown_category.casefold()] == pytest.approx(100 + geometry.cooldown_seconds)
        assert state.cast_ready_at > 100
    else:
        # The rejection packet can fail too; that is still an unpaid cast.
        assert combat.player_resource[123][0] == 0
        assert state.cooldowns == {}
        assert state.cast_ready_at == 0


def test_resource_helper_false_after_debit_does_not_release_timing(combat):
    s = funded_session(combat, 100)
    index, geometry = skill(combat, "frozenorb")
    debit = combat.apply_player_skill_resource

    def debit_and_replace_inventory(*args, **kwargs):
        result = debit(*args, **kwargs)
        s.gameplay_inventory = SimpleNamespace(health_points=1000, secondary_resource=100)
        return result

    with patch.object(combat, "apply_player_skill_resource", side_effect=debit_and_replace_inventory), \
         patch.object(s, "send_reliable_ordered") as send:
        assert not CharacterService._accept_player_skill_timing(
            s, combat, skill_index=index, modifier_sources=(), speed=1, now=100,
        )
    send.assert_not_called()
    assert combat.player_resource[123][0] == pytest.approx(100 - 100 * geometry.resource_cost_fraction)
    assert s.gameplay_player_skill_timing.cooldowns[geometry.cooldown_category.casefold()] > 100
    assert s.gameplay_player_skill_timing.cast_ready_at > 100


def test_rollback_does_not_undo_concurrent_status_reset(combat):
    s = session()
    _, geometry = skill(combat, "frozenorb")
    reservation = reserve_player_skill_timing(s, geometry, now=100)
    apply_skill_cooldown_effect(s, effect("pvp_cooldown_reset"), DEFAULT_STATIC_DB_PATH, now=100)
    rollback_player_skill_timing(s, reservation)
    assert s.gameplay_player_skill_timing.cooldowns == {}


def test_repeated_flamethrower_casts_are_allowed_at_authored_cadence(combat):
    s = session()
    index, flame = skill(combat, "Flamethrower")
    with patch.object(CharacterService, "_apply_player_skill_resource", return_value=True) as pay:
        args = dict(skill_index=index, modifier_sources=(), speed=1)
        assert CharacterService._accept_player_skill_timing(s, combat, now=100, **args)
        assert not CharacterService._accept_player_skill_timing(s, combat, now=100.1, **args)
        assert CharacterService._accept_player_skill_timing(s, combat, now=100 + flame.skill_unblock_frame / 25, **args)
        assert pay.call_count == 2


@pytest.mark.parametrize("kind", ["bullet", "target_bullet", "target_point_bullet", "skill", "target_skill", "shifted_skill"])
def test_all_client_skill_commands_reject_without_payment_or_damage(kind):
    fixture = combat_fixtures.MixedEntityCastTests()
    fixture.setUp()
    try:
        s, combat = fixture.source, fixture.combat
        index, geometry = skill(combat, "frozenorb" if "bullet" in kind else "frostnova")
        s.gameplay_skill_book_ids = {index}
        s.gameplay_player_skill_timing = PlayerSkillTimingState({geometry.cooldown_category.casefold(): 1000})
        base = (0, index, 0.0, 0xFFFFFF00, 0xFFFFFF00)
        direction = (0.0, 0.0, 1.0, 0.0)
        target = (25.0, 0.0, 53.0)
        commands = {
            "bullet": (protocol.BULLET_SKILL_COMMAND, protocol.BulletSkill(*base, direction, 0)),
            "target_bullet": (protocol.TARGET_BULLET_SKILL_COMMAND, protocol.TargetBulletSkill(*base, direction, 0, fixture.monster)),
            "target_point_bullet": (protocol.TARGET_POINT_BULLET_SKILL_COMMAND, protocol.TargetPointBulletSkill(*base, direction, 0, target)),
            "skill": (protocol.SKILL_COMMAND, protocol.SkillCast(*base)),
            "target_skill": (protocol.TARGET_SKILL_COMMAND, protocol.TargetSkillCast(*base, fixture.monster)),
            "shifted_skill": (protocol.SHIFTED_SKILL_COMMAND, protocol.ShiftedSkill(*base, 0, target)),
        }
        command, request = commands[kind]
        stats = SimpleNamespace(minimum_damage=10, maximum_damage=10, critical_hit_chance=0,
                                critical_damage_multiplier=2, attack_speed=1, skill_modifier_sources=())
        with ExitStack() as stack:
            decoder = "decode_" + kind + ("_skill" if "bullet" in kind else "")
            stack.enter_context(patch.object(player_combat, decoder, return_value=request))
            stack.enter_context(patch.object(player_combat, "calculate_player_combat_stats", return_value=stats))
            stack.enter_context(patch.object(player_combat.time, "perf_counter", return_value=100))
            operations = [stack.enter_context(patch.object(CharacterService, name)) for name in (
                "_apply_player_skill_resource", "_consume_active_essence", "_cancel_mount_cast",
                "_remove_mount_ride", "_cleanse_player_skill_debuffs", "_snapshot_pvp_skill_targets",
            )]
            handler = CharacterService._handle_bullet_skill if "bullet" in kind else CharacterService._handle_spatial_skill
            handler(s, b"\x8b" + command.to_bytes(2, "little"))
            for operation in operations:
                operation.assert_not_called()
        assert not combat.pending_runtime_player_hits
        assert not s.gameplay_pending_pvp_hits
        assert not s.gameplay_projectile_timers
    finally:
        fixture.doCleanups()


def test_rejected_recast_preserves_all_first_cast_projectile_and_pvp_impacts():
    fixture = combat_fixtures.MixedEntityCastTests()
    fixture.setUp()
    try:
        index, geometry = skill(fixture.combat, "magicmissile")
        fixture.source.gameplay_skill_book_ids = {index}
        fixture.skill = replace(fixture.skill, skill_index=index)
        fixture.combat._player_skill_geometry[index] = replace(
            geometry, bullet_count=3, impact_count=2, reproduction_count=0,
            impact_radius=0, location_status_effects=(), location_visual_effects=(),
            bullet_death_effects=(),
        )
        monsters, players = fixture.cast()
        assert len(monsters) == len(players) == 3
        before_monsters = dict(fixture.combat.pending_runtime_player_hits)
        before_players = dict(fixture.source.gameplay_pending_pvp_hits)
        stats = SimpleNamespace(minimum_damage=10, maximum_damage=10, critical_hit_chance=0,
                                critical_damage_multiplier=2, attack_speed=1, skill_modifier_sources=())
        with patch.object(player_combat, "decode_bullet_skill", return_value=replace(fixture.skill, cast_id=999)), \
             patch.object(player_combat, "calculate_player_combat_stats", return_value=stats), \
             patch.object(CharacterService, "_apply_player_skill_resource") as pay:
            CharacterService._handle_bullet_skill(fixture.source, b"\x8b" + protocol.BULLET_SKILL_COMMAND.to_bytes(2, "little"))
        pay.assert_not_called()
        assert fixture.combat.pending_runtime_player_hits == before_monsters
        assert fixture.source.gameplay_pending_pvp_hits == before_players
        for pending in monsters:
            result = fixture.combat.resolve_runtime_player_hit(
                pending.source_actor_id, pending.cast_id, pending.impact_index, now=pending.due_at,
            )
            assert result is not None and result.damage > 0
    finally:
        fixture.doCleanups()
