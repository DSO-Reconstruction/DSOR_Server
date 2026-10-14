# Bella Chinese: jewels and runes

Bella's fourth tab in Kingscity is **Jewels & Runes**, priced at one gold per
item. It stocks all tiers of the current `EquipmentJewel` / `EquipmentRune`
templates that fit `JewelTrinket` / `RuneTrinket` sockets, have authored effects,
and are not obsolete. Ordinary `EquipmentGem` items and legacy equipment-slot
jewels/runes are excluded.

The catalogue has 340 templates: 192 jewels and 148 runes. The ordinary shop
loader applies each item's `CharClass` restriction. Warrior, mage, ranger and
dwarf each see 153 jewels and 148 runes; niwalk sees 140 shared jewels and all
148 runes. Existing weapons and cloaks retain their offers and prices.

The native client has a fixed tab enum. Bella uses the otherwise unused-in-NPC-
shops `ItemGemCe55` category (30), with its English label changed to
`Jewels & Runes` in `C:/game-client/export_win32/language/en/gui.window.xml`.
The ordinary `ItemGem` tab remains labelled `Gems`.

Bella's X position is 54.5 (previously 55.5), with Y and Z unchanged. Both the
server and client Kingscity map databases have the same transform; the update
preserves the existing NPC row ID and GUID.

`C:/server/tools/add_debug_vendor.py` reproduces the stock, tab label and
position when reinstalling vendors. Backups and verification details for the
installed update are recorded in
`data/diagnostics/bella-jewels-runes-update.json`. Restart the server and client
to reload the map and localization assets.
