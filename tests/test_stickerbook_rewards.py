"""Collector's Bag rewards: every category pays what its count reaches, once."""

import sqlite3
from unittest.mock import Mock

import pytest

from dso_server.gameplay.player_combat_stats import calculate_player_combat_stats
from dso_server.gameplay.stickerbook_state import (
    collector_effect_definitions, collector_item_rewards, reconcile_collector_effects,
)
from dso_server.paths import DEFAULT_STATIC_DB_PATH
from dso_server.persistence.account_state import _state_from_document, _state_to_document
from dso_server.protocol.gameplay_protocol import (
    AddStickerbookFromInventoryRequest, Message, StatusEffectEntry,
    encode_add_stickerbook_from_inventory,
)
from dso_server.protocol.inventory_codec import InventoryInfo
from dso_server.protocol.player_init_protocol import (
    StickerbookEntry, decode_server_command_batch,
)
from dso_server.services.base import CharacterService
from dso_server.tests.test_rune_progression import add_item, player, saved_state  # noqa: F401 (fixture)


def _database():
    database = sqlite3.connect(f"file:{DEFAULT_STATIC_DB_PATH}?mode=ro", uri=True)
    database.row_factory = sqlite3.Row
    return database


def _live_items(subgroup=None):
    query = "SELECT Id, StickerbookId FROM _Template_StickerbookItem WHERE IsLive<>0"
    return [row["Id"] for row in _database().execute(query)
            if subgroup is None or row["StickerbookId"] == subgroup]


def _authored_rewards():
    """(key, reward, amount) of every item reward a real collection can reach."""
    database = _database()
    live = {}
    for row in database.execute(
            "SELECT StickerbookId FROM _Template_StickerbookItem WHERE IsLive<>0"):
        live[row[0]] = live.get(row[0], 0) + 1
    effects = {row[0] for row in database.execute("SELECT Id FROM _Template_StatusEffect")}
    rewards = []
    for row in database.execute("SELECT Id, StickerbookRewardItems FROM _Template_Stickerbook"):
        for encoded in str(row[1] or "").split(";"):
            fields = [field.strip() for field in encoded.split(":")]
            if len(fields) < 2 or fields[1] in effects:
                continue
            if int(fields[0]) <= live.get(row[0], 0):
                rewards.append((
                    f"{row[0]}:{fields[0]}:{fields[1]}", fields[1],
                    int(fields[2]) if len(fields) > 2 and fields[2] else 1,
                ))
    return rewards


def _own(session, *resources):
    session.gameplay_stickerbook_entries = {
        resource: StickerbookEntry(2, 0, resource) for resource in resources
    }


def _grant(server, session):
    """The item-received messages of one grant, as (amount, item id, template)."""
    session.connected = True
    session.send_reliable_ordered_split = Mock()
    CharacterService._grant_stickerbook_rewards(server, session)
    messages = []
    for call in session.send_reliable_ordered_split.call_args_list:
        commands = decode_server_command_batch(call.args[0], call.kwargs["total_bit_length"])
        messages.extend(command.value for command in commands
                        if isinstance(command.value, Message))
    assert all(message.message_type == 2 for message in messages)
    return [
        (message.arguments[1].uint32_value, message.arguments[2].uint32_value,
         message.arguments[3].string_value if len(message.arguments) > 3 else None)
        for message in messages
    ]


def _stack(session, template):
    return next((item for item in session.gameplay_inventory.items
                 if item.template_id == template), None)


def test_reward_keys_identify_each_reward_once():
    keys = [key for key, _reward, _amount in _authored_rewards()]
    assert len(keys) == 125
    assert len(set(keys)) == len(keys)


def test_every_item_reward_of_every_category_is_paid_once(player):
    server, session, _ = player
    session.gameplay_inventory.storage_size = 2000
    session.gameplay_game_currencies = {}
    _own(session, *_live_items())
    authored = {key: (reward, amount) for key, reward, amount in _authored_rewards()}
    notifications = _grant(server, session)
    assert set(session.gameplay_stickerbook_rewards) == set(authored)
    assert sum(amount for amount, _id, _name in notifications) == sum(
        amount for _reward, amount in authored.values())
    assert _grant(server, session) == []
    session.send_reliable_ordered_split.assert_not_called()


def test_a_reward_arrives_at_its_threshold(player):
    server, session, _ = player
    items = _live_items("sb_costumes_strong_women")
    _own(session, *items[:1])
    assert _grant(server, session) == []
    _own(session, *items[:2])
    (lockpicks,) = _grant(server, session)
    assert lockpicks[0] == 25
    _own(session, *items[:3])
    assert _grant(server, session) == []
    _own(session, *items[:4])
    (boxes,) = _grant(server, session)
    assert boxes[0] == 2
    assert session.gameplay_stickerbook_rewards == (
        "sb_costumes_strong_women:2:lockpick",
        "sb_costumes_strong_women:4:lootbox_gems_d",
    )


def test_the_notification_names_the_item_received(player):
    server, session, _ = player
    _own(session, *_live_items("sb_costumes_strong_women")[:2])
    (lockpicks,) = _grant(server, session)
    assert lockpicks == (25, _stack(session, "lockpick").item_id, "lockpick")


def test_a_currency_reward_is_credited_and_notified_without_an_item(player):
    server, session, _ = player
    before = session.gameplay_currency_state.currency_type
    _own(session, *_live_items("sb_emotes_showing_power"))
    notifications = _grant(server, session)
    assert session.gameplay_currency_state.currency_type == before + 5000
    assert _stack(session, "money_rc") is None
    (andermant,) = [entry for entry in notifications if entry[2] == "money_rc"]
    assert andermant[0] == 5000
    assert andermant[1] not in {item.item_id for item in session.gameplay_inventory.items}
    assert andermant[1] < session.gameplay_next_item_id


def test_status_effect_rewards_are_not_items(player):
    server, session, _ = player
    _own(session, *_live_items("sb_mounts_birds")[:5])
    _grant(server, session)
    assert session.gameplay_stickerbook_rewards == ("sb_mounts_birds:3:e_sar_chest_key",)
    assert [definition.resource_id for definition in collector_effect_definitions(
        DEFAULT_STATIC_DB_PATH, _live_items("sb_mounts_birds")[:5],
    )] == ["sb_mounts_birds_increase_ride_speed"]


def test_a_parent_category_without_items_is_never_reached():
    assert not any(reward.key.startswith("sb_mercenary:") for reward in collector_item_rewards(
        DEFAULT_STATIC_DB_PATH, _live_items()))


def test_a_reward_that_does_not_fit_waits_and_the_rest_is_paid(player):
    server, session, _ = player
    session.gameplay_inventory.storage_size = 0
    session.gameplay_inventory.extra_storage_size = 0
    _own(session, *_live_items("sb_costumes_strong_women"))
    # Lockpicks go to the ingredient bag, which has no slots to run out of.
    assert [entry[0] for entry in _grant(server, session)] == [25]
    assert session.gameplay_stickerbook_rewards == ("sb_costumes_strong_women:2:lockpick",)
    assert _stack(session, "lootbox_gems_d") is None

    session.gameplay_inventory.storage_size = 100
    assert [entry[0] for entry in _grant(server, session)] == [2]
    assert _stack(session, "lootbox_gems_d").amount == 2
    assert _stack(session, "lockpick").amount == 25
    assert len(session.gameplay_stickerbook_rewards) == 2


def test_paid_rewards_survive_a_save(player):
    server, session, _ = player
    _own(session, *_live_items("sb_costumes_strong_women")[:2])
    _grant(server, session)
    saved = saved_state(server, session)
    assert saved.stickerbook_rewards == ("sb_costumes_strong_women:2:lockpick",)
    assert _state_from_document(_state_to_document(saved)).stickerbook_rewards == (
        saved.stickerbook_rewards)


def test_unlocking_a_sticker_pays_the_threshold_it_reaches(player):
    server, session, _ = player
    resource, *rest = _live_items("sb_costumes_strong_women")
    _own(session, rest[0])
    session.connected = True
    session.send_reliable_ordered_split = Mock()
    source_items = _database().execute(
        "SELECT Items FROM _Template_StickerbookItem WHERE Id=?", (resource,),
    ).fetchone()[0].split(";")
    item_id = add_item(session, source_items[0].strip())
    request, _ = encode_add_stickerbook_from_inventory(
        AddStickerbookFromInventoryRequest(item_id))
    server._handle_add_stickerbook_from_inventory(session, request)
    assert session.gameplay_stickerbook_rewards == ("sb_costumes_strong_women:2:lockpick",)
    assert _stack(session, "lockpick").amount == 25


def _effect(resource, instance):
    row = _database().execute(
        "SELECT rowid FROM _Template_StatusEffect WHERE Id=?", (resource,),
    ).fetchone()
    return StatusEffectEntry(
        effect_id=row[0] - 1, field_04=instance, field_0c=0, field_14=25, field_08=0,
        field_18=0, field_10=0, field_1c=100, field_20=1, field_25=False, field_26=False,
        field_39=True, has_details=True,
    )


def _damage(*resources):
    return calculate_player_combat_stats(
        DEFAULT_STATIC_DB_PATH, class_name="warrior", level=50, inventory=InventoryInfo(),
        modifier_sources=tuple(_effect(resource, index + 1)
                               for index, resource in enumerate(resources)),
    ).minimum_damage


def test_the_pet_damage_bonus_needs_a_pet_and_counts_once():
    base = _damage()
    assert _damage("sb_pets_increased_damage_01") == base
    assert _damage("pet_dragon") == base
    assert _damage("pet_dragon", "sb_pets_increased_damage_01") == pytest.approx(base * 1.02)


# Domestic Animals reaches 3 with plain pets. Water and Fairy Beings reach it only
# through an upgraded pet, owned under its rarity tier: a real character's pets.
COLLECTED_PETS = (
    "pet_rabbit", "pet_dog_01", "pet_chicken_white", "pet_jindo_uncommon",
    "pet_turtle_uncommon", "e_sar_pet_kraken", "pet_frogman_epic",
    "pet_pixie_uncommon", "pet_redhair_harpy_uncommon", "pet_harpy_black_10_percent_armor",
)
HEALTH_REWARD = "sb_pets_increased_hitpoints_01"


def test_an_upgraded_pet_counts_for_its_collection():
    assert [effect.resource_id for effect in collector_effect_definitions(
        DEFAULT_STATIC_DB_PATH, COLLECTED_PETS,
    )] == [HEALTH_REWARD] * 3


def test_a_pet_counts_once_whichever_tiers_are_owned():
    assert collector_effect_definitions(
        DEFAULT_STATIC_DB_PATH, ("pet_turtle_uncommon", "pet_frogman", "pet_frogman_epic"),
    ) == ()


def test_an_upgraded_pet_counts_for_item_rewards_too():
    rows = _database().execute(
        "SELECT StickerbookId, Items, RarityUpgradeItems FROM _Template_StickerbookItem "
        "WHERE IsLive<>0 AND COALESCE(RarityUpgradeItems, '') != ''"
    ).fetchall()
    by_subgroup = {}
    for row in rows:
        by_subgroup.setdefault(row["StickerbookId"], []).append(row)
    subgroup, books = next(
        (subgroup, books) for subgroup, books in by_subgroup.items()
        if collector_item_rewards(DEFAULT_STATIC_DB_PATH, _live_items(subgroup))
    )
    tiers = [row["RarityUpgradeItems"].split(":")[0] for row in books]
    bases = [row["Items"].split(";")[0] for row in books]
    assert {reward.key for reward in collector_item_rewards(DEFAULT_STATIC_DB_PATH, tiers)} == {
        reward.key for reward in collector_item_rewards(DEFAULT_STATIC_DB_PATH, bases)}


def _health(*resources):
    return calculate_player_combat_stats(
        DEFAULT_STATIC_DB_PATH, class_name="mage", level=100, inventory=InventoryInfo(),
        modifier_sources=tuple(_effect(resource, index + 1)
                               for index, resource in enumerate(resources)),
    ).maximum_health


def test_three_health_rewards_add_up_to_nine_percent():
    frogman = "pet_jabbax"  # the effect of pet_frogman_epic
    base = _health(frogman)
    assert _health(frogman, HEALTH_REWARD) == pytest.approx(base * 1.03)
    assert _health(frogman, HEALTH_REWARD, HEALTH_REWARD, HEALTH_REWARD) == pytest.approx(base * 1.09)


def test_a_saved_character_gains_the_rewards_it_was_missing():
    def reconcile(owned, effects, instance):
        return reconcile_collector_effects(
            DEFAULT_STATIC_DB_PATH, owned, effects,
            actor_id=1, next_instance_id=instance, server_tick=0,
        )

    saved = reconcile(COLLECTED_PETS[:3], {}, 1)
    assert len(saved.effects) == 1
    entered = reconcile(COLLECTED_PETS, saved.effects, saved.next_instance_id)
    assert len(entered.effects) == 3
    assert len(entered.status_effect.entries) == 2
    assert len({entry.field_04 for entry in entered.effects.values()}) == 3
    # The client keeps each as its own instance and, with this bit clear, ignores field_18.
    assert not any(entry.field_26 for entry in entered.effects.values())


def test_collection_effects_carry_the_retail_duration():
    (speed,) = collector_effect_definitions(
        DEFAULT_STATIC_DB_PATH, _live_items("sb_mounts_birds")[:5])
    assert speed.duration_ticks == 25
