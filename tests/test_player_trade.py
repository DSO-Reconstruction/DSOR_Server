"""Player trade (services/trade.py, a DSOR addition): the request, the answer,
the offers, both acceptances and an exchange made whole on both sides."""

import pytest

from dso_server.protocol.character_selection import BitReader, BitWriter
from dso_server.protocol.inventory_codec import InventoryInfo, ItemInfo
from dso_server.protocol.player_init_protocol import CurrencyChanged
from dso_server.protocol import trade_protocol as tp
from dso_server.services import trade as trade_service
from dso_server.tests import test_quests as quest_fixtures


def command(command_id, write=None):
    writer = BitWriter()
    writer.write_uint8(0x8B)
    writer.write_bytes(command_id.to_bytes(2, "little"))
    if write is None:
        writer.write_uint8(0)
    else:
        write(writer)
    return writer.finish()[0]


def last_state(s):
    for data, bits, _label in reversed(s.recorder.messages):
        if data[0] == 0x84 and int.from_bytes(data[1:3], "little") == tp.TRADE_STATE_COMMAND:
            r = BitReader(data, bits)
            r.read_bytes(3)
            phase, partner, my_gold = r.read_uint8(), r.read_string(), r.read_uint32_le()
            mine = [(r.read_uint32_le(), r.read_uint32_le()) for _ in range(r.read_uint32_le())]
            my_ready, their_gold = r.read_uint8(), r.read_uint32_le()
            theirs = [(r.read_uint32_le(), r.read_string(), r.read_uint32_le(), r.read_uint8()) for _ in range(r.read_uint32_le())]
            return dict(phase=phase, partner=partner, my_gold=my_gold, mine=mine, my_ready=my_ready,
                        their_gold=their_gold, theirs=theirs, their_ready=r.read_uint8(), message=r.read_string())
    return None


@pytest.fixture
def traders():
    case = quest_fixtures.QuestServiceTests(methodName="runTest"); case.setUp()
    sessions = []
    for port, name, gold in ((50901, "Alice", 5000), (50902, "Bob", 100)):
        s = case._session("a0200_kingscity", port, class_id=1)
        s.gameplay_character.name = name
        case.server.sessions[s.addr] = s
        s.gameplay_inventory = InventoryInfo(storage_size=21, health_points=100000)
        s.gameplay_currency_state = CurrencyChanged(0, gold, (0, 0, 0), 0, False)
        s.gameplay_next_item_id = 0x00F11000
        sessions.append(s)
    alice, bob = sessions
    # The same item id in both inventories: ids are per character.
    alice.gameplay_inventory.items.append(ItemInfo(item_id=0x00F11000, template_id="gem_ruby_a", amount=5, field_30=100))
    alice.gameplay_inventory.storage_58[0x00F11000] = 0
    bob.gameplay_inventory.items.append(ItemInfo(item_id=0x00F11000, template_id="epicmount_death_knight_horse", amount=1, field_08=4))
    bob.gameplay_inventory.storage_58[0x00F11000] = 0
    for s in sessions:
        case.server._handle_trade_command(s, command(tp.TRADE_HELLO_COMMAND, lambda w: w.write_uint8(1)))
    try:
        yield case.server, alice, bob
    finally:
        trade_service._TRADES.clear()
        trade_service._CAPABLE.clear()
        case.doCleanups()


def open_trade(server, alice, bob):
    server._handle_trade_command(alice, command(tp.TRADE_REQUEST_COMMAND, lambda w: w.write_string("bob")))
    assert last_state(alice)["phase"] == tp.PHASE_REQUESTED
    assert last_state(bob)["phase"] == tp.PHASE_INVITED and last_state(bob)["partner"] == "Alice"
    server._handle_trade_command(bob, command(tp.TRADE_ANSWER_COMMAND, lambda w: w.write_uint8(1)))
    assert last_state(alice)["phase"] == last_state(bob)["phase"] == tp.PHASE_OPEN


def offer(server, s, gold, items):
    def write(w):
        w.write_uint32_le(gold)
        w.write_uint32_le(len(items))
        for item_id, amount in items:
            w.write_uint32_le(item_id)
            w.write_uint32_le(amount)
    server._handle_trade_command(s, command(tp.TRADE_OFFER_COMMAND, write))


def ready(server, s):
    server._handle_trade_command(s, command(tp.TRADE_READY_COMMAND, lambda w: w.write_uint8(1)))


def test_both_acceptances_exchange_items_and_gold(traders):
    server, alice, bob = traders
    open_trade(server, alice, bob)
    offer(server, alice, 1000, [(0x00F11000, 2)])
    offer(server, bob, 0, [(0x00F11000, 1)])
    assert last_state(bob)["theirs"] == [(0x00F11000, "gem_ruby_a", 2, 0)]
    assert last_state(alice)["theirs"] == [(0x00F11000, "epicmount_death_knight_horse", 1, 4)]
    ready(server, alice)
    assert last_state(bob)["their_ready"] == 1
    ready(server, bob)
    assert last_state(alice)["phase"] == last_state(bob)["phase"] == tp.PHASE_DONE
    a = {i.template_id: i for i in alice.gameplay_inventory.items}
    b = {i.template_id: i for i in bob.gameplay_inventory.items}
    assert a["gem_ruby_a"].amount == 3 and a["epicmount_death_knight_horse"].amount == 1
    assert b["gem_ruby_a"].amount == 2 and "epicmount_death_knight_horse" not in b
    # Every id unique in its owner's inventory; the moved stacks re-keyed.
    for s in (alice, bob):
        ids = [int(i.item_id) for i in s.gameplay_inventory.items]
        assert len(ids) == len(set(ids))
        assert set(ids) == set(s.gameplay_inventory.storage_58)
    assert alice.gameplay_currency_state.gold == 4000
    assert bob.gameplay_currency_state.gold == 1100
    assert trade_service._TRADES == {}


def test_a_change_takes_both_acceptances_back(traders):
    server, alice, bob = traders
    open_trade(server, alice, bob)
    offer(server, alice, 10, [])
    ready(server, alice)
    offer(server, alice, 20, [])
    assert last_state(alice)["my_ready"] == 0 and last_state(bob)["their_gold"] == 20
    ready(server, bob)
    assert last_state(alice)["phase"] == tp.PHASE_OPEN
    assert alice.gameplay_currency_state.gold == 5000


def test_full_bags_refuse_the_exchange_and_change_nothing(traders):
    server, alice, bob = traders
    bob.gameplay_inventory.storage_size = 1
    open_trade(server, alice, bob)
    offer(server, alice, 0, [(0x00F11000, 1)])
    before = (alice.gameplay_inventory, bob.gameplay_inventory)
    ready(server, alice)
    ready(server, bob)
    assert last_state(bob)["message"] == "Bob's bags are full"
    assert (alice.gameplay_inventory, bob.gameplay_inventory) == before


def test_offers_refuse_what_cannot_be_given(traders):
    server, alice, bob = traders
    alice.gameplay_inventory.equipment_slots[0x00F11000] = (3,)
    open_trade(server, alice, bob)
    offer(server, alice, 999999, [(0x00F11000, 1), (0x12345, 1)])
    state = last_state(alice)
    assert state["mine"] == [] and state["my_gold"] == 5000


def test_decline_and_cancel_close_for_both(traders):
    server, alice, bob = traders
    server._handle_trade_command(alice, command(tp.TRADE_REQUEST_COMMAND, lambda w: w.write_string("Bob")))
    server._handle_trade_command(bob, command(tp.TRADE_ANSWER_COMMAND, lambda w: w.write_uint8(0)))
    assert last_state(alice)["message"] == "Bob declined the trade"
    open_trade(server, alice, bob)
    server._handle_trade_command(bob, command(tp.TRADE_CANCEL_COMMAND))
    assert last_state(alice)["phase"] == tp.PHASE_CLOSED and last_state(alice)["message"] == "Bob cancelled the trade"
    assert trade_service._TRADES == {}


def test_a_game_client_is_never_sent_trade_state(traders):
    server, alice, bob = traders
    trade_service._CAPABLE.pop(id(bob))
    server._handle_trade_command(alice, command(tp.TRADE_REQUEST_COMMAND, lambda w: w.write_string("Bob")))
    assert last_state(bob) is None
    assert "cannot trade" in last_state(alice)["message"]
