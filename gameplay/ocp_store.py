"""Server-side routing for event attire Euro offers."""

# The native payment packages already exist in static.db4 with their event
# attire names and icons. Reuse each as one offer and choose the
# character-specific item on the server; do not synthesize static database
# rows or expose the other classes' attire choices.
FULL_MOON_ATTIRE_PRODUCT_KEY = "8_event_pass_fullmoon_1.0000_0_NONE"
FULL_MOON_ATTIRE_ITEMS_BY_CLASS = {
    "warrior": "costume_event_pass_fullmoon_warrior",
    "mage": "costume_event_pass_fullmoon_mage",
    "ranger": "costume_event_pass_fullmoon_ranger",
    "dwarf": "costume_event_pass_fullmoon_dwarf",
}
# Reuse the native New Moon payment package and resolve it to the active
# character's class-specific attire on the server, as for Full Moon.
NEW_MOON_ATTIRE_PRODUCT_KEY = "8_event_pass_newmoon_1.0000_0_NONE"
NEW_MOON_ATTIRE_ITEMS_BY_CLASS = {
    "warrior": "costume_event_pass_newmoon_warrior",
    "mage": "costume_event_pass_newmoon_mage",
    "ranger": "costume_event_pass_newmoon_ranger",
    "dwarf": "costume_event_pass_newmoon_dwarf",
}
DEFEAT_UNDEFEATABLE_ATTIRE_PRODUCT_KEY = (
    "8_event_pass_dtu_1.0000_0_NONE"
)
DEFEAT_UNDEFEATABLE_ATTIRE_ITEMS_BY_CLASS = {
    "warrior": "costume_event_pass_dtu_warrior",
    "mage": "costume_event_pass_dtu_mage",
    "ranger": "costume_event_pass_dtu_ranger",
    "dwarf": "costume_event_pass_dtu_dwarf",
}
BIG_GAME_HUNT_ATTIRE_PRODUCT_KEY = "8_event_pass_bgh_1.0000_0_NONE"
BIG_GAME_HUNT_ATTIRE_ITEMS_BY_CLASS = {
    "warrior": "costume_event_pass_bgh_warrior",
    "mage": "costume_event_pass_bgh_mage",
    "ranger": "costume_event_pass_bgh_ranger",
    "dwarf": "costume_event_pass_bgh_dwarf",
}
BIG_GAME_HUNT_SMALL_BUNDLE_PRODUCT_KEY = "8_bgh_box_small_1.0000_0_NONE"
BIG_GAME_HUNT_MEDIUM_BUNDLE_PRODUCT_KEY = "8_bgh_box_med_1.0000_0_NONE"
BIG_GAME_HUNT_BIG_BUNDLE_PRODUCT_KEY = "8_bgh_box_big_1.0000_0_NONE"
BIG_GAME_HUNT_JEWEL_PACK_PRODUCT_KEY = "8_glory_jewel_pack_1_1.0000_0_NONE"

DRAGAN_ATTIRE_PRODUCT_KEY = "8_event_pass_dragan_1.0000_0_NONE"
DRAGAN_ATTIRE_ITEMS_BY_CLASS = {
    "warrior": "costume_event_pass_dragan_warrior",
    "mage": "costume_event_pass_dragan_mage",
    "ranger": "costume_event_pass_dragan_ranger",
    "dwarf": "costume_event_pass_dragan_dwarf",
}
DRAGAN_LAST_GUARDIAN_PRODUCT_KEY = "8_last_guardian_package_1_1.0000_0_NONE"
DRAGAN_JUDGMENT_DAY_PRODUCT_KEY = "8_judgment_day_package_1_1.0000_0_NONE"
DRAGAN_EYE_OF_AVERSION_PRODUCT_KEY = "8_eye_of_aversion_package_1.0000_0_NONE"
DRAGAN_EYE_OF_FEAR_PRODUCT_KEY = "8_eye_of_fear_package_1.0000_0_NONE"
DRAGAN_EYE_OF_COLLAPSE_PRODUCT_KEY = "8_eye_of_collapse_package_1.0000_0_NONE"
DRAGAN_FALLEN_HEART_PRODUCT_KEY = "8_fallen_heart_package_1.0000_0_NONE"

HALLOWEEN_SMALL_PIE_PRODUCT_KEY = "8_ghostly_pie_small_2023_1.0000_0_NONE"
HALLOWEEN_MEDIUM_PIE_PRODUCT_KEY = "8_ghostly_pie_medium_2023_1.0000_0_NONE"
HALLOWEEN_BIG_PIE_PRODUCT_KEY = "8_ghostly_pie_big_2023_1.0000_0_NONE"
HALLOWEEN_BUNDLE_PRODUCT_KEY = "8_ghost_festival_bundle_1.0000_0_NONE"
HALLOWEEN_ATTIRE_PRODUCT_KEY = "8_event_pass_halloween_1.0000_0_NONE"
HALLOWEEN_ATTIRE_ITEMS_BY_CLASS = {
    "warrior": "costume_event_pass_halloween_warrior",
    "mage": "costume_event_pass_halloween_mage",
    "ranger": "costume_event_pass_halloween_ranger",
    "dwarf": "costume_event_pass_halloween_dwarf",
}
SUMMER_FESTIVAL_ATTIRE_PRODUCT_KEY = "8_event_pass_summer_1.0000_0_NONE"
SUMMER_FESTIVAL_ATTIRE_ITEMS_BY_CLASS = {
    "warrior": "costume_event_pass_sum_warrior",
    "mage": "costume_event_pass_sum_mage",
    "ranger": "costume_event_pass_sum_ranger",
    "dwarf": "costume_event_pass_sum_dwarf",
}
SUMMER_FESTIVAL_PREP_START_PRODUCT_KEY = (
    "101_e_sum_prep_start_1.0000_0_NONE"
)
SUMMER_FESTIVAL_START_PRODUCT_KEY = (
    "101_e_sum_festival_start_1.0000_0_NONE"
)
SUMMER_FORTUNE_BAG_SMALL_PRODUCT_KEY = "8_summer_bag_small_1.0000_0_NONE"
SUMMER_FORTUNE_BAG_MEDIUM_PRODUCT_KEY = "8_summer_bag_med_1.0000_0_NONE"
SUMMER_FORTUNE_BAG_BIG_PRODUCT_KEY = "8_summer_bag_big_1.0000_0_NONE"
SUMMER_NIGHTDREAM_CHEST_PRODUCT_KEY = "101_summer_dream_1.0000_0_NONE"
SUMMER_LUCKY_CHEST_PRODUCT_KEY = "101_summer_lucky_1.0000_0_NONE"
SUMMER_SET_CHEST_PRODUCT_KEY = "101_summer_set_1.0000_0_NONE"

DARK_DWARF_LEVIN_CRYSTAL_PACKAGE_PRODUCT_KEY = (
    "101_levin_crystal_pack_1.0000_0_NONE"
)
DARK_DWARF_FORGEFYRE_MAGIC_POTION_PRODUCT_KEY = (
    "101_iridescent_magic_potion_regular_1.0000_0_NONE"
)
DARK_DWARF_FORGEFYRE_POTION_PRODUCT_KEY = (
    "101_iridescent_potion_regular_1.0000_0_NONE"
)
DARK_DWARF_LEVIN_CRYSTAL_SUPER_PACKAGE_PRODUCT_KEY = (
    "101_levin_crystal_super_pack_1.0000_0_NONE"
)
DARK_DWARF_SHOULDERS_FORTUNE_CHEST_PRODUCT_KEY = (
    "101_shoulders_fortune_chest_1.0000_0_NONE"
)
DARK_DWARF_CLOAK_FORTUNE_CHEST_PRODUCT_KEY = (
    "101_cloak_fortune_chest_1.0000_0_NONE"
)
DARK_DWARF_COPPER_FARTHING_PACKAGE_PRODUCT_KEY = (
    "101_copper_farthing_pack_1.0000_0_NONE"
)

DEFEAT_UNDEFEATABLE_LAVA_ESSENCE_PRODUCT_KEY = (
    "101_lava_essence_pack_1.0000_0_NONE"
)
DEFEAT_UNDEFEATABLE_LAVA_MATERIAL_PRODUCT_KEY = (
    "101_lava_material_pack_1.0000_0_NONE"
)
DEFEAT_UNDEFEATABLE_HERO_BAG_PRODUCT_KEY = (
    "8_invincible_hero_bag_1_1.0000_0_NONE"
)
DEFEAT_UNDEFEATABLE_ROAD_TO_TRIUMPH_PRODUCT_KEY = (
    "101_dtu216_big_1.0000_0_NONE"
)
DEFEAT_UNDEFEATABLE_UNLOCK_VICTORY_PRODUCT_KEY = (
    "101_dtu216_med_1.0000_0_NONE"
)

# Every Defeat the Undefeatable purchase is only offered during its global
# event, including the event-pass attire and the two lava packages whose
# native PaymentPackages rows already carry this event id.
EVENT_OCP_PRODUCT_EVENT_IDS = {
    FULL_MOON_ATTIRE_PRODUCT_KEY: "fullmoon_event_02",
    NEW_MOON_ATTIRE_PRODUCT_KEY: "newmoon_event_02",
    DEFEAT_UNDEFEATABLE_ATTIRE_PRODUCT_KEY: "boss_bonus_event_02",
    DEFEAT_UNDEFEATABLE_LAVA_ESSENCE_PRODUCT_KEY: "boss_bonus_event_02",
    DEFEAT_UNDEFEATABLE_LAVA_MATERIAL_PRODUCT_KEY: "boss_bonus_event_02",
    DEFEAT_UNDEFEATABLE_HERO_BAG_PRODUCT_KEY: "boss_bonus_event_02",
    DEFEAT_UNDEFEATABLE_ROAD_TO_TRIUMPH_PRODUCT_KEY: "boss_bonus_event_02",
    DEFEAT_UNDEFEATABLE_UNLOCK_VICTORY_PRODUCT_KEY: "boss_bonus_event_02",
    BIG_GAME_HUNT_ATTIRE_PRODUCT_KEY: "scaling_dungeon_kill_event_01",
    BIG_GAME_HUNT_SMALL_BUNDLE_PRODUCT_KEY: "scaling_dungeon_kill_event_01",
    BIG_GAME_HUNT_MEDIUM_BUNDLE_PRODUCT_KEY: "scaling_dungeon_kill_event_01",
    BIG_GAME_HUNT_BIG_BUNDLE_PRODUCT_KEY: "scaling_dungeon_kill_event_01",
    BIG_GAME_HUNT_JEWEL_PACK_PRODUCT_KEY: "scaling_dungeon_kill_event_01",
    DRAGAN_ATTIRE_PRODUCT_KEY: "dragan_event_03",
    DRAGAN_LAST_GUARDIAN_PRODUCT_KEY: "dragan_event_03",
    DRAGAN_JUDGMENT_DAY_PRODUCT_KEY: "dragan_event_03",
    DRAGAN_EYE_OF_AVERSION_PRODUCT_KEY: "dragan_event_03",
    DRAGAN_EYE_OF_FEAR_PRODUCT_KEY: "dragan_event_03",
    DRAGAN_EYE_OF_COLLAPSE_PRODUCT_KEY: "dragan_event_03",
    DRAGAN_FALLEN_HEART_PRODUCT_KEY: "dragan_event_03",
    HALLOWEEN_SMALL_PIE_PRODUCT_KEY: "halloween_event_01",
    HALLOWEEN_MEDIUM_PIE_PRODUCT_KEY: "halloween_event_01",
    HALLOWEEN_BIG_PIE_PRODUCT_KEY: "halloween_event_01",
    HALLOWEEN_BUNDLE_PRODUCT_KEY: "halloween_event_01",
    HALLOWEEN_ATTIRE_PRODUCT_KEY: "halloween_event_01",
    # The summer costume is sold throughout preparation and festival phases.
    SUMMER_FESTIVAL_ATTIRE_PRODUCT_KEY: (
        "summer_festival_event_01",
        "summer_festival_event_01_prefarming",
        "summer_festival_event_01_quests",
    ),
    SUMMER_FESTIVAL_PREP_START_PRODUCT_KEY:
        "summer_festival_event_01_prefarming",
    SUMMER_FESTIVAL_START_PRODUCT_KEY: "summer_festival_event_01",
    SUMMER_FORTUNE_BAG_SMALL_PRODUCT_KEY:
        "summer_festival_event_01_prefarming",
    SUMMER_FORTUNE_BAG_MEDIUM_PRODUCT_KEY: (
        "summer_festival_event_01",
        "summer_festival_event_01_prefarming",
    ),
    SUMMER_FORTUNE_BAG_BIG_PRODUCT_KEY: (
        "summer_festival_event_01",
        "summer_festival_event_01_prefarming",
    ),
    SUMMER_NIGHTDREAM_CHEST_PRODUCT_KEY: (
        "summer_festival_event_01",
        "summer_festival_event_01_prefarming",
    ),
    SUMMER_LUCKY_CHEST_PRODUCT_KEY: (
        "summer_festival_event_01",
        "summer_festival_event_01_prefarming",
    ),
    SUMMER_SET_CHEST_PRODUCT_KEY: (
        "summer_festival_event_01",
        "summer_festival_event_01_prefarming",
    ),
    DARK_DWARF_LEVIN_CRYSTAL_PACKAGE_PRODUCT_KEY: "e_ide_challenge_01",
    DARK_DWARF_FORGEFYRE_MAGIC_POTION_PRODUCT_KEY: "e_ide_challenge_01",
    DARK_DWARF_FORGEFYRE_POTION_PRODUCT_KEY: "e_ide_challenge_01",
    DARK_DWARF_LEVIN_CRYSTAL_SUPER_PACKAGE_PRODUCT_KEY: "e_ide_challenge_01",
    DARK_DWARF_SHOULDERS_FORTUNE_CHEST_PRODUCT_KEY: "e_ide_challenge_01",
    DARK_DWARF_CLOAK_FORTUNE_CHEST_PRODUCT_KEY: "e_ide_challenge_01",
    DARK_DWARF_COPPER_FARTHING_PACKAGE_PRODUCT_KEY: "e_ide_challenge_01",
}

EVENT_ATTIRE_EVENT_IDS_BY_PRODUCT = {
    product_key: EVENT_OCP_PRODUCT_EVENT_IDS[product_key]
    for product_key in (
        FULL_MOON_ATTIRE_PRODUCT_KEY,
        NEW_MOON_ATTIRE_PRODUCT_KEY,
        DEFEAT_UNDEFEATABLE_ATTIRE_PRODUCT_KEY,
        BIG_GAME_HUNT_ATTIRE_PRODUCT_KEY,
        DRAGAN_ATTIRE_PRODUCT_KEY,
        HALLOWEEN_ATTIRE_PRODUCT_KEY,
        SUMMER_FESTIVAL_ATTIRE_PRODUCT_KEY,
    )
}
EVENT_ATTIRE_ITEMS_BY_PRODUCT_CLASS = {
    FULL_MOON_ATTIRE_PRODUCT_KEY: FULL_MOON_ATTIRE_ITEMS_BY_CLASS,
    NEW_MOON_ATTIRE_PRODUCT_KEY: NEW_MOON_ATTIRE_ITEMS_BY_CLASS,
    DEFEAT_UNDEFEATABLE_ATTIRE_PRODUCT_KEY:
        DEFEAT_UNDEFEATABLE_ATTIRE_ITEMS_BY_CLASS,
    BIG_GAME_HUNT_ATTIRE_PRODUCT_KEY: BIG_GAME_HUNT_ATTIRE_ITEMS_BY_CLASS,
    DRAGAN_ATTIRE_PRODUCT_KEY: DRAGAN_ATTIRE_ITEMS_BY_CLASS,
    HALLOWEEN_ATTIRE_PRODUCT_KEY: HALLOWEEN_ATTIRE_ITEMS_BY_CLASS,
    SUMMER_FESTIVAL_ATTIRE_PRODUCT_KEY: SUMMER_FESTIVAL_ATTIRE_ITEMS_BY_CLASS,
}
EVENT_ATTIRE_PRODUCT_KEYS = frozenset(EVENT_ATTIRE_EVENT_IDS_BY_PRODUCT)
EVENT_OCP_PRODUCT_KEYS = frozenset(EVENT_OCP_PRODUCT_EVENT_IDS)


def ocp_product_event_is_active(product_key, active_event_ids):
    """Return whether any event phase that enables this OCP product is active."""

    required_events = EVENT_OCP_PRODUCT_EVENT_IDS.get(product_key)
    if required_events is None:
        return False
    if isinstance(required_events, str):
        required_events = (required_events,)
    active = {str(event_id).casefold() for event_id in active_event_ids}
    return any(str(event_id).casefold() in active for event_id in required_events)

# Most OCP purchases settle as one inventory grant. Road to Triumph and Unlock
# Victory are native multi-item offer groups, so their contents are granted as
# an aligned list after the purchase is confirmed.
DEFEAT_UNDEFEATABLE_BUNDLE_REWARDS = {
    # Keep native Dragan loot bags intact so their own tooltip and random
    # rewards run after the player opens them.
    DRAGAN_LAST_GUARDIAN_PRODUCT_KEY: (("event_dra_payment_package_01", 1),),
    DRAGAN_JUDGMENT_DAY_PRODUCT_KEY: (("event_dra_payment_package_02", 1),),
    DRAGAN_EYE_OF_AVERSION_PRODUCT_KEY: (
        ("item_ingredient_eye_of_aversion", 1),
        ("e_dra_03_hardmode_bosskey", 2),
        ("e_sar_chest_key", 5),
    ),
    DRAGAN_EYE_OF_FEAR_PRODUCT_KEY: (
        ("item_ingredient_eye_of_fear", 1),
        ("e_dra_03_hardmode_bosskey", 2),
        ("e_sar_chest_key", 5),
    ),
    DRAGAN_EYE_OF_COLLAPSE_PRODUCT_KEY: (
        ("item_ingredient_eye_of_collapse", 1),
        ("e_dra_03_hardmode_bosskey", 4),
        ("e_sar_chest_key", 10),
    ),
    DRAGAN_FALLEN_HEART_PRODUCT_KEY: (
        ("item_ingredient_fallen_heart", 1),
        ("e_dra_03_hardmode_bosskey", 4),
        ("e_sar_chest_key", 20),
    ),
    HALLOWEEN_SMALL_PIE_PRODUCT_KEY: (("item_event_hal_food_ghost_cake", 1),),
    HALLOWEEN_MEDIUM_PIE_PRODUCT_KEY: (("item_event_hal_food_ghost_cake", 10),),
    HALLOWEEN_BIG_PIE_PRODUCT_KEY: (("item_event_hal_food_ghost_cake", 50),),
    HALLOWEEN_BUNDLE_PRODUCT_KEY: (("ghost_festival_bundle", 1),),
    DEFEAT_UNDEFEATABLE_LAVA_ESSENCE_PRODUCT_KEY:
        (("lava_essence_l", 5),),
    DEFEAT_UNDEFEATABLE_LAVA_MATERIAL_PRODUCT_KEY:
        (("item_ingredient_mercenary_heartlava", 500),),
    DEFEAT_UNDEFEATABLE_HERO_BAG_PRODUCT_KEY:
        (("invincible_hero_bag", 1),),
    DEFEAT_UNDEFEATABLE_ROAD_TO_TRIUMPH_PRODUCT_KEY:
        (
            ("currency_stickerbook_upgrade", 25),
            ("jewel_buff_dtu_weaponmod_common", 1),
            ("ammunition_damage_epic", 999),
            ("potion_health_restore_rarity_03", 20),
            ("respawn_soulguardian_2", 20),
            ("misc_resurrect_partymember_01", 20),
        ),
    DEFEAT_UNDEFEATABLE_UNLOCK_VICTORY_PRODUCT_KEY:
        (
            ("elemental_dungeon_key", 10),
            ("e_bos_02_circus_teleport_02", 10),
            ("e_bos_02_chest_key", 10),
            ("scaling_dungeon_boss_key_01", 10),
            ("ammunition_damage_rare", 999),
            ("potion_health_restore_rarity_02", 10),
            ("respawn_soulguardian_1", 10),
        ),
    # Guaranteed contents from the native BGH offer groups. Keep the jewel
    # pack intact so the client's normal random-reward table remains active.
    BIG_GAME_HUNT_SMALL_BUNDLE_PRODUCT_KEY: (
        ("difficulty_key_common", 300),
        ("e_bgh_all_drop_ingredient", 200),
        ("e_bgh_all_ingredient", 3),
        ("scaling_dungeon_boss_key_01", 100),
    ),
    BIG_GAME_HUNT_MEDIUM_BUNDLE_PRODUCT_KEY: (
        ("e_bgh_boss_all_travelitem", 2),
        ("e_bgh_boss_heredur_travelitem", 2),
        ("e_bgh_boss_herald_travelitem", 2),
        ("e_bgh_boss_sargon_travelitem", 2),
        ("e_bgh_boss_nefertari_travelitem", 2),
        ("e_bgh_boss_balor_travelitem", 2),
        ("e_bgh_boss_mortis_travelitem", 2),
        ("e_bgh_boss_guardian_black", 2),
        ("e_bgh_boss_guardian_all", 2),
    ),
    BIG_GAME_HUNT_BIG_BUNDLE_PRODUCT_KEY: (
        ("difficulty_key_common", 380),
        ("e_bgh_all_drop_ingredient", 250),
        ("e_bgh_all_ingredient", 5),
        ("scaling_dungeon_boss_key_01", 120),
        ("e_bgh_boss_all_travelitem", 3),
        ("e_bgh_boss_balor_travelitem", 3),
        ("e_bgh_boss_guardian_all", 3),
        ("e_bgh_boss_guardian_black", 3),
        ("e_bgh_boss_herald_travelitem", 3),
        ("e_bgh_boss_heredur_travelitem", 3),
        ("e_bgh_boss_mortis_travelitem", 3),
        ("e_bgh_boss_nefertari_travelitem", 3),
        ("e_bgh_boss_sargon_travelitem", 3),
        ("bgh_event_chest", 1),
    ),
    BIG_GAME_HUNT_JEWEL_PACK_PRODUCT_KEY:
        (("surprise_jewel_pack_glory", 1),),
    # Summer offers reuse their native PaymentPackages/Offergroup contents.
    SUMMER_FESTIVAL_PREP_START_PRODUCT_KEY: (
        ("e_sum_01_reward_key", 1),
        ("e_sum_01_ferry_ticket", 10),
    ),
    SUMMER_FESTIVAL_START_PRODUCT_KEY: (
        ("e_sum_01_reward_key", 10),
        ("e_sum_01_minigame_ticket_01", 40),
        ("e_sum_01_fishing_rod_01", 30),
        ("difficulty_key_common", 100),
        ("e_sum_coins", 1500),
    ),
    SUMMER_FORTUNE_BAG_SMALL_PRODUCT_KEY: (("summer_fortune_bag", 1),),
    SUMMER_FORTUNE_BAG_MEDIUM_PRODUCT_KEY: (("summer_fortune_bag", 10),),
    SUMMER_FORTUNE_BAG_BIG_PRODUCT_KEY: (("summer_fortune_bag", 50),),
    SUMMER_NIGHTDREAM_CHEST_PRODUCT_KEY: (("summer_nightdream_chest", 1),),
    SUMMER_LUCKY_CHEST_PRODUCT_KEY: (("summer_lucky_chest", 1),),
    SUMMER_SET_CHEST_PRODUCT_KEY: (("item_ingredient_mercenary_verdant", 100),),
    DARK_DWARF_LEVIN_CRYSTAL_PACKAGE_PRODUCT_KEY:
        (("lightningset_material_general", 100),),
    DARK_DWARF_FORGEFYRE_MAGIC_POTION_PRODUCT_KEY:
        (("darkdwarf_potion_hour_s", 2),),
    DARK_DWARF_FORGEFYRE_POTION_PRODUCT_KEY:
        (("darkdwarf_potion_times_s", 20),),
    DARK_DWARF_LEVIN_CRYSTAL_SUPER_PACKAGE_PRODUCT_KEY:
        (("lightningset_material_general", 1000),),
    DARK_DWARF_SHOULDERS_FORTUNE_CHEST_PRODUCT_KEY:
        (("darkdwarf_lightningset_shoulders_chest", 1),),
    DARK_DWARF_CLOAK_FORTUNE_CHEST_PRODUCT_KEY:
        (("darkdwarf_lightningset_cloak_chest", 1),),
    # Dark Dwarf Heist 2026 FAQ package contents. The older _Offer rows in
    # this client build still point at Sargon/difficulty keys instead.
    DARK_DWARF_COPPER_FARTHING_PACKAGE_PRODUCT_KEY: (
        ("epicmount_steambike_02", 1),
        ("lightningset_material_general", 200),
        ("e_sar_chest_key", 20),
        ("e_e_viscanium_hardmode_key", 30),
        ("cooking_key", 100),
        ("ammunition_damage_epic", 1999),
    ),
}

# Older server versions advertised one synthetic key per class. Keep their
# pending bookings settleable without advertising those keys to clients.
LEGACY_FULL_MOON_ATTIRE_PRODUCT_ITEMS = {
    "8_costume_event_pass_fullmoon_mage_1.0000_0_NONE":
        "costume_event_pass_fullmoon_mage",
    "8_costume_event_pass_fullmoon_warrior_1.0000_0_NONE":
        "costume_event_pass_fullmoon_warrior",
    "8_costume_event_pass_fullmoon_ranger_1.0000_0_NONE":
        "costume_event_pass_fullmoon_ranger",
    "8_costume_event_pass_fullmoon_dwarf_1.0000_0_NONE":
        "costume_event_pass_fullmoon_dwarf",
}
