"""Resolve inventory chest contents from ``chest_contents.json``.

The static client database names most server-side loot lists but does not ship
their contents.  ``chest_contents.json`` combines the explicit database
edges with contents recovered from the client tooltip text.  This module is a
strict runtime adapter for that report:

* explicit item-table rewards are preferred;
* tooltip candidates must resolve to a real item template;
* a complete consecutive 100% chance block is one weighted choice;
* remaining percentage entries are independent bonus rolls; and
* nested containers are awarded as items rather than opened recursively.

Unresolved or genuinely ambiguous mandatory entries reject the open before
the caller consumes the chest.  This prevents a partial/guessed reward from
silently destroying an item.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import json
from pathlib import Path
import random
import re
import sqlite3
from typing import Any

from dso_server.runtime import resident_content
from dso_server.gameplay.mystic_cube_config import (
    CONTENT_KEYS,
    cube_content_weights,
)
from dso_server.paths import DEFAULT_CHEST_CONTENTS_MAP, DESCRIPTION_CHEST_CONTENTS_MAP

from dso_server.protocol.commerce_protocol import (
    OFFER_RARITY_IDS,
    PurchaseItemDefinition,
    load_purchase_item_definition,
)
from dso_server.protocol.currency_protocol import GameCurrencyChanged
from dso_server.protocol.inventory_codec import InventoryInfo
from dso_server.protocol.inventory_economy_protocol import (
    InventoryMutationBuilder,
    credit_currency_template,
    credit_fixed_currency,
    refresh_inventory_economy_values,
)
from dso_server.protocol.player_init_protocol import CurrencyChanged
from dso_server.protocol.world_loot_protocol import (
    EQUIPMENT_RARITIES,
    roll_equipment_reward,
    roll_new_equipment_enchantments,
)



_INVENTORY_CONTAINER_CATEGORIES = {
    "LootGenItem",
    "LootGenItemAutoIdent",
    "Selective",
    "LotteryTicket",
    "Consumable",
    "HatchEgg",
}
_RARITY_HINT = re.compile(
    r"\((common|uncommon|improved|magic|magical|rare|extraordinary|epic|legendary|unique|set|mythic)\)\s*$",
    re.IGNORECASE,
)
_LEVEL_SUFFIX = re.compile(r"(?:^|_)Lv(\d+)$", re.IGNORECASE)
_ADVENTURER_CHEST_LEVEL = re.compile(r"^levelchest_lvl_?(\d+)$", re.IGNORECASE)
_LYDIAS_GRACE_LEVEL = re.compile(r"^ancestral_books_lv(\d+)$")
# The ten Lydia's Grace boxes, level 10 to 100. No client data ships their
# loot lists; the official Ancestral Ruins article states that each opening
# levels the box by 10 and pays Polished Dragon Scales, and that the tenth
# opening also pays a Statue of Abundance.
# UNVERIFIED: the article does not state how many scales an opening pays, so
# one is given. Each scale is "consumed when used" for 5 to 15 random
# Andermant, as its own description says.
_LYDIAS_GRACE_LEVELS = tuple(range(10, 101, 10))
_LYDIAS_GRACE_SCALES = 1
_LYDIAS_GRACE_FINAL_REWARD = "wealth_stone_carving"
_RARITY_NAMES = {
    "common": "Common",
    "uncommon": "Uncommon",
    "improved": "Uncommon",
    "magic": "Magic",
    "magical": "Magic",
    "rare": "Rare",
    "extraordinary": "Rare",
    "epic": "Epic",
    "legendary": "Epic",
    "unique": "Unique",
    "set": "Set",
    "mythic": "Mythic",
}
_CANONICAL_CURRENCY_IDS = (
    "money_rc",
    "money_vc_gold",
    "money_vc",
    "money_gc_no_auto_use",
    "money_uc_no_auto_use",
    "money_hc_no_auto_use",
    "e_chr_2012_event_currency",
)
_RARITY_VARIANT_NUMBERS = {
    "Common": "01",
    "Uncommon": "02",
    "Magic": "03",
    "Rare": "04",
    "Epic": "05",
}
_STANDARD_GEM_FAMILIES = (
    "amethyst",
    "cyanit",
    "diamond",
    "onyx",
    "ruby",
)
_TOOLTIP_ITEM_ALIASES = {
    # Marketing names are not the internal item Name values, but the
    # stickerbook database identifies the exact live pet resources.
    "gloomy harpy pet (magic, increased armor value)": (
        "pet_harpy_black_10_percent_armor"
    ),
    "bug-collecting jabbax pet (epic)": "pet_frogman_epic",
    "radiant cyanit": "gem_cyanit_f",
}


UNRESOLVED_CHEST_MESSAGE = (
    "This chest has not been implemented. "
    "Please send a bug report with the expected chest contents."
)


class UnresolvedChestError(ValueError):
    """No loot table or tooltip resolves the chest's contents."""


# The containers a player knows as chests; consumables and eggs without
# contents are not "chests that have not been implemented". Selective
# chests never roll (roll_chest_contents).
_CHEST_CATEGORIES = frozenset({
    "LootGenItem", "LootGenItemAutoIdent", "LotteryTicket",
})


@dataclass(frozen=True)
class _MysticCubeTier:
    """Authored payout and gem ladder for one Mystic Cube tier."""

    gold: int
    glyph: int
    wisdom: int
    gem_ranks: tuple[str, ...]


# One weighted choice per cube (Integer boundaries, total 100):
# unique 15, class set 22, gold 15, Ancient Wisdom 15, Glyph 15, gem 15,
# andermant 3.
_MYSTIC_CUBE_TIERS = {
    "mystic_cube_common": _MysticCubeTier(19, 99, 5, ("a", "b", "c")),
    "mystic_cube_uncommon": _MysticCubeTier(50, 199, 5, ("b", "c", "d")),
    "mystic_cube_magic": _MysticCubeTier(199, 299, 10, ("d", "e", "f")),
    "mystic_cube_rare": _MysticCubeTier(499, 499, 15, ("d", "e", "f", "g", "h")),
    "mystic_cube_epic": _MysticCubeTier(999, 999, 20, ("e", "f", "g", "h", "i", "j")),
}
_MYSTIC_CUBE_ANDERMANT = 999
_MYSTIC_CUBE_SHARED_UNIQUES = (
    "pet_fat_demon",             # Horned Fire Gnome
    "pet_raptor_01",             # Little Raptor
    "pet_rooster",               # Fire Rooster
    "pet_fennec",                # Fennec
    "emote_psy",                 # Dracanian Style
    "costume_skeleton_02",       # Glowing Bone Man Costume
    "costume_skeleton_03",       # Armored Skeleton Costume
    "epicmount_rocket_02",       # Firework Rocket
    "epicmount_indian_rhino_01", # Rash Rhino
    "epicmount_scorpion_01",     # Black War Scorpion
    "epicmount_jumong_horse_01", # The Legendary King's War Horse
)
# The premium trader's three uniques, one set per class.
_MYSTIC_CUBE_PREMIUM_UNIQUES = {
    "warrior": (
        "warrior_unique_2h_mace_premium_01",
        "warrior_unique_lh_shield_premium_01",
        "warrior_unique_shoulders_premium_01",
    ),
    "ranger": (
        "ranger_unique_2h_bow_premium_01",
        "ranger_unique_lh_offensequiver_premium_01",
        "ranger_unique_shoulders_premium_01",
    ),
    "mage": (
        "mage_unique_2h_staff_premium_01",
        "mage_unique_lh_book_premium_01",
        "mage_unique_shoulders_premium_01",
    ),
    "dwarf": (
        "dwarf_unique_2h_rifle_premium_01",
        "dwarf_unique_lh_wrench_premium_01",
        "dwarf_unique_shoulders_premium_01",
    ),
}
_MYSTIC_CUBE_SET_PIECES = {
    "warrior": (
        "warrior_unique_amulet_mysticcube_set_02",
        "warrior_unique_ring_mysticcube_set_02",
        "warrior_unique_weaponmodifier_mysticcube_set_02",
        "warrior_unique_rh_mace_mysticcube_set_02",
        "warrior_unique_lh_shield_mysticcube_set_02",
    ),
    "ranger": (
        "ranger_unique_amulet_mysticcube_set_02",
        "ranger_unique_ring_mysticcube_set_02",
        "ranger_unique_weaponmodifier_mysticcube_set_02",
        "ranger_unique_rh_longbow_mysticcube_set_02",
        "ranger_unique_lh_defensequiver_mysticcube_set_02",
    ),
    "mage": (
        "mage_unique_amulet_mysticcube_set_02",
        "mage_unique_ring_mysticcube_set_02",
        "mage_unique_weaponmodifier_mysticcube_set_02",
        "mage_unique_rh_staff_mysticcube_set_02",
        "mage_unique_lh_orb_mysticcube_set_02",
    ),
    "dwarf": (
        "dwarf_unique_amulet_mysticcube_set_02",
        "dwarf_unique_ring_mysticcube_set_02",
        "dwarf_unique_weaponmodifier_mysticcube_set_02",
        "dwarf_unique_rh_pistol_mysticcube_set_02",
        "dwarf_unique_lh_absorber_mysticcube_set_02",
    ),
}

# Boxes whose chest_contents.json record is wrong, written from each box's
# own description: the Thanksgiving records lost their first line, the
# packages hold their offer bundle's text, the Undefeatable starter pack's
# record drops its realm paths and the Epic tag, and the Dragan bag is named
# like the skin it holds. ``{cls}`` is the player's class; ``{back}`` is the back
# item family, a dwarf's banner or everyone else's cloak.
# UNVERIFIED: "Bag Extension" names no item (the 7-slot Inventory Enhancer
# is given), and "Pickaxes" has no count (5, as the Viscanium offers).
_BAG_EXTENSION = "misc_bag_extension_no_droplevel_restriction"
_AUTHORED_CHEST_CONTENTS: dict[str, tuple[tuple[str, int], ...]] = {
    "thanksgiving_level_10_rewards": (("misc_resurrect_partymember_01", 5), (_BAG_EXTENSION, 1)),
    "thanksgiving_level_20_rewards": (("lockpick", 30), (_BAG_EXTENSION, 1)),
    "thanksgiving_level_30_rewards": (("lockpick", 70), (_BAG_EXTENSION, 1)),
    "thanksgiving_level_40_rewards": (("thanksgiving_cloak_white_2023_{cls}", 1), ("lockpick", 100)),
    "thanksgiving_level_50_rewards": (
        ("money_rc", 200), ("crafting_stateobject_key", 1), ("scaling_dungeon_boss_key_01", 100),
    ),
    "thanksgiving_level_55_rewards": (("gem_block_f", 5), (_BAG_EXTENSION, 1)),
    "thanksgiving_level_60_rewards": (
        ("money_rc", 300), ("crafting_stateobject_key", 3), ("scaling_dungeon_boss_key_01", 100),
    ),
    "thanksgiving_level_65_rewards": (
        ("ingredient_gem_dust", 5000), ("lockpick", 100), ("e_ful_01_teleport_01", 10),
    ),
    "thanksgiving_level_70_rewards": (
        ("money_rc", 500), ("crafting_stateobject_key", 6), ("scaling_dungeon_boss_key_01", 100),
    ),
    "thanksgiving_level_75_rewards": (
        ("buff_xp_100_03", 10), ("lockpick", 100), ("ingredient_gem_dust", 5000),
    ),
    "thanksgiving_level_80_rewards": (
        ("money_rc", 1000), ("scaling_dungeon_boss_key_01", 100), ("mortis_key", 1),
    ),
    "thanksgiving_level_85_rewards": (
        ("ingredient_gem_dust", 10000), ("lockpick", 100), ("e_new_01_teleport_01", 10),
    ),
    "thanksgiving_level_90_rewards": (
        ("money_rc", 3000), ("scaling_dungeon_boss_key_01", 100), ("mortis_key", 2),
    ),
    "thanksgiving_level_95_rewards": (
        ("thanksgiving_cloak_pruple_2023_{cls}", 1), ("scaling_dungeon_boss_key_01", 100),
    ),
    "thanksgiving_level_100_rewards": (
        ("money_rc", 5000), ("lootbox_level_rallye_jewels_random", 1), ("mortis_key", 5),
    ),
    "e_bos_02_starterbox": (
        ("e_bos_02_circus_teleport_01", 20), ("e_bos_02_chest_key", 5),
        ("rc_ammunition_epic", 999), ("pet_nefertari_part_01", 1),
        ("misc_resurrect_partymember_01", 5),
    ),
    "e_bos_starterbox": (
        ("e_bos_02_circus_teleport_01", 20), ("e_bos_02_chest_key", 5),
        ("ammunition_damage_epic", 999), ("misc_resurrect_partymember_01", 5),
        ("pet_ingredient_feather_02", 20), ("pet_ingredient_crystal_03", 20),
    ),
    "e_ide_01_notification_package": (
        ("generic_cot_teaser", 1), ("event_identify_drop_crystal_piece_01", 1),
    ),
    "e_ide_02_notification_package": (
        ("e_ide_02_location_key", 1), ("e_ide_01_pickaxe_01", 5),
        ("event_identify_drop_crystal_piece_01", 1),
    ),
    "e_ide_02_progress_package": (("event_identify_drop_crystal_piece_02", 40),),
    "dragan_event_attire_skin": (("skin_{back}_unique_backitem_attire_dragan_01", 1),),
    # Treasure-map relics: "Consumed when used" for the stated currency.
    "perforated_dragon_scale": (("money_vc_gold", 5),),
    "perforated_dragon_bone": (("money_vc_gold", 10),),
    "perforated_dragon_tooth": (("money_rc", 100),),
    "motley_glass_plate": (("community_coins", 10),),
    "motley_glass_bowl": (("community_coins", 50),),
    "three_hole_bone_flute": (("e_chr_2012_event_currency", 20),),
    "five_hole_bone_flute": (("e_chr_2012_event_currency", 100),),
    "seven_hole_bone_flute": (("e_chr_2012_event_currency", 500),),
    # "Once open, it gives you a 100% chance for a complete Dragon Stone."
    "dragon_crystal_box": (("e_bgh_ancestors_soul", 1),),
}

# Boxes whose server-side loot lists no client data ships and whose own
# descriptions state odds that chest_contents_descriptions.json could not
# parse. A weighted table (odds sum to 100) yields exactly one reward; a roll
# table rolls every entry independently.
# UNVERIFIED: the 6-month Premium Membership has no item row; six one-month
# teasers (180 days) are given. The community coin draw gives a random
# 100/50/40/20 (the description lists the four without odds).
# Fool's Lucky Gift only says "you have a chance to receive one of the
# following items", with no odds: the eight mounts are drawn evenly.
_NEW_YEAR_FORTUNE_COOKIE_ITEMS = tuple(
    f"item_lootbox_fortune_cookie_{number}"
    for number in range(1, 11)
)
_NEW_YEAR_FORTUNE_COOKIE_REWARDS = (
    ("epicmount_newyear_dragon_red", 1),
    ("all_unique_ring_defense", 1),
    ("pet_new_year_lion", 1),
    ("event_cny_2024_costume_lion_cap_{cls}", 1),
    ("costume_anniversary_dragon_red_02", 1),
    ("e_sar_chest_key", 10),
    ("lootbox_level_rallye_jewels_random", 1),
    ("money_rc", 5000),
    ("crafting_stateobject_key", 10),
    ("premium_teaser_one_day_consumable_7d", 1),
    ("money_cc", 20),
    ("cooking_key", 20),
    ("e_cny_boss_essence_04", 999),
    ("e_newyear_2024_travelitem_newyearfirecracker", 1),
    ("pet_panda", 1),
    ("epicmount_goldfish", 1),
)
_AUTHORED_WEIGHTED_CHESTS: dict[str, tuple[tuple[str, int, float], ...]] = {
    # These are inventory LootGenItems. Their loot lists are server-owned,
    # so the map-prop loot catalog does not participate when opening them
    # from the inventory; define their complete one-of tables here.
    # The Fortune Cookie Box gives one of its ten cookie variants; each
    # cookie gives one of the sixteen rewards in its tooltip (uniform odds).
    "item_lootbox_fortune_cookie_box": tuple(
        (template_id, 1, 10.0)
        for template_id in _NEW_YEAR_FORTUNE_COOKIE_ITEMS
    ),
    **{
        template_id: tuple(
            (reward_id, amount, 100.0 / len(_NEW_YEAR_FORTUNE_COOKIE_REWARDS))
            for reward_id, amount in _NEW_YEAR_FORTUNE_COOKIE_REWARDS
        )
        for template_id in _NEW_YEAR_FORTUNE_COOKIE_ITEMS
    },
    "darkdwarf_lightningset_cloak_chest": (
        ("set_blackdwarf2026_backitem_{cls}", 1, 0.5),
        ("money_rc", 8000, 5.5),
        ("lightningset_material_general", 25, 10.0),
        ("event_identify_drop_crystal_piece_02", 200, 14.0),
        ("e_ide_02_challenge_progress_01", 200, 14.0),
        ("e_ide_02_challenge_progress_02", 200, 14.0),
        ("e_ide_01_pickaxe_01", 30, 8.0),
        ("e_ide_01_pickaxe_01", 15, 6.0),
        ("e_ide_01_pickaxe_01", 20, 4.0),
        ("e_sar_chest_key", 10, 10.0),
        ("e_sar_chest_key", 15, 5.0),
        ("e_sar_chest_key", 20, 9.0),
    ),
    "darkdwarf_lightningset_shoulders_chest": (
        ("set_blackdwarf2026_shoulders_{cls}", 1, 0.5),
        ("money_rc", 8000, 6.5),
        ("lightningset_material_general", 25, 9.0),
        ("event_identify_drop_crystal_piece_02", 200, 9.0),
        ("e_ide_02_challenge_progress_01", 200, 9.0),
        ("e_ide_02_challenge_progress_02", 200, 10.0),
        ("e_ide_01_pickaxe_01", 15, 15.0),
        ("e_ide_01_pickaxe_01", 20, 10.0),
        ("e_ide_01_pickaxe_01", 30, 6.0),
        ("e_sar_chest_key", 10, 10.0),
        ("e_sar_chest_key", 15, 5.0),
        ("e_sar_chest_key", 20, 10.0),
    ),
    # The retro chest tooltip calls the rare cold-flame cosmetic by its
    # former name; the current client title for this item is Cold Flame Array.
    "ghsot_retro_chest": (
        ("vanity_groundmarker_42", 1, 0.5),
        ("e_hal_01_entry_dark_path", 20, 15.0),
        ("e_sar_chest_key", 20, 15.0),
        ("e_light_ammunition_rc_300", 2999, 15.0),
        ("pw_enter_locked_difficulty_key", 100, 20.0),
        ("crafting_stateobject_key", 100, 19.5),
        ("e_hal_01_teleport_castle_01", 8, 15.0),
    ),
    # Ghost Festival tooltips: each use selects exactly one reward. The
    # amount placeholders preserve the tooltip's random Andermant/voucher
    # ranges, while all ordinary entries use their authored fixed stacks.
    "item_event_hal_food_ghost_cake": (
        ("pw_enter_locked_difficulty_key", 30, 30.0),
        ("item_event_hal_food_fruit_candy", 1000, 25.0),
        ("ce_darkforce_key", 1, 25.0),
        ("@hal_random_andermant", 0, 13.2),
        ("@hal_random_voucher", 0, 5.5),
        ("@hal_tiny_troublemaker", 1, 1.3),
    ),
    "item_event_hal_food_sparkling_chocolate": (
        ("pw_enter_locked_difficulty_key", 30, 35.0),
        ("item_event_hal_food_fruit_candy", 1000, 20.0),
        ("ce_darkforce_key", 1, 30.0),
        ("@hal_random_voucher", 0, 4.5),
        ("@hal_tiny_troublemaker", 1, 10.5),
    ),
    "ghost_festival_chest": (
        ("pet_jindo_youling", 1, 0.5),
        ("vanity_groundmarker_28", 1, 1.5),
        ("costume_effect_halloween_2025", 1, 2.0),
        ("item_event_hal_food_sparkling_chocolate", 1, 2.0),
        ("crafting_stateobject_key", 5, 35.0),
        ("e_hal_01_entry_dark_path", 5, 30.0),
        ("e_hal_01_teleport_castle_01", 5, 28.5),
    ),
    "ghost_festival_cooking_chest": (
        ("item_event_hal_food_sweet_milk", 1, 12.0),
        ("item_event_hal_food_cooked_beef", 1, 8.0),
        ("item_event_hal_food_berry_juice", 1, 12.0),
        ("item_event_hal_food_apple_juice", 1, 12.0),
        ("item_event_hal_food_cocoa_milk", 1, 12.0),
        ("item_event_hal_food_pumpkin_jam", 1, 8.0),
        ("item_event_hal_food_chocolate_pie", 1, 12.0),
        ("item_event_hal_food_berry_beef_pie", 1, 12.0),
        ("item_event_hal_food_apple_pie", 1, 12.0),
    ),
    # The Dragan package tooltip states the exact one-of odds. Spirit Essence
    # is class ammunition in the client templates, so resolve it per opener.
    "surprise_dragan_package": (
        ("@spirit_essence", 3000, 10.0),
        ("e_dra_03_hardmode_bosskey", 1, 30.0),
        ("e_dra_03_hardmode_bosskey", 2, 35.0),
        ("e_dra_03_hardmode_bosskey", 3, 20.0),
        ("item_ingredient_eye_of_collapse", 1, 4.0),
        ("item_ingredient_fallen_heart", 1, 1.0),
    ),
    # The Surprise Jewel Pack's client description gives exact probabilities.
    # Jewel of Glory is character-specific in the static item templates.
    "surprise_jewel_pack_glory": (
        ("@glory_jewel", 1, 25.0),
        ("ammunition_damage_bgh", 15000, 25.0),
        ("money_rc", 60000, 50.0),
    ),
    # Summer Fortune Bag lists twelve outcomes but no probabilities; use an
    # even one-of split. Jewel types are shown without a rarity, so resolve
    # each one to a random rarity after that outcome is selected.
    "summer_fortune_bag": tuple(
        (item, amount, 100.0 / 12.0)
        for item, amount in (
            ("pw_enter_locked_difficulty_key", 30),
            ("ammunition_damage_epic", 2000),
            ("ce_darkforce_key", 1),
            ("@summer_fortune_coins", 1),
            ("lootbox_level_rallye_jewels_random", 1),
            ("e_sum_01_reward_key", 1),
            ("@summer_ingredient_jewel", 1),
            ("@summer_gem_fortune_jewel", 1),
            ("@summer_focus_jewel", 1),
            ("epicmount_wolf_summer", 1),
            ("costume_effect_summerevent", 1),
            ("pet_dog_summer", 1),
        )
    ),
    # Summer Lucky Chest tooltip gives exact mutually exclusive outcomes.
    "summer_lucky_chest": (
        ("epicmount_elk_01", 1, 15.0),
        ("e_summer_essence_fire_2024", 2, 25.0),
        ("item_ingredient_mercenary_verdant", 500, 20.0),
        ("money_rc", 50000, 40.0),
    ),
    # The official 2026 Summer Solstice rebalance publishes a one-of table
    # for this chest; Kalodoro's four reward chests grant it as an additional
    # item during the Festival phase.
    "summer_solstice_celebration_chest": (
        ("dragon_blood_box", 1, 20.0),
        ("e_sum_coins", 50, 25.0),
        ("e_sum_coins", 30, 10.0),
        ("pw_enter_locked_difficulty_key", 50, 15.0),
        ("e_sar_chest_key", 10, 15.0),
        ("e_sum_01_reward_key", 1, 8.0),
        ("item_ingredient_mercenary_viridis", 1, 5.0),
        ("epicmount_elk_01", 1, 1.5),
        ("pet_dog_summer", 1, 0.5),
    ),
    # The tooltip lists two possible outcomes but no probabilities; use an
    # even split. The jewel has three static rarity templates, selected evenly.
    "bgh_event_chest": (
        ("@strenuousness_jewel", 1, 50.0),
        ("ammunition_damage_bgh", 15000, 50.0),
    ),
    # Defeat the Undefeatable event loot. The client descriptions provide
    # explicit odds for the hero bag; the random doll body has no odds, so its
    # ten eligible bodies are selected evenly (Nian is explicitly excluded).
    "invincible_hero_bag": (
        ("pet_eyeball_purple", 1, 0.5),
        ("costume_effect_the_eye_of_fear", 1, 3.0),
        ("pet_cyclops_blue", 1, 1.5),
        ("perforated_dragon_tooth", 100, 30.0),
        ("ancestor_treasure_map", 1, 10.0),
        ("e_hon_pvp_boost", 1, 15.0),
        ("e_bos_02_chest_key", 5, 20.0),
        ("e_chr_2012_event_currency", 150, 20.0),
    ),
    "lootbox_event_dtu_random_dollbody": tuple(
        (template_id, 1, 10.0)
        for template_id in (
            "pet_ingredient_body_heredur",
            "pet_ingredient_body_bloodmage",
            "pet_ingredient_body_arachna",
            "pet_ingredient_body_karabossa",
            "pet_ingredient_body_magotina",
            "pet_ingredient_body_bearach",
            "pet_ingredient_body_asar",
            "pet_ingredient_body_medusa",
            "pet_ingredient_body_nefertari",
            "item_ingredient_vamok_dolls_body",
        )
    ),
    "event_fools_lucky_gift": (
        ("mythical_mount_skeleton_dragon_01_3d", 1, 12.5),
        ("epicmount_ghost_02_1d", 1, 12.5),
        ("epicmount_ghost_01_1d", 1, 12.5),
        ("epicmount_wealth_ston_doll_1d", 1, 12.5),
        ("epicmount_ghost_bone_horse_1d", 1, 12.5),
        ("epicmount_ghost_bone_monoceros_1d", 1, 12.5),
        ("epicmount_valentines_day_chair_1d", 1, 12.5),
        ("epicmount_chaos_dragon_01_1d", 1, 12.5),
    ),
    "item_lootbox_lucky_bag_2024": (
        ("item_lootbox_glitter_clover", 1, 0.1),
        ("ingredient_rune_resistance_ice_fire_light", 1, 0.2),
        ("jewel_buff_lords_magic", 1, 0.2),
        ("jewel_boss_gem_drop_common", 1, 0.15),
        ("jewel_ingredient_find_common", 1, 0.15),
        ("premium_teaser_one_month", 6, 0.2),
        ("lootbox_level_rallye_jewels_random", 1, 1.5),
        ("money_rc", 5000, 1.0),
        ("@community_coins", 1, 66.5),
        ("mortis_key", 2, 10.0),
        ("ce_darkforce_key", 2, 10.0),
        ("pw_enter_locked_difficulty_key", 40, 10.0),
    ),
}
# Boxes that pay a fixed list and then draw one bonus from a weighted table.
# "You also have a chance to receive one of the following items" (the odds
# sum to 100, so exactly one bonus).
# UNVERIFIED: the Challenge Pass Package is a shop offer with no item row;
# its offer's contents (10 Challenge Pass + 10 Blue Dragon Package) are given.
_AUTHORED_BONUS_CHESTS: dict[
    str,
    tuple[
        tuple[tuple[str, int], ...],
        tuple[tuple[str, int, float], ...],
    ],
] = {
    "immortal_relic_chest": (
        (("event_fools_lucky_gift", 1), ("item_season_pass_experience", 5)),
        (
            ("ancestors_molten_chest", 1, 25.0),
            ("season_pass_chest", 1, 25.0),
            ("e_cny_boss_essence_04", 1999, 25.0),
            ("ce2024_black_dragon_mount", 1, 20.0),
            ("@challenge_pass_package", 1, 5.0),
        ),
    ),
}
_AUTHORED_FIXED_ROLL_CHESTS: dict[
    str,
    tuple[
        tuple[tuple[str, int], ...],
        tuple[tuple[str, int, float], ...],
    ],
] = {
    # Summer Night Dream Chest guarantees Verdant Essence Shards, then rolls
    # each listed bonus independently at the odds in its tooltip.
    "summer_nightdream_chest": (
        (("item_ingredient_mercenary_verdant", 100),),
        (
            ("pw_enter_locked_difficulty_key", 100, 20.0),
            ("e_sum_coins", 90, 20.0),
            ("e_sum_01_ferry_ticket", 10, 20.0),
            ("e_sum_01_reward_key", 1, 15.0),
            ("e_sar_chest_key", 10, 20.0),
            ("epicmount_elk_01", 1, 5.0),
            ("e_summer_essence_fire_2024", 1, 10.0),
        ),
    ),
}
_AUTHORED_REWARD_EXPANSIONS: dict[str, tuple[tuple[str, int], ...]] = {
    "@challenge_pass_package": (
        ("endlesstower_key", 10), ("ce2024_blue_dragon_mount", 10),
    ),
}
_COMMUNITY_COIN_AMOUNTS = (100, 50, 40, 20)
# Items consumed for a random amount of one currency stated in their own
# description: template id -> (currency, minimum, maximum).
_AUTHORED_RANDOM_CHESTS: dict[str, tuple[str, int, int]] = {
    # "Use it to get 5 to 15 random Andermants."
    "polished_squama": ("money_rc", 5, 15),
}
_AUTHORED_ROLL_CHESTS: dict[str, tuple[tuple[str, int, float], ...]] = {
    # Dragan's paid packages have fixed rewards plus independent bonus rolls.
    # Their exact contents and chances are present in the client tooltips,
    # while the server-owned loot lists are absent from the static database.
    "event_dra_payment_package_01": (
        ("e_dra_03_hardmode_bosskey", 1, 100.0),
        ("@spirit_essence", 1000, 100.0),
        ("pet_gargoyle_skull_dra", 1, 3.0),
    ),
    "event_dra_payment_package_02": (
        ("e_dra_03_hardmode_bosskey", 1, 100.0),
        ("community_coins", 10, 100.0),
        ("costumes_headless_horseman_06", 1, 1.0),
        ("vanity_trail_13", 1, 1.0),
        ("e_bgh_ancestors_soul", 1, 50.0),
    ),
    "ancestor_treasure_map": (
        ("perforated_dragon_scale", 1, 100.0),
        ("perforated_dragon_bone", 1, 80.0),
        ("perforated_dragon_tooth", 1, 5.0),
        ("motley_glass_plate", 1, 10.0),
        ("motley_glass_bowl", 1, 2.0),
        ("motley_glass_cup", 1, 0.1),
        ("three_hole_bone_flute", 1, 25.0),
        ("five_hole_bone_flute", 1, 5.0),
        ("seven_hole_bone_flute", 1, 1.0),
        ("e_bgh_ancestors_soul", 1, 2.0),
        ("ancestral_books_lv10", 1, 10.0),
        ("@harvest_horn", 1, 0.5),
        ("@golden_goose", 1, 2.0),
        ("magic_lamp_1_filthy_1", 1, 0.2),
        ("magic_dragon_egg_fossil", 1, 0.1),
        ("dragon_blood_box", 1, 5.0),
        ("dragon_crystal_box", 1, 1.0),
        ("money_rc", 1000, 50.0),
        ("money_rc", 2000, 100.0),
        ("money_rc", 4000, 5.0),
        ("money_rc", 6000, 3.0),
        ("money_rc", 8000, 1.0),
        ("money_rc", 10000, 0.6),
    ),
}
# The description lists categories, not amounts: give one random basic
# ingredient (Bottle/Vial/Flask/Filter Paper, any rarity) and one Multitool.
# The region chests ("Crafting Chest: Duria") list formulas instead, so they
# are not served these contents. UNVERIFIED: the amounts and the odds.
_CRAFTING_CHEST_TEMPLATES = frozenset({"crafting_quest_reward_box"})
_CRAFTING_CHEST_CONTENTS = (
    ("@basic_ingredient", 1, 100.0),
    ("crafting_stateobject_key", 1, 100.0),
)
_BASIC_INGREDIENTS = tuple(
    f"basic_ingredient_{family}_rarity_{rarity:02d}"
    for family in ("buff", "elixir", "potion", "tonic")
    for rarity in range(1, 6)
)
_HARVEST_HORN_LEVELS = (20, 40, 60, 80, 100)
_GOLDEN_GOOSE_LEVELS = tuple(range(50, 100, 5))


def _unresolved_contents(category: str, message: str) -> ValueError:
    if str(category) in _CHEST_CATEGORIES:
        return UnresolvedChestError(message)
    return ValueError(message)


@dataclass(frozen=True)
class ChestReward:
    definition: PurchaseItemDefinition
    amount: int
    item_level: int | None = None
    rarity: int | None = None
    appearance: str = ""
    # Authored rewards that must bypass the template-name mapping: "glyph"
    # credits the client's Glyph of Power field, "wisdom" is applied to the
    # Knowledge progression by the caller.
    fixed_currency: str = ""


@dataclass(frozen=True)
class ChestContents:
    box_template_id: str
    source: str
    rewards: tuple[ChestReward, ...]
    used_tooltip_mapping: bool = False


@dataclass(frozen=True)
class ChestOpenMutation:
    contents: ChestContents
    inventory: InventoryInfo
    currency: CurrencyChanged
    game_currencies: dict[str, int]
    game_currency_updates: tuple[GameCurrencyChanged, ...]
    currency_changed: bool
    delta: InventoryInfo
    final: InventoryInfo
    next_item_id: int
    wisdom_amount: int = 0


@dataclass(frozen=True)
class _MappedReward:
    template_id: str
    amount: int
    chance_percent: float | None = None
    equipment_rarity: str | None = None
    equipment_slot_types: tuple[str, ...] = ()
    # An event skin region for the rolled item (christmas presents: e_chr).
    equipment_appearance_region: str = ""


def _positive_amount(value: Any, *, default: int = 1) -> int:
    if value is None or value == "":
        return default
    text = str(value).strip()
    # "5/20/50x Lockpicks": alternatives without odds give the first, as
    # tools/chest_contents_from_descriptions.py does.
    first, _, alternatives = text.partition("/")
    if alternatives and all(part.strip().isdigit() for part in alternatives.split("/")):
        text = first.strip()
    if not text.isdigit():
        raise ValueError(f"reward amount {value!r} is not a positive integer")
    amount = int(text, 10)
    if amount <= 0:
        raise ValueError(f"reward amount must be positive, got {value!r}")
    return amount


def _split_embedded_amount(
    template_id: str, amount: Any,
) -> tuple[str, int | tuple[int, int]]:
    """Handle report edges such as ``item_template,5`` from wishing wells."""

    template_id = str(template_id or "").strip()
    if amount is None and "," in template_id:
        possible_id, possible_amount = template_id.rsplit(",", 1)
        if possible_amount.strip().isdigit():
            template_id = possible_id.strip()
            amount = possible_amount.strip()
    if not template_id:
        raise ValueError("reward has no item template id")
    if isinstance(amount, (list, tuple)):
        if len(amount) != 2:
            raise ValueError("reward amount range must have two values")
        low, high = int(amount[0]), int(amount[1])
        if low <= 0 or high < low:
            raise ValueError(f"invalid reward amount range {amount!r}")
        return template_id, (low, high)
    return template_id, _positive_amount(amount)


@lru_cache(maxsize=8)
def _load_index_cached(
    path_string: str, modified_ns: int, size: int,
) -> dict[str, tuple[dict[str, Any], ...]]:
    del modified_ns, size
    path = Path(path_string)
    payload = json.loads(resident_content.read_text(path, encoding="utf-8"))
    containers = payload.get("containers")
    if not isinstance(containers, list):
        raise ValueError("chest contents map has no containers array")
    indexed: dict[str, list[dict[str, Any]]] = {}
    for raw in containers:
        if not isinstance(raw, dict):
            continue
        container_id = str(raw.get("container") or "").strip()
        if container_id:
            indexed.setdefault(container_id, []).append(raw)
    return {key: tuple(values) for key, values in indexed.items()}


def _load_index(path: str | Path) -> dict[str, tuple[dict[str, Any], ...]]:
    resolved = resident_content.resolved(path)
    if not resident_content.is_file(resolved):
        raise FileNotFoundError(f"chest contents map not found: {resolved}")
    stat = resident_content.stat(resolved)
    return _load_index_cached(
        str(resolved), int(stat.st_mtime_ns), int(stat.st_size)
    )


def _inventory_record(
    records: tuple[dict[str, Any], ...],
) -> dict[str, Any] | None:
    """Choose the item record, not a same-named offer/stickerbook alias."""

    preferred_sources = (
        "_Template_Item.Loot",
        "_Template_HatchEggs.EggGetAwardItem",
        "none",
    )
    for source in preferred_sources:
        for record in reversed(records):
            if str(record.get("source") or "") == source:
                return record
    for record in reversed(records):
        if str(record.get("category") or "") in _INVENTORY_CONTAINER_CATEGORIES:
            return record
    return None


def _definition(
    static_database_path: str | Path, template_id: str,
) -> PurchaseItemDefinition:
    try:
        return load_purchase_item_definition(static_database_path, template_id)
    except (OSError, ValueError) as exc:
        raise ValueError(
            f"mapped reward {template_id!r} is not a valid item template"
        ) from exc


@lru_cache(maxsize=4096)
def _definitions_for_label(
    static_database_path: str,
    label: str,
    rarity_name: str,
) -> tuple[PurchaseItemDefinition, ...]:
    """Resolve tooltip-only rewards by their authored item display name."""

    path = Path(static_database_path)
    database = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    database.row_factory = sqlite3.Row
    try:
        rows = database.execute(
            "SELECT Id, DefaultRarity FROM _Template_Item "
            "WHERE lower(Name)=lower(?) AND Obsolete=0",
            (str(label),),
        ).fetchall()
    finally:
        database.close()
    if rarity_name:
        narrowed = tuple(
            row for row in rows
            if str(row["DefaultRarity"] or "Common") == rarity_name
        )
        if narrowed:
            rows = narrowed
    return tuple(
        _definition(path, str(row["Id"])) for row in rows
    )


def _candidate_definition(
    static_database_path: str | Path,
    entry: dict[str, Any],
    *,
    player_level: int,
    rarity_hint: str = "",
    class_name: str = "",
) -> PurchaseItemDefinition:
    candidates = tuple(dict.fromkeys(
        str(value).strip()
        for value in (entry.get("candidates") or ())
        if str(value).strip()
    ))
    text = str(entry.get("text") or "")
    rarity_match = _RARITY_HINT.search(text)
    rarity_name = (
        _RARITY_NAMES[rarity_match.group(1).lower()]
        if rarity_match is not None
        else str(rarity_hint or "")
    )
    definitions = tuple(
        _definition(static_database_path, candidate)
        for candidate in candidates
    )
    normalized_class = str(class_name or "").casefold()
    if normalized_class in {"warrior", "mage", "ranger", "dwarf"}:
        class_specific = tuple(
            definition
            for definition in definitions
            if definition.template_id.casefold().startswith(
                normalized_class + "_"
            )
        )
        if class_specific:
            definitions = class_specific
    if not definitions:
        aliased = _TOOLTIP_ITEM_ALIASES.get(text.casefold().strip())
        if aliased:
            definitions = (_definition(static_database_path, aliased),)
    if not definitions:
        label = _RARITY_HINT.sub("", text).strip()
        definitions = _definitions_for_label(
            str(Path(static_database_path)), label, rarity_name
        )
    if not definitions:
        raise ValueError(
            f"tooltip reward {entry.get('text')!r} has no item candidate"
        )
    if len(definitions) == 1:
        return definitions[0]

    if rarity_name:
        rarity_id = OFFER_RARITY_IDS[rarity_name]
        narrowed = tuple(
            definition
            for definition in definitions
            if int(definition.rarity) == int(rarity_id)
        )
        if len(narrowed) == 1:
            return narrowed[0]
        numbered_suffix = "_rarity_" + _RARITY_VARIANT_NUMBERS.get(
            rarity_name, ""
        )
        narrowed = tuple(
            definition
            for definition in definitions
            if numbered_suffix != "_rarity_"
            and definition.template_id.lower().endswith(numbered_suffix)
        )
        if len(narrowed) == 1:
            return narrowed[0]
        suffix = "_" + rarity_name.lower()
        narrowed = tuple(
            definition
            for definition in definitions
            if definition.template_id.lower().endswith(suffix)
        )
        if len(narrowed) == 1:
            return narrowed[0]
        standard = tuple(
            definition
            for definition in narrowed
            if not definition.template_id.casefold().startswith("e_")
        )
        if len(standard) == 1:
            return standard[0]

    for canonical in _CANONICAL_CURRENCY_IDS:
        for definition in definitions:
            if definition.template_id == canonical:
                return definition

    level_variants = []
    for definition in definitions:
        match = _LEVEL_SUFFIX.search(definition.template_id)
        if match is not None:
            level_variants.append((int(match.group(1)), definition))
    if len(level_variants) == len(definitions):
        eligible = [pair for pair in level_variants if pair[0] <= player_level]
        return max(eligible or level_variants, key=lambda pair: pair[0])[1]

    # A number of consumables use rarity suffixes but the tooltip deliberately
    # omits a rarity.  The unsuffixed/common/normal variant is the authored
    # base reward and is preferable to randomly changing reward rank.
    base_variants = tuple(
        definition
        for definition in definitions
        if definition.template_id.lower().endswith(("_normal", "_common"))
    )
    if len(base_variants) == 1:
        return base_variants[0]

    numbered_variants: list[tuple[int, PurchaseItemDefinition]] = []
    for definition in definitions:
        match = re.search(
            r"_rarity_(\d+)$", definition.template_id, re.IGNORECASE
        )
        if match is not None:
            numbered_variants.append((int(match.group(1)), definition))
    if len(numbered_variants) == len(definitions):
        # An uncoloured tooltip means the base/common consumable. Coloured
        # tooltip markup is handled above. This avoids arbitrarily awarding a
        # more valuable rank just because every database variant shares a
        # display name.
        return min(numbered_variants, key=lambda pair: pair[0])[1]

    non_quest = tuple(
        definition
        for definition in definitions
        if not definition.template_id.casefold().startswith("quest_")
        and definition.item_category != "QuestItem"
    )
    if len(non_quest) == 1:
        return non_quest[0]

    conventional_gems = tuple(
        definition
        for definition in definitions
        if re.fullmatch(
            r"gem_[a-z]+_[a-z]",
            definition.template_id,
            re.IGNORECASE,
        )
    )
    if len(conventional_gems) == 1:
        return conventional_gems[0]

    # Generated tooltip mappings frequently list an unsuffixed authored item
    # together with its upgrade variants. The unsuffixed record is the exact
    # base reward named by the tooltip.
    suffixed_rarities = tuple(
        "_" + name.casefold() for name in EQUIPMENT_RARITIES
    )
    unsuffixed = tuple(
        definition for definition in definitions
        if not definition.template_id.casefold().endswith(suffixed_rarities)
    )
    if len(unsuffixed) == 1:
        return unsuffixed[0]

    raise ValueError(
        f"tooltip reward {text!r} is ambiguous: "
        f"{tuple(definition.template_id for definition in definitions)!r}"
    )


def _direct_entries(
    static_database_path: str | Path,
    record: dict[str, Any],
    *,
    class_name: str = "",
    rng: random.Random | None = None,
) -> tuple[list[_MappedReward], list[_MappedReward], list[_MappedReward], bool]:
    guaranteed: list[_MappedReward] = []
    choices: list[_MappedReward] = []
    chance: list[_MappedReward] = []
    saw_choose = False
    for entry in record.get("yields") or ():
        mode = str(entry.get("mode") or "guaranteed")
        if mode == "rarity_upgrade":
            continue
        if mode == "one_of_equipment":
            rarity = _RARITY_NAMES.get(
                str(entry.get("rarity") or "").casefold()
            )
            if rarity not in EQUIPMENT_RARITIES:
                raise ValueError(
                    f"invalid generated equipment rarity "
                    f"{entry.get('rarity')!r} for "
                    f"{record.get('container')!r}"
                )
            raw_slots = entry.get("slot_types") or ()
            if not isinstance(raw_slots, (list, tuple)):
                raise ValueError("generated equipment slot_types must be a list")
            choices.append(_MappedReward(
                template_id="@equipment",
                amount=1,
                equipment_rarity=rarity,
                equipment_slot_types=tuple(str(slot) for slot in raw_slots),
                equipment_appearance_region=str(
                    entry.get("appearance_region") or ""
                ),
            ))
            continue
        if mode == "class_set":
            normalized_class = str(
                entry.get("fixed_class") or class_name or ""
            ).casefold()
            class_items = (entry.get("by_class") or {}).get(normalized_class)
            if normalized_class not in {"warrior", "mage", "ranger", "dwarf"}:
                raise ValueError(
                    f"class-specific container "
                    f"{record.get('container')!r} requires a valid player class"
                )
            if not isinstance(class_items, list) or not class_items:
                raise ValueError(
                    f"class-specific container "
                    f"{record.get('container')!r} has no rewards for "
                    f"class {normalized_class!r}"
                )
            for class_item in class_items:
                if isinstance(class_item, dict):
                    class_template_id, class_amount = _split_embedded_amount(
                        class_item.get("item"), class_item.get("amount")
                    )
                else:
                    class_template_id, class_amount = _split_embedded_amount(
                        class_item, None
                    )
                if class_template_id == str(record.get("container") or ""):
                    raise ValueError(
                        f"container {class_template_id!r} maps to itself"
                    )
                _definition(static_database_path, class_template_id)
                guaranteed.append(_MappedReward(class_template_id, class_amount))
            continue
        if mode == "one_of_class":
            normalized_class = str(class_name or "").casefold()
            class_items = (entry.get("by_class") or {}).get(normalized_class)
            if normalized_class not in {"warrior", "mage", "ranger", "dwarf"}:
                raise ValueError(
                    f"class-specific container "
                    f"{record.get('container')!r} requires a valid player class"
                )
            if class_items is None:
                raise ValueError(
                    f"class-specific container "
                    f"{record.get('container')!r} has no reward for "
                    f"class {normalized_class!r}"
                )
            class_template_id, class_amount = _split_embedded_amount(
                class_items.get("item") if isinstance(class_items, dict)
                else class_items,
                class_items.get("amount") if isinstance(class_items, dict)
                else None,
            )
            if class_template_id == str(record.get("container") or ""):
                raise ValueError(
                    f"container {class_template_id!r} maps to itself"
                )
            _definition(static_database_path, class_template_id)
            choices.append(_MappedReward(class_template_id, class_amount))
            continue
        template_id, raw_amount = _split_embedded_amount(
            entry.get("item"), entry.get("amount", 1)
        )
        if isinstance(raw_amount, (list, tuple)):
            if len(raw_amount) != 2:
                raise ValueError(
                    f"reward amount range for {template_id!r} must have "
                    "two values"
                )
            minimum, maximum = (int(value) for value in raw_amount)
            if minimum <= 0 or maximum < minimum:
                raise ValueError(
                    f"invalid reward amount range for {template_id!r}"
                )
            amount = (rng or random.SystemRandom()).randint(minimum, maximum)
        else:
            amount = _positive_amount(raw_amount)
        if template_id == str(record.get("container") or ""):
            continue
        _definition(static_database_path, template_id)
        chance_percent = entry.get("chance_percent")
        reward = _MappedReward(
            template_id,
            amount,
            float(chance_percent) if chance_percent is not None else None,
        )
        if mode == "guaranteed":
            guaranteed.append(reward)
        elif mode == "one_of":
            choices.append(reward)
        elif mode == "chance":
            if chance_percent is None or not 0.0 < reward.chance_percent <= 100.0:
                raise ValueError(
                    f"invalid reward chance {chance_percent!r} for "
                    f"{template_id!r}"
                )
            chance.append(reward)
        elif mode == "choose":
            saw_choose = True
        else:
            raise ValueError(f"unsupported direct reward mode {mode!r}")
    return guaranteed, choices, chance, saw_choose


def _tooltip_entries(
    static_database_path: str | Path,
    record: dict[str, Any],
    *,
    player_level: int,
    class_name: str,
) -> tuple[list[_MappedReward], list[_MappedReward], bool]:
    ui = record.get("ui") or {}
    guaranteed: list[_MappedReward] = []
    chance: list[_MappedReward] = []
    # Rewards a tooltip lists without odds ("Open this package to receive one
    # of the following items:"): kept apart, used by the caller as an
    # even one-of draw only when the box has nothing else to give.
    # FAILURE: every Lucky Sphere was "no resolvable rewards" and could not
    #   be opened ("les lucky sphere ... je peux pas utiliser").
    unweighted: list[_MappedReward] = []
    unresolved: list[ValueError] = []
    saw_choose = False
    prose = str(ui.get("prose") or "")
    for entry in ui.get("entries") or ():
        mode = str(entry.get("mode") or ui.get("mode") or "unknown")
        if mode == "choose":
            saw_choose = True
            continue
        if mode == "chance" and entry.get("chance_percent") is None:
            try:
                definition = _candidate_definition(
                    static_database_path,
                    entry,
                    player_level=player_level,
                    rarity_hint="",
                    class_name=class_name,
                )
            except ValueError as exc:
                # An even draw over names the item twice (an event copy):
                # its first candidate. One unnamed reward does not make the
                # whole box unusable.
                first = next(iter(entry.get("candidates") or ()), None)
                if first is None:
                    unresolved.append(exc)
                    continue
                definition = _definition(static_database_path, str(first))
            unweighted.append(_MappedReward(
                definition.template_id, _positive_amount(entry.get("amount")), None
            ))
            continue
        rarity_hint = ""
        entry_text = str(entry.get("text") or "")
        entry_offset = prose.find(entry_text)
        if entry_offset >= 0:
            prefix = prose[max(0, entry_offset - 96):entry_offset]
            # A tag applies only until its matching ``</>`` marker. Do not
            # leak the previous bullet's colour/rarity into an uncoloured
            # reward that happens to be nearby in the prose string.
            last_close = prefix.rfind("</>")
            tagged_tail = prefix[last_close + 3:]
            tags = re.findall(
                r"<Rarity([A-Za-z]+)Start>",
                tagged_tail,
                re.IGNORECASE,
            )
            if tags:
                rarity_hint = _RARITY_NAMES.get(tags[-1].casefold(), "")
        try:
            definition = _candidate_definition(
                static_database_path,
                entry,
                player_level=player_level,
                rarity_hint=rarity_hint,
                class_name=class_name,
            )
        except ValueError as exc:
            unresolved.append(exc)
            continue
        amount = _positive_amount(entry.get("amount"))
        chance_percent = entry.get("chance_percent")
        if chance_percent is not None:
            chance_percent = float(chance_percent)
            if not 0.0 < chance_percent <= 100.0:
                raise ValueError(
                    f"invalid reward chance {chance_percent!r} for "
                    f"{definition.template_id!r}"
                )
            mode = "chance"
        reward = _MappedReward(
            definition.template_id, amount, chance_percent
        )
        if mode == "guaranteed":
            guaranteed.append(reward)
        elif mode == "chance":
            chance.append(reward)
        else:
            raise ValueError(
                f"tooltip reward {entry.get('text')!r} has unknown mode"
            )
    if not guaranteed and not chance and not unweighted and unresolved:
        raise _unresolved_contents(
            str(record.get("category") or ""), str(unresolved[0]),
        ) from unresolved[0]
    return guaranteed, chance, saw_choose, unweighted


def _pick_weighted(
    entries: list[_MappedReward], rng: random.Random,
) -> _MappedReward:
    total = sum(float(entry.chance_percent or 0.0) for entry in entries)
    marker = rng.random() * total
    cumulative = 0.0
    for entry in entries:
        cumulative += float(entry.chance_percent or 0.0)
        if marker < cumulative:
            return entry
    return entries[-1]


def _roll_chances(
    entries: list[_MappedReward], rng: random.Random,
) -> list[_MappedReward]:
    """Roll consecutive complete 100% blocks, then independent bonuses."""

    selected: list[_MappedReward] = []
    pending: list[_MappedReward] = []
    pending_total = 0.0

    def flush_independent() -> None:
        nonlocal pending, pending_total
        for entry in pending:
            if rng.random() * 100.0 < float(entry.chance_percent or 0.0):
                selected.append(entry)
        pending = []
        pending_total = 0.0

    for entry in entries:
        chance = float(entry.chance_percent or 0.0)
        if pending and pending_total + chance > 100.000001:
            flush_independent()
        pending.append(entry)
        pending_total += chance
        if abs(pending_total - 100.0) <= 0.000001:
            selected.append(_pick_weighted(pending, rng))
            pending = []
            pending_total = 0.0
    flush_independent()
    return selected


def _aggregate(entries: list[_MappedReward]) -> tuple[tuple[str, int], ...]:
    totals: dict[str, int] = {}
    for entry in entries:
        totals[entry.template_id] = (
            totals.get(entry.template_id, 0) + int(entry.amount)
        )
    return tuple(totals.items())


def _equipment_chest_reward(
    static_database_path: str | Path,
    record: dict[str, Any],
    *,
    class_name: str,
    player_level: int,
    rng: random.Random,
) -> ChestReward | None:
    """Resolve server-owned ``LOOTBOX_*equipment*`` reward lists."""

    loot_list = str(record.get("loot_list") or "")
    lowered = loot_list.casefold()
    # The daily challenges' chests ("Open this chest to receive random
    # rewards."): one equipment item of the chest's rarity.
    # UNVERIFIED: LOOTBOX_daily_challenge_* is server-side; its other
    #   rewards are unknown.
    if "equipment" not in lowered and "backitem" not in lowered and "daily_challenge" not in lowered:
        return None
    hints = tuple((record.get("ui") or {}).get("rarity_hints") or ())
    rarity = next(
        (
            _RARITY_NAMES[str(hint).casefold()]
            for hint in hints
            if str(hint).casefold() in _RARITY_NAMES
        ),
        "",
    )
    if not rarity:
        rarity = next(
            (
                name for name in EQUIPMENT_RARITIES
                if re.search(
                    rf"(?:^|_){re.escape(name.casefold())}(?:_|$)",
                    lowered,
                )
            ),
            "",
        )
    if not rarity:
        return None
    slot_types: tuple[str, ...] = ()
    if "backitem" in lowered:
        slot_types = ("CloakSlot",)
    roll = roll_equipment_reward(
        static_database_path,
        class_name=str(class_name),
        item_level=max(1, int(player_level)),
        rarity=rarity,
        slot_types=slot_types,
        rng=rng,
    )
    return ChestReward(
        _definition(static_database_path, roll.template_id),
        1,
        item_level=roll.item_level,
        rarity=roll.rarity,
        appearance=roll.appearance,
    )


def _gem_chest_reward(
    static_database_path: str | Path,
    record: dict[str, Any],
    *,
    rng: random.Random,
) -> ChestReward | None:
    """Resolve generic server-owned gem-bag lists by their rank suffix."""

    loot_list = str(record.get("loot_list") or "")
    match = re.search(r"(?:^|_)gem_bag_([a-s])$", loot_list, re.IGNORECASE)
    # This older starter bag predates the lettered bag tiers in the loot
    # report. It still needs to yield a real gem rather than disappear.
    rank = match.group(1).casefold() if match is not None else None
    if rank is None and loot_list.casefold() == "lootbox_e_misc_gem_bag_01":
        rank = "c"
    if rank is None:
        return None
    candidates: list[PurchaseItemDefinition] = []
    for family in _STANDARD_GEM_FAMILIES:
        try:
            candidates.append(
                _definition(static_database_path, f"gem_{family}_{rank}")
            )
        except ValueError:
            continue
    if not candidates:
        return None
    return ChestReward(candidates[rng.randrange(len(candidates))], 1)


def _rune_bag_reward(
    static_database_path: str | Path,
    record: dict[str, Any],
    *,
    rng: random.Random,
) -> ChestReward | None:
    """Resolve the rune bag to one of its 22 base-level standard runes."""

    loot_list = str(record.get("loot_list") or "").casefold()
    if loot_list != "lootbox_lootbox_runes_00":
        return None
    path = Path(static_database_path)
    database = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        rows = database.execute(
            "SELECT Id FROM _Template_Item "
            "WHERE ItemCategory='EquipmentRune' AND RuneGem=0 "
            "AND Id IN ("
            "'unique_gem_xp_gain_00', "
            "'unique_gem_wisdom_find_00', "
            "'unique_gem_resource_00', "
            "'unique_gem_resourceregen_00', "
            "'unique_gem_resistance_poison_00', "
            "'unique_gem_resistance_lightning_00', "
            "'unique_gem_resistance_ice_00', "
            "'unique_gem_resistance_fire_00', "
            "'unique_gem_resistance_andermagic_00', "
            "'unique_gem_resistance_00', "
            "'unique_gem_materi_find_00', "
            "'unique_gem_hitpoints_00', "
            "'unique_gem_healthregen_00', "
            "'unique_gem_grouptalent_all_prog_wisdom_00', "
            "'unique_gem_grouptalent_all_prog_xp_00', "
            "'unique_gem_grouptalent_all_prog_shards_00', "
            "'unique_gem_grouptalent_all_essentials_vc_00', "
            "'unique_gem_grouptalent_all_essentials_rf_00', "
            "'unique_gem_grouptalent_all_essentials_rc_00', "
            "'unique_gem_damage_00', "
            "'unique_gem_faster_run_00', "
            "'unique_gem_critical_00') AND Obsolete=0 "
            "ORDER BY Id"
        ).fetchall()
    finally:
        database.close()
    candidates = [
        _definition(path, str(row[0])) for row in rows
    ]
    if not candidates:
        return None
    return ChestReward(candidates[rng.randrange(len(candidates))], 1)


def _adventurer_chest_contents(
    static_database_path: str | Path,
    box_template_id: str,
    *,
    player_level: int,
    class_name: str,
    rng: random.Random,
) -> ChestContents | None:
    """Build the authored level-up chest chain when server loot lists are absent.

    The client static database contains the chest templates and their next-level
    names, but not the server-side LOOTBOX contents. Keep the progression
    available from the server's known item templates and the published chest
    reward tables. Unsupported legacy rewards are omitted rather than turned
    into guessed or invalid item templates.
    """

    match = _ADVENTURER_CHEST_LEVEL.fullmatch(str(box_template_id))
    if match is None:
        return None
    level = int(match.group(1))
    if level < 10 or level > 100 or level % 5:
        return None
    if int(player_level) < level:
        raise ValueError(
            f"container {box_template_id!r} requires level {level}"
        )

    # The first era of the chest chain has published rewards. Dark Legacy
    # changed the 60+ contents; those rewards are represented with current
    # server item templates (glyph currency, gem/rune bags and anxiety shards).
    loot: dict[int, tuple[tuple[str, int], ...]] = {
        10: (("potion_health_restore_rarity_01", 2), ("ammunition_damage_common", 250)),
        15: (("money_vc_gold", 25), ("lockpick", 10), ("lootbox_gems_a", 2)),
        20: (("mount_common_horse_01_3d", 1),),
        25: (("money_vc_gold", 75), ("respawn_soulguardian_2", 3), ("ammunition_damage_common", 999)),
        30: (("lockpick", 35), ("e_misc_gem_bag_01", 1)),
        35: (("ammunition_damage_common", 999), ("mount_terror_horse_blue_7d", 1)),
        40: (("respawn_soulguardian_2", 5), ("potion_health_restore_rarity_01", 5)),
        45: (("money_vc_gold", 150), ("lootbox_gems_e", 2), ("ammunition_damage_common", 999)),
        50: (("lockpick", 50), ("ammunition_damage_common", 1998), ("lootbox_gems_f", 1)),
        55: (("ammunition_damage_common", 3000), ("lootbox_runes_00", 1)),
        60: (("money_gc", 5000), ("lootbox_gems_f", 2), ("lootbox_runes_00", 1)),
        65: (("lootbox_gems_f", 2), ("lootbox_runes_00", 1)),
        70: (("money_gc", 17000), ("soul_shard_common", 20), ("lootbox_gems_f", 3)),
        75: (("money_gc", 18000), ("lockpick", 50), ("lootbox_gems_f", 3)),
        80: (("soul_shard_uncommon", 20), ("respawn_soulguardian_2", 10), ("lootbox_gems_g", 1)),
        85: (("soul_shard_magic", 25), ("lootbox_runes_00", 1), ("lockpick", 150)),
        90: (("money_gc", 30000), ("lootbox_gems_g", 1), ("respawn_soulguardian_2", 15)),
        95: (("soul_shard_rare", 32), ("money_gc", 35000), ("lockpick", 175)),
        100: (("lootbox_runes_00", 1), ("lootbox_gems_g", 1), ("money_gc", 50000), ("soul_shard_epic", 35), ("lockpick", 150), ("respawn_soulguardian_2", 15)),
    }

    rewards: list[ChestReward] = []
    for template_id, amount in loot.get(level, ()):
        try:
            rewards.append(ChestReward(_definition(static_database_path, template_id), amount))
        except ValueError:
            # A downstream client database may be older than the server's
            # static data. Skip an unavailable optional reward, but still keep
            # the chest progression itself intact.
            continue

    next_level = level + 5
    next_template_id = (
        "levelchest_lvl_60" if next_level == 60
        else f"levelchest_lvl{next_level}"
    )
    try:
        rewards.append(
            ChestReward(
                _definition(static_database_path, next_template_id),
                1,
            )
        )
    except ValueError:
        pass

    if level in {60, 85, 95} and str(class_name):
        try:
            roll = roll_equipment_reward(
                static_database_path,
                class_name=str(class_name),
                item_level=max(level, int(player_level)),
                rarity="Epic",
                rng=rng,
            )
            rewards.append(
                ChestReward(
                    _definition(static_database_path, roll.template_id),
                    1,
                    item_level=roll.item_level,
                    rarity=roll.rarity,
                    appearance=roll.appearance,
                )
            )
        except (OSError, ValueError):
            pass

    if not rewards:
        raise ValueError(f"adventurer chest {box_template_id!r} has no rewards")
    return ChestContents(
        box_template_id=box_template_id,
        source="adventurer_chest_progression",
        rewards=tuple(rewards),
        used_tooltip_mapping=False,
    )


def _authored_chest_contents(
    static_database_path: str | Path,
    box_definition: PurchaseItemDefinition,
    *,
    player_level: int,
    class_name: str,
    rng: random.Random | None = None,
) -> ChestContents | None:
    """Give one of the boxes in ``_AUTHORED_CHEST_CONTENTS``."""

    if box_definition.template_id in {
        "e_ful_02_small_rewardchest",
        "e_ful_02_big_rewardchest",
    }:
        if int(player_level) < int(box_definition.required_level):
            raise ValueError(
                f"container {box_definition.template_id!r} requires level "
                f"{int(box_definition.required_level)}"
            )
        class_name = str(class_name).casefold()
        if class_name not in {"warrior", "mage", "ranger", "dwarf"}:
            raise ValueError(
                "Fullmoon Reward Chest requires a valid character class"
            )
        chooser = rng or random.SystemRandom()
        blood_rune_items = {
            "warrior": (
                "warrior_unique_2h_sword2h_bloodmoon_set",
                "warrior_unique_amulet_bloodmoon_set",
                "warrior_unique_belt_bloodmoon_set",
                "warrior_unique_ring_bloodmoon_set",
            ),
            "mage": (
                "mage_unique_2h_staff2h_bloodmoon_set",
                "mage_unique_amulet_bloodmoon_set",
                "mage_unique_belt_bloodmoon_set",
                "mage_unique_ring_bloodmoon_set",
            ),
            "ranger": (
                "ranger_unique_2h_bow_bloodmoon_set",
                "ranger_unique_amulet_bloodmoon_set",
                "ranger_unique_belt_bloodmoon_set",
                "ranger_unique_ring_bloodmoon_set",
            ),
            "dwarf": (
                "dwarf_unique_2h_rifle2h_bloodmoon_set",
                "dwarf_unique_amulet_bloodmoon_set",
                "dwarf_unique_belt_bloodmoon_set",
                "dwarf_unique_ring_bloodmoon_set",
            ),
        }
        wolf_slayer_items = {
            "warrior": (
                "warrior_unique_rh_axe_fullmoon_set",
                "warrior_unique_lh_shield_fullmoon_set",
                "warrior_unique_helmet_fullmoon_set",
                "warrior_unique_weaponmod_fullmoon_set",
            ),
            "mage": (
                "mage_unique_rh_staff_fullmoon_set",
                "mage_unique_lh_book_fullmoon_set",
                "mage_unique_helmet_fullmoon_set",
                "mage_unique_weaponmod_fullmoon_set",
            ),
            "ranger": (
                "ranger_unique_rh_longbow_fullmoon_set",
                "ranger_unique_lh_offensequiver_fullmoon_set",
                "ranger_unique_helmet_fullmoon_set",
                "ranger_unique_weaponmod_fullmoon_set",
            ),
            "dwarf": (
                "dwarf_unique_rh_pistol_fullmoon_set",
                "dwarf_unique_lh_gadget_fullmoon_set",
                "dwarf_unique_helmet_fullmoon_set",
                "dwarf_unique_weaponmod_fullmoon_set",
            ),
        }
        # The reward chest always gives one class-specific Blood Rune or
        # Wolf Slayer item, plus one cosmetic reward. These are two separate
        # draws; combining both groups into one choice left one reward out.
        reward_templates = (
            chooser.choice((
                *blood_rune_items[class_name],
                *wolf_slayer_items[class_name],
            )),
            chooser.choice((
                "e_ful_02_costume_bloodwhirl",
                "e_ful_02_emote_moonwalk",
                "epicmount_wolf_black",
            )),
        )
        return ChestContents(
            box_template_id=box_definition.template_id,
            source="fullmoon_reward_chest",
            rewards=tuple(
                ChestReward(_definition(static_database_path, template_id), 1)
                for template_id in reward_templates
            ),
            used_tooltip_mapping=False,
        )

    authored = _AUTHORED_CHEST_CONTENTS.get(box_definition.template_id)
    if authored is None:
        return None
    if int(player_level) < int(box_definition.required_level):
        raise ValueError(
            f"container {box_definition.template_id!r} requires level "
            f"{int(box_definition.required_level)}"
        )
    class_name = str(class_name)
    rewards = []
    for template_id, amount in authored:
        if "{cls}" in template_id and not class_name:
            raise ValueError(
                f"class-specific container {box_definition.template_id!r} "
                "requires a valid player class"
            )
        template_id = template_id.format(
            cls=class_name, back="dwarf" if class_name == "dwarf" else "all",
        )
        rewards.append(ChestReward(_definition(static_database_path, template_id), amount))
    return ChestContents(
        box_template_id=box_definition.template_id,
        source="authored_chest_contents",
        rewards=tuple(rewards),
        used_tooltip_mapping=False,
    )


def _level_scaled_template(
    prefix: str, levels: tuple[int, ...], player_level: int,
) -> str:
    eligible = [level for level in levels if level <= int(player_level)]
    return f"{prefix}{eligible[-1] if eligible else levels[0]}"


def _authored_reward_template(
    template_id: str, *, player_level: int, rng: random.Random,
) -> str:
    if template_id == "@harvest_horn":
        return _level_scaled_template(
            "horn_of_harvest_lv", _HARVEST_HORN_LEVELS, player_level,
        )
    if template_id == "@golden_goose":
        return _level_scaled_template(
            "golden_goose_Lv", _GOLDEN_GOOSE_LEVELS, player_level,
        )
    if template_id == "@basic_ingredient":
        return rng.choice(_BASIC_INGREDIENTS)
    return template_id


def _authored_reward_entries(
    entry: _MappedReward, *, player_level: int, class_name: str,
    rng: random.Random,
) -> tuple[tuple[str, int], ...]:
    """(template id, amount) for one rolled entry, expanding placeholders."""

    if "{cls}" in entry.template_id:
        normalized_class = str(class_name).casefold()
        if normalized_class not in {"warrior", "mage", "ranger", "dwarf"}:
            raise ValueError(
                f"class-specific chest reward {entry.template_id!r} "
                "requires a supported character class"
            )
        return ((entry.template_id.format(cls=normalized_class), entry.amount),)
    expansion = _AUTHORED_REWARD_EXPANSIONS.get(entry.template_id)
    if expansion is not None:
        return expansion
    if entry.template_id == "@glory_jewel":
        jewel = {
            "warrior": "jewel_bgh_warrior",
            "mage": "jewel_bgh_mage",
            "ranger": "jewel_bgh_ranger",
            "dwarf": "jewel_bgh_dwarf",
        }.get(str(class_name).casefold())
        if jewel is None:
            raise ValueError(
                "Surprise Jewel Pack requires a supported character class"
            )
        return ((jewel, entry.amount),)
    if entry.template_id == "@spirit_essence":
        ammunition = {
            "warrior": "rc_ammunition_ammotype_11",
            "ranger": "rc_ammunition_ammotype_12",
            "dwarf": "rc_ammunition_ammotype_13",
            "mage": "rc_ammunition_ammotype_14",
        }.get(str(class_name).casefold())
        if ammunition is None:
            raise ValueError(
                "Surprise Dragan Package requires a supported character class"
            )
        return ((ammunition, entry.amount),)
    if entry.template_id == "@strenuousness_jewel":
        return ((rng.choice((
            "jewel_bgh_key_drop_magic",
            "jewel_bgh_key_drop_rare",
            "jewel_bgh_key_drop_epic",
        )), entry.amount),)
    if entry.template_id == "@community_coins":
        return (("community_coins", rng.choice(_COMMUNITY_COIN_AMOUNTS)),)
    if entry.template_id == "@summer_fortune_coins":
        return (("e_sum_coins", rng.choice((25, 50, 100, 200))),)
    if entry.template_id == "@summer_ingredient_jewel":
        return (("jewel_ingredient_find_common", entry.amount),)
    if entry.template_id == "@summer_gem_fortune_jewel":
        return (("jewel_boss_gem_drop_common", entry.amount),)
    if entry.template_id == "@summer_focus_jewel":
        return (("jewel_skill_cooldown_common", entry.amount),)
    if entry.template_id == "@hal_random_andermant":
        return (("money_rc", rng.randint(2000, 30000)),)
    if entry.template_id == "@hal_random_voucher":
        return (("item_event_hal_exchange", rng.randint(1, 3)),)
    if entry.template_id == "@hal_tiny_troublemaker":
        return ((rng.choice((
            "pet_nanguatouyouling_01", "pet_nanguatouyouling_02",
            "pet_nanguatouyouling_03", "pet_nanguatouyouling_04",
            "pet_nanguatouyouling_05", "pet_nanguatouyouling_06",
            "pet_nanguatouyouling_07", "pet_nanguatouyouling_08",
            "pet_nanguatouyouling_09",
        )), entry.amount),)
    return ((
        _authored_reward_template(
            entry.template_id, player_level=player_level, rng=rng,
        ),
        entry.amount,
    ),)


def _authored_chance_contents(
    static_database_path: str | Path,
    box_definition: PurchaseItemDefinition,
    *,
    player_level: int,
    class_name: str,
    rng: random.Random,
) -> ChestContents | None:
    """Roll a box written in the authored weighted/bonus/roll tables."""

    template_id = box_definition.template_id
    random_payout = _AUTHORED_RANDOM_CHESTS.get(template_id)
    if random_payout is not None:
        if int(player_level) < int(box_definition.required_level):
            raise ValueError(
                f"container {template_id!r} requires level "
                f"{int(box_definition.required_level)}"
            )
        currency, minimum, maximum = random_payout
        return ChestContents(
            box_template_id=template_id,
            source="authored_random_chest",
            rewards=(ChestReward(
                _definition(static_database_path, currency),
                rng.randint(int(minimum), int(maximum)),
            ),),
            used_tooltip_mapping=False,
        )
    weighted = _AUTHORED_WEIGHTED_CHESTS.get(template_id)
    bonus = _AUTHORED_BONUS_CHESTS.get(template_id)
    fixed_roll = _AUTHORED_FIXED_ROLL_CHESTS.get(template_id)
    rolls = _AUTHORED_ROLL_CHESTS.get(template_id)
    source = "authored_roll_chest"
    if weighted is not None:
        source = "authored_weighted_chest"
    elif bonus is not None:
        source = "authored_bonus_chest"
    elif fixed_roll is not None:
        source = "authored_fixed_roll_chest"
    elif rolls is None:
        if template_id not in _CRAFTING_CHEST_TEMPLATES:
            return None
        rolls = _CRAFTING_CHEST_CONTENTS
        source = "authored_crafting_chest"
    if int(player_level) < int(box_definition.required_level):
        raise ValueError(
            f"container {template_id!r} requires level "
            f"{int(box_definition.required_level)}"
        )
    if weighted is not None:
        selected = [_pick_weighted(
            [_MappedReward(item, amount, chance) for item, amount, chance in weighted],
            rng,
        )]
    elif bonus is not None:
        fixed, table = bonus
        selected = [
            _MappedReward(item, amount) for item, amount in fixed
        ] + [_pick_weighted(
            [_MappedReward(item, amount, chance) for item, amount, chance in table],
            rng,
        )]
    elif fixed_roll is not None:
        fixed, table = fixed_roll
        selected = [
            _MappedReward(item, amount) for item, amount in fixed
        ] + [
            _MappedReward(item, amount, chance)
            for item, amount, chance in table
            if rng.random() * 100.0 < chance
        ]
    else:
        selected = [
            _MappedReward(item, amount, chance)
            for item, amount, chance in rolls
            if rng.random() * 100.0 < chance
        ]
    rewards = tuple(
        ChestReward(_definition(static_database_path, reward_template_id), amount)
        for entry in selected
        for reward_template_id, amount in _authored_reward_entries(
            entry, player_level=player_level, class_name=class_name,
            rng=rng,
        )
    )
    if not rewards:
        raise ValueError(
            f"container {template_id!r} rolled no reward"
        )
    return ChestContents(
        box_template_id=template_id,
        source=source,
        rewards=rewards,
        used_tooltip_mapping=False,
    )


def _mystic_cube_contents(
    static_database_path: str | Path,
    box_definition: PurchaseItemDefinition,
    *,
    player_level: int,
    class_name: str,
    rng: random.Random,
) -> ChestContents | None:
    """Roll one Mystic Cube: a single weighted category, never a stack.

    The database authors the cube tiers but not their contents, so the table
    below is the authority for both payout and gem ranks.
    """

    tier = _MYSTIC_CUBE_TIERS.get(box_definition.template_id)
    if tier is None:
        return None
    if int(player_level) < int(box_definition.required_level):
        raise ValueError(
            f"container {box_definition.template_id!r} requires level "
            f"{int(box_definition.required_level)}"
        )
    class_name = str(class_name).casefold()
    if class_name not in _MYSTIC_CUBE_SET_PIECES:
        raise ValueError(
            f"mystic cube {box_definition.template_id!r} requires a valid "
            "player class"
        )

    weights = cube_content_weights(box_definition.template_id)
    roll = rng.randrange(100)
    cumulative = 0
    category = CONTENT_KEYS[-1]
    for key in CONTENT_KEYS:
        cumulative += weights[key]
        if roll < cumulative:
            category = key
            break

    if category == "unique":
        pool = (
            _MYSTIC_CUBE_SHARED_UNIQUES
            + _MYSTIC_CUBE_PREMIUM_UNIQUES[class_name]
        )
        reward = ChestReward(
            _definition(static_database_path, rng.choice(pool)), 1
        )
    elif category == "set":
        reward = ChestReward(
            _definition(
                static_database_path,
                rng.choice(_MYSTIC_CUBE_SET_PIECES[class_name]),
            ),
            1,
        )
    elif category == "gold":
        reward = ChestReward(
            _definition(static_database_path, "money_vc_gold"),
            int(tier.gold),
        )
    elif category == "wisdom":
        reward = ChestReward(
            _definition(static_database_path, "money_gc"),
            int(tier.wisdom),
            fixed_currency="wisdom",
        )
    elif category == "glyph":
        reward = ChestReward(
            _definition(static_database_path, "money_uc_no_auto_use"),
            int(tier.glyph),
            fixed_currency="glyph",
        )
    elif category == "gem":
        rank = rng.choice(tier.gem_ranks)
        family = rng.choice(_STANDARD_GEM_FAMILIES)
        reward = ChestReward(
            _definition(static_database_path, f"gem_{family}_{rank}"), 1
        )
    else:
        reward = ChestReward(
            _definition(static_database_path, "money_rc"),
            _MYSTIC_CUBE_ANDERMANT,
        )
    return ChestContents(
        box_template_id=box_definition.template_id,
        source="mystic_cube",
        rewards=(reward,),
        used_tooltip_mapping=False,
    )


def _lydias_grace_contents(
    static_database_path: str | Path,
    box_definition: PurchaseItemDefinition,
    *,
    player_level: int,
) -> ChestContents | None:
    """Pay one Lydia's Grace box and hand over the next box of the chain.

    The ten ``ancestral_books_lv*`` boxes name server-side loot lists that no
    client data ships; their descriptions only say the box "contains endless
    money" and "can be used at level N". The official Ancestral Ruins article
    states that each opening levels the box by 10 and pays Polished Dragon
    Scales, and that reaching level 100 also pays a Statue of Abundance.
    Each box therefore pays its scales plus the box ten levels up; the level
    100 box ends the chain with the statue instead.
    """

    match = _LYDIAS_GRACE_LEVEL.fullmatch(box_definition.template_id)
    if match is None:
        return None
    level = int(match.group(1))
    if level not in _LYDIAS_GRACE_LEVELS:
        return None
    # RequiredLevel is the client's own gate and matches the description.
    required_level = max(level, int(box_definition.required_level))
    if int(player_level) < required_level:
        raise ValueError(
            f"container {box_definition.template_id!r} requires level "
            f"{required_level}"
        )
    rewards = [ChestReward(
        _definition(static_database_path, "polished_squama"),
        _LYDIAS_GRACE_SCALES,
    )]
    next_level = level + 10
    if next_level in _LYDIAS_GRACE_LEVELS:
        rewards.append(ChestReward(
            _definition(static_database_path, f"ancestral_books_lv{next_level}"),
            1,
        ))
    else:
        rewards.append(ChestReward(
            _definition(static_database_path, _LYDIAS_GRACE_FINAL_REWARD), 1,
        ))
    return ChestContents(
        box_template_id=box_definition.template_id,
        source="lydias_grace_chain",
        rewards=tuple(rewards),
        used_tooltip_mapping=False,
    )


def roll_chest_contents(
    static_database_path: str | Path,
    box_template_id: str,
    *,
    map_path: str | Path = DEFAULT_CHEST_CONTENTS_MAP,
    player_level: int = 1,
    class_name: str = "",
    rng: random.Random | None = None,
) -> ChestContents:
    """Resolve and roll one mapped inventory chest without mutating state."""

    box_template_id = str(box_template_id)
    box_definition = _definition(static_database_path, box_template_id)
    if box_definition.item_category not in _INVENTORY_CONTAINER_CATEGORIES:
        raise ValueError(
            f"item {box_template_id!r} is category "
            f"{box_definition.item_category!r}, not an inventory container"
        )
    # The client opens these in its selection widget (SelectWishingWellMsg,
    # services/seasonal_activities.py); a roll would pick for the player.
    if box_definition.item_category == "Selective":
        raise ValueError(
            f"selective container {box_template_id!r} requires a client "
            "reward selection"
        )
    # chest_contents.json lists the Lydia's Grace boxes with nothing in them.
    lydias_grace = _lydias_grace_contents(
        static_database_path, box_definition, player_level=player_level,
    )
    if lydias_grace is not None:
        return lydias_grace
    mystic_cube = _mystic_cube_contents(
        static_database_path,
        box_definition,
        player_level=player_level,
        class_name=class_name,
        rng=rng or random.SystemRandom(),
    )
    if mystic_cube is not None:
        return mystic_cube
    authored = _authored_chest_contents(
        static_database_path, box_definition,
        player_level=player_level, class_name=class_name,
        rng=rng,
    )
    if authored is not None:
        return authored
    chance_authored = _authored_chance_contents(
        static_database_path, box_definition,
        player_level=player_level,
        class_name=class_name,
        rng=rng or random.SystemRandom(),
    )
    if chance_authored is not None:
        return chance_authored
    # A box whose loot list is server-side only and that chest_contents.json
    # lacks or cannot resolve: what its description lists, or the category
    # it names (tools/chest_contents_from_descriptions.py).
    # FAILURE: "Daily limited chest" (emilia_daily_andermant_cost) and the
    #   category Lucky Spheres could not be opened.
    records = None
    mapped_records = _load_index(map_path).get(box_template_id)
    mapped_item_loot = any(
        str(record.get("source") or "") == "_Template_Item.Loot"
        and record.get("yields")
        for record in (mapped_records or ())
    )
    records = mapped_records if mapped_item_loot else None
    if not records and DESCRIPTION_CHEST_CONTENTS_MAP.is_file():
        records = _load_index(DESCRIPTION_CHEST_CONTENTS_MAP).get(box_template_id)
    if not records:
        records = mapped_records
    if not records:
        adventurer_contents = _adventurer_chest_contents(
            static_database_path,
            box_template_id,
            player_level=player_level,
            class_name=class_name,
            rng=rng or random.SystemRandom(),
        )
        if adventurer_contents is not None:
            return adventurer_contents
        if box_template_id in {"e_misc_gem_bag_01", "lootbox_runes_00"}:
            if int(player_level) < int(box_definition.required_level):
                raise ValueError(
                    f"container {box_template_id!r} requires level "
                    f"{int(box_definition.required_level)}"
                )
            database = sqlite3.connect(
                f"file:{Path(static_database_path).as_posix()}?mode=ro",
                uri=True,
            )
            try:
                row = database.execute(
                    "SELECT Loot FROM _Template_Item WHERE Id=?",
                    (box_template_id,),
                ).fetchone()
            finally:
                database.close()
            synthetic_record = {"loot_list": str(row[0] or "")} if row else {}
            fallback_rng = rng or random.SystemRandom()
            nested_reward = _gem_chest_reward(
                static_database_path, synthetic_record, rng=fallback_rng
            )
            if nested_reward is None:
                nested_reward = _rune_bag_reward(
                    static_database_path, synthetic_record, rng=fallback_rng
                )
            if nested_reward is not None:
                return ChestContents(
                    box_template_id=box_template_id,
                    source=str(synthetic_record.get("loot_list") or "static item loot"),
                    rewards=(nested_reward,),
                    used_tooltip_mapping=False,
                )
        # An unmapped equipment or daily challenge box: its loot list's
        # rarity (_equipment_chest_reward).
        database = sqlite3.connect(
            f"file:{Path(static_database_path).as_posix()}?mode=ro", uri=True,
        )
        try:
            row = database.execute(
                "SELECT Loot FROM _Template_Item WHERE Id=?", (box_template_id,),
            ).fetchone()
        finally:
            database.close()
        equipment_reward = _equipment_chest_reward(
            static_database_path,
            {"loot_list": str(row[0] or "") if row else ""},
            class_name=str(class_name),
            player_level=max(1, int(player_level)),
            rng=rng or random.SystemRandom(),
        )
        if equipment_reward is not None:
            return ChestContents(
                box_template_id=box_template_id,
                source=str(row[0]),
                rewards=(equipment_reward,),
                used_tooltip_mapping=False,
            )
        raise _unresolved_contents(
            box_definition.item_category,
            f"inventory container {box_template_id!r} has no JSON mapping",
        )
    record = _inventory_record(records)
    if record is None:
        # Only an offer bundle or wishing well row shares the box's name.
        raise _unresolved_contents(
            box_definition.item_category,
            f"mapping {box_template_id!r} is not an inventory container",
        )
    required_level = max(1, int(record.get("required_level") or 1))
    if int(player_level) < required_level:
        raise ValueError(
            f"container {box_template_id!r} requires level {required_level}"
        )
    if rng is None:
        rng = random.SystemRandom()

    guaranteed, one_of, chance, saw_choose = _direct_entries(
        static_database_path, record, class_name=str(class_name), rng=rng
    )
    used_tooltip = False
    if not guaranteed and not one_of and not chance:
        guaranteed, tooltip_chance, saw_ui_choose, unweighted = _tooltip_entries(
            static_database_path,
            record,
            player_level=max(1, int(player_level)),
            class_name=str(class_name),
        )
        chance = tooltip_chance
        if not guaranteed and not chance:
            one_of = unweighted
        saw_choose = saw_choose or saw_ui_choose
        used_tooltip = True
    if saw_choose:
        raise ValueError(
            f"selective container {box_template_id!r} requires a client "
            "reward selection"
        )
    if not guaranteed and not one_of and not chance:
        equipment_reward = _equipment_chest_reward(
            static_database_path,
            record,
            class_name=str(class_name),
            player_level=max(1, int(player_level)),
            rng=rng,
        )
        if equipment_reward is not None:
            return ChestContents(
                box_template_id=box_template_id,
                source=str(record.get("loot_list") or record.get("source")),
                rewards=(equipment_reward,),
                used_tooltip_mapping=False,
            )
        gem_reward = _gem_chest_reward(
            static_database_path, record, rng=rng
        )
        if gem_reward is not None:
            return ChestContents(
                box_template_id=box_template_id,
                source=str(record.get("loot_list") or record.get("source")),
                rewards=(gem_reward,),
                used_tooltip_mapping=False,
            )
        rune_reward = _rune_bag_reward(
            static_database_path, record, rng=rng
        )
        if rune_reward is not None:
            return ChestContents(
                box_template_id=box_template_id,
                source=str(record.get("loot_list") or record.get("source")),
                rewards=(rune_reward,),
                used_tooltip_mapping=False,
            )
        raise _unresolved_contents(
            box_definition.item_category,
            f"inventory container {box_template_id!r} has no resolvable rewards",
        )

    selected = list(guaranteed)
    if one_of:
        selected.append(one_of[rng.randrange(len(one_of))])
    selected.extend(_roll_chances(chance, rng))
    if any(entry.template_id == box_template_id for entry in selected):
        raise ValueError(
            f"container {box_template_id!r} maps to itself rather than loot"
        )
    rewards_list: list[ChestReward] = []
    ordinary_selected: list[_MappedReward] = []
    for entry in selected:
        if entry.equipment_rarity:
            roll = roll_equipment_reward(
                static_database_path,
                class_name=str(class_name),
                item_level=max(1, int(player_level)),
                rarity=entry.equipment_rarity,
                slot_types=entry.equipment_slot_types,
                rng=rng,
                appearance_region=entry.equipment_appearance_region,
            )
            rewards_list.append(ChestReward(
                _definition(static_database_path, roll.template_id),
                entry.amount,
                item_level=roll.item_level,
                rarity=roll.rarity,
                appearance=roll.appearance,
            ))
        else:
            ordinary_selected.append(entry)
    rewards_list.extend(
        ChestReward(_definition(static_database_path, template_id), amount)
        for template_id, amount in _aggregate(ordinary_selected)
    )
    rewards = tuple(rewards_list)
    return ChestContents(
        box_template_id=box_template_id,
        source=str(record.get("source") or "chest_contents.json"),
        rewards=rewards,
        used_tooltip_mapping=used_tooltip,
    )


def open_mapped_chest(
    inventory: InventoryInfo,
    currency: CurrencyChanged,
    game_currencies: dict[str, int],
    contents: ChestContents,
    item_id: int,
    *,
    static_database_path: str | Path,
    next_item_id: int,
    player_level: int,
    class_name: str,
    map_difficulty: int = 0,
    rng: random.Random | None = None,
) -> ChestOpenMutation:
    """Atomically consume one chest and materialize its already-rolled loot."""

    builder = InventoryMutationBuilder(inventory, next_item_id)
    consumed = builder.consume(item_id, 1)
    if consumed.template_id != contents.box_template_id:
        raise ValueError(
            f"inventory item {item_id} is {consumed.template_id!r}, expected "
            f"{contents.box_template_id!r}"
        )

    updated_currency = currency
    updated_game_currencies = dict(game_currencies)
    game_updates: dict[str, GameCurrencyChanged] = {}
    fixed_currency_changed = False
    wisdom_amount = 0
    for reward in contents.rewards:
        if reward.fixed_currency == "wisdom":
            wisdom_amount += int(reward.amount)
            continue
        if reward.fixed_currency == "glyph":
            updated_currency = credit_fixed_currency(
                updated_currency, glyph=int(reward.amount)
            )
            fixed_currency_changed = True
            continue
        if reward.fixed_currency:
            raise ValueError(
                f"unknown fixed currency {reward.fixed_currency!r} on "
                f"container {contents.box_template_id!r}"
            )
        credit = credit_currency_template(
            static_database_path,
            updated_currency,
            updated_game_currencies,
            reward.definition.template_id,
            reward.amount,
            scale_by_item_value=False,
        )
        if credit is not None:
            fixed_currency_changed = (
                fixed_currency_changed or credit.currency != updated_currency
            )
            updated_currency = credit.currency
            updated_game_currencies = dict(credit.game_currencies)
            if credit.game_currency_update is not None:
                game_updates[
                    credit.game_currency_update.resource_id
                ] = credit.game_currency_update
            continue
        item_level = max(
            int(reward.definition.minimum_item_level),
            min(
                max(
                    1,
                    int(
                        reward.item_level
                        if reward.item_level is not None
                        else player_level
                    ),
                ),
                int(reward.definition.maximum_item_level),
            ),
        )
        before_ids = {
            int(item.item_id) for item in builder.inventory.items
        }
        builder.add_item(
            reward.definition,
            reward.amount,
            item_level=item_level,
        )
        if reward.rarity is not None:
            for added in builder.inventory.items:
                if int(added.item_id) in before_ids:
                    continue
                added.rarity = int(reward.rarity)
                added.item_level = int(item_level)
                if reward.appearance:
                    added.appearance = str(reward.appearance)

    changed_ids = tuple(
        int(item.item_id) for item in builder.changed_items
    )
    if changed_ids:
        roll_new_equipment_enchantments(
            static_database_path,
            builder.inventory,
            changed_ids,
            class_name=class_name,
            map_difficulty=map_difficulty,
            rng=rng,
        )
        refresh_inventory_economy_values(
            static_database_path,
            builder.inventory,
            item_ids=changed_ids,
            force_melt_value=True,
        )
        current_items = {
            int(item.item_id): item for item in builder.inventory.items
        }
        builder.changed_items = [
            current_items.get(int(item.item_id), item)
            for item in builder.changed_items
        ]
    mutation = builder.finish()
    updates = tuple(game_updates.values())
    return ChestOpenMutation(
        contents=contents,
        inventory=mutation.inventory,
        currency=updated_currency,
        game_currencies=updated_game_currencies,
        game_currency_updates=updates,
        currency_changed=fixed_currency_changed or bool(updates),
        delta=mutation.delta,
        final=mutation.final,
        next_item_id=mutation.next_item_id,
        wisdom_amount=wisdom_amount,
    )


__all__ = [
    "ChestContents",
    "ChestOpenMutation",
    "ChestReward",
    "DEFAULT_CHEST_CONTENTS_MAP",
    "UNRESOLVED_CHEST_MESSAGE",
    "UnresolvedChestError",
    "open_mapped_chest",
    "roll_chest_contents",
]
