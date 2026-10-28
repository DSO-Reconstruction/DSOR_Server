"""Equipment transmog requests, authored prices, and persistent item changes."""
from dataclasses import dataclass
from contextlib import closing
from pathlib import Path
import copy
import math
import sqlite3
import struct

from dso_server.runtime import resident_content
from dso_server.protocol.character_selection import BitReader, CLASS_NAMES
from dso_server.protocol.command_ids import command_id
from dso_server.protocol.commerce_protocol import PurchasePrice, debit_currency
from dso_server.protocol.inventory_economy_protocol import InventoryMutationBuilder

APPLY_ITEM_CUSTOMIZATION_COMMAND = command_id(
    'Commands::ApplyItemCustomizationChangesCommand', 0x0142)

# The request carries EquipmentSlotType, not an Inventory equipment index.
# CloakSlot is type 12 but lives in inventory slot 7.
CUSTOMIZATION_SLOTS = {0: 0, 1: 1, 2: 2, 3: 3, 4: 4, 5: 5, 6: 6, 12: 7}


@dataclass(frozen=True)
class ItemCustomizationChange:
    slot_type: int
    apply_skin: str
    remove_skin: str
    skin: str
    effect_skin: str
    apply_color: str
    remove_color: str


def decode_item_customization_changes(payload: bytes) -> tuple[ItemCustomizationChange, ...]:
    reader = BitReader(payload)
    if reader.read_uint8() != 0x8B or int.from_bytes(reader.read_bytes(2), 'little') != APPLY_ITEM_CUSTOMIZATION_COMMAND:
        raise ValueError('not an ApplyItemCustomizationChanges command')
    count = reader.read_uint32_le()
    if not 1 <= count <= len(CUSTOMIZATION_SLOTS):
        raise ValueError('invalid customization slot count')
    slots = tuple(reader.read_uint32_le() for _ in range(count))
    if len(set(slots)) != count or any(slot not in CUSTOMIZATION_SLOTS for slot in slots):
        raise ValueError('invalid or duplicate customization slot')
    if reader.read_uint32_le() != count:
        raise ValueError('customization arrays have different lengths')
    changes = []
    for slot in slots:
        if reader.read_uint32_le() != 6:
            raise ValueError('customization requires six resource strings')
        changes.append(ItemCustomizationChange(slot, *(reader.read_string() for _ in range(6))))
    if reader.remaining:
        raise ValueError('trailing customization data')
    return tuple(changes)


def item_tint(item):
    """Unpack native ARGB dye; ItemInfo.field_100 is a world position."""
    packed = int(item.field_20)
    if packed:
        return tuple(((packed >> shift) & 255) / 255.0 for shift in (16, 8, 0, 24))
    return None


def apply_item_customizations(inventory, currency, changes, *, database_path,
                              class_id, level, next_item_id):
    """Validate and price the complete request before publishing any mutation."""
    builder = InventoryMutationBuilder(inventory, next_item_id)
    updated = []
    seen = {}
    price = PurchasePrice()
    with closing(sqlite3.connect(resident_content.resolved(database_path).as_uri() + '?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row

        def row(table, resource):
            result = db.execute(f'SELECT * FROM {table} WHERE Id=?', (resource,)).fetchone()
            if result is None:
                raise ValueError(f'unknown customization resource {resource!r}')
            return result

        def color(resource):
            definition = row('_Template_EquipmentColor', resource)
            palette = db.execute('SELECT PrimaryColor, ColorId FROM _Template_ItemRarityColors '
                                 'WHERE Rarity=?', (resource,)).fetchone()
            if palette is None:
                raise ValueError(f'missing dye palette {resource!r}')
            rgba = tuple(max(0, min(255, math.floor(v * 255 + .5)))
                         for v in struct.unpack('<4f', palette['PrimaryColor']))
            r, g, b, a = rgba
            return definition, (a << 24) | (r << 16) | (g << 8) | b, int(palette['ColorId'])

        scaling = db.execute('SELECT VirtualCurrencyCostMultiplier FROM _Template_DynamicLevelCosts '
                             'WHERE Level=?', (max(1, int(level)),)).fetchone()
        if scaling is None:
            raise ValueError('missing customization price level')
        factor = float(scaling[0])
        for change in changes:
            slot = CUSTOMIZATION_SLOTS[change.slot_type]
            ids = [item_id for item_id, slots in builder.inventory.equipment_slots.items() if slot in slots]
            if len(ids) != 1:
                raise ValueError(f'customization slot {slot} has no equipped item')
            item = builder.item(ids[0])
            fields = (change.apply_skin, change.remove_skin, change.skin,
                      change.effect_skin, change.apply_color, change.remove_color)
            if item.item_id in seen:
                if seen[item.item_id] != fields:
                    raise ValueError('conflicting customization of a two-handed item')
                continue
            seen[item.item_id] = fields
            before = copy.deepcopy(item)
            if change.apply_skin or change.remove_skin:
                if item.field_3c:
                    raise ValueError('identify equipment before customizing it')
                item_row = row('_Template_Item', item.template_id)
                desired = change.apply_skin
                if item.field_f8 != desired:
                    # A request can name the item's default skin when replacing
                    # it for the first time. Prices come from authored rows.
                    old = item.field_f8 or str(item_row['EquipmentSkinId'] or '')
                    if change.remove_skin and change.remove_skin != old:
                        raise ValueError('stale skin selection')
                    if change.remove_skin:
                        old_row = row('_Template_EquipmentSkin', change.remove_skin)
                        price += PurchasePrice(vc=math.floor(int(old_row['RemovePrice']) * factor + .5))
                    if desired:
                        skin_row = row('_Template_EquipmentSkin', desired)
                        classes = str(skin_row['CharClass'] or '').split(';')
                        if CLASS_NAMES[int(class_id)] not in classes:
                            raise ValueError('skin belongs to another class')
                        if set(str(skin_row['SlotType']).split(',')) != set(str(item_row['SlotType']).split(',')):
                            raise ValueError('skin does not fit this equipment')
                        price += PurchasePrice(rc=int(skin_row['ApplyPriceRC']),
                                               vc=math.floor(int(skin_row['ApplyPriceVC']) * factor + .5))
                    item.field_f8 = desired
            if change.apply_color or change.remove_color:
                if item.field_3c:
                    raise ValueError('identify equipment before customizing it')
                desired = change.apply_color
                old = item.field_28 if item.field_28 not in ('', 'none') else ''
                if desired != old:
                    if change.remove_color and change.remove_color != old:
                        raise ValueError('stale dye selection')
                    if old:
                        price += PurchasePrice(vc=int(row('_Template_EquipmentColor', old)['RemovePrice']))
                    if desired:
                        definition, packed, color_id = color(desired)
                        dye_template = str(definition['Color'])
                        accessible = (set(builder.inventory.storage_58) | set(builder.inventory.storage_80)
                                      | set(builder.inventory.field_180))
                        dye = next((i for i in builder.inventory.items if i.template_id == dye_template
                                    and i.item_id in accessible and i.amount > 0), None)
                        if dye is not None:
                            builder.consume(dye.item_id, 1, preserve_item_id=True)
                        else:
                            price += PurchasePrice(rc=int(definition['ApplyPriceRC']),
                                                   vc=int(definition['ApplyPriceVC']))
                        item.field_20, item.field_1c, item.field_28 = packed, color_id, desired
                    else:
                        item.field_20, item.field_1c, item.field_28 = 0, 0, 'none'
            if item != before:
                updated.append(copy.deepcopy(item))
    paid, _ = debit_currency(currency, price)
    # Several slots can consume the same dye stack. Publish only its final
    # amount, and never recreate a stack exhausted by the last slot.
    changed_ids = {item.item_id for item in builder.changed_items}
    builder.changed_items = [copy.deepcopy(item) for item in builder.inventory.items
                             if item.item_id in changed_ids]
    return builder.finish(), paid, tuple(updated)
