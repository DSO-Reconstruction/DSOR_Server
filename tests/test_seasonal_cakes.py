"""Anniversary cake milestones: server-wide crafting total and claims."""

import pytest

from dso_server.persistence.seasonal_state import SeasonalStateStore
from dso_server.protocol import seasonal_activity_protocol as proto
from dso_server.protocol.character_selection import BitWriter
from dso_server.protocol.currency_protocol import GAME_CURRENCY_CHANGED_COMMAND
from dso_server.protocol.inventory_economy_protocol import CRAFTING_COMMAND
from dso_server.services.seasonal_activities import CAKE_CLAIM_KEY
from dso_server.tests.test_seasonal_protocol import seasonal  # noqa: F401


RECIPE = "anniversary_cook_cake_01"
EVENT = "anniversary_event"


def craft_packet(item_ids, amount=1):
    writer = BitWriter()
    writer.write_bytes(b"\x8b" + CRAFTING_COMMAND.to_bytes(2, "little"))
    writer.write_uint8(0)
    writer.write_uint32_le(len(item_ids))
    for item_id in item_ids:
        writer.write_uint32_le(item_id)
    writer.write_uint8(0)      # gold
    writer.write_uint8(0xFF)
    writer.write_uint32_le(amount)
    writer.write_string(RECIPE)
    for value in (2, 0, 0):
        writer.write_uint32_le(value)
    return writer.finish()[0]


def cake_ingredients(world, crafts=1):
    return (
        world.add_item("e_anniversary_cake_ingredientltem_flour", 3 * crafts),
        world.add_item("e_anniversary_cake_ingredientltem_egg", 2 * crafts),
        world.add_item("e_anniversary_cake_ingredientltem_cream", 1 * crafts),
    )


def store(world):
    return world.server._seasonal_store()


def info(world):
    world.dispatch(proto.encode_seasonal_info_request(proto.GET_CRAFTING_CAKES_INFO_COMMAND))
    return world.commands(proto.GET_CRAFTING_CAKES_INFO_RESULT_COMMAND)[-1].value


def test_crafted_cakes_count_toward_the_server_total_only_during_the_event(seasonal):
    world = seasonal
    world.active_events = {EVENT}
    world.server._handle_crafting(world.session, craft_packet(cake_ingredients(world, 2), 2))
    assert world.count("e_anniversary_cake") == 2
    assert store(world).crafting_cakes_total(RECIPE) == 2
    assert info(world) == proto.CraftingCakesInfoResult(2, ())

    world.active_events = set()
    world.server._handle_crafting(world.session, craft_packet(cake_ingredients(world)))
    assert world.count("e_anniversary_cake") == 3
    assert store(world).crafting_cakes_total(RECIPE) == 2

    # Other recipes never count, and the total survives a store reload.
    world.server._seasonal_record_crafting(world.session, "some_other_recipe", 5)
    assert SeasonalStateStore(store(world).path).crafting_cakes_total(RECIPE) == 2


def test_milestone_claim_is_idempotent_and_persisted(seasonal):
    world = seasonal
    world.active_events = {EVENT}
    store(world).add_crafting_cakes(RECIPE, 799)
    cot_before = world.session.gameplay_game_currencies.get("e_anniversary_cot", 0)

    world.dispatch(proto.encode_crafting_cakes_award("1"))
    # anniversary_cake_reward_01: 100 Crystals of Truth + 5 arena keys.
    assert world.session.gameplay_game_currencies["e_anniversary_cot"] == cot_before + 100
    assert world.count("e_anniversary_arena_key_01") == 5
    assert world.commands(GAME_CURRENCY_CHANGED_COMMAND)
    assert world.commands(proto.GET_CRAFTING_CAKES_INFO_RESULT_COMMAND)[-1].value == (
        proto.CraftingCakesInfoResult(799, ("1",))
    )
    key = CAKE_CLAIM_KEY.format(recipe=RECIPE, milestone="1")
    assert world.saved().timed_offer_purchases[key] == 1

    # Replay: refused, and the client is refreshed with the claimed state.
    world.dispatch(proto.encode_crafting_cakes_award("1"))
    assert world.count("e_anniversary_arena_key_01") == 5
    assert world.session.gameplay_game_currencies["e_anniversary_cot"] == cot_before + 100
    assert world.commands(proto.GET_CRAFTING_CAKES_INFO_RESULT_COMMAND)[-1].value.claimed == ("1",)

    # Milestone 2 needs 800.
    world.dispatch(proto.encode_crafting_cakes_award("2"))
    assert world.count("e_anniversary_random_travelbox") == 0
    store(world).add_crafting_cakes(RECIPE, 1)
    world.dispatch(proto.encode_crafting_cakes_award("2"))
    assert world.count("e_anniversary_random_travelbox") == 3

    world.relog()
    assert info(world) == proto.CraftingCakesInfoResult(800, ("1", "2"))


@pytest.mark.parametrize("case", ["unknown", "inactive", "below"])
def test_invalid_milestone_claims_change_nothing(seasonal, case):
    world = seasonal
    world.active_events = {EVENT}
    store(world).add_crafting_cakes(RECIPE, 4999 if case == "below" else 5000)
    milestone = "5"
    if case == "unknown":
        milestone = "6"
    elif case == "inactive":
        world.active_events = set()
    before = (dict(world.session.gameplay_timed_offer_purchases),
              dict(world.session.gameplay_game_currencies), world.count("e_anniversary_boss_portal_key"))
    world.dispatch(proto.encode_crafting_cakes_award(milestone))
    assert (dict(world.session.gameplay_timed_offer_purchases),
            dict(world.session.gameplay_game_currencies),
            world.count("e_anniversary_boss_portal_key")) == before
