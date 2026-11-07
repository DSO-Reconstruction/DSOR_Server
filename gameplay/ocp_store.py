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
}

EVENT_ATTIRE_EVENT_IDS_BY_PRODUCT = {
    product_key: EVENT_OCP_PRODUCT_EVENT_IDS[product_key]
    for product_key in (
        FULL_MOON_ATTIRE_PRODUCT_KEY,
        NEW_MOON_ATTIRE_PRODUCT_KEY,
        DEFEAT_UNDEFEATABLE_ATTIRE_PRODUCT_KEY,
    )
}
EVENT_ATTIRE_ITEMS_BY_PRODUCT_CLASS = {
    FULL_MOON_ATTIRE_PRODUCT_KEY: FULL_MOON_ATTIRE_ITEMS_BY_CLASS,
    NEW_MOON_ATTIRE_PRODUCT_KEY: NEW_MOON_ATTIRE_ITEMS_BY_CLASS,
    DEFEAT_UNDEFEATABLE_ATTIRE_PRODUCT_KEY:
        DEFEAT_UNDEFEATABLE_ATTIRE_ITEMS_BY_CLASS,
}
EVENT_ATTIRE_PRODUCT_KEYS = frozenset(EVENT_ATTIRE_EVENT_IDS_BY_PRODUCT)
EVENT_OCP_PRODUCT_KEYS = frozenset(EVENT_OCP_PRODUCT_EVENT_IDS)

# Most OCP purchases settle as one inventory grant. Road to Triumph and Unlock
# Victory are native multi-item offer groups, so their contents are granted as
# an aligned list after the purchase is confirmed.
DEFEAT_UNDEFEATABLE_BUNDLE_REWARDS = {
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
