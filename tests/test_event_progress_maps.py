"""Event progress collectibles drop only where their event's content lives."""

import json
from pathlib import Path
import random
import sqlite3

import pytest

from dso_server.gameplay.event_system import load_event_catalog
from dso_server.paths import DEFAULT_STATIC_DB_PATH


WINTER_SUMMIT = dict(
    map_id="e_b1205_dun_chr_2012_02",  # access group
    map_aliases=("e_b1205_dun_chr_2012_01", "e_b1205_dun_chr_2012_03"),
)


@pytest.fixture
def catalog(tmp_path):
    def load(*templates, **entry):
        path = Path(tmp_path) / f"events-{len(list(tmp_path.iterdir()))}.json"
        path.write_text(json.dumps({"events": [
            {"event_id": 100 + index, "template_id": template, "state": "active",
             "collectible_drop_chance": 1.0, **entry}
            for index, template in enumerate(templates)
        ]}), encoding="utf-8")
        return load_event_catalog(DEFAULT_STATIC_DB_PATH, path)
    return load


def drops(catalog, *, map_id, level=120, map_aliases=(), monster=""):
    return {item for item, _amount in catalog.collectible_drops(
        map_id=map_id, player_level=level, monster_rank="normal",
        participation={}, status_effects=(), rng=random.Random(5),
        map_aliases=map_aliases, monster_template_id=monster,
    )}


def test_christmas_progress_drops_only_in_its_event_maps(catalog):
    events = catalog("christmas_2012")
    assert drops(events, **WINTER_SUMMIT) == {"e_chr_progress_item"}
    assert drops(events, map_id="e_chr_challenge_dun_01") == {"e_chr_progress_item"}
    for overworld in ("a0302_wildforest", "a0200_kingscity", "b1101_pve"):
        assert drops(events, map_id=overworld) == set()


def test_a_mini_event_drops_only_behind_the_exits_it_opens(catalog):
    # Winter Solstice - Frozen Field opens the Winter Summit's exits to the
    # Frozen Field (e_chr_2015_challenge_04=True); its Soul of Hope drops
    # there, not on the Winter Summit hosting the entrance.
    events = catalog("e_chr_2015_challenge_04")
    frozen_field = dict(map_id="e_b1205_dun_chr_2024",
                        map_aliases=("e_b1205_dun_chr_2024", "e_b1205_dun_chr_2024_03"))
    assert drops(events, **frozen_field) == {"e_chr_challenge_2024_progress"}
    assert drops(events, **WINTER_SUMMIT) == set()
    assert drops(events, map_id="e_chr_challenge_dun_02") == set()
    assert drops(events, map_id="a0200_kingscity") == set()
    # Each Kranparus challenge owns its own lair the same way.
    events = catalog("e_chr_2015_challenge_01")
    assert drops(events, map_id="e_chr_challenge_dun_01") == {"e_chr_challenge_progress_01"}
    assert drops(events, map_id="e_chr_challenge_dun_02") == set()
    assert drops(events, **WINTER_SUMMIT) == set()


def test_item_region_matches_the_monster_region(catalog):
    events = catalog("e_ks_blitz_dragon_fortress")
    worm = next(
        monster for monster, regions in events.monster_regions.items()
        if "sewersblitzworm" in regions
    )
    assert drops(events, map_id="a0600_dragonfortress", level=60, monster=worm) == {
        "e_ks_blitz_01_event_progress_01"}
    assert drops(events, map_id="a0600_dragonfortress", level=60, monster="other") == set()


def test_item_region_matches_the_map_region_and_first_stage_maps(catalog):
    events = catalog("desert_of_essences_02")
    assert drops(events, map_id="e_doe_map_02_blue_diff_00", level=60) == {"e_ess_01_progress_item_01"}
    assert drops(events, map_id="e_doe_map_01_diff_00", level=60) == {"e_ess_01_progress_item_01"}
    assert drops(events, map_id="e_bgh_boss_all", level=60) == set()


def test_leaderboard_progress_never_drops_from_monsters(catalog):
    events = catalog("pvp_seasons_event", "pve_season_event")
    assert drops(events, map_id="a0302_wildforest") == set()


def test_global_kill_events_drop_everywhere_but_excluded_maps(catalog):
    events = catalog("mini_kill_event_1d_mantis")
    assert drops(events, map_id="a0302_wildforest") == {"mini_kill_progress_item"}
    assert drops(events, map_id="a0002_start_hub", level=60) == set()


def test_glimpse_of_the_past_drops_only_on_the_maps_its_text_names(catalog):
    # "Head to Ghastly Grave, Wild Forest at night or Mystra at night": the
    # client's text is the only thing tying this event to a place.
    events = catalog("dragan_event_03_mini")
    soul = {"e_dra_mini_progress_item"}
    for map_id in ("e_dra_03_dun_grave_01", "e_dra_03_dun_wildforestnight_01",
                   "e_dra_03_dun_mystranight_01"):
        assert drops(events, map_id=map_id) == soul
    # A higher difficulty comes as the access group, with the access id aside.
    assert drops(events, map_id="e_dra_03_dun_grave_01",
                 map_aliases=("e_dra_03_dun_grave_01", "e_dra_03_dun_grave_07")) == soul
    for elsewhere in ("a0302_wildforest", "c2101_pve", "e_dra_03_dun_01",
                      "e_dra_03_dun_gloomypastures_01", "e_dra_03_dun_ironforestnight_01"):
        assert drops(events, map_id=elsewhere) == set()


def test_dark_dwarf_progress_comes_from_the_dark_dwarves(catalog):
    events = catalog("anniversary_event_challenge")
    dwarf = "e_anniversary_dds_normal_dark_dwarf_duria"
    assert drops(events, map_id="a0600_dragonfortress", level=60, monster=dwarf) == {
        "e_anniversary_challenge_progress_01"}
    assert drops(events, map_id="a0600_dragonfortress", level=60,
                 monster="a0008_normal_defender_gravedigger") == set()


def test_heroes_assemble_drops_in_the_lands_it_names(catalog):
    events = catalog("steam_welcome")
    proof = {"e_steam_welcome_progress"}
    assert drops(events, map_id="a0302_wildforest", level=40) == proof
    assert drops(events, map_id="c2101_pve", level=40) == proof  # Helios
    assert drops(events, map_id="e_dra_03_dun_01", level=40) == set()


def test_live_stream_progress_is_not_a_kill_drop(catalog):
    # Its text: "Receive special bonus codes that grant Coin Blanks".
    assert drops(catalog("stream_event_01"), map_id="a0302_wildforest", level=60) == set()


def test_a_map_no_event_names_drops_only_monster_hunt_badges(catalog):
    with sqlite3.connect(f"file:{DEFAULT_STATIC_DB_PATH}?mode=ro", uri=True) as database:
        templates = [row[0] for row in database.execute(
            "SELECT Id FROM _Template_Events WHERE COALESCE(EventProgressBars, '') != ''"
        )]
    events = catalog(*templates)
    for level in (40, 60, 120):
        assert drops(events, map_id="a_map_no_event_names", level=level) == {
            "mini_kill_progress_item"}


def test_explicit_schedule_maps_stay_authoritative(catalog):
    events = catalog("christmas_2012", collectible_maps=["a0302_wildforest"])
    assert drops(events, map_id="a0302_wildforest") == {"e_chr_progress_item"}
    assert drops(events, **WINTER_SUMMIT) == set()
    # Derived maps are kept apart, so the CMS never copies them forward.
    assert events.events[0].collectible_maps == frozenset({"a0302_wildforest"})
    assert "e_b1205_dun_chr_2012_02" in events.events[0].progress_maps


def beets(events, *, map_id="a0302_wildforest", monster="a0008_normal_defender_gravedigger",
          rank="monster", level=60, seed=5):
    return events.event_loot_drops(
        map_id=map_id, player_level=level, monster_template_id=monster,
        monster_rank=rank, monster_level=20, participation={}, rng=random.Random(seed),
    )


@pytest.fixture
def sure_loot(tmp_path):
    path = tmp_path / "event_loot.json"
    path.write_text(json.dumps({"loot_lists": {"e_chr_2012_loot_gen_minions": [{
        "item": "e_chr_2012_dungeon_key_normal", "chance": {"monster": 1.0},
        "amount": {"monster": [2, 2], "boss": [7, 7]}}]}}), encoding="utf-8")
    return path


def test_shipped_event_loot_gives_christmas_sugar_beets(catalog):
    events = catalog("christmas_2012")
    assert events.events[0].template.loot_list_id == "e_chr_2012_loot_gen_minions"
    entries = events.event_loot["e_chr_2012_loot_gen_minions"]
    assert entries[0].item_id == "e_chr_2012_dungeon_key_normal" and not entries[0].regions
    assert {entry.regions for entry in entries[1:]} == {frozenset({"e_chr"})}
    rolls = [beets(events, rank="leader", seed=seed) for seed in range(20)]
    assert all(roll and roll[0][0] == "e_chr_2012_dungeon_key_normal" for roll in rolls)


def test_sugar_beets_drop_from_worthy_opponents_on_every_open_map(tmp_path, sure_loot):
    schedule = tmp_path / "schedule.json"
    schedule.write_text(json.dumps({"events": [
        {"event_id": 7, "template_id": "christmas_2012", "state": "active"}]}), encoding="utf-8")
    events = load_event_catalog(DEFAULT_STATIC_DB_PATH, schedule, sure_loot)
    assert beets(events) == (("e_chr_2012_dungeon_key_normal", 2),)
    assert beets(events, map_id="c2101_pve", rank="worldboss") == (("e_chr_2012_dungeon_key_normal", 7),)
    # Winter Summit monsters, excluded maps, under-level players, opted-out players.
    assert beets(events, map_id="e_b1205_dun_chr_2012_02", monster="e_chr_b1205_normal_troll_01") == ()
    assert beets(events, map_id="a0002_start_hub") == ()
    assert beets(events, level=19) == ()
    assert events.event_loot_drops(
        map_id="a0302_wildforest", player_level=60, monster_template_id="a0008_normal_defender_gravedigger",
        monster_rank="monster", monster_level=20, participation={"7": False}, rng=random.Random(1),
    ) == ()
    expired = tmp_path / "expired.json"
    expired.write_text(json.dumps({"events": [
        {"event_id": 7, "template_id": "christmas_2012", "state": "expired"}]}), encoding="utf-8")
    assert beets(load_event_catalog(DEFAULT_STATIC_DB_PATH, expired, sure_loot)) == ()


def test_event_loot_becomes_an_auto_picked_key_drop():
    from dso_server.protocol.world_loot_protocol import generate_monster_loot

    generation = generate_monster_loot(
        DEFAULT_STATIC_DB_PATH, "a0008_normal_defender_gravedigger", class_name="mage",
        player_level=60, position=(0.0, 0.0, 0.0), next_item_id=1, rng=random.Random(3),
        additional_event_loot=(("e_chr_2012_dungeon_key_normal", 4),),
    )
    drop = next(drop for drop in generation.drops
                if drop.item.template_id == "e_chr_2012_dungeon_key_normal")
    assert (drop.item_category, drop.auto_pick_up, drop.item.amount) == ("Key", "All", 4)


def test_fixed_page_bars_drop_the_current_pages_item_like_retail(catalog):
    # Retail 2026-10-28 capture, e_dra_03_dun_02: a first kill dropped two
    # Rise of Dragan keys (NewItem Key, rarity 0, level 1, f30 0); picking
    # them up stored the key and sent EventProgressChanged with amount 2.
    events = catalog("dragan_event_03")
    event_id = str(events.events[0].event_id)

    def page_drop(amount):
        return events.collectible_drops(
            map_id="e_dra_03_dun_02", player_level=50, monster_rank="monster",
            participation={}, status_effects=(), rng=random.Random(1),
            progress={event_id: {"amount": amount}} if amount else {},
        )

    assert page_drop(0) == (("e_dra_02_RiseOfDragan_key_for_maps_01", 2),)
    assert page_drop(900) == (("e_dra_progress_item", 2),)
    assert page_drop(5000) == (("e_dra_progress_item_boss", 2),)


def test_stored_collectibles_drop_as_retail_new_items():
    from dso_server.protocol.world_loot_protocol import generate_monster_loot

    generation = generate_monster_loot(
        DEFAULT_STATIC_DB_PATH, "a0008_normal_defender_gravedigger", class_name="mage",
        player_level=50, position=(0.0, 0.0, 0.0), next_item_id=1, rng=random.Random(3),
        additional_instant_consumables=(("e_dra_02_RiseOfDragan_key_for_maps_01", 2),),
    )
    drop = next(drop for drop in generation.drops
                if drop.item.template_id == "e_dra_02_RiseOfDragan_key_for_maps_01")
    item = drop.item
    assert (drop.item_category, drop.auto_pick_up) == ("Key", "All")
    assert (item.amount, item.field_08, item.item_level, item.field_28, item.field_30,
            item.field_3c, item.field_3e, item.field_38, item.socket_count) == (
        2, 0, 1, "none", 0, False, True, 0, 0)


def page_drops(events, *, map_id, level=90, amount=0, monster=""):
    event_id = str(events.events[0].event_id)
    return events.collectible_drops(
        map_id=map_id, player_level=level, monster_rank="monster",
        participation={}, status_effects=(), rng=random.Random(1),
        monster_template_id=monster,
        progress={event_id: {"amount": amount}} if amount else {},
    )


def test_per_page_lists_without_the_fixed_flag_drop_one_item_per_kill(catalog):
    # Full Moon lists the same item for each of its three pages; read as
    # progress values it dropped three stacks on every kill.
    events = catalog("fullmoon_event_02")
    monster = next(m for m, regions in events.monster_regions.items() if "progressmonster" in regions)
    assert page_drops(events, map_id="e_fullmoon_varholm_dun", monster=monster) == (
        ("e_ful_01_progress_item_01", 3),)
    # Lost Viscanium changes the item from page to page.
    events = catalog("e_ide_challenge_01")
    assert page_drops(events, map_id="e_z0110_dun", level=120) == (
        ("event_identify_drop_crystal_piece_02", 1),)
    assert page_drops(events, map_id="e_z0110_dun", level=120, amount=10**6) == (
        ("e_ide_02_challenge_progress_02", 1),)


def test_anniversary_drops_the_page_its_player_is_on(catalog):
    events = catalog("anniversary_event")
    assert page_drops(events, map_id="e_anniversary_arena_01_duria", level=60) == (
        ("e_anniversary_main_progress_duria", 1),)


def test_happy_hour_bars_never_drop_andermant(catalog):
    for template in ("e_hh_01", "e_hh_01_24000", "e_sm_greed"):
        events = catalog(template)
        assert page_drops(events, map_id="e_hh_boss_t0_diff_00", level=60) == ()


def test_events_owning_no_maps_fall_back_to_the_maps_scripting_them(catalog):
    events = catalog("helios_event_01")
    assert page_drops(events, map_id="a0201_basicarena", level=60) == (
        ("event_olympia_trademarks", 1),)
    assert page_drops(events, map_id="a0302_wildforest", level=60) == ()
    # Region-scoped progress keeps its own scope: only the blitz worm.
    events = catalog("e_ks_blitz_dragon_fortress")
    assert page_drops(events, map_id="a0600_dragonfortress", level=60, monster="other") == ()


def test_an_event_maps_final_boss_pays_a_large_progress_stack(catalog):
    from dso_server.protocol.world_loot_protocol import generate_monster_loot

    events = catalog("christmas_2012", collectible_drop_chance=0.0)
    # Bosses always drop, whatever the run's drop chance.
    big_paws = events.collectible_drops(
        map_id=WINTER_SUMMIT["map_id"], map_aliases=WINTER_SUMMIT["map_aliases"],
        player_level=120, monster_rank="worldboss", participation={},
        status_effects=(), rng=random.Random(1),
    )
    assert big_paws == (("e_chr_progress_item", 5),)
    assert drops(events, **WINTER_SUMMIT) == set()

    def stack(template, difficulty):
        generation = generate_monster_loot(
            DEFAULT_STATIC_DB_PATH, template, class_name="mage", player_level=100,
            position=(0.0, 0.0, 0.0), next_item_id=1, rng=random.Random(1),
            map_difficulty=difficulty,
            additional_instant_consumables=(("e_chr_progress_item", 5),),
        )
        return next(d.item.amount for d in generation.drops
                    if d.item.template_id == "e_chr_progress_item")

    assert stack("e_chr_b1205_normal_troll_01", 0) == 5
    assert stack("e_chr_boss_big_paws", 0) == 125  # 25x the base drop
    assert stack("e_chr_boss_big_paws", 4) > stack("e_chr_boss_big_paws", 0)


def test_christmas_presents_drop_on_the_winter_maps_by_difficulty(catalog):
    events = catalog("christmas_2012")

    def presents(map_id, monster, difficulty, kills=400):
        found = set()
        for seed in range(kills):
            for item, _amount in events.event_loot_drops(
                map_id=map_id, map_aliases=(map_id,), player_level=100,
                monster_template_id=monster, monster_rank="monster", monster_level=100,
                participation={}, rng=random.Random(seed), map_difficulty=difficulty,
            ):
                if item.startswith("e_chr_progress_box_"):
                    found.add(item)
        return found

    assert presents("e_b1205_dun_chr_2012_02", "e_chr_b1205_normal_norsegnome_01", 3) >= {
        "e_chr_progress_box_uncommon_Diff_03", "e_chr_progress_box_magic_Diff_03"}
    assert presents("a0302_wildforest", "a0008_normal_defender_gravedigger", 3) == set()


def test_a_christmas_present_holds_snow_essence_or_a_christmas_weapon():
    from dso_server.protocol.chest_loot_protocol import roll_chest_contents

    seen = set()
    for seed in range(30):
        contents = roll_chest_contents(
            DEFAULT_STATIC_DB_PATH, "e_chr_progress_box_rare_Diff_03",
            player_level=100, class_name="mage", rng=random.Random(seed),
        )
        reward, = contents.rewards
        if reward.appearance:
            assert reward.appearance.endswith("_christmas") and reward.rarity == 3
            seen.add("weapon")
        else:
            assert (reward.definition.template_id, reward.amount) == (
                "e_chr_2012_snow_ammunition", 500)
            seen.add("essence")
    assert seen == {"weapon", "essence"}
