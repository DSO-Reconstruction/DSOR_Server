"""Chest contents the server-side loot lists hold but chest_contents.json lacks,
read from the boxes' own descriptions.

Usage: python tools/chest_contents_from_descriptions.py <inventory.items.json>

<inventory.items.json> is DSOR_Client's export (tools/inventory_catalog.py):
item id -> English name, description and category, from the client's loca.
A LootGenItem with no record in data/catalogs/chest_contents.json (its
loot list is server-side: "Daily limited chest" emilia_daily_andermant_cost,
lootlist_emilia_daily_andermant_cost) lists what it gives in its description:

    Open to receive one of the following:\\n   1x Winter Solstice Breeze\\n ...
    Contains:\\n • 100x Andermant\\n • 3x Gem Bag ...

Each such line becomes a tooltip entry ("ui" in chest_contents.json's
format), its candidates the item ids carrying that English name. A header
naming a choice ("one of", "random", "chance", "might") makes the lines under
it a draw without odds (protocol/chest_loot_protocol.py draws one evenly);
any other header makes them guaranteed.

Writes data/catalogs/chest_contents_descriptions.json, read by
roll_chest_contents before chest_contents.json (it holds only boxes that map
lacks or cannot resolve).
UNVERIFIED: the odds of a draw (the descriptions give none) and an amount
written as alternatives ("5/10/50x Kitchen Key"): the first is given.
"""

import json
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LINE = re.compile(r"^\s*(?:[•\-\*–]\s*)?(\d+)(?:\s*[/\-]\s*\d+)*\s*x?\s+(.+?)\s*$", re.IGNORECASE)
CHOICE = re.compile(r"one of|random|chance|might|possible", re.IGNORECASE)
MARKUP = re.compile(r"<[^>]*>")

# Lucky Spheres whose description names a category, not items ("a random pet
# from the Free Spirits category!"): the items of that category, one drawn.
# UNVERIFIED: the server-side lists may differ (event ingredients: the
# event bosses guessed; the Harpy's enchantment tiers split by sphere).
STICKERBOOKS = {
    "ls_pets_stickerbook_free_spirits_p": "sb_pets_fairy_beings",
    "ls_pets_stickerbook_domestic_creatures_p": "sb_pets_domestic_animals",
    "ls_pets_stickerbook_dragon_spawn_p": "sb_pets_dragon_spawn",
    "ls_pets_dragon_spawn_c": "sb_pets_dragon_spawn",
    "ls_pets_stickerbook_made_alive_p": "sb_pets_made_alive",
    "ls_mount_stickerbook_horses_p": "sb_mounts_horses",
    "ls_mount_stickerbook_dragons_p": "sb_mounts_dragons",
}
EVENT_BOSSES = ("fullmoon", "newmoon", "halloween", "xmas", "easter", "summer", "nye", "dragan", "sewers", "dtu", "stego")


def category_draws(database) -> dict[str, list[tuple[str, int]]]:
    draws: dict[str, list[tuple[str, int]]] = {}
    for box, book in STICKERBOOKS.items():
        rows = database.execute(
            "SELECT Items FROM _Template_StickerbookItem WHERE StickerbookId=? AND IsLive=1", (book,)
        ).fetchall()
        draws[box] = [(str(items).split(";")[0].split(":")[0], 1) for (items,) in rows if items]
    ids = lambda sql, *args: [row[0] for row in database.execute(sql, args)]
    draws["ls_dyes_c"] = [(i, 1) for i in ids("SELECT Id FROM _Template_Item WHERE ItemCategory='ColorizeItem' AND Obsolete=0")]
    for box, rarity in (("ls_ingredients_c", 1), ("ls_ingredients_m", 3), ("ls_ingredients_e", 5)):
        draws[box] = [(f"boss_ingredient_{boss}_rarity_0{rarity}", 100) for boss in EVENT_BOSSES]
    harpies = ids("SELECT Id FROM _Template_Item WHERE Id LIKE 'pet_harpy_black_%' AND Obsolete=0")
    draws["ls_pets_harpy_c"] = [(i, 1) for i in harpies if not i.endswith(("_rare", "_epic"))]
    draws["ls_pets_harpy_g"] = [(i, 1) for i in harpies if i.endswith(("_magic", "_rare", "_epic"))]
    draws["ls_equip_black_squire_c"] = [(f"skin_all_unique_{part}_dragan_black_squire", 1) for part in ("backitem", "helmet", "shoulders")]
    known = set(ids("SELECT Id FROM _Template_Item WHERE Obsolete=0"))
    return {box: [d for d in items if d[0] in known] for box, items in draws.items()}


def main(items_path: str) -> None:
    items = json.loads(Path(items_path).read_text(encoding="utf-8"))["items"]
    mapped = json.loads((ROOT / "data/catalogs/chest_contents.json").read_text(encoding="utf-8"))
    known = {record.get("container") for record in mapped["containers"]}
    database = sqlite3.connect(f"file:{(ROOT / 'data/db/static.db4').as_posix()}?mode=ro", uri=True)
    boxes = database.execute(
        "SELECT Id, ItemCategory, RequiredLevel, Loot FROM _Template_Item "
        "WHERE ItemCategory IN ('LootGenItem', 'LootGenItemAutoIdent') AND Obsolete=0"
    ).fetchall()
    by_name: dict[str, list[str]] = {}
    for item_id, item in items.items():
        name = str(item.get("name") or "").strip().casefold()
        if name:
            by_name.setdefault(name, []).append(item_id)
    containers = []
    draws = category_draws(database)
    for box_id, category, required_level, loot in boxes:
        if box_id in draws and draws[box_id]:
            entries = [{"text": items.get(i, {}).get("name") or i, "amount": n, "chance_percent": None, "mode": "chance",
                        "candidates": [i], "match": "exact", "ambiguous": False} for i, n in draws[box_id]]
            containers.append({
                "container": box_id, "name": box_id, "source": "_Template_Item.Loot", "category": category,
                "obsolete": False, "required_level": int(required_level or 1), "resolved": False, "loot_list": loot,
                "yields": [], "transitive_yields": [], "blocked_by_loot_lists": [],
                "ui": {"display_name": items.get(box_id, {}).get("name"), "style": "category", "mode": "chance",
                       "prose": "", "chance_percent": None, "rarity_hints": None, "entries": entries},
            })
            continue
        if box_id in known or box_id not in items:
            continue
        description = MARKUP.sub("", str(items[box_id].get("description") or "")).replace("\\n", "\n")
        mode = "guaranteed"
        entries = []
        for raw in description.split("\n"):
            line = raw.strip()
            if not line:
                continue
            match = LINE.match(line)
            if match is None:
                if line.endswith(":"):
                    mode = "chance" if CHOICE.search(line) else "guaranteed"
                continue
            text = match.group(2).rstrip(".")
            # "5 Essence Tears": the item is named in the singular.
            candidates = next((by_name[name] for name in (text.casefold(), re.sub(r"(?<=[^s])s$", "", text.casefold()),
                re.sub(r"es$", "", text.casefold())) if name in by_name), [])
            entries.append({
                "text": text, "amount": int(match.group(1)), "chance_percent": None,
                "mode": mode, "candidates": candidates, "match": "exact" if candidates else "none",
                "ambiguous": len(candidates) > 1,
            })
        if not entries:
            continue
        containers.append({
            "container": box_id, "name": box_id, "source": "_Template_Item.Loot",
            "category": category, "obsolete": False, "required_level": int(required_level or 1),
            "resolved": False, "loot_list": loot, "yields": [], "transitive_yields": [],
            "blocked_by_loot_lists": [],
            "ui": {"display_name": items[box_id].get("name"), "style": "bulleted", "mode": entries[0]["mode"],
                   "prose": description, "chance_percent": None, "rarity_hints": None, "entries": entries},
        })
    out = ROOT / "data/catalogs/chest_contents_descriptions.json"
    out.write_text(json.dumps({"containers": containers}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{len(containers)} containers from descriptions -> {out}")


if __name__ == "__main__":
    main(sys.argv[1])
