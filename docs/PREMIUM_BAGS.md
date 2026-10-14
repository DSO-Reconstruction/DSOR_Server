# Premium bags

Active, unexpired Premium (1) and Deluxe (2) unlock the ninth inventory bag
and eighth bank bag. Each matches its normal counterpart: 28 inventory slots
(four rows of seven) and 40 bank slots (eight rows of five). These dimensions
match the local client inventory/bank layouts and the extension row sizes in
`_Globals`. The normal purchased capacities and upgrade levels stay intact.

The existing extra containers carry these bags: `storage_80` /
`extra_storage_size` for inventory and `storage_d0` /
`extra_bank_storage_size` for the bank. Moves and splits use their existing
protocol operations. Purchases and loot fill ordinary inventory slots first,
then premium slots; purchases also stack into the premium inventory bag.

On expiry, items move into free ordinary slots in the same container. Any
remaining whole stacks enter `premium_inventory_overflow` or
`premium_storage_overflow`. These server-only lists retain the complete item
record, including quantity, enchantments, sockets, and identity, but stay out
of usable item lists, allocations, and client packets. Item actions cannot
consume or move these records by a stale item ID.

After inventory changes, pending items return to free slots in order. A
renewed membership also allows recovery into its extra slots. Recovery
registers only previously hidden items with the client and then publishes
complete allocations, avoiding duplicate registration of existing items.

Overflow is saved alongside inventory in the existing account document and
survives restarts and map changes. Map transfers rekey hidden items together
with active items to prevent ID collisions. Older saves default to empty
overflow lists. Login reconciles membership before the initial inventory is
sent; a cancellable session deadline handles expiry while idle, and client
requests check expiry before acting. Long deadlines recheck daily to stay
within Windows timer limits.

Verification: `py -3.12 -m pytest tests/test_premium_bags.py -q`.
