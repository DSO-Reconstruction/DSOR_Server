"""Durable CMS-managed NPC shop categories, offers, and price overrides."""

from __future__ import annotations

from contextlib import closing
import sqlite3
import threading
import uuid
from pathlib import Path


PRICE_COLUMNS = {
    "price_rc": "PriceRC",
    "price_vc": "PriceVC",
    "price_gc": "PriceGC",
    "price_ec": "PriceEC",
    "price_uc": "PriceUC",
    "price_arena": "PriceArena",
    "price_ic": "PriceIC",
}
_LOCK = threading.RLock()


class NpcShopOverrideStore:
    def __init__(self, static_database_path):
        self.static_path = Path(static_database_path).resolve()
        self.path = self.static_path.parent / "npc_shop_overrides.sqlite3"
        self._initialize()

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def _static(self):
        if not self.static_path.is_file():
            raise FileNotFoundError(f"static database not found: {self.static_path}")
        db = sqlite3.connect(
            self.static_path.as_uri() + "?mode=ro", uri=True
        )
        # _shop_row and the CMS catalog readers address static columns by
        # name. Keep the read-only connection consistent with _connect().
        db.row_factory = sqlite3.Row
        return db

    def _initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with _LOCK, closing(self._connect()) as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS npc_shop_categories (
                    shop_id TEXT NOT NULL,
                    category_id INTEGER NOT NULL,
                    category_name TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    created_at REAL NOT NULL DEFAULT (unixepoch()),
                    PRIMARY KEY(shop_id, category_id)
                );
                CREATE TABLE IF NOT EXISTS npc_shop_offers (
                    offer_id TEXT PRIMARY KEY,
                    shop_id TEXT NOT NULL,
                    category_id INTEGER NOT NULL,
                    category_name TEXT NOT NULL,
                    item_id TEXT NOT NULL,
                    amount INTEGER NOT NULL,
                    price_rc INTEGER NOT NULL DEFAULT 0,
                    price_vc INTEGER NOT NULL DEFAULT 0,
                    price_gc INTEGER NOT NULL DEFAULT 0,
                    price_ec INTEGER NOT NULL DEFAULT 0,
                    price_uc INTEGER NOT NULL DEFAULT 0,
                    price_arena INTEGER NOT NULL DEFAULT 0,
                    price_ic INTEGER NOT NULL DEFAULT 0,
                    actor TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    created_at REAL NOT NULL DEFAULT (unixepoch())
                );
                CREATE INDEX IF NOT EXISTS npc_shop_offers_by_shop
                    ON npc_shop_offers(shop_id);
                CREATE TABLE IF NOT EXISTS npc_shop_price_overrides (
                    shop_id TEXT NOT NULL,
                    offer_id TEXT NOT NULL,
                    price_rc INTEGER NOT NULL,
                    price_vc INTEGER NOT NULL,
                    price_gc INTEGER NOT NULL,
                    price_ec INTEGER NOT NULL,
                    price_uc INTEGER NOT NULL,
                    price_arena INTEGER NOT NULL,
                    price_ic INTEGER NOT NULL,
                    actor TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    updated_at REAL NOT NULL DEFAULT (unixepoch()),
                    PRIMARY KEY(shop_id, offer_id)
                );
                """
            )

    def _shop_row(self, db, shop_id):
        row = db.execute(
            "SELECT * FROM _Template_Shop WHERE Id=?", (shop_id,)
        ).fetchone()
        if row is None:
            raise ValueError("NPC shop was not found")
        npcs = db.execute(
            "SELECT Id,Profession,ShopLocaId FROM _Template_NPC "
            "WHERE ShopId=? ORDER BY Id", (shop_id,)
        ).fetchall()
        if not npcs:
            raise ValueError("shop is not assigned to an NPC")
        return row, npcs

    @staticmethod
    def _offer_ids(shop_row):
        return tuple(
            value.strip() for value in str(shop_row["Offers"] or "").split(";")
            if value.strip()
        )

    def list_shops(self, search=""):
        search = str(search or "").strip().casefold()
        with closing(self._static()) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute(
                "SELECT s.Id,s.OfferCategory,s.Offers,s.PlayerCanSell,n.Id AS npc_id,"
                "n.Profession,n.ShopLocaId FROM _Template_Shop s "
                "JOIN _Template_NPC n ON n.ShopId=s.Id ORDER BY s.Id,n.Id"
            ).fetchall()
        grouped = {}
        for row in rows:
            shop_id = str(row["Id"])
            entry = grouped.setdefault(shop_id, {
                "shop_id": shop_id,
                "offer_count": len(self._offer_ids(row)),
                "offer_categories": str(row["OfferCategory"] or ""),
                "player_can_sell": bool(row["PlayerCanSell"]),
                "npcs": [],
            })
            entry["npcs"].append({
                "npc_id": str(row["npc_id"]),
                "profession": str(row["Profession"] or ""),
                "shop_loca_id": str(row["ShopLocaId"] or ""),
            })
        with closing(self._connect()) as overlay:
            extra_categories = {}
            for row in overlay.execute(
                "SELECT shop_id,category_name FROM npc_shop_categories"
            ):
                extra_categories.setdefault(str(row["shop_id"]), set()).add(
                    str(row["category_name"])
                )
            custom_counts = {
                str(row["shop_id"]): int(row["amount"])
                for row in overlay.execute(
                    "SELECT shop_id,COUNT(*) AS amount FROM npc_shop_offers GROUP BY shop_id"
                )
            }
            for shop_id, entry in grouped.items():
                entry["offer_count"] += custom_counts.get(shop_id, 0)
                category_names = {
                    value.strip() for value in entry["offer_categories"].split(";")
                    if value.strip()
                }
                category_names.update(extra_categories.get(shop_id, set()))
                if shop_id == "jewelry_hub_all":
                    category_names.add("ItemGemCe55")
                entry["offer_categories"] = ", ".join(sorted(category_names))
        return [row for row in grouped.values() if not search or any(
            search in row["shop_id"].casefold()
            or search in npc["npc_id"].casefold()
            or search in npc["profession"].casefold()
            for npc in row["npcs"]
        )]

    def detail(self, shop_id, *, search="", offset=0, limit=50, category_ids=None):
        from dso_server.protocol.shop_protocol import OFFER_CATEGORY_IDS
        from dso_server.services.admin_items import _display_name, item_language_directory, locale_titles

        category_ids = category_ids or OFFER_CATEGORY_IDS
        category_names = {}
        for name, value in category_ids.items():
            category_names.setdefault(value, name)
        with closing(self._static()) as static, closing(self._connect()) as overlay:
            static.row_factory = sqlite3.Row
            shop, npcs = self._shop_row(static, shop_id)
            titles = locale_titles(item_language_directory())
            ids = self._offer_ids(shop)
            base_rows = []
            if ids:
                marks = ",".join("?" for _ in ids)
                base_rows = static.execute(
                    "SELECT o.*,i.Name AS ItemName,i.LocaleIdTitle AS ItemLocaleTitle "
                    "FROM _Template_Offer o LEFT JOIN _Template_Item i ON i.Id=o.Item "
                    f"WHERE o.Id IN ({marks}) ORDER BY o.Id", ids
                ).fetchall()
            category_rows = overlay.execute(
                "SELECT category_id,category_name,actor,reason,created_at "
                "FROM npc_shop_categories WHERE shop_id=? ORDER BY category_id",
                (shop_id,),
            ).fetchall()
            custom_rows = overlay.execute(
                "SELECT * FROM npc_shop_offers WHERE shop_id=? ORDER BY offer_id",
                (shop_id,),
            ).fetchall()
            overrides = {
                str(row["offer_id"]): dict(row)
                for row in overlay.execute(
                    "SELECT * FROM npc_shop_price_overrides WHERE shop_id=?",
                    (shop_id,),
                )
            }
            base_used = set()
            offers = []
            for row in base_rows:
                offer_id = str(row["Id"])
                category_name = str(row["OfferCategory"] or "Misc")
                category_id = category_ids.get(category_name, category_ids["Misc"])
                base_used.add(category_id)
                prices = {
                    key: int(row[column] or 0)
                    for key, column in PRICE_COLUMNS.items()
                }
                override = overrides.get(offer_id)
                if override:
                    prices = {key: int(override[key]) for key in PRICE_COLUMNS}
                offers.append({
                    "offer_id": offer_id,
                    "item_id": str(row["Item"] or ""),
                    "item_name": _display_name(
                        titles, row["Item"], row["ItemName"], row["ItemLocaleTitle"]
                    ),
                    "category_id": category_id,
                    "category_name": category_name,
                    "amount": int(row["Amount"] or 1),
                    "prices": prices,
                    "custom": False,
                    "price_overridden": bool(override),
                })
            for row in custom_rows:
                category_id = int(row["category_id"])
                category_name = str(row["category_name"])
                item = static.execute(
                    "SELECT Name,LocaleIdTitle FROM _Template_Item WHERE Id=?",
                    (row["item_id"],),
                ).fetchone()
                offers.append({
                    "offer_id": str(row["offer_id"]),
                    "item_id": str(row["item_id"]),
                    "item_name": _display_name(
                        titles, row["item_id"],
                        item["Name"] if item else row["item_id"],
                        item["LocaleIdTitle"] if item else "",
                    ),
                    "category_id": category_id,
                    "category_name": category_name,
                    "amount": int(row["amount"]),
                    "prices": {key: int(row[key]) for key in PRICE_COLUMNS},
                    "custom": True,
                    "price_overridden": True,
                })
            if shop_id == "jewelry_hub_all":
                special_category = category_ids.get("ItemGemCe55", -1)
                base_used.add(special_category)
                from dso_server.protocol.shop_protocol import _max_tier_trinket_gems
                from dso_server.protocol.character_selection import CLASS_NAMES
                dynamic_ids = {offer["offer_id"] for offer in offers}
                max_tier_items = {}
                for class_name in CLASS_NAMES.values():
                    for item in _max_tier_trinket_gems(static, str(class_name).casefold()):
                        max_tier_items[str(item["Id"])] = item
                for template_id, item in max_tier_items.items():
                    offer_id = f"jewelry_max_tier_{template_id}"
                    if offer_id in dynamic_ids:
                        continue
                    prices = {key: 0 for key in PRICE_COLUMNS}
                    prices["price_vc"] = 1
                    if offer_id in overrides:
                        prices = {key: int(overrides[offer_id][key]) for key in PRICE_COLUMNS}
                    category_name = "ItemGemCe55"
                    offers.append({
                        "offer_id": offer_id, "item_id": template_id,
                        "item_name": _display_name(titles, template_id, item["Name"], item["LocaleIdTitle"]),
                        "category_id": special_category, "category_name": category_name,
                        "amount": 1, "prices": prices, "custom": False,
                        "price_overridden": offer_id in overrides,
                    })
                trinket_items = static.execute(
                    "SELECT Id,Name,LocaleIdTitle FROM _Template_Item "
                    "WHERE ItemCategory IN ('RuneTrinket','JewelTrinket') "
                    "AND Obsolete=0 ORDER BY Id"
                ).fetchall()
                for item in trinket_items:
                    template_id = str(item["Id"])
                    offer_id = f"jewelry_max_sockets_{template_id}"
                    if offer_id in dynamic_ids:
                        continue
                    prices = {key: 0 for key in PRICE_COLUMNS}
                    prices["price_vc"] = 1
                    if offer_id in overrides:
                        prices = {key: int(overrides[offer_id][key]) for key in PRICE_COLUMNS}
                    offers.append({
                        "offer_id": offer_id, "item_id": template_id,
                        "item_name": _display_name(titles, template_id, item["Name"], item["LocaleIdTitle"]),
                        "category_id": special_category, "category_name": "ItemGemCe55",
                        "amount": 1, "prices": prices, "custom": False,
                        "price_overridden": offer_id in overrides,
                    })
            categories = [
                {"category_id": cid, "category_name": category_names.get(cid, "Misc"),
                 "created": False}
                for cid in sorted(base_used) if cid >= 0
            ]
            categories.extend({
                "category_id": int(row["category_id"]),
                "category_name": str(row["category_name"]),
                "created": True,
            } for row in category_rows if int(row["category_id"]) not in base_used)
            used_category_ids = {item["category_id"] for item in categories}
            available, seen_category_ids = [], set()
            for name, value in category_ids.items():
                if name == "Key" or value in seen_category_ids:
                    continue
                seen_category_ids.add(value)
                if value not in used_category_ids:
                    available.append({"category_id": value, "category_name": name})
            query = str(search or "").casefold()
            filtered = [offer for offer in offers if not query or any(
                query in str(offer[key]).casefold()
                for key in ("offer_id", "item_id", "item_name", "category_name")
            )]
            return {
                "shop": {
                    "shop_id": shop_id,
                    "offer_categories": str(shop["OfferCategory"] or ""),
                    "player_can_sell": bool(shop["PlayerCanSell"]),
                    "npcs": [{"npc_id": str(row["Id"]),
                              "profession": str(row["Profession"] or ""),
                              "shop_loca_id": str(row["ShopLocaId"] or "")}
                             for row in npcs],
                },
                "categories": categories,
                "available_categories": available,
                "offers": filtered[offset:offset + limit],
                "total": len(filtered),
                "offset": offset,
                "limit": limit,
            }

    def overrides(self, shop_id):
        with closing(self._connect()) as db:
            categories = [dict(row) for row in db.execute(
                "SELECT category_id,category_name FROM npc_shop_categories "
                "WHERE shop_id=?", (shop_id,),
            )]
            offers = [dict(row) for row in db.execute(
                "SELECT * FROM npc_shop_offers WHERE shop_id=?", (shop_id,),
            )]
            prices = {
                str(row["offer_id"]): {
                    key: int(row[key]) for key in PRICE_COLUMNS
                }
                for row in db.execute(
                    "SELECT * FROM npc_shop_price_overrides WHERE shop_id=?",
                    (shop_id,),
                )
            }
        return {"categories": categories, "offers": offers, "prices": prices}

    def create_category(self, shop_id, category_id, category_name, actor, reason):
        with closing(self._static()) as static:
            shop, _ = self._shop_row(static, shop_id)
            ids = self._offer_ids(shop)
            if ids:
                marks = ",".join("?" for _ in ids)
                rows = static.execute(
                    f"SELECT DISTINCT OfferCategory FROM _Template_Offer WHERE Id IN ({marks})",
                    ids,
                ).fetchall()
                used = {str(row[0]) for row in rows}
            else:
                used = set()
        from dso_server.protocol.shop_protocol import OFFER_CATEGORY_IDS
        used_ids = {OFFER_CATEGORY_IDS[name] for name in used if name in OFFER_CATEGORY_IDS}
        if shop_id == "jewelry_hub_all":
            used_ids.add(OFFER_CATEGORY_IDS["ItemGemCe55"])
        with closing(self._connect()) as db, _LOCK:
            if int(category_id) in used_ids or db.execute(
                "SELECT 1 FROM npc_shop_categories WHERE shop_id=? AND category_id=?",
                (shop_id, int(category_id)),
            ).fetchone():
                raise ValueError("this NPC already has that category")
            db.execute(
                "INSERT INTO npc_shop_categories(shop_id,category_id,category_name,actor,reason) "
                "VALUES(?,?,?,?,?)",
                (shop_id, int(category_id), category_name, actor, reason),
            )
            db.commit()

    @staticmethod
    def _checked_prices(prices):
        if not isinstance(prices, dict) or set(prices) != set(PRICE_COLUMNS):
            raise ValueError("all seven shop prices are required")
        result = {}
        for key in PRICE_COLUMNS:
            value = prices[key]
            if type(value) is not int or not 0 <= value <= 0xFFFFFFFF:
                raise ValueError(f"{key} must be an integer from 0 to 4294967295")
            result[key] = value
        if not any(result.values()):
            raise ValueError("an offer must have a non-zero price")
        return result

    def add_offer(self, shop_id, category_id, category_name, item_id, amount, prices, actor, reason):
        from dso_server.protocol.shop_protocol import OFFER_CATEGORY_IDS
        prices = self._checked_prices(prices)
        category_id, amount = int(category_id), int(amount)
        if not 1 <= amount <= 1000:
            raise ValueError("amount must be from 1 to 1000")
        if category_name not in OFFER_CATEGORY_IDS or OFFER_CATEGORY_IDS[category_name] != category_id:
            raise ValueError("invalid client shop category")
        with closing(self._static()) as static:
            self._shop_row(static, shop_id)
            if static.execute("SELECT 1 FROM _Template_Item WHERE Id=?", (item_id,)).fetchone() is None:
                raise ValueError("item template was not found")
        current = self.detail(shop_id, limit=1_000_000, category_ids=OFFER_CATEGORY_IDS)
        if category_id not in {row["category_id"] for row in current["categories"]}:
            raise ValueError("create this NPC category before adding an offer")
        offer_id = "cms_" + uuid.uuid4().hex
        columns = ",".join(PRICE_COLUMNS)
        placeholders = ",".join("?" for _ in range(15))
        with closing(self._connect()) as db, _LOCK:
            db.execute(
                "INSERT OR IGNORE INTO npc_shop_categories(shop_id,category_id,category_name,actor,reason) "
                "VALUES(?,?,?,?,?)", (shop_id, category_id, category_name, actor, reason),
            )
            db.execute(
                f"INSERT INTO npc_shop_offers(offer_id,shop_id,category_id,category_name,item_id,amount,{columns},actor,reason) "
                f"VALUES({placeholders})",
                (offer_id, shop_id, category_id, category_name, item_id, amount,
                 *(prices[key] for key in PRICE_COLUMNS), actor, reason),
            )
            db.commit()
        return offer_id

    def delete_offer(self, shop_id, offer_id):
        with closing(self._static()) as static, closing(self._connect()) as db, _LOCK:
            self._shop_row(static, shop_id)
            row = db.execute(
                "SELECT offer_id,item_id,category_id,category_name,amount "
                "FROM npc_shop_offers WHERE shop_id=? AND offer_id=?",
                (shop_id, offer_id),
            ).fetchone()
            if row is None:
                raise ValueError("only CMS-created offers can be deleted")
            db.execute(
                "DELETE FROM npc_shop_offers WHERE shop_id=? AND offer_id=?",
                (shop_id, offer_id),
            )
            db.commit()
            return dict(row)

    def update_prices(self, shop_id, offer_id, prices, actor, reason):
        prices = self._checked_prices(prices)
        with closing(self._static()) as static, closing(self._connect()) as db, _LOCK:
            shop, _ = self._shop_row(static, shop_id)
            ids = self._offer_ids(shop)
            is_base = offer_id in ids and static.execute(
                "SELECT 1 FROM _Template_Offer WHERE Id=?", (offer_id,)
            ).fetchone() is not None
            custom = db.execute(
                "SELECT 1 FROM npc_shop_offers WHERE shop_id=? AND offer_id=?",
                (shop_id, offer_id),
            ).fetchone() is not None
            dynamic = False
            if shop_id == "jewelry_hub_all" and offer_id.startswith((
                "jewelry_max_tier_", "jewelry_max_sockets_"
            )):
                template_id = offer_id.split("_", 3)[-1] if offer_id.startswith("jewelry_max_tier_") else offer_id.removeprefix("jewelry_max_sockets_")
                item = static.execute(
                    "SELECT ItemCategory FROM _Template_Item WHERE Id=?", (template_id,)
                ).fetchone()
                expected = {"EquipmentJewel", "EquipmentRune"} if offer_id.startswith("jewelry_max_tier_") else {"RuneTrinket", "JewelTrinket"}
                dynamic = item is not None and str(item[0]) in expected
            if not is_base and not custom and not dynamic:
                raise ValueError("offer was not found in this NPC shop")
            if custom:
                assignments = ",".join(f"{key}=?" for key in PRICE_COLUMNS)
                db.execute(
                    f"UPDATE npc_shop_offers SET {assignments},actor=?,reason=? "
                    "WHERE shop_id=? AND offer_id=?",
                    (*(prices[key] for key in PRICE_COLUMNS), actor, reason, shop_id, offer_id),
                )
            else:
                columns = ",".join(PRICE_COLUMNS)
                placeholders = ",".join("?" for _ in PRICE_COLUMNS)
                db.execute(
                    "INSERT INTO npc_shop_price_overrides(shop_id,offer_id," + columns + ",actor,reason) "
                    "VALUES(?, ?," + placeholders + ",?,?) "
                    "ON CONFLICT(shop_id,offer_id) DO UPDATE SET "
                    + ",".join(f"{key}=excluded.{key}" for key in PRICE_COLUMNS)
                    + ",actor=excluded.actor,reason=excluded.reason,updated_at=unixepoch()",
                    (shop_id, offer_id, *(prices[key] for key in PRICE_COLUMNS), actor, reason),
                )
            db.commit()
