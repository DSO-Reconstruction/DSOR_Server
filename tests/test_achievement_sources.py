"""Remaining achievement sources: difficulty-qualified kills (retired
"nomob" kill rows stay unsupported), high-score places, PvP modes, veteran
ranks and collected skins."""

from datetime import datetime, timezone
from types import SimpleNamespace
import sqlite3

import pytest

from dso_server.gameplay.achievement_content import (
    apply_achievement_event,
    battleground_mode,
    load_achievement_catalog,
)
from dso_server.gameplay.skin_collection import held_skins, load_skin_catalog
from dso_server.gameplay.veteran_levels import (
    account_age_years,
    load_veteran_rules,
    veteran_level,
)
from dso_server.paths import DEFAULT_STATIC_DB_PATH
from dso_server.persistence.account_state import _state_from_document, _state_to_document
from dso_server.protocol.inventory_codec import InventoryInfo, ItemInfo
from dso_server.services.achievements import AchievementService
from dso_server.tests import test_achievements as achievement_fixtures

NOW = datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def catalog():
    return load_achievement_catalog(DEFAULT_STATIC_DB_PATH)


@pytest.fixture
def player():
    case = achievement_fixtures.AchievementServiceTests()
    case.setUp()
    try:
        yield case.server, case.session, case.saves
    finally:
        case.doCleanups()


def _static(query, *args):
    with sqlite3.connect(f"file:{DEFAULT_STATIC_DB_PATH.as_posix()}?mode=ro", uri=True) as db:
        return db.execute(query, args).fetchall()


# ---------------------------------------------------------------------------
# Coverage, retired ("nomob") kill rows and difficulty-qualified kills
# ---------------------------------------------------------------------------

RETIRED_KILL_QUERY = (
    "SELECT Id FROM _Template_Achievements WHERE EnabledLive=1 AND Type='Kill' "
    "AND COALESCE(Param, '') IN ('', 'nomob')"
)


def test_only_the_retired_kill_rows_have_no_evaluator(catalog):
    retired = sorted(row[0].casefold() for row in _static(RETIRED_KILL_QUERY))
    assert len(retired) == 75
    missing = sorted(d.key for d in catalog.achievements.values() if d.event_kind is None)
    assert missing == retired
    indexed = sum(len(rows) for rows in catalog.by_event_kind.values())
    assert (indexed, len(catalog.achievements)) == (848, 923)


def test_retired_kill_rows_never_progress_from_any_kill(catalog):
    state = {}
    for template in ("boss_grimmag", "boss_mortis_phase2", "a0604_normal_g_Herold_scale",
                     "pw100_01_guardian_death_knight_heroic", "g0000_achievement_clawstrider_02",
                     "nomob", ""):
        for difficulty in range(11):
            apply_achievement_event(catalog, state, "kill", (template, f"{template}:{difficulty}"), count=100)
    for (achievement_id,) in _static(RETIRED_KILL_QUERY):
        progress = state.get(achievement_id.casefold())
        assert progress is None or (progress.progress == 0 and not progress.completed), achievement_id


def test_saved_progress_on_a_retired_row_is_kept_but_never_completes(catalog):
    from dso_server.gameplay.achievement_content import (
        AchievementProgress, refresh_derived_achievements,
    )

    # An older save may hold progress at the target while the (also retired)
    # prerequisite is open, or with no prerequisite at all.
    state = {
        "pw_kill_boss_grimmag_05": AchievementProgress(1),
        "general_kill_mortis_01": AchievementProgress(1),
    }
    assert refresh_derived_achievements(catalog, state) == ()
    apply_achievement_event(catalog, state, "kill", ("boss_grimmag", "boss_grimmag:4"))
    assert not state["pw_kill_boss_grimmag_05"].completed
    assert not state["general_kill_mortis_01"].completed
    assert state["pw_kill_boss_grimmag_05"].progress == 1


def test_one_kill_is_one_hit_even_when_a_row_names_template_and_difficulty(catalog):
    state = {}
    apply_achievement_event(catalog, state, "kill", ("boss_grimmag", "boss_grimmag:5"), count=3)
    # pw_kill_boss_grimmag_02 names the bare template, pw_difficulty_08 the token.
    assert state["pw_kill_boss_grimmag_02"].progress == 3
    assert state["pw_difficulty_08_defeatboss"].progress == 3


def test_kill_hook_adds_the_session_difficulty_token(player):
    server, session, _saves = player
    session.gameplay_map_difficulty = 7
    monsters = (SimpleNamespace(template_id="boss_grimmag"),) * 2
    server._achievement_on_monsters_killed(session, monsters)
    assert session.gameplay_achievements["pw_difficulty_09_defeatboss"].progress == 2
    assert "pw_difficulty_08_defeatboss" not in session.gameplay_achievements \
        or session.gameplay_achievements["pw_difficulty_08_defeatboss"].progress == 0
    # The retired Infernal IV row has no evaluator.
    assert not getattr(session.gameplay_achievements.get("pw_kill_boss_grimmag_07"), "progress", 0)
    # Slider encounters carry the tier in the scaling difficulty instead.
    session.gameplay_map_difficulty = 0
    session.gameplay_scaling_difficulty = 8
    server._achievement_on_monsters_killed(session, (SimpleNamespace(template_id="boss_mortis_unbound"),))
    assert session.gameplay_achievements["kill_defeat_mortis_difficulty_08"].completed


# ---------------------------------------------------------------------------
# HighScoreRanking
# ---------------------------------------------------------------------------

def test_high_score_place_counts_once_per_settlement_within_the_threshold(catalog):
    state = {}
    for place in ("0", "4", "-1", "first", ""):
        assert apply_achievement_event(catalog, state, "highscoreranking", (place,)) == ()
    apply_achievement_event(catalog, state, "highscoreranking", ("3", "3"))
    assert state["fracturedplanes_rank3"].progress == 1
    for place in ("1", "2", "3", "2"):
        apply_achievement_event(catalog, state, "highscoreranking", (place,))
    assert state["fracturedplanes_rank3"].completed
    assert apply_achievement_event(catalog, state, "highscoreranking", ("1",)) == ()


def test_high_score_feed_through_the_service(player):
    server, session, saves = player
    for _ in range(5):
        server._achievement_feed(session, "highscoreranking", (str(2),))
    assert session.gameplay_achievements["fracturedplanes_rank3"].completed
    assert "achievement-highscoreranking" in saves


# ---------------------------------------------------------------------------
# PvP_Match
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value,key", (
    ("CaptureTheFlag_5v5", "pvp_win_ctf_01"),
    ("capturetheflag_5V5", "pvp_win_ctf_01"),
    ("CaptureTheFlag_5v5_01", "pvp_win_ctf_01"),
    ("CaptureTheFlag_5v5_01_Predefined", "pvp_win_ctf_01"),
    ("ASSAULTTHEFORTRESS_6V6", "pvp_win_atf_01"),
    ("AssaultTheFortress_6v6_Predefined", "pvp_win_atf_01"),
    ("BestOf5_3v3", "pvp_win_tdm_01"),
    ("bestof3_1v1_01_default", "pvp_win_dm_01"),
))
def test_pvp_match_modes_match_case_insensitively(catalog, value, key):
    state = {}
    apply_achievement_event(catalog, state, "pvp_match", (value,))
    assert [name for name, progress in state.items()
            if name.startswith("pvp_win_") and progress.completed] == [key]


@pytest.mark.parametrize("value", (
    "BestOf5_5v5_01_default", "AssaultTheFortress_5v5_01", "CaptureTheFlag_2v2", "LastManStanding_8ffa_01",
))
def test_other_settings_ids_do_not_name_an_authored_mode(catalog, value):
    state = {}
    apply_achievement_event(catalog, state, "pvp_match", (value,))
    assert not any(key.startswith("pvp_win_") and progress.progress for key, progress in state.items())


def test_battleground_mode_strips_only_number_and_variant_suffixes():
    assert battleground_mode("BestOf5_5v5_01_default") == "bestof5_5v5"
    assert battleground_mode("BestOf3_1v1") == "bestof3_1v1"
    assert battleground_mode("AssaultTheFortress_DEBUG_Predefined") == "assaultthefortress_debug"


# ---------------------------------------------------------------------------
# VeteranLevelUnlock
# ---------------------------------------------------------------------------

def _epoch(*args):
    return datetime(*args, tzinfo=timezone.utc).timestamp()


def test_veteran_rules_come_from_the_static_rows():
    rules = load_veteran_rules(DEFAULT_STATIC_DB_PATH)
    assert rules.character_level == 100
    assert [(r.level_id, r.min_account_years, r.min_characters) for r in rules.levels] == [
        ("Veteran1", 4, 1), ("Veteran2", 5, 2), ("Veteran3", 7, 3)]


def test_account_age_counts_whole_anniversaries():
    created = _epoch(2020, 10, 26, 13, 0)
    assert account_age_years(created, NOW) == 5
    assert account_age_years(created, datetime(2026, 10, 26, 13, 0, tzinfo=timezone.utc)) == 6


@pytest.mark.parametrize("years,levels,expected", (
    (4, (100,), "Veteran1"),
    (4, (99, 99), None),
    (5, (100, 100), "Veteran2"),
    (7, (100, 100), "Veteran2"),
    (7, (100, 100, 105), "Veteran3"),
    (6, (100, 100, 100), "Veteran2"),
    (3, (100, 100, 100), None),
))
def test_veteran_rank_needs_both_account_age_and_level_100_characters(years, levels, expected):
    rules = load_veteran_rules(DEFAULT_STATIC_DB_PATH)
    created = _epoch(2026 - years, 1, 1)
    rank = veteran_level(rules, account_created_at=created, character_levels=levels, now=NOW)
    assert (rank.level_id if rank else None) == expected


def test_veteran_status_completes_the_reached_ranks_once(player):
    server, session, _saves = player
    other_profiles = {7001: SimpleNamespace(level=100), 7002: SimpleNamespace(level=40)}
    session.gameplay_character.level = 100
    server.account_registry = SimpleNamespace(character_owner=lambda cid: SimpleNamespace(
        created_at=_epoch(2020, 1, 1), character_ids=(cid, 7001, 7002)))
    server.account_store = SimpleNamespace(
        get_profile=other_profiles.get,
        achievement_snapshot=lambda character_id, catalog: {},
    )
    server._achievement_on_veteran_status(session)
    achievements = session.gameplay_achievements
    assert achievements["veteran_level_01"].completed
    assert achievements["veteran_level_02"].completed
    assert "veteran_level_03" not in achievements or not achievements["veteran_level_03"].completed
    stamped = achievements["veteran_level_02"].completed_at
    count = len(session.recorder.messages)
    server._achievement_on_veteran_status(session)
    assert achievements["veteran_level_02"].completed_at == stamped
    assert len(session.recorder.messages) == count


def test_veteran_status_without_account_record_feeds_nothing(player):
    server, session, _saves = player
    server._achievement_on_veteran_status(session)
    server.account_registry = SimpleNamespace(character_owner=lambda cid: None)
    server._achievement_on_veteran_status(session)
    assert not any(key.startswith("veteran_level") for key in session.gameplay_achievements)


# ---------------------------------------------------------------------------
# UnlockSkin
# ---------------------------------------------------------------------------

def test_held_items_contribute_their_own_class_look():
    skins = load_skin_catalog(DEFAULT_STATIC_DB_PATH)
    items = [
        ItemInfo(item_id=1, template_id="ranger_base_helmet_armor_block_damage"),
        # A generated drop shows its rolled appearance, not the template's.
        ItemInfo(item_id=2, template_id="ranger_base_helmet_armor_block_damage",
                 field_80="ranger_rh_shortbow_02"),
        # Another class's look, an unidentified item and a skin consumable.
        ItemInfo(item_id=3, template_id="mage_unique_rh_staff_gnob"),
        ItemInfo(item_id=4, template_id="ranger_rh_shortbow_unknown", field_3c=True,
                 field_80="ranger_rh_shortbow_03"),
        ItemInfo(item_id=5, template_id="skin_ranger_unique_boots_manticore_01"),
    ]
    assert held_skins(skins, items, "Ranger") == {
        "ranger_basic_helmet_01", "ranger_basic_rh_shortbow_02"}


def _ranger_items(*templates):
    return InventoryInfo(
        items=[ItemInfo(item_id=index + 1, template_id=template) for index, template in enumerate(templates)],
        storage_58={index + 1: index for index in range(len(templates))},
    )


def _ranger_looks(count):
    skins = load_skin_catalog(DEFAULT_STATIC_DB_PATH)
    looks = {}
    for template, skin in skins.template_skins.items():
        if "ranger" in skins.skin_classes[skin] and skin not in looks.values():
            looks[template] = skin
        if len(looks) == count:
            return tuple(looks)
    raise AssertionError("not enough ranger skins")


def test_collection_grows_once_per_new_look_and_survives_selling(player):
    server, session, saves = player
    templates = _ranger_looks(12)
    AchievementService._restore_achievement_state(session, SimpleNamespace(achievements={}, chapters_unlocked=()))
    session.gameplay_inventory = _ranger_items(*templates[:9])
    server._achievement_on_equipment_changed(session)
    assert len(session.gameplay_unlocked_skins) == 9
    assert session.gameplay_achievements["skin_unlock_amount_01"].progress == 9
    # Repeating the hook, holding duplicates or relogging adds nothing.
    session.gameplay_inventory = _ranger_items(*templates[:9], *templates[:9])
    for _ in range(3):
        server._achievement_on_equipment_changed(session)
        server._achievement_on_item_collected(session, templates[0])
    assert session.gameplay_achievements["skin_unlock_amount_01"].progress == 9
    # The tenth look completes the first rank; selling items keeps the record.
    session.gameplay_inventory = _ranger_items(templates[9])
    server._achievement_on_item_collected(session, templates[9])
    assert session.gameplay_achievements["skin_unlock_amount_01"].completed
    assert session.gameplay_achievements["skin_unlock_amount_02"].progress == 10
    assert len(session.gameplay_unlocked_skins) == 10
    assert "achievement-unlockskin" in saves


def test_collection_round_trips_and_login_reconcile_does_not_double_count(player):
    server, session, _saves = player
    templates = _ranger_looks(11)
    state = achievement_fixtures._account_state(5, unlocked_skins=("b_skin", "a_skin", "a_skin"))
    document = _state_to_document(state)
    assert document["unlocked_skins"] == ["a_skin", "b_skin"]
    assert _state_from_document(document).unlocked_skins == ("a_skin", "b_skin")
    legacy = dict(document)
    del legacy["unlocked_skins"]
    assert _state_from_document(legacy).unlocked_skins == ()

    looks = load_skin_catalog(DEFAULT_STATIC_DB_PATH).template_skins
    persisted = SimpleNamespace(achievements={}, chapters_unlocked=(),
                                unlocked_skins=tuple(looks[t] for t in templates[:10]))
    AchievementService._restore_achievement_state(session, persisted)
    # The ten collected looks are held again plus one new one.
    session.gameplay_inventory = _ranger_items(*templates)
    server._achievement_on_skins_collected(session, reconcile=True)
    assert len(session.gameplay_unlocked_skins) == 11
    assert session.gameplay_achievements["skin_unlock_amount_02"].progress == 11
    server._achievement_on_skins_collected(session, reconcile=True)
    assert session.gameplay_achievements["skin_unlock_amount_02"].progress == 11


def test_unrestored_session_never_writes_a_collection(player):
    server, session, saves = player
    session.gameplay_inventory = _ranger_items(*_ranger_looks(3))
    server._achievement_on_skins_collected(session, reconcile=True)
    assert getattr(session, "gameplay_unlocked_skins", None) is None
    assert "achievement-unlockskin" not in saves


def test_real_initialization_collects_starter_looks_once_and_persists_them():
    from dso_server.tests import test_map_unlocks as init_fixtures

    case = init_fixtures.MapUnlockTests()
    case.setUp()
    try:
        session, _ = case.enter("a0001_start_tutorial_dun")
        character_id = session.gameplay_character.character_id
        saved = case.server.account_store.get(character_id)
        skins = load_skin_catalog(DEFAULT_STATIC_DB_PATH)
        class_name = {0: "warrior", 1: "mage", 2: "ranger", 3: "dwarf"}[
            int(session.gameplay_character.class_id)]
        starter = held_skins(skins, session.gameplay_inventory.items, class_name)
        # The tutorial's starter equipment counts, like retail's tutorial run
        # where skin_unlock_amount_* counted up as progress-only records.
        assert starter and set(saved.unlocked_skins) == starter
        progress = saved.achievements["skin_unlock_amount_01"]["progress"]
        assert progress == len(starter)
        restored, _ = case.enter("a0001_start_tutorial_dun")
        assert restored.gameplay_unlocked_skins == starter
        assert case.server.account_store.get(character_id).achievements[
            "skin_unlock_amount_01"]["progress"] == progress
    finally:
        case.doCleanups()
