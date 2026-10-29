"""Vendor prices, exact item repurchases, and account-owned premium currency."""
import copy
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from dso_server.paths import DEFAULT_STATIC_DB_PATH
from dso_server.protocol.character_selection import BitReader
from dso_server.protocol.shop_protocol import (
    load_shop_offers, encode_offer_command, decode_offer_command, OfferCommand,
)
from dso_server.protocol.commerce_protocol import (
    debit_currency, PurchasePrice, Transaction, TransactionEntry,
)
from dso_server.protocol.inventory_codec import InventoryInfo, ItemInfo, ItemEnchantmentInfo
from dso_server.protocol.inventory_economy_protocol import credit_fixed_currency
from dso_server.protocol.player_init_protocol import CurrencyChanged, QuickbarInfo
from dso_server.persistence.account_state import AccountState, AccountStateStore, CharacterProfile
from dso_server.persistence.account_registry import AccountRegistry
from dso_server.runtime.transport import Session
from dso_server.services import commerce
from dso_server.tests import test_quests as quest_fixtures
from dso_server.tests.test_pvp_matchmaking import _FakeSocket


@pytest.mark.parametrize("shop,field,slot", [
    ("arena_merchant_hub_all", "price_arena", 2),
    ("monger_gnob", "price_ec", 4),
])
def test_native_price_slots(shop, field, slot):
    value = load_shop_offers(DEFAULT_STATIC_DB_PATH, shop, player_level=100, class_id=1)
    offer = next(o for o in value.offers if getattr(o, field))
    value = replace(value, offers=(offer,))
    packet, bits = encode_offer_command(value)
    reader = BitReader(packet, bits)
    reader.read_bytes(3); reader.read_string(); reader.read_bool(); reader.read_bool()
    assert reader.read_uint32_le() == 1
    assert reader.read_bool()
    reader.read_string()
    prices = [reader.read_uint32_le() for _ in range(7)]
    assert prices[slot] == getattr(offer, field)
    assert decode_offer_command(packet, bits)[0] == value


@pytest.fixture
def vendor():
    case = quest_fixtures.QuestServiceTests(methodName="runTest"); case.setUp()
    s = case._session("a0200_kingscity", 50791, class_id=1)
    case.server.sessions[s.addr] = s
    s.gameplay_inventory = InventoryInfo(storage_size=21, health_points=100000)
    s.gameplay_currency_state = CurrencyChanged(1000, 10000, (0,0,0), 0, False)
    s.gameplay_open_shop_id = "smith_hub_a0200"
    s.gameplay_open_shop_offers = OfferCommand(s.gameplay_open_shop_id, False, True, (), (), ())
    s.gameplay_next_item_id = 999
    s.gameplay_quickbar_info = QuickbarInfo(0, 0)
    item = ItemInfo(item_id=800, template_id="gem_ruby_a", amount=3, field_30=60)
    s.gameplay_inventory.items.append(item); s.gameplay_inventory.storage_58[800] = 0
    try: yield case, s, item
    finally: case.doCleanups()


def trade(case, s, operation, item_id=800):
    request = Transaction((TransactionEntry("", item_id, 0, operation),),
                          s.gameplay_open_shop_id, False)
    with patch.object(commerce, "decode_transaction", return_value=request):
        case.server._handle_transaction(s, b"")


@pytest.mark.parametrize("balance,offer,amount,expected", [
    (5000, "quickbar_1", 1, 2),
    (5000, "quickbar_1", 3, 2),
    (2399, "quickbar_1", 1, 1),
    (5000, "quickbar_2", 1, 1),
    (5000, "quickbar_1", 2, 1),
])
def test_direct_quickbar_purchase_validates_price_and_next_tier(vendor, balance, offer, amount, expected):
    case, s, item = vendor
    s.gameplay_quickbar_info = QuickbarInfo(1, 0)
    s.gameplay_currency_state = replace(s.gameplay_currency_state, currency_type=balance)
    request = Transaction((TransactionEntry(offer, 0, 0, amount),), "", False)
    with patch.object(commerce, "decode_transaction", return_value=request):
        case.server._handle_transaction(s, b"")
        assert s.gameplay_quickbar_info.quickbar_amount == expected
        assert s.gameplay_currency_state.andermant == balance - (2400 if expected == 2 else 0)
        if expected == 2:
            case.server._handle_transaction(s, b"")
            assert s.gameplay_currency_state.andermant == balance - 2400
            assert s.gameplay_quickbar_info.quickbar_amount == 2
    assert s.gameplay_inventory.items == [item]


def test_sell_repurchases_exact_item_and_cannot_replay(vendor):
    case,s,item = vendor
    original = copy.deepcopy(item); before = s.gameplay_currency_state.gold
    trade(case,s,4)
    assert not s.gameplay_inventory.items
    assert s.gameplay_vendor_buyback[0][0] == original
    offered = replace(s.gameplay_open_shop_offers, sold_items=(original,))
    packet,bits = encode_offer_command(offered)
    assert decode_offer_command(packet,bits)[0].sold_items == (original,)
    trade(case,s,5)
    assert s.gameplay_inventory.items == [original]
    assert not s.gameplay_vendor_buyback
    assert s.gameplay_currency_state.gold == before
    trade(case,s,5)
    assert s.gameplay_inventory.items == [original]
    assert s.gameplay_currency_state.gold == before


@pytest.mark.parametrize("failure", ["full", "poor", "wrongshop", "unknown", "equipped"])
def test_failed_item_transaction_leaves_state_intact(vendor, failure):
    case,s,item = vendor
    if failure == "equipped":
        s.gameplay_inventory.equipment_slots[item.item_id] = (1,)
        operation = 4
    else:
        trade(case,s,4); operation = 5
        if failure == "full":
            s.gameplay_inventory.storage_size = 0
            s.gameplay_inventory.extra_storage_size = 0
        if failure == "poor":
            s.gameplay_currency_state = replace(s.gameplay_currency_state, amount=0)
        if failure == "wrongshop":
            s.gameplay_open_shop_offers = replace(s.gameplay_open_shop_offers, player_can_sell=False)
        if failure == "unknown": operation = 99
    case.server._sync_inventory_live_health(s)
    before = copy.deepcopy((s.gameplay_inventory,s.gameplay_currency_state,s.gameplay_vendor_buyback))
    trade(case,s,operation)
    assert (s.gameplay_inventory,s.gameplay_currency_state,s.gameplay_vendor_buyback) == before


def state(cid, premium):
    return AccountState(cid, "a0200_kingscity", None, InventoryInfo(),
        CurrencyChanged(premium,1000,(10,20,30),40,False), 0, 1000,
        QuickbarInfo(0,0), {}, (), None)


@pytest.fixture
def wallets(tmp_path):
    registry = AccountRegistry(tmp_path / "accounts.sqlite3")
    store = AccountStateStore(tmp_path / "players.sqlite3")
    registry.register("alice", "Correct-Horse-9"); registry.register("bob", "Correct-Horse-9")
    for cid,who,balance in ((101,"alice",1000),(102,"alice",2000),(103,"bob",300)):
        registry.claim_character(who,cid)
        store.put(state(cid,balance))
    store.account_registry = registry
    try: yield store,registry
    finally: store.close(); registry.close()


def test_wallet_migrates_highest_and_survives_stale_saves_and_reopen(wallets):
    store,registry = wallets
    first=store.get(101); stale=store.get(102)
    assert first.currency.andermant == stale.currency.andermant == 2000
    first.currency,_=debit_currency(first.currency,PurchasePrice(rc=100))
    store.put(first,flush=True)
    store.put(stale,flush=True)
    assert store.get(101).currency.andermant == store.get(102).currency.andermant == 1900
    assert store.get(103).currency.andermant == 300
    store.close()
    again=AccountStateStore(store.path); again.account_registry=registry
    try: assert again.get(102).currency.andermant == 1900
    finally: again.close()


def test_live_characters_share_credits_and_concurrent_debits_cannot_overdraw(wallets):
    store,registry=wallets
    sessions=[]
    for cid in (101,102):
        s=Session(("127.0.0.1",cid),_FakeSocket())
        s.gameplay_character=SimpleNamespace(character_id=cid)
        s.gameplay_currency_store=store
        s.gameplay_currency_state=store.get(cid).currency
        sessions.append(s)
    a,b=sessions
    a.gameplay_currency_state=credit_fixed_currency(a.gameplay_currency_state,andermant=200)
    assert b.gameplay_currency_state.andermant == 2200
    ac,_=debit_currency(a.gameplay_currency_state,PurchasePrice(rc=2000))
    bc,_=debit_currency(b.gameplay_currency_state,PurchasePrice(rc=2000))
    a.gameplay_currency_state=ac
    with pytest.raises(ValueError): b.gameplay_currency_state=bc
    assert b.gameplay_currency_state.andermant == 200
    assert b.gameplay_currency_state.gold == 1000


def test_repurchase_records_survive_persistence(wallets):
    store,_=wallets
    saved=store.get(101)
    item=ItemInfo(item_id=845,template_id="gem_ruby_a",amount=7)
    saved.vendor_buyback=((item,123),)
    store.put(saved,flush=True)
    again=AccountStateStore(store.path)
    try: assert again.get(101).vendor_buyback == ((item,123),)
    finally: again.close()


def test_new_character_does_not_add_another_starting_grant(wallets):
    store,registry=wallets
    assert store.get(101).currency.andermant == 2000
    registry.claim_character("alice",104)
    profile=CharacterProfile(104,"FreshAlt","a0002_start_hub",1,0,0,0,0,0)
    store.put_profile(profile)
    assert store.get_profile(104).premium_currency == 2000
    store.put(state(104,9_999_999),flush=True)
    assert store.get(104).currency.andermant == 2000
    assert store.get(101).currency.andermant == 2000


def test_shared_wallet_keeps_other_currencies_character_owned(wallets):
    store,_=wallets
    a=store.get(101); b=store.get(102)
    a.currency=credit_fixed_currency(a.currency,andermant=100,gold=500,draken=70,badge=80)
    store.put(a,flush=True)
    result=store.get(102).currency
    assert result.andermant == 2100
    assert (result.gold,result.draken,result.badge_of_honor)==(b.currency.gold,b.currency.draken,b.currency.badge_of_honor)


def test_new_player_bootstrap_reads_account_balance_for_an_unplayed_alt(wallets):
    from dso_server.services.player_init_state import PlayerInitializationStateService, PlayerInitializationState
    store,registry=wallets
    assert store.get(101).currency.andermant == 2000
    registry.claim_character("alice",104)
    profile=CharacterProfile(104,"FreshAlt","a0200_kingscity",1,0,0,0,0,0)
    store.put_profile(profile)
    bootstrap=PlayerInitializationState(profile,1,profile.map_id,None,None,
        (0,0,0),None,(),(),(0,0,0,0),{},(0,0,0,0),inventory=InventoryInfo())
    service=SimpleNamespace(account_store=store)
    PlayerInitializationStateService._encode_initialization_new_player(service,bootstrap)
    assert bootstrap.new_player.fields_170[6] == 2000


def test_repurchase_preserves_equipment_upgrades_sockets_and_enchantments(vendor):
    case,s,item=vendor
    item.template_id="warrior_base_helmet_crafted"
    item.amount=1; item.socket_count=1; item.socketed_templates=["gem_ruby_a"]
    item.item_level=100; item.field_20=7
    item.enchantments=[ItemEnchantmentInfo(value=0.5,template_id="test_roll")]
    original=copy.deepcopy(item)
    trade(case,s,4); trade(case,s,5)
    assert s.gameplay_inventory.items == [original]
