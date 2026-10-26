"""Read-only access to the static content shared with the game client."""

from __future__ import annotations

import os
from collections import OrderedDict
from contextlib import closing
from functools import wraps
from pathlib import Path
import sqlite3
import threading

from dso_server.persistence import static_schema_compat as compat
from dso_server.protocol.inventory_codec import AMMUNITION_SLOTS
from dso_server.protocol.character_selection import (
    CLASS_NAMES,
    CLIENT_SKILL_MANAGER_ROW_OFFSET,
    NEW_CHARACTER_QUICKBAR_SKILL_SLOTS,
    CharacterSelection,
    CharacterSelectionError,
    EquipmentRecord,
)


def _cached_content(method):
    """Cache immutable content results for one explicit database lifetime."""

    @wraps(method)
    def cached(self, *args, **kwargs):
        key = (str(self.path), method.__name__, args, tuple(sorted(kwargs.items())))
        with self._content_cache_lock:
            revision = self._content_revision
            if key in self._content_cache:
                self._content_cache.move_to_end(key)
                return self._content_cache[key]
        value = method(self, *args, **kwargs)
        with self._content_cache_lock:
            if revision == self._content_revision:
                self._content_cache[key] = value
                self._content_cache.move_to_end(key)
                while len(self._content_cache) > 4096:
                    self._content_cache.popitem(last=False)
        return value

    return cached


class StaticGameDatabase:
    """Read-only validator for identifiers shared by the client and server."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self._content_cache = OrderedDict()
        self._content_cache_lock = threading.Lock()
        self._content_revision = 0

    def invalidate_content_cache(self) -> None:
        """Call after publishing replacement static content, outside gameplay.

        Normal reads never stat the database. A new database instance also
        starts a fresh cache, matching the server's content reload lifecycle.
        """
        with self._content_cache_lock:
            self._content_revision += 1
            self._content_cache.clear()

    def _connect(self) -> sqlite3.Connection:
        if not self.path.is_file():
            raise FileNotFoundError(self.path)
        return sqlite3.connect(f"file:{self.path.as_posix()}?mode=ro", uri=True)

    @staticmethod
    def _exists(
        connection: sqlite3.Connection, table: str, column: str, value: str
    ) -> bool:
        return (
            connection.execute(
                f'SELECT 1 FROM "{table}" WHERE "{column}"=? LIMIT 1',
                (value,),
            ).fetchone()
            is not None
        )

    def validate(self, model: CharacterSelection) -> list[str]:
        warnings = []
        with closing(self._connect()) as connection:
            map_lookup = compat.map_template_row_query(
                connection, ("MapTemplate",)
            )
            for character in model.characters:
                if connection.execute(
                    map_lookup, (character.map_id,)
                ).fetchone() is None:
                    warnings.append(
                        f"{character.name}: unknown map template {character.map_id!r}"
                    )

                class_name = CLASS_NAMES.get(character.class_id)
                if class_name is None:
                    warnings.append(
                        f"{character.name}: unknown class id {character.class_id}"
                    )
                    continue
                gender_name = "Male" if character.gender == 0 else "Female"
                class_column = class_name.capitalize() + gender_name
                columns = {
                    row[1]
                    for row in connection.execute(
                        'PRAGMA table_info("_Template_CharacterHair")'
                    )
                }
                if class_column in columns:
                    hair = connection.execute(
                        f'SELECT 1 FROM "_Template_CharacterHair" '
                        f'WHERE "Index"=? AND "{class_column}"=1 LIMIT 1',
                        (character.hair_style,),
                    ).fetchone()
                    if hair is None:
                        warnings.append(
                            f"{character.name}: hair index {character.hair_style} "
                            f"is not enabled for {class_name}/{gender_name.lower()}"
                        )
                    beard = connection.execute(
                        f'SELECT 1 FROM "_Template_CharacterBeard" '
                        f'WHERE "Index"=? AND "{class_column}"=1 LIMIT 1',
                        (character.beard_style,),
                    ).fetchone()
                    if beard is None:
                        warnings.append(
                            f"{character.name}: beard index {character.beard_style} "
                            f"is not enabled for {class_name}/{gender_name.lower()}"
                        )

            # Equipment skins postdate the legacy content generation, so the
            # identifier may only be resolvable through the tables that build
            # actually has.
            identifier_tables = tuple(
                (table, column)
                for table, column in (
                    ("_Template_Item", "Id"),
                    ("_Template_ItemAppearance", "Id"),
                    ("_Template_EquipmentSkin", "Id"),
                )
                if compat.table_exists(connection, table)
            )
            for index, equipment in enumerate(model.equipment):
                for label, value in (
                    ("item", equipment.item_id),
                    ("appearance", equipment.appearance_id),
                    ("effect", equipment.effect_id),
                ):
                    if not value:
                        continue
                    if not any(
                        self._exists(connection, table, column, value)
                        for table, column in identifier_tables
                    ):
                        warnings.append(
                            f"equipment[{index}] {label} id {value!r} is absent "
                            "from item/appearance/skin templates"
                        )
        return warnings

    def character_generation_equipment(
        self, class_id: int
    ) -> list[EquipmentRecord]:
        """Return the visual loadout used by the character-creation renderer.

        CharacterSelection operation 9 expects item template identifiers, not
        full inventory objects.  ``CharGenItems`` is the database-owned list
        for that renderer; ``Items`` contains the separate gameplay starter
        inventory and duplicates the hand slots, so it is deliberately not
        included here.
        """
        class_name = CLASS_NAMES.get(int(class_id))
        if class_name is None:
            raise CharacterSelectionError(f"unknown class id {class_id}")

        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT "CharGenItems" FROM "_Template_StartEquipment" '
                'WHERE "Id"=? LIMIT 1',
                (f"{class_name}_default_equipment",),
            ).fetchone()
            if row is None:
                raise CharacterSelectionError(
                    f"no start-equipment template for class {class_name!r}"
                )
            item_ids = [
                item_id.strip()
                for item_id in (row[0] or "").split(";")
                if item_id.strip()
            ]
            missing = [
                item_id
                for item_id in item_ids
                if not self._exists(
                    connection, "_Template_Item", "Id", item_id
                )
            ]
            if missing:
                raise CharacterSelectionError(
                    f"{class_name} start equipment contains unknown items: "
                    + ", ".join(missing)
                )
        return [EquipmentRecord(item_id=item_id) for item_id in item_ids]

    def item_equipment_slots(
        self,
        template_id: str,
        *,
        preferred_slots: tuple[int, ...] = (),
        occupied_slots: tuple[int, ...] = (),
    ) -> tuple[int, ...]:
        """Resolve an item's generated SlotType to Inventory slot ids.

        Equivalent physical slots choose the explicitly replaced slot first,
        then the first unoccupied slot. The numeric mapping is recovered from
        retail InventoryInfo equipment records: rings are 8/9, amulet 10,
        belt 11, weapon modifier/banner 12, and the hand pair is 5/6. The
        essence pair is 35 (active) and 36 (secondary), confirmed on the
        live client; 35 comes first so a lone essence lands in the active
        slot.
        """

        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT "SlotType" FROM "_Template_Item" '
                'WHERE "Id"=? LIMIT 1',
                (str(template_id),),
            ).fetchone()
        if row is None:
            raise CharacterSelectionError(
                f"unknown item template {template_id!r}"
            )
        slot_type = str(row[0] or "")
        fixed_slots = {
            "HelmetSlot": (0,),
            "ShoulderSlot": (1,),
            "TorsoSlot": (2,),
            "GlovesSlot": (3,),
            "BootsSlot": (4,),
            "RightHandSlot": (5,),
            "LeftHandSlot": (6,),
            "RightHandSlot,LeftHandSlot": (5, 6),
            "CloakSlot": (7,),
            "AmuletSlot": (10,),
            "BeltSlot": (11,),
            "WeaponModSlot": (12,),
            # The Arcane Vessel / Emblem UI slot sits after the three jewel
            # trinket slots and before the Ancient Trinket slot in the
            # client's inventory allocation table.
            "EmblemSlot": (23,),
            "AncientTrinketSlot": (24,),
            "MercenaryAmuletSlot": (25,),
            "MercenaryCloakSlot": (26,),
            "MercenaryBeltSlot": (27,),
        }
        if slot_type in fixed_slots:
            return fixed_slots[slot_type]
        equivalent_slots = {
            "RingSlot": (8, 9),
            "RuneTrinketSlot": (13, 14, 15, 16, 17, 18, 19),
            "JewelTrinketSlot": (20, 21, 22),
            "AmmoSlot": AMMUNITION_SLOTS,
            "ConsumableSlot": (37, 38, 39, 40),
        }
        try:
            candidates = equivalent_slots[slot_type]
        except KeyError as exc:
            raise CharacterSelectionError(
                f"item {template_id!r} has unsupported equipment SlotType "
                f"{slot_type!r}"
            ) from exc
        for slot in preferred_slots:
            slot = int(slot)
            if slot in candidates:
                return (slot,)
        occupied = {int(slot) for slot in occupied_slots}
        for slot in candidates:
            if slot not in occupied:
                return (slot,)
        # If every equivalent slot is occupied and the client did not name a
        # replacement, use the first one deterministically.
        return (candidates[0],)

    @_cached_content
    def player_template_row_index(self, class_id: int) -> int:
        """Return the zero-based row in TemplateManager's player array.

        ``NewPlayerCommand`` resolves this value against the dedicated player
        template vector at ``TemplateManager + 0x1e8``. It is therefore the
        local row in ``_Template_Player``, not an index accumulated across all
        category template tables.
        """

        class_name = CLASS_NAMES.get(int(class_id))
        if class_name is None:
            raise CharacterSelectionError(f"unknown class id {class_id}")
        template_id = f"human_male_{class_name}"

        with closing(self._connect()) as connection:
            player_row = connection.execute(
                'SELECT rowid FROM "_Template_Player" '
                'WHERE "Id"=? LIMIT 1',
                (template_id,),
            ).fetchone()
            if player_row is None:
                raise CharacterSelectionError(
                    f"no player template for class {class_name!r}"
                )
            local_index = connection.execute(
                'SELECT COUNT(*) FROM "_Template_Player" WHERE rowid < ?',
                (player_row[0],),
            ).fetchone()[0]
        return local_index

    @_cached_content
    def remote_player_animation_set(self, class_id: int) -> str:
        """Return the client-valid base animation set for a remote class.

        ``RemotePlayerInfoCommand`` dispatches this string directly to the
        remote actor's graphics property. An empty string is not a generated
        default there: the client asserts while resolving its base animation
        set. The dedicated remote-player table owns the class-safe value used
        before a weapon-specific visual update is available.
        """

        class_name = CLASS_NAMES.get(int(class_id))
        if class_name is None:
            raise CharacterSelectionError(f"unknown class id {class_id}")
        template_id = f"remote_human_male_{class_name}"

        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT "AnimSet" FROM "_Template_RemotePlayer" '
                'WHERE "Id"=? LIMIT 1',
                (template_id,),
            ).fetchone()
        animation_set = str(row[0] or "") if row is not None else ""
        if not animation_set:
            raise CharacterSelectionError(
                f"remote player template {template_id!r} has no AnimSet"
            )
        return animation_set

    @_cached_content
    def remote_player_template_row_index(self, class_id: int) -> int:
        """Return the zero-based template row used by NewRemotePlayer.

        Remote players are instantiated from ``_Template_RemotePlayer``.  The
        player and remote-player tables currently share an order, but they are
        separate template vectors and must not rely on that incidental match.
        """

        class_name = CLASS_NAMES.get(int(class_id))
        if class_name is None:
            raise CharacterSelectionError(f"unknown class id {class_id}")
        template_id = f"remote_human_male_{class_name}"
        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT rowid FROM "_Template_RemotePlayer" '
                'WHERE "Id"=? LIMIT 1',
                (template_id,),
            ).fetchone()
            if row is None:
                raise CharacterSelectionError(
                    f"no remote-player template for class {class_name!r}"
                )
            index = connection.execute(
                'SELECT COUNT(*) FROM "_Template_RemotePlayer" '
                'WHERE rowid < ?',
                (row[0],),
            ).fetchone()[0]
        return int(index)

    @_cached_content
    def character_selection_visual_item_template(
            self, item_template_id: str, appearance_id: str = "") -> str:
        """Resolve a selected equipment skin to the item used by previews.

        The character-selection equipment record builds its model from the
        item template. Unlike RemotePlayerInfo, it does not apply a
        ``SkinGraphics`` value as a runtime slot override. Newer equipment
        skins provide their preview model through ``TooltipItem``; older item
        appearances link to one of those skins through ``EquipmentSkinId``.
        """

        fallback = str(item_template_id)
        appearance_id = str(appearance_id or "")
        if not appearance_id:
            return fallback
        with closing(self._connect()) as connection:
            if not compat.table_exists(connection, "_Template_EquipmentSkin"):
                return fallback
            skin_id = appearance_id
            if not self._exists(
                connection, "_Template_EquipmentSkin", "Id", skin_id
            ):
                if not compat.table_exists(
                    connection, "_Template_ItemAppearance"
                ):
                    return fallback
                row = connection.execute(
                    'SELECT "EquipmentSkinId" '
                    'FROM "_Template_ItemAppearance" '
                    'WHERE "Id"=? LIMIT 1',
                    (appearance_id,),
                ).fetchone()
                if row is None or not row[0]:
                    return fallback
                skin_id = str(row[0])
            tooltip_item = connection.execute(
                'SELECT ' + compat.projection(
                    connection, "_Template_EquipmentSkin", ("TooltipItem",)
                ) + ' FROM "_Template_EquipmentSkin" '
                'WHERE "Id"=? LIMIT 1',
                (skin_id,),
            ).fetchone()
            candidate = str(tooltip_item[0] or "") if tooltip_item else ""
            # Basic equipment skins commonly omit TooltipItem even though
            # their skin id has a corresponding item template.  In that case
            # use the same-id template as the preview model instead of
            # falling back to the currently equipped item's default model.
            if not candidate and self._exists(
                connection, "_Template_Item", "Id", skin_id
            ):
                candidate = skin_id
            if candidate and self._exists(
                connection, "_Template_Item", "Id", candidate
            ):
                return candidate
        return fallback

    @_cached_content
    def remote_equipment_skin(
        self, template_id: str, appearance_id: str = ""
    ) -> str:
        """Resolve an equipped item to the skin sent in RemotePlayerInfo.

        A runtime appearance override wins over the item's default skin.
        Older/basic items often leave ``_Template_Item.Skin`` empty and point
        through ``EquipmentSkinId`` instead, so all three database-owned
        representations are handled.
        """

        template_id = str(template_id)
        appearance_id = str(appearance_id or "")
        with closing(self._connect()) as connection:
            if appearance_id:
                for table in (
                    "_Template_ItemAppearance",
                    "_Template_EquipmentSkin",
                ):
                    if not compat.table_exists(connection, table):
                        continue
                    row = connection.execute(
                        f'SELECT "SkinGraphics" FROM "{table}" '
                        'WHERE "Id"=? LIMIT 1',
                        (appearance_id,),
                    ).fetchone()
                    if row is not None and row[0]:
                        return str(row[0])

            # EquipmentSkinId arrived with equipment skins; reading it as NULL
            # on an older build falls through to the plain Skin column below.
            row = connection.execute(
                "SELECT " + compat.projection(
                    connection, "_Template_Item", ("Skin", "EquipmentSkinId")
                ) + ' FROM "_Template_Item" WHERE "Id"=? LIMIT 1',
                (template_id,),
            ).fetchone()
            if row is None:
                raise CharacterSelectionError(
                    f"unknown item template {template_id!r}"
                )
            skin, equipment_skin_id = row
            if skin:
                return str(skin)
            if equipment_skin_id:
                skin_row = connection.execute(
                    'SELECT "SkinGraphics" '
                    'FROM "_Template_EquipmentSkin" '
                    'WHERE "Id"=? LIMIT 1',
                    (str(equipment_skin_id),),
                ).fetchone()
                if skin_row is not None and skin_row[0]:
                    return str(skin_row[0])
        raise CharacterSelectionError(
            f"item {template_id!r} has no remote equipment skin"
        )

    @_cached_content
    def remote_equipment_icon(self, template_id: str) -> str:
        """Return the client icon used for non-geometry equipment slots."""

        with closing(self._connect()) as connection:
            row = connection.execute(
                'SELECT "IconBrush" FROM "_Template_Item" '
                'WHERE "Id"=? LIMIT 1',
                (str(template_id),),
            ).fetchone()
        if row is None or not row[0]:
            raise CharacterSelectionError(
                f"item {template_id!r} has no equipment icon"
            )
        return str(row[0])

    def gameplay_map_ruleset(self, map_id: str) -> int:
        """Return the client ruleset used by the map-load packet.

        The retail protocol does not use one ruleset for every gameplay map:
        hubs are announced with ruleset 5, while combat PvE maps are announced
        with ruleset 1. ``AreaType`` is the database-owned distinction, so do
        not infer this from a map-name suffix.
        """

        with closing(self._connect()) as connection:
            row = connection.execute(
                compat.map_template_row_query(connection, ("AreaType",)),
                (str(map_id),),
            ).fetchone()
        if row is None:
            raise CharacterSelectionError(
                f"no map template found for {map_id!r}"
            )
        # UNRESOLVED for the legacy build: AreaType exists nowhere in that
        # content generation -- not in _Template_MapConfig, not in the per-map
        # _Instance_MapConfig, not even in its _Attributes registry -- so this
        # reads NULL and every map, hubs included, is announced with the combat
        # ruleset. Whether that build drew the distinction at all is a question
        # about its map-load packet rather than about its database. See
        # dso_server_legacy/docs/LEGACY_FORK.md.
        return 5 if str(row[0]) == "Hub" else 1

    def gameplay_map_chat_region(self, map_id: str) -> str:
        """Return the database-owned chat region for a gameplay map."""

        with closing(self._connect()) as connection:
            row = connection.execute(
                compat.map_template_row_query(connection, ("ChatRegion",)),
                (str(map_id),),
            ).fetchone()
        if row is None:
            raise CharacterSelectionError(
                f"no map template found for {map_id!r}"
            )
        region = str(row[0] or "").strip()
        if not region:
            raise CharacterSelectionError(
                f"map template {map_id!r} has no ChatRegion"
            )
        return region

    def player_skill_manager_indices(self, class_id: int) -> tuple[int, ...]:
        """Return SkillBookInfo ids for a player class in this client build.

        These are runtime SkillManager indexes for the local content export.
        The database also verifies that the selected class exists and has a
        contiguous skill dataset.
        """

        class_id = int(class_id)
        class_name = CLASS_NAMES.get(class_id)
        if class_name is None:
            raise CharacterSelectionError(f"unknown class id {class_id}")

        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT rowid FROM "_Template_Skill" '
                'WHERE "CharClass"=? ORDER BY rowid',
                (class_name,),
            ).fetchall()
        if not rows:
            raise CharacterSelectionError(
                f"{class_name} skill dataset contains no rows"
            )
        rowids = tuple(int(row[0]) for row in rows)
        return tuple(
            rowid + CLIENT_SKILL_MANAGER_ROW_OFFSET for rowid in rowids
        )

    def quickbar_skill_slots(
        self,
        class_id: int,
        level: int,
        saved_slots: tuple[str | None, ...] | None = None,
    ) -> tuple[str | None, ...]:
        """Return validated saved skill positions, or a class fallback.

        This is deliberately independent from ``player_skill_manager_indices``:
        SkillBookInfo describes available skills, while QuickSlotsInfo describes
        the character's saved bar positions.
        """

        class_id = int(class_id)
        class_name = CLASS_NAMES.get(class_id)
        if (
            class_name is None
            or class_id not in NEW_CHARACTER_QUICKBAR_SKILL_SLOTS
        ):
            raise CharacterSelectionError(f"unknown class id {class_id}")
        level = max(1, int(level))
        using_fallback = saved_slots is None
        requested = (
            NEW_CHARACTER_QUICKBAR_SKILL_SLOTS[class_id]
            if using_fallback
            else tuple(saved_slots)
        )
        if len(requested) > 8:
            raise CharacterSelectionError(
                "quickbar supports at most eight saved skill positions"
            )
        resource_ids = tuple(
            resource_id for resource_id in requested if resource_id is not None
        )
        if not resource_ids:
            return ()

        placeholders = ",".join("?" for _ in resource_ids)
        with closing(self._connect()) as connection:
            rows = connection.execute(
                f'SELECT "Id", "CharClass", "UnlockLevel", "Hidden", "IsLive" '
                f'FROM "_Template_Skill" WHERE "Id" IN ({placeholders})',
                resource_ids,
            ).fetchall()
        by_id = {str(row[0]): row for row in rows}
        missing = [
            resource_id
            for resource_id in resource_ids
            if resource_id not in by_id
        ]
        if missing:
            raise CharacterSelectionError(
                f"{class_name} quickbar contains unknown skills: " + ", ".join(missing)
            )

        result = []
        for resource_id in requested:
            if resource_id is None:
                result.append(None)
                continue
            _identifier, row_class, unlock_level, hidden, is_live = by_id[resource_id]
            if str(row_class) != class_name:
                raise CharacterSelectionError(
                    f"quickbar skill {resource_id!r} belongs to {row_class!r}, "
                    f"not {class_name!r}"
                )
            if bool(hidden) or not bool(is_live):
                raise CharacterSelectionError(
                    f"quickbar skill {resource_id!r} is hidden or not live"
                )
            if int(unlock_level or 0) > level:
                if not using_fallback:
                    raise CharacterSelectionError(
                        f"quickbar skill {resource_id!r} unlocks at level "
                        f"{int(unlock_level)}, above character level {level}"
                    )
                result.append(None)
            else:
                result.append(resource_id)
        return tuple(result)

    def travel_map_unlock_words(
        self,
        map_ids: tuple[str, ...],
        *,
        word_count: int = 4,
    ) -> tuple[int, ...]:
        """Return NewPlayer's travel-map access bitset for ``map_ids``.

        ``_Template_TravelMap.MapLookupIndex`` is one-based, while the access
        array itself is an ordinary zero-based bitset. This client build stores
        128 access bits as four little-endian uint32 words in NewPlayer at
        offset +0x130.
        """

        requested = tuple(dict.fromkeys(str(map_id) for map_id in map_ids))
        if not requested:
            return (0,) * word_count
        placeholders = ",".join("?" for _ in requested)
        with closing(self._connect()) as connection:
            rows = connection.execute(
                f'SELECT "Id", "MapLookupIndex" FROM "_Template_TravelMap" '
                f'WHERE "Id" IN ({placeholders})',
                requested,
            ).fetchall()
        by_id = {str(map_id): int(index) for map_id, index in rows}
        missing = [map_id for map_id in requested if map_id not in by_id]
        if missing:
            raise CharacterSelectionError(
                "unknown travel maps: " + ", ".join(missing)
            )
        words = [0] * word_count
        for map_id in requested:
            lookup_index = by_id[map_id]
            bit_index = lookup_index - 1
            if not 0 <= bit_index < word_count * 32:
                raise CharacterSelectionError(
                    f"travel map {map_id!r} has out-of-range lookup index "
                    f"{lookup_index}"
                )
            words[bit_index // 32] |= 1 << (bit_index % 32)
        return tuple(words)

    def fast_travel_map_ids(
        self,
        *,
        word_count: int = 4,
    ) -> tuple[str, ...]:
        """Return every live map with a usable travel point in this export.

        Inactive regional duplicates and boss/quest maps with
        ``HasTravelPoint=0`` are deliberately excluded.  Duplicate lookup
        indexes are rejected because two live destinations cannot safely
        share one client access bit.
        """

        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT "Id", "MapLookupIndex" '
                'FROM "_Template_TravelMap" '
                'WHERE "IsLive"<>0 AND "HasTravelPoint"<>0 '
                'ORDER BY "MapLookupIndex", rowid'
            ).fetchall()
        result = []
        by_index: dict[int, str] = {}
        for raw_map_id, raw_lookup_index in rows:
            map_id = str(raw_map_id)
            lookup_index = int(raw_lookup_index)
            if not 1 <= lookup_index <= word_count * 32:
                raise CharacterSelectionError(
                    f"live travel map {map_id!r} has out-of-range lookup "
                    f"index {lookup_index}"
                )
            previous = by_index.get(lookup_index)
            if previous is not None and previous != map_id:
                raise CharacterSelectionError(
                    f"live travel maps {previous!r} and {map_id!r} share "
                    f"lookup index {lookup_index}"
                )
            by_index[lookup_index] = map_id
            result.append(map_id)
        return tuple(result)

    def player_xp_bounds(self, class_id: int, level: int) -> tuple[int, int]:
        """Return cumulative XP at ``level`` and at the following level.

        ``NewPlayerCommand`` carries both values.  The client subtracts them
        when handling its initial ``SetXPInfo`` event and asserts if the
        resulting per-level XP span is not positive.
        """

        class_name = CLASS_NAMES.get(int(class_id))
        if class_name is None:
            raise CharacterSelectionError(f"unknown class id {class_id}")
        level = int(level)
        if level < 1 or level > 100:
            raise CharacterSelectionError(f"invalid player level {level}")

        next_level = min(level + 1, 100)
        with closing(self._connect()) as connection:
            rows = connection.execute(
                'SELECT "Level", "LevelXP" FROM "_Template_XPLevels" '
                'WHERE "CharClass"=? AND "Level" IN (?, ?)',
                (class_name, level, next_level),
            ).fetchall()
        xp_by_level = {int(row[0]): int(row[1]) for row in rows}
        if level not in xp_by_level or next_level not in xp_by_level:
            if level == 100 and level in xp_by_level:
                return xp_by_level[level], xp_by_level[level] + 1
            raise CharacterSelectionError(
                f"missing XP progression for {class_name} level {level}"
            )

        level_base_xp = xp_by_level[level]
        next_level_base_xp = (
            xp_by_level[next_level] if next_level > level else level_base_xp + 1
        )
        if next_level_base_xp <= level_base_xp:
            raise CharacterSelectionError(
                f"non-positive XP span for {class_name} level {level}: "
                f"{level_base_xp} -> {next_level_base_xp}"
            )
        return level_base_xp, next_level_base_xp
