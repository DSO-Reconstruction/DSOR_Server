"""Shared protocol imports, configuration, and pure runtime helpers."""

from dso_server.protocol.command_ids import command_id
import socket
import struct
import threading
import time
import os
import copy
import random
import json
import math
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from dso_server.services.base import CharacterService
from dso_server.paths import DATA_ROOT, DEFAULT_SOCIAL_STATE_PATH

from dso_server.persistence.account_state import (
    AccountState,
    AccountStateStore,
    CharacterProfile,
)
from dso_server.protocol.chat_protocol import (
    CHAT_CLIENT_SERVICE,
    decode_chat_client_hello,
    decode_chat_text,
    decode_map_chat_message,
    decode_whisper_chat_message,
    encode_chat_text,
    encode_received_map_chat,
    encode_received_whisper,
    WhisperChatMessage,
)

from dso_server.protocol.character_selection import (
    CLASS_NAMES,
    CharacterRecord,
    CharacterSelectionError,
    EquipmentRecord,
    BitReader,
    LOCAL_CHARACTER_ID_BASE,
    encode_character_selection,
)
from dso_server.paths import (
    DEFAULT_STATIC_DB_PATH,
)
from dso_server.persistence.static_game_database import StaticGameDatabase
from dso_server.protocol.character_generation import (
    CharacterGeneration,
    character_generation_editor_response,
    character_generation_result,
    decode_character_generation,
    encode_character_generation,
)
from dso_server.protocol.combat_protocol import (
    BULLET_SKILL_COMMAND,
    SHIFTED_SKILL_COMMAND,
    SKILL_COMMAND,
    SKILL_COMMAND_DERIVED_FLAG,
    SUSTAINED_SKILL_COMMAND,
    SUSTAINED_SUMMON_BATCH_BITS,
    TARGET_BULLET_SKILL_COMMAND,
    TARGET_POINT_BULLET_SKILL_COMMAND,
    TARGET_SKILL_COMMAND,
    BulletSkill,
    BulletSkillServer,
    BulletSkillServerCommand,
    CombatBatch,
    Hit,
    HitServerCommand,
    Kill,
    KillServerCommand,
    MonsterAttackBatch,
    ShiftedSkill,
    ShiftedSkillServerCommand,
    SustainedSkillServerCommand,
    SkillCast,
    SkillExecutionContext,
    SkillServerCommand,
    TargetSkillCast,
    TargetBulletSkill,
    TargetBulletSkillServerCommand,
    TargetPointBulletSkill,
    TargetPointBulletSkillServerCommand,
    TargetSkill,
    TargetSkillServerCommand,
    decode_bullet_skill,
    decode_monster_attack_batch,
    decode_shifted_skill,
    decode_skill,
    decode_target_skill,
    decode_target_bullet_skill,
    decode_target_point_bullet_skill,
    encode_combat_batch,
    encode_monster_attack_batch,
)
from dso_server.protocol.consumable_protocol import (
    ConsumableDefinition,
    HealthModifier,
    load_consumable_catalog,
    load_consumable_definition,
)
from dso_server.protocol.gameplay_commands import (
    LOCAL_GOLD_BALANCE,
    LOCAL_PLAYER_ACTOR_ID,
    encode_new_player,
)
from dso_server.protocol.gem_protocol import (
    INSERT_GEM_ACTION,
    REMOVE_GEM_ACTION,
    UNLOCK_GEM_SOCKET_ACTION,
    encode_gem_removal_sequence,
    encode_item_info_response,
    encode_socket_unlock_sequence,
    insert_gem,
    promote_local_loot_equipment_levels,
    remove_gem,
    upgrade_gem,
    upgrade_rune,
    unlock_gem_socket,
)
from dso_server.protocol.commerce_protocol import (
    PurchasePrice,
    TRANSACTION_COMMAND,
    add_purchased_item,
    debit_currency,
    debit_purchase_currency,
    decode_transaction,
    load_purchase_item_definition,
    load_shop_currency_resource,
    price_for_offer,
)
from dso_server.protocol.currency_protocol import (
    GAME_CURRENCY_BY_WIRE_ID,
    GAME_CURRENCY_CHANGED_COMMAND,
    GameCurrencyChanged,
)
from dso_server.protocol.item_receipt_protocol import (
    ITEM_RECEIVED_TEMPLATE_CATEGORIES,
    ItemReceiptGrant,
    build_item_receipts,
    item_received_message,
)
from dso_server.protocol.inventory_economy_protocol import (
    CRAFTING_COMMAND,
    CRAFTING_RESULT_ACCEPTANCE_COMMAND,
    SALVAGE_COMMAND,
    InventoryMutationBuilder,
    crafting_execution_count,
    crafting_price_for_payment,
    credit_currency_item,
    credit_fixed_currency,
    decode_crafting_request,
    decode_salvage_request,
    item_salvage_value,
    item_melt_reward,
    item_sell_value,
    load_crafting_recipe,
    load_enchantment_transfer_factor,
    load_recipe_book_resources,
    load_recipe_unlock_achievements,
    refresh_inventory_economy_values,
)
from dso_server.protocol.inventory_codec import (
    INVENTORY_INFO_COMMAND,
    clone_inventory_info,
    decode_inventory_info,
    equipment_allocation_snapshot,
    ACTIVE_AMMUNITION_SLOT,
    active_equipment_slots,
    consume_equipped_stack,
    equip_inventory_item,
    encode_bootstrap_inventory_info,
    inventory_armament_state,
    rekey_inventory_items,
    repair_bootstrap_class_equipment,
    repair_ammunition_slot_allocations,
    store_inventory_item,
)
from dso_server.protocol.item_set_protocol import ItemSetDatabase
from dso_server.protocol.socket_effect_protocol import (
    DISABLED_TRIGGERS,
    SocketEffectDatabase,
    apply_socket_drop_amounts,
    normalize_trigger as normalize_socket_trigger,
    parse_socket_modifier,
    prime_socket_effect_database,
)
from dso_server.protocol.socket_minion_protocol import load_socket_minion_definition
from dso_server.gameplay.item_set_procs import (
    collect_rules as collect_set_proc_rules,
    resolve_for_cast as resolve_set_procs_for_cast,
    resolve_for_trigger as resolve_set_trigger_effects,
)
from dso_server.protocol.gameplay_protocol import (
    ADD_STICKER_BOOK_FROM_INVENTORY_COMMAND,
    MESSAGE_COMMAND,
    MOVE_ITEM_EQUIPMENT_OPERATION,
    MOVE_ITEM_STORAGE_OPERATIONS,
    SET_STICKER_BOOK_FAVORITE_COMMAND,
    SET_TITLE_COMMAND,
    USE_TRAVEL_ITEM_COMMAND,
    SPLIT_STACK_OPERATIONS,
    UPGRADE_STICKER_BOOK_ITEM_COMMAND,
    InstanceConfigClientInfo,
    ActorStatsUpdate,
    ActorPlacement,
    ActorsEnterVicinity,
    ActorsEnterVicinityBatch,
    LocationEffectInfo,
    LocationEffectInfoBatch,
    TrapInfo,
    Move,
    MoveBatch,
    MoveServerCommand,
    MonsterSpawnBatch,
    MonsterUpdate,
    MonsterUpdateServerCommand,
    MonsterVicinityBatch,
    NewMonsterServerCommand,
    NPCBatch,
    OCPOfferBatch,
    PROP_INFO_COMMAND,
    StatusEffect,
    StatusEffectEntry,
    StatusEffectServerCommand,
    decode_gauntlet_notification,
    decode_add_stickerbook_from_inventory,
    decode_set_stickerbook_favorite,
    decode_upgrade_stickerbook_item,
    decode_inventory_item_action,
    decode_logout,
    decode_map_travel_request,
    decode_use_travel_item_request,
    decode_set_title_request,
    decode_move,
    decode_simple_gameplay_command,
    decode_unlock_map_request,
    encode_actors_enter_vicinity_batch,
    encode_actor_stats_update_batch,
    encode_discard_monster_batch,
    encode_gauntlet_notification_response,
    encode_monster_update_batch,
    encode_monster_vicinity_batch,
    encode_monster_spawn_batch,
    encode_move,
    encode_move_batch,
    encode_npc_batch,
    encode_instance_config_client_info,
    encode_discard_location_effect_batch,
    encode_location_effect_info_batch,
    encode_new_location_effect_batch,
    encode_new_trap_batch,
    encode_trap_tripped_batch,
    encode_discard_trap_batch,
    encode_ocp_methods,
    encode_ocp_offer_batch,
    encode_ocp_offer_response,
    encode_status_effect_batch,
    encode_force_transform_batch,
    ForceTransform,
)
from dso_server.gameplay.local_map_state import (
    BASELINE_MAP_EVENT_FLAGS,
    load_local_crafting_props,
    load_local_entry_position,
    load_local_map_exits,
    filter_parallel_world_exits_for_access,
    load_map_coordinate_frame,
    load_local_map_initialization,
    load_local_npc_definition,
    load_local_npc_definitions,
    load_local_npc_shop_id,
    load_local_spawn_position,
    load_local_travel_point_position,
    load_local_team_entries,
    local_npc_info,
    map_database_path,
)
from dso_server.gameplay.local_combat_state import (
    DamageResolution,
    MONSTER_CHASE_INTERVAL_SECONDS,
    MONSTER_MOVEMENT_PUBLISH_INTERVAL_TICKS,
    MONSTER_SIMULATION_RADIUS,
    MONSTER_VISIBILITY_ENTER_RADIUS,
    MONSTER_VISIBILITY_EXIT_RADIUS,
    SERVER_TICKS_PER_SECOND,
    STATUS_EFFECTS_DISABLED,
    VICTIM_STATUS_EFFECTS_DISABLED,
    LocalCombatState,
)
from dso_server.gameplay.player_combat_stats import (
    calculate_player_combat_stats,
    invalidate_player_combat_stats,
    player_combat_stats_cache_metrics,
    prime_player_combat_database,
)

PLAYER_ACTIVITY_VICINITY_RADIUS = max(
    MONSTER_SIMULATION_RADIUS,
    float(os.getenv("DSO_PLAYER_ACTIVITY_RADIUS", "60")),
)
WORLD_LOOT_VICINITY_RADIUS = max(
    4.0,
    float(
        os.getenv(
            "DSO_WORLD_LOOT_VICINITY_RADIUS",
            str(PLAYER_ACTIVITY_VICINITY_RADIUS),
        )
    ),
)
from dso_server.gameplay.local_mount_state import (
    BLACK_BEAR_MOUNT,
    begin_mount_cast,
    begin_mount_ride,
    find_mount,
    mount_status_effect,
    mount_transition_status_effects,
)
from dso_server.gameplay.local_monster_state import (
    load_local_monster_encounter,
    load_local_player_only_encounter,
)
from dso_server.gameplay.map_difficulty_state import resolve_map_access
from dso_server.protocol.loot_protocol import (
    load_payment_package_contents,
    open_payment_package,
)
from dso_server.protocol.chest_loot_protocol import (
    DEFAULT_CHEST_CONTENTS_MAP,
    open_mapped_chest,
    roll_chest_contents,
)
from dso_server.protocol.map_packets import (
    GameplaySwitchMapPacket,
    MapLoadPacket,
    SwitchMapPacket,
)
from dso_server.protocol.membership_protocol import (
    DELUXE_MEMBERSHIP,
    default_deluxe_expiry,
    membership_update_from_expiry,
)
from dso_server.protocol.new_player_codec import decode_new_player_prefix
from dso_server.gameplay.event_system import (
    UINT32_MAX,
    event_status_effect_ids,
    event_status_effect_specs,
    event_status_entry,
    load_event_catalog,
)
from dso_server.protocol.player_init_protocol import (
    ACTIVITY_SIGNIN_CALENDAR_COMMAND,
    CRAFTING_RESULT_COMMAND,
    CURRENCY_CHANGED_COMMAND,
    QUICKBAR_INFO_COMMAND,
    QUICK_SLOTS_INFO_COMMAND,
    TIMED_OFFERS_INFO_COMMAND,
    EVENT_PARTICIPATION_INFO_COMMAND,
    EVENT_PROGRESS_CHANGED_COMMAND,
    EVENT_PROGRESS_INFO_COMMAND,
    EVENT_UPDATE_COMMAND,
    NEW_NPC_COMMAND,
    EventUpdateEntry,
    EventProgressChanged,
    UPDATE_PROGRESS_TIER_SPENDER_CHALLENGES_COMMAND,
    ActivitySigninCalendar,
    ActivitySigninDay,
    Achievement,
    AchievementInfo,
    CurrencyChanged,
    CraftingResult,
    EventUpdate,
    QuickSlotInfo,
    QuickSlotsInfo,
    QuickbarInfo,
    Notification,
    RecipeBookEntry,
    RecipeBookInfo,
    ServerCommand,
    StickerbookEntry,
    StickerbookInfo,
    StickerbookItemInfo,
    STICKERBOOK_INFO_COMMAND,
    STICKERBOOK_ITEM_INFO_COMMAND,
    TimedOffer,
    TimedOffersInfo,
    TALENT_CATEGORY_POINTS_INFO_COMMAND,
    TALENT_SELECTION_INFO_COMMAND,
    STATUS_EFFECT_COMMAND,
    TalentCategoryPointsInfo,
    TalentCategorySelection,
    TalentSelectionEntry,
    TalentSelectionInfo,
    UpdateProgressTierSpenderChallenges,
    build_local_player_init_commands,
    local_client_player_init_commands,
    encode_event_update,
    encode_player_init_prefix,
    encode_server_command_batch,
    make_notification_entry,
)
from dso_server.protocol.pvp_protocol import (
    PVP_FLAG_COMMAND,
    PVPFlag,
    decode_pvp_flag,
    encode_pvp_flag_response,
)
from dso_server.protocol.pvp_match_protocol import (
    ABORT_MATCH_QUEUE_COMMAND,
    JOIN_BATTLEGROUND_RESPONSE_COMMAND,
    JOIN_MATCH_QUEUE_COMMAND,
    LEAVE_PVP_COMMAND,
    MATCH_MODE_INVALID,
    MATCHMAKING_EVENT_ABORTED_BY_SERVER,
    MATCHMAKING_EVENT_ABORTED_BY_USER,
    MATCHMAKING_EVENT_JOINED_BY_USER,
    MATCH_TEAM_SIZES,
    QUEUE_KIND_SINGLE,
    battleground_ruleset,
    CalendarTime,
    HONOR_LEVEL_THRESHOLDS,
    HonorPointsChanged,
    honor_level_title_for,
    JoinBattleground,
    JoinMatchQueueInfo,
    JoinMatchQueueResponse,
    MatchDescription,
    MatchInfo,
    MatchMakingInfo,
    MatchOver,
    MatchPlayer,
    MatchStart,
    MatchTeam,
    PVPMatchProtocolError,
    SetBattlegroundId,
    decode_abort_match_queue,
    decode_join_battleground_response,
    decode_join_match_queue,
    decode_leave_pvp,
    encode_honor_points_changed,
    encode_join_battleground,
    encode_join_match_queue_info,
    encode_join_match_queue_response,
    encode_match_countdown,
    encode_match_description,
    encode_match_info,
    encode_match_over,
    encode_match_start,
    encode_matchmaking_info,
    encode_set_battleground_id,
)
from dso_server.runtime.gameplay_routes import GameplayRoute, GameplayRouteTable
from dso_server.gameplay.pvp_match_state import (
    HONOR_PER_KILL,
    HONOR_PER_ROUND_PLAYED,
    HONOR_PER_ROUND_WON,
)
from dso_server.protocol.reward_protocol import (
    ACCEPTED_TIER_SPENDER_COMMAND,
    SHUFFLE_TIER_SPENDER_DEALS_COMMAND,
    ACTIVITY_SIGNIN_PROTOCOL_COMMAND,
    LOCAL_OCP_METHODS,
    LOCAL_OCP_OFFERS,
    NOTIFICATION_COMMAND as CLIENT_NOTIFICATION_COMMAND,
    OCP_BOOKING_COMMAND,
    OCP_BOOKING_NOTIFICATION_KIND,
    OCP_BOOKING_NOTIFICATION_VALUES,
    RewardDefinition,
    daily_login_reward_group,
    decode_activity_signin_request,
    decode_notification_action,
    decode_ocp_booking_request,
    decode_tier_spender_accept,
    decode_tier_spender_shuffle,
    load_offer_group_rewards,
    load_payment_signin_rewards,
    load_spender_rewards,
    ocp_booking_completed_key,
    ocp_booking_notification_payload,
    ocp_booking_pending_key,
    parse_ocp_booking_pending_key,
    pending_daily_login_day,
    premium_daily_login_reward_group,
    resolve_ocp_booking_reward,
)
from dso_server.protocol.talent_protocol import (
    ACTIVE_SKILL_TALENT_PRESELECTION,
    RESET_TALENT_CATEGORY_COMMAND,
    RESET_TALENT_PRESELECTION_COMMAND,
    SELECT_TALENT_COMMAND,
    SELECT_TALENT_PRESELECTION_COMMAND,
    TALENT_CATEGORY_NAMES,
    TalentDatabase,
    decode_reset_talent_category,
    decode_reset_talent_preselection,
    decode_select_talent,
    decode_select_talent_preselection,
)
from dso_server.protocol.shop_protocol import (
    decode_request_offer,
    encode_offer_response_batch,
    load_shop_offers,
)
from dso_server.gameplay.stickerbook_state import (
    collector_effect_definitions,
    collector_effect_wire_ids,
    consume_sticker_inventory_item,
    load_cosmetic_definition,
    load_inventory_mount_definition,
    load_sticker_definition,
    load_sticker_upgrade,
    owned_source_item_ids,
    reconcile_collector_effects,
    sticker_status_entry,
)
from dso_server.protocol.social_protocol import (
    BUDDY_LIST_COMMAND,
    GROUP_MEMBER_QUERY_OK,
    GROUP_MEMBER_QUERY_FAILED,
    decode_buddy_list_request,
    encode_buddy_status,
    CREATE_GROUP_COMMAND,
    CREATE_GUILD_COMMAND,
    CREATE_GUILD_RESULT_COMMAND,
    GROUP_FINDER_QUERY_COMMAND,
    GROUP_INVITATION_RESPONSE_COMMAND,
    GROUP_INVITE_COMMAND,
    GROUP_LEAVE_COMMAND,
    GROUP_MEMBER_QUERY_COMMAND,
    GROUP_STATUS_REQUEST_COMMAND,
    GROUPING_FAILED_ALREADY_IN_A_GROUP,
    GROUPING_FAILED_GROUP_IS_FULL,
    GROUPING_FAILED_NO_GROUP,
    GROUPING_FAILED_REQUEST_EXPIRED,
    GUILD_INVITATION_RESPONSE_COMMAND,
    GUILD_INVITE_COMMAND,
    GUILD_LEAVE_COMMAND,
    GUILD_LIST_REQUEST_COMMAND,
    FoundationRequest,
    GroupFinderEntry,
    GroupStatus,
    GuildMember as SocialGuildMember,
    GuildStatus as SocialGuildStatus,
    PlayerSummary,
    RemoteEquipmentVisual,
    RemotePlayer,
    UPDATE_SHOW_PLAYER_OVERHEAD_ICONS_COMMAND,
    decode_update_show_player_overhead_icons,
    decode_foundation_request,
    decode_group_invite,
    decode_group_member_query,
    decode_guild_invite,
    decode_invitation_response,
    decode_group_finder_query,
    decode_player_query,
    decode_uint32_request,
    encode_actors_left,
    encode_discard_player,
    encode_create_group_result,
    encode_grouping_failed,
    encode_guild_foundation_result,
    encode_group_finder_result,
    encode_group_member_query_result,
    encode_group_status,
    encode_guild_status,
    encode_invitation_response,
    encode_new_remote_player,
    encode_remote_player_info,
    encode_update_show_player_overhead_icons,
    encode_player_query_result,
)
from dso_server.persistence.social_state import (
    GROUP_MEMBER_LIMIT,
    GUILD_RANK_LEADER,
    GUILD_RANK_OFFICER,
    Group,
    GroupFullError,
    Guild,
    SocialStateStore,
    make_social_member,
)
from dso_server.protocol.world_loot_protocol import (
    PICKUP_ITEM_COMMAND,
    WISDOM_ITEM_TEMPLATE,
    add_world_drop_to_inventory,
    backfill_missing_equipment_enchantments,
    credit_currency_drop,
    decode_pickup_item,
    encode_discard_item_batch,
    encode_new_item_batch,
    encode_pickup_info_batch,
    encode_wisdom_pickup_response,
    encode_world_pickup_response,
    generate_monster_loot,
    prime_world_loot_cache,
    roll_new_equipment_enchantments,
)

# ─── Config ────────────────────────────────────────────────────────────────────

# The local player is synthesized after the gameplay-ready signal using the
# command order recovered from the retail initialization batch.
HOST        = os.getenv('DSO_HOST', '127.0.0.1')
CHEST_CONTENTS_MAP_PATH = Path(
    os.getenv("DSO_CHEST_CONTENTS_MAP", str(DEFAULT_CHEST_CONTENTS_MAP))
)
DAILY_LOGIN_REWARDS_ENABLED = os.getenv(
    "DSO_DAILY_LOGIN_REWARDS_ENABLED", "1"
).strip().lower() in {"1", "true", "yes", "on"}
EVENT_SCHEDULE_PATH = Path(
    os.getenv(
        "DSO_EVENT_SCHEDULE",
        str(DATA_ROOT / "events.json"),
    )
)
SET_EVENT_PARTICIPATION_COMMAND = command_id("Commands::SetEventParticipationCommand", 0x0078)
ACTIVE_EVENTS_COMMAND = command_id("Commands::ActiveEventsCommand", 0x00DE)
SERVER_GUID = bytes.fromhex('1234567890abcdef')
FRAG_SIZE   = 1208   # bytes per split fragment (MTU 1232 - 24B RakNet overhead)
TAIL_FRAG_SIZE = 1232  # captured 2192 tail uses MTU 1292 and up to 1232B of data
TAIL_PACE_SECONDS = 0.001
POST_SELECTION_CONFIG_DELAY_SECONDS = float(
    os.getenv(
        'DSO_POST_SELECTION_CONFIG_DELAY_SECONDS',
        # Session 57602 starts 0x00dd about 35 ms after the first character
        # response. Client cmd 0x006f arrives while that response is in flight.
        os.getenv('DSO_POST_SELECTION_FALLBACK_SECONDS', '0.03'),
    )
)
POST_SELECTION_TAIL_FALLBACK_SECONDS = float(
    os.getenv('DSO_POST_SELECTION_TAIL_FALLBACK_SECONDS', '0.08')
)
PRE_CHARACTER_SYNC_DELAY_SECONDS = float(
    os.getenv('DSO_PRE_CHARACTER_SYNC_DELAY_SECONDS', '0.017')
)
GAMEPLAY_ENTITY_DELAY_SECONDS = float(
    os.getenv('DSO_GAMEPLAY_ENTITY_DELAY_SECONDS', '1.0')
)
# Where a freshly created character first spawns: the tutorial dungeon, as
# on retail, now that its quests run (docs/QUEST_PROTOCOL.md). Kingshill,
# the town hub, is still reachable through DSO_NEW_CHARACTER_MAP.
NEW_CHARACTER_GAMEPLAY_MAP = os.getenv(
    "DSO_NEW_CHARACTER_MAP", "a0001_start_tutorial_dun"
)
# Wisdom progress a character starts with. This doubles as the cap applied to
# a persisted value, so zero also clears the full-progress default older saves
# were given. Set DSO_DEFAULT_WISDOM_XP to restore that.
DEFAULT_WISDOM_PROGRESS_XP = max(
    0, int(os.getenv("DSO_DEFAULT_WISDOM_XP", "0"))
)
GAMEPLAY_ROUTE_TTL_SECONDS = float(
    os.getenv('DSO_GAMEPLAY_ROUTE_TTL_SECONDS', '30')
)
MOUNT_CAST_DELAY_SECONDS = float(
    os.getenv(
        'DSO_MOUNT_CAST_DELAY_SECONDS',
        str(BLACK_BEAR_MOUNT.cast_duration_seconds),
    )
)
CHAT_CHANNEL_JOIN_DELAY_SECONDS = float(
    os.getenv('DSO_CHAT_CHANNEL_JOIN_DELAY_SECONDS', '0.05')
)
CHAT_REGION_IDS = {
    # Client ChatRegion enum order.  _Template_MapTemplate.ChatRegion owns
    # the per-map selection (Kingscity -> Kingshill -> region channel 2).
    'Grimford': 1,
    'Kingshill': 2,
    'Slifmoor': 3,
    'Teganswall': 4,
    'Norselands': 5,
    'Helios': 6,
    'Atlantis': 7,
    'Andrakasch': 8,
    'LorTak': 9,
    'Sea of Schadows': 10,
    'Qaizah': 11,
    'Land of Immortals': 12,
}
ACCOUNT_STATE_SAVE_INTERVAL_SECONDS = float(
    os.getenv('DSO_ACCOUNT_SAVE_INTERVAL_SECONDS', '15')
)
# High-frequency combat state is captured by the periodic autosave and every
# lifecycle boundary. Each eager snapshot detaches the whole account, so doing
# that per cast or per monster kill would compete with map simulation for the
# session lock without improving orderly disconnect/map-transfer durability.
COALESCED_ACCOUNT_SAVE_REASONS = frozenset({
    'skill resource changed',
    'monster kill XP',
    'essence consumed',
})
COALESCED_ACCOUNT_SAVE_INTERVAL_SECONDS = max(
    0.0,
    float(os.getenv('DSO_COALESCED_ACCOUNT_SAVE_SECONDS', '2.0')),
)
# A NAK can race the send of a replacement datagram when several gameplay
# threads are producing packets at once.  Do not immediately re-key a packet
# that was only just sent: its ACK may already be in flight.  The timed retry
# loop remains the fallback for a genuinely lost packet.
NAK_RETRANSMIT_GRACE_SECONDS = max(
    0.0, float(os.getenv('DSO_NAK_RETRANSMIT_GRACE_SECONDS', '0.10'))
)
RELIABLE_MAX_ATTEMPTS = max(
    1, int(os.getenv('DSO_RELIABLE_MAX_ATTEMPTS', '5'))
)
# A client that closes its UDP endpoint cannot send the ACKs which retire
# reliable gameplay traffic.  Without a liveness bound, its monster loop and
# reliable retries remain active forever and can eventually starve every
# service thread.  Ordinary connected clients continually send RakNet ACKs,
# movement, or state commands, so ten seconds is deliberately conservative.
RELIABLE_SESSION_IDLE_TIMEOUT_SECONDS = max(
    2.0,
    float(os.getenv('DSO_RELIABLE_SESSION_IDLE_TIMEOUT_SECONDS', '10')),
)
# A client that keeps sending is never timed out, even while it never
# acknowledges one reliable datagram. Report such a frame after this long.
RELIABLE_STUCK_WARNING_SECONDS = max(
    1.0,
    float(os.getenv('DSO_RELIABLE_STUCK_WARNING_SECONDS', '5')),
)
# Reliable-ordered traffic is head-of-line blocked when even one UDP datagram
# is lost. Stop producing optional simulation/loot traffic before a burst of
# AOE kills can overflow the client's receive window and trigger a NAK storm.
RELIABLE_BACKLOG_SOFT_LIMIT = max(
    16,
    int(os.getenv('DSO_RELIABLE_BACKLOG_SOFT_LIMIT', '32')),
)
# How many overdue reliable datagrams one session may re-send per retransmit
# pass. Recovery of an ordered stream is gated by its *oldest* missing frame,
# so re-sending the whole backlog at once buys nothing and re-floods the
# receive window that dropped it -- loss then feeds more loss and a 250 ms
# recovery becomes seconds. At the 20 Hz retransmit cadence this still drains
# 320 datagrams/second.
# How often each map instance reports what its simulation is producing. This
# is what separates "the simulation stopped" from "it ran and the client never
# showed it" during a client-visible freeze.
MONSTER_SIMULATION_HEARTBEAT_SECONDS = max(
    0.25,
    float(os.getenv('DSO_MONSTER_HEARTBEAT_SECONDS', '1.0')),
)
# A session's outbound lock is taken by the shared map simulation thread once
# per viewer, so a long hold by one client's own traffic stalls monster
# updates for everyone on that map. Report crossings of this bound.
OUTBOUND_CONTENTION_LOG_SECONDS = max(
    0.001,
    float(os.getenv('DSO_OUTBOUND_CONTENTION_LOG_SECONDS', '0.100')),
)
RELIABLE_RETRANSMIT_BURST_LIMIT = max(
    1,
    int(os.getenv('DSO_RELIABLE_RETRANSMIT_BURST_LIMIT', '16')),
)
# Walking samples travel unreliable, so they are never fragmented. Each
# MoveCommand adds 22 bytes; 55 fill a 1,225-byte datagram, the most the
# transport's datagram budget allows. The client drops a larger datagram
# unseen.
MONSTER_MOVEMENT_PACKET_BUDGET = min(
    55,
    max(
        8,
        int(os.getenv('DSO_MONSTER_MOVES_PER_PACKET', '40')),
    ),
)
# Spend the active essence's AmmoCost on every accepted cast. Off leaves
# essences as permanent damage buffs, the behaviour before consumption landed.
ESSENCE_CONSUMPTION_ENABLED = (
    os.getenv('DSO_CONSUME_ESSENCES', '1').strip().lower()
    not in {'0', 'false', 'no', 'off'}
)
VERBOSE_PACKET_LOGGING = (
    os.getenv('DSO_VERBOSE_PACKET_LOGGING', '0').strip().lower()
    not in {'0', 'false', 'no', 'off'}
)
GAMEPLAY_SERVICE_NAME = 'DrasaOnlineMapServer'
DEFAULT_GAMEPLAY_MAP = 'a0200_kingscity'

# Diagnostic trace flag for NPC interaction and map-transition initialization.
# Set DSO_NPC_TRACE=1 to print detailed logs for _begin_local_player_initialization,
# _active_npc_definitions, _resolve_npc_interaction, and _process_npc_interaction.
NPC_TRACE = os.getenv('DSO_NPC_TRACE', '').strip().casefold() not in ('', '0', 'false', 'no', 'off')

# Temporary local stress-testing switch. When enabled, every monster kill
# produces health/gold/Andermant plus one equipment item of each of the eight
# client rarities. It is deliberately disabled by default: one AOE kill can
# otherwise create hundreds of split reliable datagrams.
FORCE_LOOT_DROPS_FOR_TESTING = (
    os.getenv('DSO_FORCE_LOOT_DROPS', '0').strip().lower()
    not in {'0', 'false', 'no', 'off'}
)
# GameItem uses Util::Id's low 16 bits as its manager index.  The old runtime
# base 0x00F10000 generated indexes 1..4, colliding with bootstrap items
# 0x00F00001..04 even though their complete uint32 values differed.  Keep
# runtime-created inventory/world items in a disjoint index range.
GAME_ITEM_INDEX_MASK = 0x0000FFFF
RUNTIME_ITEM_ID_BASE = 0x00F11000
RAKNET_MAGIC = bytes.fromhex('00ffff00fefefefefdfdfdfd12345678')
CMD_001B, CMD_001B_BITS = encode_instance_config_client_info(
    InstanceConfigClientInfo(config_id=1, enabled=True), actor_id=0
)
EMPTY_EVENT_UPDATE, EMPTY_EVENT_UPDATE_BITS = encode_event_update(EventUpdate(()))
EMPTY_OCP_OFFERS, EMPTY_OCP_OFFERS_BITS = encode_ocp_offer_response((), actor_id=0)
EMPTY_OCP_METHODS, EMPTY_OCP_METHODS_BITS = encode_ocp_methods((), actor_id=0)

START_MS = int(time.time() * 1000)

def server_time():
    return int(time.time() * 1000) - START_MS

def pack_addr(ip: str, port: int) -> bytes:
    raw_ip = socket.inet_aton(ip)
    encoded_ip = bytes(byte ^ 0xff for byte in raw_ip)
    return b'\x04' + encoded_ip + struct.pack('>H', port)


def switch_map_payload(
        server_addr: str, *, enter_character_selection: bool = True):
    """Build cmd 0x0070 (SwitchMapCommand), including its final bool bit.

    The final bit is not padding.  The client handler clears its internal
    ``is game map`` flag when this bit is true.  Login -> character selection
    therefore uses true, while character selection -> gameplay must use false
    so ConnectState returns ``GameState`` and constructs IngameWidget.
    """
    return SwitchMapPacket(
        server_address=server_addr,
        enter_character_selection=enter_character_selection,
    ).encode()
