import math
import sqlite3
import threading
import time
from types import SimpleNamespace

from dso_server.gameplay.local_combat_state import LocalCombatState
from dso_server.gameplay.local_monster_state import load_local_monster_encounter
from dso_server.paths import DEFAULT_STATIC_DB_PATH
from dso_server.protocol.gameplay_protocol import StatusEffectEntry
from dso_server.services.base import CharacterService
from dso_server.services.pvp_combat import PVP_ARENA_DAMAGE_MULTIPLIER
import dso_server.services.character_server  # Binds CharacterService


def _create_mock_session(character_id, class_id=1, level=100, health=2000, pvp_enabled=True):
    s = SimpleNamespace()
    s.connected = True
    s.flow_lock = threading.RLock()
    s.gameplay_character = SimpleNamespace(
        character_id=character_id,
        class_id=class_id,
        level=level,
        pvp_level=1,
    )
    s.gameplay_inventory = SimpleNamespace(health_points=float(health), equipped_items={})
    s.gameplay_pvp_enabled = pvp_enabled
    s.gameplay_respawn_allowed = True
    s.gameplay_chase_generation = 1
    s.gameplay_chase_server_tick = 100
    s.gameplay_remote_actor_id = character_id
    s.gameplay_runtime_status_effects = {}
    s.gameplay_victim_status_effect_instances = {}
    s.gameplay_persistent_status_effects = {}
    s.gameplay_socket_effects = {}
    s.gameplay_item_set_effects = {}
    s.gameplay_item_set_programs = False
    s.gameplay_item_set_evaluation = None
    s.gameplay_socket_effect_evaluation = None
    s.gameplay_projectile_timers = {}
    s.gameplay_timed_status_effect_expiries = {}
    s.gameplay_pending_pvp_hits = {}
    s.gameplay_match = None
    s.gameplay_match_callback = None
    s.gameplay_presence_callback = lambda s, ev: True if ev[0] == "pvp_hostile" else None
    s.gameplay_next_status_effect_instance_id = 1
    s.gameplay_combat_state = None
    s.gameplay_socket_event_cooldowns = {}
    s.gameplay_socket_event_budget = {}
    s.gameplay_socket_event_state = {}
    s.gameplay_active_talents = ()
    s.gameplay_last_move = SimpleNamespace(relative_position=(0, 0, 0, 0), yaw=0.0)
    s.send_message = lambda *args, **kwargs: None
    s.send_reliable_ordered = lambda *args, **kwargs: None
    s.gameplay_deadline_timer = lambda delay, fn, args=(), kwargs={}: SimpleNamespace(
        cancel=lambda: None, start=lambda: None, daemon=True, args=args
    )
    return s


def test_banner_of_war_battle_frenzy_enemy_count():
    combat = LocalCombatState(
        load_local_monster_encounter(DEFAULT_STATIC_DB_PATH, "b1201_pve", include_spawn_areas=False),
        player_actor_id=200000001,
        player_health=2000,
    )
    warrior = _create_mock_session(200000001, class_id=1, health=2000)
    warrior.gameplay_combat_state = combat

    with sqlite3.connect(DEFAULT_STATIC_DB_PATH) as db:
        defiance_index = db.execute("SELECT rowid - 1 FROM _Template_Skill WHERE Id='defiance'").fetchone()[0]

    # Test player_skill_location_owner_effects with target_count=3
    effects = combat.player_skill_location_owner_effects(
        defiance_index,
        cast_id=101,
        target_count=3,
    )
    damage_effects = [e for e in effects if e.resource_id == "skill_defiance_buff_skill_damage"]
    assert len(damage_effects) == 3
    assert damage_effects[0].max_stack_size == 10
    assert damage_effects[0].values[1] == 0.02
    assert damage_effects[0].values[2] == 0.05

    # Test capped at 10 stacks when target_count=15
    effects_max = combat.player_skill_location_owner_effects(
        defiance_index,
        cast_id=102,
        target_count=15,
    )
    damage_effects_max = [e for e in effects_max if e.resource_id == "skill_defiance_buff_skill_damage"]
    assert len(damage_effects_max) == 10


def test_spikeshield_pvp_damage_reflection():
    combat = LocalCombatState(
        load_local_monster_encounter(DEFAULT_STATIC_DB_PATH, "b1201_pve", include_spawn_areas=False),
        player_actor_id=200000001,
        player_health=2000,
    )
    combat.player_health[200000001] = [2000, 2000]
    combat.player_health[200000002] = [2000, 2000]

    # Attacker: Mage (id: 200000002)
    attacker = _create_mock_session(200000002, class_id=2, health=2000, pvp_enabled=True)
    attacker.gameplay_combat_state = combat

    # Defender: Warrior (id: 200000001) with Spike Shield active (105% reflection)
    defender = _create_mock_session(200000001, class_id=1, health=2000, pvp_enabled=True)
    defender.gameplay_combat_state = combat

    with sqlite3.connect(DEFAULT_STATIC_DB_PATH) as db:
        spiked_buff_id = db.execute("SELECT rowid - 1 FROM _Template_StatusEffect WHERE Id='warrior_spikedShield_buff'").fetchone()[0]

    # Install warrior_spikedShield_buff with 1.05 reflection
    defender.gameplay_runtime_status_effects[1000] = (
        StatusEffectEntry(
            effect_id=spiked_buff_id,
            instance_id=1000,
            end_tick=500,
            tick_interval=25,
            start_tick=0,
            stack_index=0,
            total_duration=500,
            source_level=100,
            source_actor_id=200000001,
            update_timing=False,
            stacked=False,
            permanent=False,
            has_details=True,
            values=(1.05,),
        ),
        time.perf_counter() + 15.0,
    )

    published_resolutions = []
    attacker.gameplay_presence_callback = lambda s, msg: (
        published_resolutions.append(msg[1]) if msg[0] == "pvp_combat_resolution" else None
    )

    # Attacker hits defender for 400 damage
    key = ("pvp-hit", 999)
    pending = SimpleNamespace(
        round_token=None,
        combat=combat,
        target_session=defender,
        target_is_summon=False,
        target_actor_id=200000001,
        source_actor_id=200000002,
        skill_index=1,
        cast_id=1,
        projectile_index=0,
        impact_index=0,
        server_tick=100,
        position=(0.0, 0.0, 0.0),
        damage=400,
        rift_damage=0,
        victim_status_effects=(),
        user_status_effects=(),
        frost_hit=False,
        damage_type="Physical",
        player_damage_range=(100.0, 100.0),
        critical_hit=False,
        blocked=False,
    )
    attacker.gameplay_pending_pvp_hits[key] = pending

    CharacterService._resolve_pvp_skill_hit(attacker, 1, key)

    # Defender should have taken 400 damage
    assert defender.gameplay_inventory.health_points == 1600
    assert combat.player_health[200000001][0] == 1600

    # Attacker should have received reflected damage!
    # Reflected raw: 400 * 1.05 = 420 (in Open PvP, full 1.0x damage, no 0.25x scaling)
    assert combat.player_health[200000002][0] < 2000
    reflected_damage = 2000 - combat.player_health[200000002][0]
    assert reflected_damage == 420
    assert attacker.gameplay_inventory.health_points == 2000 - reflected_damage

    # Both resolutions should be published (direct hit + reflected hit)
    assert len(published_resolutions) == 2
    assert published_resolutions[0].target_actor_id == 200000001
    assert published_resolutions[1].target_actor_id == 200000002
    assert published_resolutions[1].damage == reflected_damage
    assert published_resolutions[1].attacker_actor_id == 200000001


def test_spikeshield_on_hit_armor_stacks():
    from dso_server.services.talent_events import dispatch_incoming_hit
    from dso_server.gameplay.local_combat_state import DamageResolution

    combat = LocalCombatState(
        load_local_monster_encounter(DEFAULT_STATIC_DB_PATH, "b1201_pve", include_spawn_areas=False),
        player_actor_id=200000001,
        player_health=2000,
    )
    warrior = _create_mock_session(200000001, class_id=1, health=2000)
    warrior.gameplay_combat_state = combat

    with sqlite3.connect(DEFAULT_STATIC_DB_PATH) as db:
        trigger_id = db.execute("SELECT rowid - 1 FROM _Template_StatusEffect WHERE Id='skill_spikedshield_buff_armor_trigger'").fetchone()[0]
        armor_buff_id = db.execute("SELECT rowid - 1 FROM _Template_StatusEffect WHERE Id='skill_spikedshield_buff_armor'").fetchone()[0]

    # Install skill_spikedshield_buff_armor_trigger on warrior
    warrior.gameplay_runtime_status_effects[1000] = (
        StatusEffectEntry(
            effect_id=trigger_id,
            instance_id=1000,
            end_tick=500,
            tick_interval=25,
            start_tick=0,
            stack_index=0,
            total_duration=500,
            source_level=100,
            source_actor_id=200000001,
            update_timing=False,
            stacked=False,
            permanent=False,
            has_details=True,
            values=(),
        ),
        time.perf_counter() + 15.0,
    )

    # 1st incoming hit lands on warrior
    resolution1 = DamageResolution(
        target_actor_id=200000001,
        attacker_actor_id=999,
        current_health=1800,
        maximum_health=2000,
        damage=200,
        killed=False,
        position=(0.0, 0.0, 0.0),
        server_tick=100,
        hit_tags=(0,),
        skill_index=0,
        victim_status_effects=(),
        source_actor_id=999,
    )
    dispatch_incoming_hit(warrior, resolution1)

    # Warrior should have gained 1 stack of skill_spikedshield_buff_armor
    armor_effects = [
        entry for entry, _exp in warrior.gameplay_runtime_status_effects.values()
        if entry.effect_id == armor_buff_id
    ]
    assert len(armor_effects) == 1
    assert armor_effects[0].stack_index == 0  # 1 stack

    # 2nd incoming hit lands on warrior
    resolution2 = DamageResolution(
        target_actor_id=200000001,
        attacker_actor_id=999,
        current_health=1600,
        maximum_health=2000,
        damage=200,
        killed=False,
        position=(0.0, 0.0, 0.0),
        server_tick=105,
        hit_tags=(0,),
        skill_index=0,
        victim_status_effects=(),
        source_actor_id=999,
    )
    dispatch_incoming_hit(warrior, resolution2)

    armor_effects2 = [
        entry for entry, _exp in warrior.gameplay_runtime_status_effects.values()
        if entry.effect_id == armor_buff_id
    ]
    assert len(armor_effects2) == 1
    assert armor_effects2[0].stack_index == 1  # 2 stacks


def test_spikeshield_healing_shield_on_hit_heal():
    from dso_server.services.talent_events import dispatch_incoming_hit
    from dso_server.gameplay.local_combat_state import DamageResolution
    from dso_server.protocol.talent_protocol import TalentDatabase

    combat = LocalCombatState(
        load_local_monster_encounter(DEFAULT_STATIC_DB_PATH, "b1201_pve", include_spawn_areas=False),
        player_actor_id=200000001,
        player_health=2000,
    )
    warrior = _create_mock_session(200000001, class_id=1, health=2000)
    combat.player_health[200000001] = [1200, 2000]
    warrior.gameplay_inventory.health_points = 1200
    warrior.gameplay_combat_state = combat

    # Learn Healing Shield talent
    tdb = TalentDatabase(DEFAULT_STATIC_DB_PATH)
    defn = tdb.definition("warrior_talent_spikeshield_heal")
    entries = tdb.status_effect_entries(defn, target_rank=1, actor_id=200000001, first_instance_id=100, server_tick=0)
    for e in entries:
        warrior.gameplay_persistent_status_effects[e.instance_id] = e

    with sqlite3.connect(DEFAULT_STATIC_DB_PATH) as db:
        spiked_index = db.execute("SELECT rowid - 1 FROM _Template_Skill WHERE Id='spikedShield'").fetchone()[0]

    # Cast Spike Shield with talent active
    mod_sources = CharacterService._active_skill_modifier_sources(warrior)
    user_effects = combat.player_skill_user_effects(spiked_index, cast_id=1, trigger="skillstart", modifier_effect_ids=mod_sources)
    CharacterService._send_player_skill_status_effects(warrior, user_effects, server_tick=100, label="cast-spikedshield")

    sent_packets = []
    warrior.send_reliable_ordered = lambda packet, *args, **kwargs: sent_packets.append(packet)

    # 1st incoming hit lands: 200 damage took warrior from 1200 to 1000
    combat.player_health[200000001][0] = 1000
    resolution1 = DamageResolution(
        target_actor_id=200000001,
        attacker_actor_id=999,
        current_health=1000,
        maximum_health=2000,
        damage=200,
        killed=False,
        position=(0.0, 0.0, 0.0),
        server_tick=105,
        hit_tags=(0,),
        skill_index=0,
        victim_status_effects=(),
        source_actor_id=999,
    )
    dispatch_incoming_hit(warrior, resolution1)

    # 5% of 2000 is 100 HP recovered: 1000 + 100 = 1100
    assert combat.player_health[200000001][0] == 1100
    assert warrior.gameplay_inventory.health_points == 1100.0
    assert len(sent_packets) >= 1

    # 2nd incoming hit lands: 100 damage took warrior from 1100 to 1000
    combat.player_health[200000001][0] = 1000
    resolution2 = DamageResolution(
        target_actor_id=200000001,
        attacker_actor_id=999,
        current_health=1000,
        maximum_health=2000,
        damage=100,
        killed=False,
        position=(0.0, 0.0, 0.0),
        server_tick=110,
        hit_tags=(0,),
        skill_index=0,
        victim_status_effects=(),
        source_actor_id=999,
    )
    dispatch_incoming_hit(warrior, resolution2)

    # Another 100 HP recovered: 1000 + 100 = 1100
    assert combat.player_health[200000001][0] == 1100
    assert warrior.gameplay_inventory.health_points == 1100.0


def test_spikeshield_pvp_heal_and_reflection():
    from dso_server.protocol.talent_protocol import TalentDatabase

    combat = LocalCombatState(
        load_local_monster_encounter(DEFAULT_STATIC_DB_PATH, "b1201_pve", include_spawn_areas=False),
        player_actor_id=200000001,
        player_health=2000,
    )
    combat.player_health[200000001] = [1500, 2000]
    combat.player_health[200000002] = [2000, 2000]

    # Attacker: Mage (id: 200000002)
    attacker = _create_mock_session(200000002, class_id=2, health=2000, pvp_enabled=True)
    attacker.gameplay_combat_state = combat

    # Defender: Warrior (id: 200000001) with Spike Shield active (105% reflection) AND Healing Shield
    defender = _create_mock_session(200000001, class_id=1, health=2000, pvp_enabled=True)
    defender.gameplay_inventory.health_points = 1500
    defender.gameplay_combat_state = combat

    # Add Healing Shield talent
    tdb = TalentDatabase(DEFAULT_STATIC_DB_PATH)
    defn = tdb.definition("warrior_talent_spikeshield_heal")
    entries = tdb.status_effect_entries(defn, target_rank=1, actor_id=200000001, first_instance_id=100, server_tick=0)
    for e in entries:
        defender.gameplay_persistent_status_effects[e.instance_id] = e

    with sqlite3.connect(DEFAULT_STATIC_DB_PATH) as db:
        spiked_buff_id = db.execute("SELECT rowid - 1 FROM _Template_StatusEffect WHERE Id='warrior_spikedShield_buff'").fetchone()[0]

    defender.gameplay_runtime_status_effects[1000] = (
        StatusEffectEntry(
            effect_id=spiked_buff_id,
            instance_id=1000,
            end_tick=500,
            tick_interval=25,
            start_tick=0,
            stack_index=0,
            total_duration=500,
            source_level=100,
            source_actor_id=200000001,
            update_timing=False,
            stacked=False,
            permanent=False,
            has_details=True,
            values=(1.05,),
        ),
        time.perf_counter() + 15.0,
    )

    sent_heal_packets = []
    defender.send_reliable_ordered = lambda packet, *args, **kwargs: sent_heal_packets.append(packet)

    published_resolutions = []
    attacker.gameplay_presence_callback = lambda s, msg: (
        published_resolutions.append(msg[1]) if msg[0] == "pvp_combat_resolution" else None
    )

    # Attacker hits defender for 400 damage
    key = ("pvp-hit", 999)
    pending = SimpleNamespace(
        round_token=None,
        combat=combat,
        target_session=defender,
        target_is_summon=False,
        target_actor_id=200000001,
        source_actor_id=200000002,
        skill_index=1,
        cast_id=1,
        projectile_index=0,
        impact_index=0,
        server_tick=100,
        position=(0.0, 0.0, 0.0),
        damage=400,
        rift_damage=0,
        victim_status_effects=(),
        user_status_effects=(),
        frost_hit=False,
        damage_type="Physical",
        player_damage_range=(100.0, 100.0),
        critical_hit=False,
        blocked=False,
    )
    attacker.gameplay_pending_pvp_hits[key] = pending

    CharacterService._resolve_pvp_skill_hit(attacker, 1, key)

    # Defender took 400 damage from 1500 -> 1100, then healed 5% of 2000 (+100) -> 1200!
    assert defender.gameplay_inventory.health_points == 1200
    assert combat.player_health[200000001][0] == 1200
    assert len(sent_heal_packets) >= 1

    # Attacker took 420 reflected damage
    assert combat.player_health[200000002][0] == 2000 - 420


