# Vendors and account wallets

## NPC and station availability

`_Instance_EventSets.EventFlags` lists alternative positive influences. Grima
is enabled by any of her listed events; the old conjunction required all of
them at once. Negative flags still disable the affected content. Initial NPC
loading and map-runtime transitions use the same rule.

The server mirrors authored `ClientSignal` rules for map entry and level up,
using server-known map, level and quest facts. This activates Matrona at level
12 and the corresponding workbenches. Other client-only signals remain outside
the automatic evaluator. Shop requests resolve the same current map-instance
NPC definitions and actor aliases as NPC dialogues.

Every active `OpenCraftingWindow*`/`OpenFilterCraftingWindow:*` scene object is
registered with `PropInfo`, including the cooking pot whose name does not start
with `workbench_`. Crafting stations remain usable after repeated interaction.

## Currency prices

Native `Offer` serializer `0x140a04e14` writes seven price fields beginning at
`+0x58`. `ShopWidgetGameOffer::ConfigureUI` (`0x140517d94`) reads Honor from
`+0x60` and Draken from `+0x68`, i.e. wire price slots 2 and 4. These were sent
in database-column order instead, placing both prices in slots the UI does not
render. The codec now maps those fields explicitly; server purchase pricing
continues to use named database currency domains.

## Repurchase

`OfferCommand +0xc8` is the sold-item `Array<ItemInfo>` consumed by native
`ClientTradingManager::HandleOfferCommand` (`0x14027b128`). The server retains
the 20 most recently sold stacks per character, along with the sale value.
Items retain their identity, enchantments, sockets, upgrades and stack amount.
The history survives map transfers and relogs in the character document.

Empty-offer transactions distinguish operation 4 (sell) from operation 5
(repurchase), as native `RestoreItem` (`0x14027f0bc`) does. A successful trade
refreshes the repurchase list with `field_60=True`. Repurchase restores the exact
item at its recorded sale value; missing items, full inventory, insufficient
gold and shops that prohibit selling are rejected without a partial trade.

## Account-owned Andermant

`account_wallets` in the player SQLite database stores one premium balance per
registry account. Existing accounts migrate once to the **highest character
balance**, taking durable gameplay state in preference to stale roster metadata.
New characters read that account balance and do not add another starting grant.
Gold, Honor, Draken, Glyphs and keyed currencies remain character-owned.

Session currency reads overlay the account balance. Currency mutations retain
the balance they read as local, non-wire metadata and commit a delta under the
store lock. Concurrent debits cannot overdraw the wallet, and stale autosaves
cannot reset it. The asynchronous player writer commits wallet and character
snapshots together. Offline and live CMS grants use the same wallet; sequential
bookings re-read the committed balance, preserving receipt-based retries.

The roster and NewPlayer bootstrap also use the account wallet. Characters
without registry ownership retain their standalone development behavior.

## Verification

`tests/test_vendor_repairs.py` covers native currency offsets, exact repurchases,
failed/repeated transactions, persistence, account isolation, stale saves,
concurrent debits and new-character grants. `tests/test_event_waves_and_vendors.py`
covers all five anniversary waves, reward activation and vendor/station gates.
CMS tests cover multiple offline grants and live account-shared grants.
These are server/protocol tests; the interactive client still needs a smoke test.
