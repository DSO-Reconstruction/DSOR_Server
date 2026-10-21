"""Daily previews, exact-level unlocks, recovery, and monster loot provenance."""
from dataclasses import replace
import json
from pathlib import Path
import random
import sqlite3
import unittest
from unittest.mock import Mock, patch

import pytest

from dso_server.paths import DEFAULT_STATIC_DB_PATH as DB
from dso_server.gameplay.local_combat_state import LocalCombatState
from dso_server.gameplay.local_monster_state import load_local_monster_encounter
from dso_server.gameplay.player_combat_stats import CombatModifier, calculate_player_combat_stats
from dso_server.persistence.account_state import _inventory_from_document, _inventory_to_document
from dso_server.protocol.character_selection import CLIENT_SKILL_MANAGER_ROW_OFFSET
from dso_server.protocol.inventory_codec import InventoryInfo
from dso_server.protocol.player_init_protocol import (
    ExitInfo, QuickSlotInfo, QuickSlotsInfo, SKILL_BOOK_INFO_COMMAND, XP_CHANGED_COMMAND,
    SPECIAL_OFFER_COMMAND, NOTIFICATION_COMMAND,
    decode_server_command_batch, encode_server_command_batch,
)
from dso_server.protocol.reward_protocol import daily_login_offers, load_offer_group_rewards
from dso_server.protocol.skill_book_protocol import experience_commands, skill_book_snapshot
from dso_server.protocol.world_loot_protocol import (
    EQUIPMENT_ITEM_CATEGORIES,
    add_world_drop_to_inventory, decode_world_pickup_response,
    encode_world_pickup_response, generate_monster_loot,
)
from dso_server.services.player_progression import load_player_progression_catalog
from dso_server.runtime.transport import Session
from dso_server.services.player_init_world import PlayerInitializationWorld
from dso_server.tests.fixtures.characters import character_profile, character_server


def test_daily_reward_previews_match_retail_packet():
    capture = json.loads((Path(__file__).parent / 'fixtures/daily_login_offers_retail.json').read_text())
    packet = bytes.fromhex(capture['payload'])
    commands = decode_server_command_batch(packet, capture['bit_length'])
    assert daily_login_offers(DB, 'mage') == commands[0].value
    assert encode_server_command_batch(commands) == (packet, capture['bit_length'])


def test_real_initialization_sends_previews_before_notification_and_read_skill_book():
    case = unittest.TestCase()
    try:
        server = character_server(case, (character_profile(),))
        session = Session(('127.0.0.1', 40993), Mock())
        session.gameplay_character = server.character_selection.active_character
        state = server._resolve_initialization_character(session)
        server._resolve_initialization_inventory(state)
        server._encode_initialization_new_player(state)
        server._resolve_initialization_talents(state)
        server._resolve_initialization_persistent_effects(session, state)
        server._resolve_initialization_vitals(state)
        server._apply_initialization_state(session, state)
        world = PlayerInitializationWorld((), ExitInfo(()), None, None, (), None)
        with patch('dso_server.services.player_init_world.DAILY_LOGIN_REWARDS_ENABLED', True):
            commands = server._build_initialization_commands(session, state, world)
        offers = next(c for c in commands if c.command_id == SPECIAL_OFFER_COMMAND)
        assert offers.value == daily_login_offers(DB, 'mage')
        notifications = [c for c in commands if c.command_id == NOTIFICATION_COMMAND and c.value.entries]
        assert len(notifications) == 1
        assert commands.index(offers) < commands.index(notifications[0])
        book = next(c for c in commands if c.command_id == SKILL_BOOK_INFO_COMMAND)
        assert book.value.skills and not any(entry.flags[2] for entry in book.value.skills)
        subset = (offers, notifications[0], book)
        assert decode_server_command_batch(*encode_server_command_batch(subset)) == subset
    finally:
        case.doCleanups()


@pytest.mark.parametrize('class_name', ['warrior', 'mage', 'ranger', 'dwarf'])
def test_each_daily_preview_contains_exactly_the_claimable_items(class_name):
    offers = daily_login_offers(DB, class_name)
    for groups in (offers.groups_40, offers.groups_60):
        assert len(groups) == 6 and groups[-1] == groups[-2]
        for group in groups:
            rewards = load_offer_group_rewards(DB, group.resource_id)
            assert [(entry.offer.field_50, entry.offer.field_74) for entry in group.offers] == [
                (reward.item_id, reward.amount) for reward in rewards]
            assert all(entry.offer.field_c0 and entry.offer.field_74 > 0 for entry in group.offers)


def warrior_skill_id(resource):
    with sqlite3.connect(DB) as db:
        row = db.execute('SELECT rowid FROM _Template_Skill WHERE Id=?', (resource,)).fetchone()
    return row[0] + CLIENT_SKILL_MANAGER_ROW_OFFSET


def test_reconnect_skill_book_has_no_new_flags_and_future_skills_stay_locked():
    entries = {entry.skill_id: entry.flags for entry in skill_book_snapshot(DB, 0, 1).skills}
    assert entries[warrior_skill_id('angrystrike')] == (True, False, False)
    assert entries[warrior_skill_id('mightyswing')] == (False, False, False)
    assert all(not flags[2] for flags in entries.values())


@pytest.mark.parametrize('start,end,expected', [(1,2,None), (2,3,'mightyswing'), (3,4,None), (1,4,None)])
def test_only_skills_at_the_exact_new_level_are_announced_once(start, end, expected):
    catalog = load_player_progression_catalog(DB)
    levels = catalog.player_level_xp['warrior']
    transition = catalog.advance_player(class_id=0, current_level=start,
        current_xp=levels[start], amount=levels[end]-levels[start])
    commands = experience_commands(DB, 0, transition, actor_id=1)
    assert [c.command_id for c in commands] == [SKILL_BOOK_INFO_COMMAND, XP_CHANGED_COMMAND, SKILL_BOOK_INFO_COMMAND]
    fresh = [entry.skill_id for entry in commands[0].value.skills if entry.flags[2]]
    assert fresh == ([] if expected is None else [warrior_skill_id(expected)])
    assert all(not entry.flags[2] for entry in commands[2].value.skills)
    assert decode_server_command_batch(*encode_server_command_batch(commands)) == commands


def test_skill_already_on_another_action_bar_is_not_added_again():
    slots = QuickSlotsInfo(((QuickSlotInfo(0, 'angrystrike'),), (QuickSlotInfo(0, 'mightyswing'),)))
    book = skill_book_snapshot(DB, 0, 3, new_level=3, quick_slots=slots)
    assert not any(entry.flags[2] for entry in book.skills)


@pytest.fixture(scope='module')
def encounter():
    return load_local_monster_encounter(DB, 'b1201_pve', include_spawn_areas=False)


@pytest.mark.parametrize('class_name', ['warrior', 'mage', 'ranger', 'dwarf'])
@pytest.mark.parametrize('inventory', [None, InventoryInfo()])
def test_zero_displayed_health_regeneration_does_not_heal(encounter, class_name, inventory):
    stats = calculate_player_combat_stats(DB, class_name=class_name, level=100, inventory=inventory)
    combat = LocalCombatState(encounter, player_actor_id=1, player_health=1000)
    combat.register_player(1, 1000, current_health=500,
        regeneration_fraction=stats.health_regeneration_fraction,
        regeneration_absolute=stats.health_regeneration_absolute,
        regeneration_interval=stats.health_regeneration_interval)
    assert combat.regenerate_players(now=combat.last_player_health_regeneration_at[1]+60) == ()
    assert combat.player_health[1][0] == 500


def test_authored_regeneration_bonus_still_heals(encounter):
    stats = calculate_player_combat_stats(DB, class_name='warrior', level=100,
        inventory=InventoryInfo(), modifier_sources=(
            CombatModifier('HealthPointsRegeneration', ('0.02', 'relative')),
            CombatModifier('HealthPointsRegeneration', ('12', 'absolute')),
        ))
    combat = LocalCombatState(encounter, player_actor_id=1, player_health=1000)
    combat.register_player(1, 1000, current_health=500,
        regeneration_fraction=stats.health_regeneration_fraction,
        regeneration_absolute=stats.health_regeneration_absolute,
        regeneration_interval=1)
    transitions = combat.regenerate_players(now=combat.last_player_health_regeneration_at[1]+1)
    assert transitions == ((1, 500, 532, 1000, 32),)


@pytest.mark.parametrize('block_chance', [0.0, 1.0])
def test_repeated_monster_hits_delay_rage_decay_including_blocked_hits(encounter, block_chance):
    combat = LocalCombatState(encounter, player_actor_id=1, player_health=100_000_000)
    combat.register_player(1, 100_000_000, maximum_resource=100, current_resource=80,
        resource_regeneration_fraction=-0.05, resource_regeneration_delay=5)
    monster_id = next(a for a in encounter.actor_ids if combat.monsters[a].attack is not None)
    position = combat.monster_positions[monster_id]
    wire = encounter.coordinate_frame.world_to_wire(position)
    attacks = combat.maybe_start_monster_attacks(wire, server_tick=100,
        now=max(combat.last_monster_attack_at.values())+10,
        target_actor_id=1, eligible_actor_ids={monster_id})
    attack = replace(attacks[0], motion_type='Target', target_position=position, projectile_origin=position)
    for index in range(8):
        pending = replace(attack, due_at=attack.due_at+index*2,
                          impact_tick=attack.impact_tick+index*50)
        combat.pending_monster_attacks[monster_id] = pending
        hits = combat.resolve_due_monster_attacks(wire, now=pending.due_at,
            player_actor_id=1, block_chance=block_chance, block_damage_reduction=.75)
        assert len(hits) == 1 and hits[0].blocked == bool(block_chance)
        assert combat.regenerate_player_resources(now=pending.due_at+1) == ()
        assert combat.player_resource[1][0] == 80
    assert combat.regenerate_player_resources(now=pending.due_at+5) == ()
    assert combat.regenerate_player_resources(now=pending.due_at+6) == ((1,75.0,100.0,-5.0),)


def test_hits_do_not_delay_mana_or_other_players_resource(encounter):
    combat = LocalCombatState(encounter, player_actor_id=1, player_health=1000)
    combat.register_player(1, 1000, current_resource=50, maximum_resource=100,
                           resource_regeneration_absolute=10)
    before = dict(combat.last_player_resource_change_at)
    now = combat.last_player_resource_regeneration_at[1]+1
    combat.note_player_attacked(1, now=now)
    combat.note_player_attacked(99, now=now)
    assert combat.last_player_resource_change_at == before
    assert combat.regenerate_player_resources(now=now) == ((1,60.0,100.0,10.0),)


def test_mana_regenerates_while_casting_faster_than_its_interval(encounter):
    """Casting every 0.8 s, mana still ticks once a second: a spend (or a vitals
    refresh) must not restart the regeneration clock; only rage decay waits."""
    combat = LocalCombatState(encounter, player_actor_id=1, player_health=1000)
    combat.register_player(1, 1000, current_resource=20, maximum_resource=100,
                           resource_regeneration_absolute=5)
    start = combat.last_player_resource_regeneration_at[1]
    gained = 0.0
    for index in range(1, 11):
        now = start + index * 0.8
        ok, *_ = combat.apply_player_skill_resource(1, cost_fraction=0.01, gain_fraction=0.0, now=now)
        assert ok
        combat.register_player(1, 1000, maximum_resource=100, resource_regeneration_absolute=5)
        gained += sum(t[3] for t in combat.regenerate_player_resources(now=now))
    # Ten casts over 8 s: eight ticks of 5.
    assert gained == 40
    assert combat.player_resource[1][0] == 20 - 10 + 40


@pytest.mark.parametrize('class_name', ['warrior', 'mage', 'ranger', 'dwarf'])
@pytest.mark.parametrize('player_level,monster_level', [(100,15), (15,100), (100,125), (100,145)])
@pytest.mark.parametrize('force_drops', [False, True])
def test_monster_equipment_uses_monster_level_and_keeps_identification_on_pickup(
        class_name, player_level, monster_level, force_drops):
    equipment = []
    # Normal rolls may have no equipment; deterministic repeated kills exercise
    # that path as well as the forced all-rarities diagnostic path.
    for seed in range(8):
        result = generate_monster_loot(DB, 'a0604_normal_g_Herold', class_name=class_name,
            player_level=player_level, monster_level=monster_level, position=(1,0,1),
            next_item_id=1000, rng=random.Random(seed), force_drops=force_drops)
        equipment.extend(drop for drop in result.drops if drop.item_category in EQUIPMENT_ITEM_CATEGORIES)
        if equipment:
            break
    assert equipment
    for drop in equipment:
        assert drop.item.item_level == monster_level
        if drop.item.field_08 == 0:
            assert not drop.item.field_3c
        elif drop.item.field_08 <= 4:
            assert drop.item.field_3c
        # Some unique/event templates explicitly do not require identification.
        mutation = add_world_drop_to_inventory(InventoryInfo(), drop)
        packet, bits = encode_world_pickup_response(drop, actor_id=1, inventory=mutation)
        decoded = decode_world_pickup_response(packet, bits)
        restored = _inventory_from_document(_inventory_to_document(mutation.inventory))
        assert restored.items[0].field_3c == drop.item.field_3c
        assert restored.items[0].item_level == monster_level
        assert decoded.inventory_states[0].items[0].field_3c == drop.item.field_3c
        assert decoded.inventory_states[0].items[0].item_level == monster_level
