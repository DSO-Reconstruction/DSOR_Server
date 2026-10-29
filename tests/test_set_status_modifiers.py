"""Item-set/status modifiers with no earlier runtime branch.

Authored data (static.db4 ``_Template_ItemSet`` / ``_Template_StatusEffect``):

* ``QuestDropFactor:1,relative,Local|Repeatable`` (eight Dragan back-item
  sets, tier 2): "Doubles the amount of dropped quest items for side quests"
  (``backitemQuestdrop``). ``event_quest_drop`` doubles Event quests; the
  Parrot's "chance" is its ``ActorStatusEffect:addAE,event_quest_drop,C:0.2``.
* ``HealthGlobeBonus:0.15,absolute,HitPoints`` (Ammon's Gleam, tier 4):
  "+ 15% Health Points from Healing Spheres"; Upgraded Spheres'
  ``HealthGlobeBonus:$trait_...,absolute,SkillResource`` restores that share
  of the Resource pool.
* Black Essence: ``AdditionalSkillCost:-1.0`` (tier 3) reduces essence use by
  one, never below 1; ``IgnoreSkillCost:0.15`` (tier 5) is a 15% chance of
  using no essence.
"""

from __future__ import annotations

from dataclasses import replace
import sqlite3
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from dso_server.gameplay.quest_content import QuestProgress
from dso_server.gameplay.status_modifier_runtime import (
    evaluate_active_status_modifiers,
    load_status_modifier_catalog,
    parse_modifier,
    snapshot_player_modifier_sources,
)
from dso_server.paths import DEFAULT_STATIC_DB_PATH
from dso_server.protocol.gameplay_protocol import (
    StatusEffectEntry,
    decode_actor_stats_update_batch,
)
from dso_server.protocol.inventory_codec import InventoryInfo
from dso_server.protocol.item_set_protocol import ItemSetDatabase
from dso_server.services import player_resources as resources_module
from dso_server.services.character_server import CharacterServer
from dso_server.tests import test_essence_consumption as essence
from dso_server.tests import test_health_globe_pickup as globes
from dso_server.tests import test_map_events as map_fixtures
from dso_server.tests import test_quests as quest_fixtures

CATALOG = load_status_modifier_catalog(DEFAULT_STATIC_DB_PATH)
SETS = ItemSetDatabase(DEFAULT_STATIC_DB_PATH)


def _effect_index(resource_id):
    with sqlite3.connect(f"file:{DEFAULT_STATIC_DB_PATH}?mode=ro", uri=True) as db:
        return int(db.execute(
            "SELECT rowid - 1 FROM _Template_StatusEffect WHERE Id=?",
            (resource_id,),
        ).fetchone()[0])


def _entry(resource_id, values=(), instance_id=900):
    return StatusEffectEntry(
        effect_id=_effect_index(resource_id), field_04=instance_id,
        field_0c=0, field_14=25, field_08=0, field_18=0, field_10=0,
        field_1c=100, field_20=1, field_25=False, field_26=False,
        field_39=True, has_details=bool(values), values=tuple(values),
    )


def _set(class_name, *parts):
    return SETS.evaluate(set(parts), class_name=class_name)


def _modifiers(*sources):
    return evaluate_active_status_modifiers(CATALOG, sources)


DRAGAN_RANGER = ("ranger_mythic_helmet_dragan_sp", "ranger_mythic_backitem_dragan_sp")
AMMON_WARRIOR = (
    "warrior_unique_helmet_pw_ammon_set", "warrior_unique_shoulders_pw_ammon_set",
    "warrior_unique_torso_pw_ammon_set", "warrior_unique_gloves_pw_ammon_set",
)
BLACK_ESSENCE_MAGE = (
    "mage_unique_helmet_essence_black_set", "mage_unique_shoulders_essence_black_set",
    "mage_unique_torso_essence_black_set", "mage_unique_gloves_essence_black_set",
    "mage_unique_boots_essence_black_set",
)


# --- QuestDropFactor -------------------------------------------------------

def test_dragan_tier_two_doubles_local_and_repeatable_quest_drops_only():
    one_part = _modifiers(*_set("ranger", DRAGAN_RANGER[0]).modifiers)
    assert one_part.quest_drop_factor("Local") == 1.0
    bonus = _modifiers(*_set("ranger", *DRAGAN_RANGER).modifiers)
    assert bonus.quest_drop_factor("Local") == 2.0
    assert bonus.quest_drop_factor("repeatable") == 2.0
    for other in ("World", "Event", "Heroic", "EventRepeatable", ""):
        assert bonus.quest_drop_factor(other) == 1.0
    assert bonus.quest_drop_bonus("local").apply(3, minimum=1) == 6


def test_parrot_event_drop_is_a_deterministic_factor_behind_a_chance():
    assert _modifiers(_entry("event_quest_drop")).quest_drop_factor("Event") == 2.0
    with sqlite3.connect(f"file:{DEFAULT_STATIC_DB_PATH}?mode=ro", uri=True) as db:
        (program,) = db.execute(
            "SELECT StartModifiers FROM _Template_StatusEffect WHERE Id='pet_parrot_04'"
        ).fetchone()
    # The "chance" of gui.statuseffects ParrotQuestDrop is the application.
    assert "ActorStatusEffect:addAE,event_quest_drop,C:0.2" in program


def test_quest_drop_factor_respects_the_attribute_limits():
    assert _modifiers(parse_modifier("QuestDropFactor:-5,relative,Local")).quest_drop_factor("Local") == 1.0
    assert _modifiers(parse_modifier("QuestDropFactor:1,absolute,Local")).quest_drop_factor("local") == 2.0


@pytest.fixture
def quest_world():
    case = quest_fixtures.QuestServiceTests(methodName="runTest")
    case.setUp()
    session = case._session(quest_fixtures.GARDEN, 50291, class_id=2)
    case.session = session
    case.server.sessions[session.addr] = session
    session.gameplay_inventory = InventoryInfo()
    session.gameplay_quest_log[quest_fixtures.GRIMMAGSTONE_02] = QuestProgress(
        quest_fixtures.GRIMMAGSTONE_02
    )
    session.gameplay_presence_callback = case.server._handle_presence_event
    with patch.object(CharacterServer, "_achievement_on_item_collected"):
        try:
            yield case
        finally:
            case.doCleanups()


def _kill_roots(case):
    case.server._quest_on_monsters_killed(case.session, (SimpleNamespace(
        template_id=quest_fixtures.ROOTS, position=(0.0, 0.0, 0.0),
    ),))
    return [drop.item.amount for drop in case.session.gameplay_world_loot.values()]


@pytest.mark.parametrize("quest_type, amount", [("local", 2), ("world", 1)])
def test_equipped_back_item_set_doubles_side_quest_ground_drops(quest_world, quest_type, amount):
    quest_world.session.gameplay_item_set_evaluation = _set("ranger", *DRAGAN_RANGER)
    catalog = quest_world.server._quest_catalog()
    lookup = catalog.quest
    authored = lookup(quest_fixtures.GRIMMAGSTONE_02)
    assert authored.quest_type.casefold() == "world"
    patched = replace(authored, quest_type=quest_type)
    with patch.object(type(catalog), "quest", lambda _self, key: (
        patched if key == quest_fixtures.GRIMMAGSTONE_02 else lookup(key)
    )):
        assert _kill_roots(quest_world) == [amount]


# --- HealthGlobeBonus ------------------------------------------------------

def test_ammon_tier_four_adds_fifteen_points_to_sphere_healing():
    assert _modifiers(*_set("warrior", *AMMON_WARRIOR[:3]).modifiers).health_globe_fractions(0.2) == (0.2, 0.0)
    four = _modifiers(*_set("warrior", *AMMON_WARRIOR).modifiers)
    assert four.health_globe_fractions(0.2) == pytest.approx((0.35, 0.0))


def test_upgraded_spheres_restore_their_rank_share_of_the_resource():
    rank = _modifiers(_entry("trait_groupaura_dwarf_resource_restore_effect", (0.02, 0.0)))
    assert rank.health_globe_fractions(0.2, 0.0) == pytest.approx((0.2, 0.02))


@pytest.fixture
def globe_world():
    case = map_fixtures.MapEventServiceTests(methodName="runTest")
    case.setUp()
    try:
        yield case
    finally:
        case.doCleanups()


def _player(session):
    combat = session.gameplay_combat_state
    return combat, combat.player_actor_id


def test_sphere_pickup_heals_with_the_set_bonus(globe_world):
    s = globe_world.session
    s.gameplay_item_set_evaluation = _set("warrior", *AMMON_WARRIOR)
    combat, actor = _player(s)
    maximum = combat.player_maximum_health
    globes.set_health(s, maximum // 25)
    item_id = globes.place_globe(s)
    assert globe_world.server._pickup_world_item(s, item_id, automatic=True)
    # The set's +15 points add to the 80% sphere floor.
    assert globes.current_health(s) == maximum // 25 + round(maximum * 0.95)


def test_upgraded_spheres_publish_the_restored_resource(globe_world):
    s = globe_world.session
    s.gameplay_runtime_status_effects[900] = (
        _entry("trait_groupaura_dwarf_resource_restore_effect", (0.02, 0.0)), None,
    )
    s.gameplay_inventory = InventoryInfo()
    combat, actor = _player(s)
    combat.player_resource[actor] = [50.0, 200.0]
    globes.set_health(s, combat.player_maximum_health // 10)
    item_id = globes.place_globe(s)
    assert globe_world.server._pickup_world_item(s, item_id, automatic=True)
    assert combat.player_resource[actor][0] == pytest.approx(54.0)
    assert s.gameplay_inventory.secondary_resource == pytest.approx(54.0)
    ((data, bits),) = [
        (data, bits) for data, bits, label in s.recorder.messages
        if label == "health-sphere-resource"
    ]
    update, _actor = decode_actor_stats_update_batch(data, bits)
    assert update.resource_points == pytest.approx(54.0)
    assert update.health_points == globes.current_health(s)


def test_a_rejected_pickup_rolls_back_health_and_resource(globe_world):
    s = globe_world.session
    s.gameplay_runtime_status_effects[900] = (
        _entry("trait_groupaura_dwarf_resource_restore_effect", (0.02, 0.0)), None,
    )
    combat, actor = _player(s)
    combat.player_resource[actor] = [50.0, 200.0]
    wounded = combat.player_maximum_health // 2
    globes.set_health(s, wounded)
    item_id = globes.place_globe(s)
    with patch(
        "dso_server.services.world.encode_world_pickup_response",
        side_effect=ValueError("inventory changed"),
    ):
        assert not globe_world.server._pickup_world_item(s, item_id, automatic=True)
    assert globes.current_health(s) == wounded
    assert combat.player_resource[actor][0] == pytest.approx(50.0)
    assert item_id in s.gameplay_world_loot
    assert not any(label == "health-sphere-resource" for *_x, label in s.recorder.messages)


# --- AdditionalSkillCost / IgnoreSkillCost ---------------------------------

def test_black_essence_cost_rules():
    three = _modifiers(*_set("mage", *BLACK_ESSENCE_MAGE[:3]).modifiers)
    assert (three.essence_cost(2), three.essence_cost(1), three.essence_cost(0)) == (1, 1, 0)
    assert three.ignore_essence_cost == 0.0
    five = _modifiers(*_set("mage", *BLACK_ESSENCE_MAGE).modifiers)
    assert five.ignore_essence_cost == pytest.approx(0.15)
    assert five.essence_cost(10, roll=0.149) == 0
    assert five.essence_cost(10, roll=0.15) == 9
    assert _modifiers(parse_modifier("IgnoreSkillCost:3.0")).ignore_essence_cost == 1.0
    # Scoped/three-argument spellings are not the essence rule.
    assert _modifiers(parse_modifier("AdditionalSkillCost:-1.0,absolute,fireball")).additional_essence_cost == 0.0


def test_session_snapshot_includes_set_rules_and_status_entries():
    session = SimpleNamespace(
        gameplay_persistent_status_effects={1: _entry("event_quest_drop")},
        gameplay_timed_status_effect_expiries={},
        gameplay_runtime_status_effects={},
        gameplay_item_set_effects={},
        gameplay_socket_effects={},
        gameplay_item_set_evaluation=_set("mage", *BLACK_ESSENCE_MAGE[:3]),
    )
    bonuses = _modifiers(*snapshot_player_modifier_sources(session))
    assert bonuses.quest_drop_factor("event") == 2.0
    assert bonuses.additional_essence_cost == -1.0


class BlackEssenceCastTests(essence.CastConsumptionTests):
    """The real essence step pays the reduced/ignored cost once per cast."""

    __test__ = True

    def _wear(self, count):
        self.session.gameplay_item_set_evaluation = _set("mage", *BLACK_ESSENCE_MAGE[:count])

    def test_three_pieces_spend_one_less_essence(self):
        self._wear(3)
        self.assertTrue(self._consume(essence.ICE_MISSILE))      # cost 2 -> 1
        self.assertEqual(self._amount(essence.ACTIVE_ID), 4)
        self.assertTrue(self._consume(essence.MAGIC_MISSILE))    # cost 1 stays 1
        self.assertEqual(self._amount(essence.ACTIVE_ID), 3)

    def test_five_pieces_sometimes_spend_nothing(self):
        self._wear(5)
        with patch.object(resources_module, "random", SimpleNamespace(random=lambda: 0.1)):
            self.assertFalse(self._consume(essence.ICE_MISSILE))
        self.assertEqual(self._amount(essence.ACTIVE_ID), 5)
        self.assertEqual(self.sent, [])
        with patch.object(resources_module, "random", SimpleNamespace(random=lambda: 0.9)):
            self.assertTrue(self._consume(essence.ICE_MISSILE))
        self.assertEqual(self._amount(essence.ACTIVE_ID), 4)


for _name in tuple(vars(essence.CastConsumptionTests)):
    # Only the Black Essence cases run here; the inherited ones run in
    # tests/test_essence_consumption.py.
    if _name.startswith("test_") and _name not in vars(BlackEssenceCastTests):
        setattr(BlackEssenceCastTests, _name, None)
