"""Auto-picked wallet currencies (Materi Fragments) land in the currency bag.

Parallel World boss reward chests spawn ``materi_fragment_01`` world items.
They are ``ItemCurrency`` wallet entries, not ordinary inventory objects, so
auto pickup must consume them into the keyed ``GameCurrencyChanged`` wallet
instead of filling a storage slot.
"""

import pytest

from dso_server.paths import DEFAULT_STATIC_DB_PATH
from dso_server.protocol.commerce_protocol import (
    _new_item,
    load_purchase_item_definition,
)
from dso_server.protocol.player_init_protocol import CurrencyChanged
from dso_server.protocol.world_loot_protocol import (
    WorldLootDrop,
    decode_world_pickup_response,
)
from dso_server.tests import test_map_events as fixtures

MATERI = "materi_fragment_01"


@pytest.fixture
def world():
    case = fixtures.MapEventServiceTests(methodName="runTest")
    case.setUp()
    try:
        yield case
    finally:
        case.doCleanups()


def place_wallet_drop(session, item_id, amount):
    definition = load_purchase_item_definition(DEFAULT_STATIC_DB_PATH, MATERI)
    item = _new_item(definition, item_id, amount)
    item.ground_position = (
        session.gameplay_monster_encounter.coordinate_frame.wire_to_world(
            session.gameplay_last_move.relative_position
        )
    )
    drop = WorldLootDrop(item, definition.item_category, "All")
    session.gameplay_world_loot[int(item.item_id)] = drop
    return int(item.item_id)


def test_wallet_item_is_auto_collected_into_the_bag(world):
    s = world.session
    s.gameplay_currency_state = CurrencyChanged(0, 0, (0, 0, 0), 0, False)
    s.gameplay_game_currencies = {MATERI: 100}
    item_id = place_wallet_drop(s, item_id=0x00F30001, amount=700)

    world.server._pickup_nearby_auto_loot(
        s, s.gameplay_last_move.relative_position
    )

    assert item_id not in s.gameplay_world_loot
    assert s.gameplay_game_currencies[MATERI] == 800
    # Nothing entered ordinary inventory storage.
    inventory = getattr(s, "gameplay_inventory", None)
    templates = (
        {item.template_id for item in inventory.items}
        if inventory is not None
        else set()
    )
    assert MATERI not in templates


def test_wallet_pickup_response_carries_the_authoritative_update(world):
    s = world.session
    s.gameplay_currency_state = CurrencyChanged(0, 0, (0, 0, 0), 0, False)
    s.gameplay_game_currencies = {MATERI: 100}
    item_id = place_wallet_drop(s, item_id=0x00F30002, amount=1234)

    world.server._pickup_world_item(s, item_id, automatic=True)

    packet, bits, _label = next(
        (data, length, label)
        for data, length, label in s.recorder.messages
        if label == "world-loot-pickup-response"
    )
    response = decode_world_pickup_response(packet, bits)
    assert response.inventory_states == ()
    # The update carries the authoritative new balance (100 seeded + 1234).
    assert [
        (update.resource_id, update.amount)
        for update in response.game_currency_updates
    ] == [(MATERI, 1334)]


def test_unmapped_item_currency_falls_back_to_inventory(world):
    # Wallet entries the session wallet does not map keep their
    # pre-existing behavior: they become ordinary inventory objects.
    s = world.session
    s.gameplay_currency_state = CurrencyChanged(0, 0, (0, 0, 0), 0, False)
    item_id = place_wallet_drop(s, item_id=0x00F30003, amount=5)

    picked = world.server._pickup_world_item(s, item_id, automatic=True)

    assert picked is False or item_id not in s.gameplay_world_loot
