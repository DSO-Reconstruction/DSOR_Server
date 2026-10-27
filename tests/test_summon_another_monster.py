"""Wild Pack's summon-shaping talent and the CNY 2026 ranger set.

``ranger_talent_packofwolves_big`` (Bond of Friends,
``ranger_talent_packofwolves_no_lifetime``) authors:
``SummonMonsterId:packofwolves,minion_packofwolves_big``,
``SummonMonsterAmount:-2.0,absolute,packofwolves``,
``SummonMonsterLimit:-2.0,absolute,packofwolves`` and
``SummonAnotherMonsterId:packofwolves,minion_nian_cny2026``.
``set_cny2026_ranger`` tier 3 authors
``SummonAnotherMonsterAmount:packofwolves,1`` ("When Wild Pack is used with
the Bond of Friends talent, you additionally summon 1 Nian",
gui.statuseffects ``set_cny2026_ranger_3``). The template comes from the
talent and the count from the set: either alone adds nothing.
"""

from __future__ import annotations

import sqlite3

import pytest

from dso_server.gameplay.local_combat_state import LocalCombatState
from dso_server.gameplay.local_monster_state import load_local_monster_encounter
from dso_server.paths import DEFAULT_STATIC_DB_PATH
from dso_server.protocol.gameplay_protocol import StatusEffectEntry
from dso_server.protocol.item_set_protocol import ItemSetDatabase
from dso_server.services.base import CharacterService
from dso_server.tests import test_map_events as map_fixtures

CNY_SET = ("set_cny2026_helmet_ranger", "set_cny2026_backitem_ranger", "set_cny2026_belt_ranger")
WOLF, BIG_WOLF, NIAN = "minion_packofwolves", "minion_packofwolves_big", "minion_nian_cny2026"


def _row(table, resource_id):
    with sqlite3.connect(f"file:{DEFAULT_STATIC_DB_PATH}?mode=ro", uri=True) as db:
        return int(db.execute(
            f"SELECT rowid - 1 FROM {table} WHERE Id=?", (resource_id,)
        ).fetchone()[0])


WILD_PACK = _row("_Template_Skill", "packofwolves")
BOND_OF_FRIENDS = StatusEffectEntry(
    effect_id=_row("_Template_StatusEffect", "ranger_talent_packofwolves_big"),
    field_04=77, field_0c=0, field_14=25, field_08=0, field_18=0, field_10=0,
    field_1c=100, field_20=1, field_25=False, field_26=False, field_39=True,
    has_details=False,
)


def _set(parts):
    return ItemSetDatabase(DEFAULT_STATIC_DB_PATH).evaluate(
        set(parts), class_name="ranger"
    ).modifiers


@pytest.fixture(scope="module")
def combat():
    encounter = load_local_monster_encounter(
        DEFAULT_STATIC_DB_PATH, "a0204_boss", include_spawn_areas=False,
        active_event_flags=frozenset({"BOSS_spawn"}),
    )
    return LocalCombatState(encounter, player_actor_id=1, player_health=1000)


def test_plain_wild_pack_is_three_temporary_wolves(combat):
    definition = combat.player_summon_definition(WILD_PACK)
    assert (definition.monster_template_id, definition.amount, definition.limit) == (WOLF, 3, 3)
    assert definition.lifetime_seconds == 30.0
    # The set without the talent has no template to add.
    with_set = combat.player_summon_definition(WILD_PACK, modifier_sources=_set(CNY_SET))
    assert with_set.monster_template_id == WOLF and with_set.additional_summons == ()


def test_bond_of_friends_summons_one_lasting_big_wolf(combat):
    definition = combat.player_summon_definition(
        WILD_PACK, modifier_sources=(BOND_OF_FRIENDS,)
    )
    assert (definition.monster_template_id, definition.amount, definition.limit) == (BIG_WOLF, 1, 1)
    assert definition.lifetime_seconds == 0.0          # "does not despawn anymore"
    assert definition.additional_summons == ()          # no SummonAnotherMonsterAmount


@pytest.mark.parametrize("parts, extra", [(CNY_SET[:2], 0), (CNY_SET, 1)])
def test_cny_tier_three_adds_one_nian_only_with_the_talent(combat, parts, extra):
    definition = combat.player_summon_definition(
        WILD_PACK, modifier_sources=(BOND_OF_FRIENDS,) + _set(parts)
    )
    assert definition.monster_template_id == BIG_WOLF
    assert len(definition.additional_summons) == extra
    if extra:
        (nian,) = definition.additional_summons
        assert (nian.monster_template_id, nian.amount, nian.limit) == (NIAN, 1, 1)
        assert nian.lifetime_seconds == 0.0
        assert nian.category != definition.category
        assert nian.location_visual_effects == ()


def test_recast_replaces_the_wolf_and_the_nian_without_stacking(combat):
    definition = combat.player_summon_definition(
        WILD_PACK, modifier_sources=(BOND_OF_FRIENDS,) + _set(CNY_SET)
    )
    kwargs = dict(owner_actor_id=1, position=(0.0, 0.0, 0.0), yaw=0.0, level=60,
                  server_tick=10, player_damage_range=(10.0, 20.0))
    first = combat.spawn_player_summon(definition, now=100.0, **kwargs)
    templates = sorted(actor.definition.monster_template_id for actor in first.actors)
    assert templates == sorted((BIG_WOLF, NIAN))
    assert all(actor.expires_at is None for actor in first.actors)
    wolf, nian = sorted(first.actors, key=lambda a: a.definition.monster_template_id != BIG_WOLF)
    # SummonAvoidsOverlapping: the extra monster does not share the spot.
    assert wolf.position[:1] + wolf.position[2:] != nian.position[:1] + nian.position[2:]
    second = combat.spawn_player_summon(definition, now=101.0, **kwargs)
    assert set(second.discarded_actor_ids) == {a.actor_id for a in first.actors}
    owned = [s for s in combat.player_summons.values() if s.owner_actor_id == 1]
    assert sorted(s.definition.monster_template_id for s in owned) == sorted((BIG_WOLF, NIAN))


@pytest.fixture
def map_world():
    case = map_fixtures.MapEventServiceTests(methodName="runTest")
    case.setUp()
    try:
        yield case
    finally:
        case.doCleanups()


def test_the_resolved_cast_publishes_both_companions(map_world):
    s = map_world.session
    combat = s.gameplay_combat_state
    owner = s.gameplay_remote_actor_id
    definition = combat.player_summon_definition(
        WILD_PACK, modifier_sources=(BOND_OF_FRIENDS,) + _set(CNY_SET)
    )
    position = s.gameplay_monster_encounter.coordinate_frame.wire_to_world(
        s.gameplay_last_move.relative_position
    )
    CharacterService._resolve_player_summon(
        s, s.gameplay_chase_generation, ("player-summon", owner, 1),
        definition, owner, tuple(position), 0.0, 60, 100, (10.0, 20.0), (),
    )
    owned = {a: x.definition.monster_template_id for a, x in combat.player_summons.items()}
    assert sorted(owned.values()) == sorted((BIG_WOLF, NIAN))
    assert set(owned) <= s.gameplay_summon_actor_ids
    labels = {label for *_packet, label in s.recorder.messages}
    for actor_id in owned:
        assert any(
            label == f"player-summon-spawn-{actor_id}"
            or label.startswith(f"player-summon-{actor_id}-")
            for label in labels
        )
    # Neither companion has a lifetime, so no expiry timer was armed.
    assert not any(key[0] == "player-summon-expire" for key in s.gameplay_projectile_timers)
