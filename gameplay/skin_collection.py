"""Equipment skins a character has collected (``UnlockSkin`` achievements).

Evidence:

* Retail's ``ItemCustomizationInfoCommand`` (0x140) lists one TransmogItem per
  skin the character has unlocked: the 2026-07-20 official capture sends 465
  mage skins for a character whose ``skin_unlock_amount_09`` (400) is
  complete.  ``skin_unlock_amount_*`` describes that number ("Unlock {0}
  skins in the Customization window").
* ``_Template_EquipmentSkin.LocaleIdDescLocked`` tells how a locked skin is
  obtained; the item-sourced texts read "This skin can be unlocked by
  collecting the eponymous equipment item ...".  Skin consumables, shop
  offers and achievement rewards are separate sources.
* A collected equipment item shows its own look: generated drops carry the
  rolled ``_Template_ItemAppearance`` row in ``ItemInfo.field_80`` (whose
  ``EquipmentSkinId`` is the skin); other items name it in
  ``_Template_Item.EquipmentSkinId``.

The local server still offers every visible class skin in the transmog
window (``gameplay/skin_unlocks.py``).  This module only decides which of
those skins a character has collected, so ``UnlockSkin`` progress counts
real collection and cannot be farmed by repeating an action: the persisted
set only grows when an item with a new look is held.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
import sqlite3
from types import MappingProxyType
from typing import Iterable, Mapping

from dso_server.runtime import resident_content


@dataclass(frozen=True)
class SkinCatalog:
    # visible skin id -> classes allowed to show it (casefolded)
    skin_classes: Mapping[str, frozenset[str]]
    # item template -> the skin it renders (SkinConsumable items excluded)
    template_skins: Mapping[str, str]
    # _Template_ItemAppearance.Id -> its EquipmentSkinId
    appearance_skins: Mapping[str, str]

    def item_skin(self, item, class_name: str) -> str:
        """The visible class skin a held item contributes, or ``""``.

        Unidentified items (``field_3c``) contribute nothing until identified:
        their look is not known to the player yet (local rule).
        """

        if getattr(item, "field_3c", False):
            return ""
        appearance = str(getattr(item, "field_80", "") or "")
        skin = self.appearance_skins.get(appearance, "") if appearance else ""
        if not skin:
            skin = self.template_skins.get(str(getattr(item, "template_id", "")), "")
        classes = self.skin_classes.get(skin)
        if not skin or classes is None or str(class_name).casefold() not in classes:
            return ""
        return skin


@lru_cache(maxsize=4)
def _load(path: str, mtime_ns: int, size: int) -> SkinCatalog:
    del mtime_ns, size
    with sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True) as database:
        skin_classes = {
            str(skin_id): frozenset(
                token.strip().casefold()
                for token in str(classes or "").split(";") if token.strip()
            )
            for skin_id, classes in database.execute(
                "SELECT Id, CharClass FROM _Template_EquipmentSkin WHERE Visible=1"
            )
        }
        template_skins = {}
        for template_id, skin_id, category in database.execute(
            "SELECT Id, COALESCE(EquipmentSkinId, ''), COALESCE(ItemCategory, '') "
            "FROM _Template_Item"
        ):
            if str(category) == "SkinConsumable":
                continue
            skin = str(skin_id) or (str(template_id) if str(template_id) in skin_classes else "")
            if skin in skin_classes:
                template_skins[str(template_id)] = skin
        appearance_skins = {
            str(appearance_id): str(skin_id)
            for appearance_id, skin_id in database.execute(
                "SELECT Id, COALESCE(EquipmentSkinId, '') FROM _Template_ItemAppearance"
            )
            if str(skin_id) in skin_classes
        }
    return SkinCatalog(
        MappingProxyType(skin_classes),
        MappingProxyType(template_skins),
        MappingProxyType(appearance_skins),
    )


def load_skin_catalog(static_database_path: str | Path) -> SkinCatalog:
    path = Path(static_database_path)
    stat = resident_content.stat(path)
    return _load(str(path), stat.st_mtime_ns, stat.st_size)


def held_skins(catalog: SkinCatalog, items: Iterable, class_name: str) -> frozenset[str]:
    """Skins of every held item (equipped, bags, bank) for one class."""

    return frozenset(
        skin for skin in (catalog.item_skin(item, class_name) for item in items) if skin
    )


__all__ = ["SkinCatalog", "held_skins", "load_skin_catalog"]
