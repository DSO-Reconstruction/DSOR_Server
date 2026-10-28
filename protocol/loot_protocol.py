"""Database-backed opening of fixed payment-package loot boxes.

The client export does not contain the authoritative loot-list tables.  It
does, however, expose a small fixed subset through
``_Template_PaymentPackageShowList``.  Those rows are keyed by a payment
package id; ``_Template_PaymentPackages.LocaleId`` identifies the actual
``LootGenItem`` placed in inventory.

This module intentionally supports only that explicit subset.  Nested
``LootGenItem`` rewards are delivered as ordinary items and are not opened
recursively.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, replace
from pathlib import Path
import sqlite3

from dso_server.runtime import resident_content
from dso_server.protocol.commerce_protocol import (
    PurchaseItemDefinition,
    add_purchased_item,
    load_purchase_item_definition,
)
from dso_server.protocol.inventory_codec import InventoryInfo, ItemInfo
from dso_server.protocol.player_init_protocol import CurrencyChanged


@dataclass(frozen=True)
class PaymentPackageReward:
    definition: PurchaseItemDefinition
    amount: int


@dataclass(frozen=True)
class PaymentPackageContents:
    package_id: str
    box_template_id: str
    rewards: tuple[PaymentPackageReward, ...]


@dataclass(frozen=True)
class PaymentPackageOpenMutation:
    contents: PaymentPackageContents
    inventory: InventoryInfo
    currency: CurrencyChanged
    currency_changed: bool
    changed_items: tuple[ItemInfo, ...]
    changed_item_ids: tuple[int, ...]
    replacements: dict[int, int]
    delta: InventoryInfo
    final: InventoryInfo
    next_item_id: int


def _open_read_only(path: Path) -> sqlite3.Connection:
    if not resident_content.is_file(path):
        raise FileNotFoundError(f"static database not found: {path}")
    database = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    database.row_factory = sqlite3.Row
    return database


def _parse_fixed_contents(value: str) -> tuple[tuple[str, int], ...]:
    """Parse and aggregate ``template,amount;`` display-list entries."""

    totals: dict[str, int] = {}
    for raw_entry in value.split(";"):
        raw_entry = raw_entry.strip()
        if not raw_entry:
            continue
        template_id, separator, raw_amount = raw_entry.rpartition(",")
        template_id = template_id.strip()
        raw_amount = raw_amount.strip()
        if not separator or not template_id or not raw_amount:
            raise ValueError(
                f"malformed payment-package item entry {raw_entry!r}"
            )
        try:
            amount = int(raw_amount, 10)
        except ValueError as exc:
            raise ValueError(
                f"invalid payment-package item amount {raw_amount!r}"
            ) from exc
        if amount <= 0:
            raise ValueError(
                f"payment-package item amount must be positive: {raw_entry!r}"
            )
        totals[template_id] = totals.get(template_id, 0) + amount
    if not totals:
        raise ValueError("payment-package display list has no item entries")
    return tuple(totals.items())


def load_payment_package_contents(
    static_database_path: str | Path,
    box_template_id: str,
) -> PaymentPackageContents:
    """Resolve a LootGenItem's fixed, client-exposed payment-package contents."""

    path = Path(static_database_path)
    database = _open_read_only(path)
    try:
        rows = database.execute(
            "SELECT p.Id AS PackageId, s.Items AS Items "
            "FROM _Template_Item i "
            "JOIN _Template_PaymentPackages p ON p.LocaleId=i.Id "
            "JOIN _Template_PaymentPackageShowList s ON s.Id=p.Id "
            "WHERE i.Id=? AND i.ItemCategory='LootGenItem' "
            "ORDER BY p.Id",
            (box_template_id,),
        ).fetchall()
    finally:
        database.close()
    if not rows:
        raise ValueError(
            f"LootGenItem {box_template_id!r} has no fixed payment-package "
            "contents"
        )
    if len(rows) != 1:
        raise ValueError(
            f"LootGenItem {box_template_id!r} resolves to multiple payment "
            "packages"
        )

    parsed = _parse_fixed_contents(str(rows[0]["Items"] or ""))
    rewards = tuple(
        PaymentPackageReward(
            load_purchase_item_definition(path, template_id), amount
        )
        for template_id, amount in parsed
    )
    return PaymentPackageContents(
        package_id=str(rows[0]["PackageId"]),
        box_template_id=box_template_id,
        rewards=rewards,
    )


def _is_allocated(info: InventoryInfo, item_id: int) -> bool:
    return any((
        item_id in info.equipment_slots,
        item_id in info.storage_58,
        item_id in info.storage_80,
        item_id in info.storage_a8,
        item_id in info.storage_d0,
        item_id in info.field_f8,
        item_id in info.field_118,
        item_id in info.field_180,
    ))


def _replace_item_id(info: InventoryInfo, old_id: int, new_id: int) -> None:
    for values in (
        info.equipment_slots,
        info.storage_58,
        info.storage_80,
        info.storage_a8,
        info.storage_d0,
    ):
        if old_id in values:
            values[new_id] = values.pop(old_id)
    for values in (info.field_f8, info.field_118, info.field_180):
        for index, value in enumerate(values):
            if value == old_id:
                values[index] = new_id


def _remove_item_id(info: InventoryInfo, item_id: int) -> None:
    for values in (
        info.equipment_slots,
        info.storage_58,
        info.storage_80,
        info.storage_a8,
        info.storage_d0,
    ):
        values.pop(item_id, None)
    for values in (info.field_f8, info.field_118, info.field_180):
        values[:] = [value for value in values if value != item_id]


def _consume_one(
    info: InventoryInfo,
    item_id: int,
    expected_template_id: str,
    next_item_id: int,
) -> tuple[list[ItemInfo], list[int], dict[int, int], int]:
    item = next((item for item in info.items if item.item_id == item_id), None)
    if item is None:
        raise ValueError(f"inventory item {item_id} does not exist")
    if item.template_id != expected_template_id:
        raise ValueError(
            f"inventory item {item_id} is {item.template_id!r}, expected "
            f"{expected_template_id!r}"
        )
    if item.amount <= 0:
        raise ValueError(f"inventory item {item_id} has an invalid stack size")
    if not _is_allocated(info, item_id):
        raise ValueError(f"inventory item {item_id} has no allocation")

    if item.amount == 1:
        info.items.remove(item)
        _remove_item_id(info, item_id)
        if (
            item.template_id in info.field_1a0
            and not any(
                other.template_id == item.template_id
                and other.item_id in info.field_180
                for other in info.items
            )
        ):
            info.field_1a0.remove(item.template_id)
        return [], [item_id], {}, next_item_id

    replacement_id = next_item_id
    replacement_item = replace(
        item,
        item_id=replacement_id,
        amount=item.amount - 1,
        field_18=replacement_id & 0xFFFF,
    )
    info.items[info.items.index(item)] = replacement_item
    _replace_item_id(info, item_id, replacement_id)
    return (
        [replacement_item],
        [item_id],
        {replacement_id: item_id},
        next_item_id + 1,
    )


def _credit_currency(
    state: CurrencyChanged,
    template_id: str,
    amount: int,
) -> CurrencyChanged:
    if template_id == "money_rc":
        new_value = int(state.currency_type) + amount
        if new_value > 0xFFFFFFFF:
            raise ValueError("premium currency reward overflows uint32")
        return CurrencyChanged(
            new_value, state.amount, state.values, state.delta, True
        )
    if template_id == "money_vc_gold":
        new_value = int(state.amount) + amount
        if new_value > 0xFFFFFFFFFFFFFFFF:
            raise ValueError("gold currency reward overflows uint64")
        return CurrencyChanged(
            state.currency_type, new_value, state.values, state.delta, True
        )
    raise ValueError(
        f"unsupported direct currency reward template {template_id!r}"
    )


def open_payment_package(
    info: InventoryInfo,
    currency: CurrencyChanged,
    contents: PaymentPackageContents,
    item_id: int,
    *,
    next_item_id: int,
) -> PaymentPackageOpenMutation:
    """Atomically consume one known box and materialize all fixed rewards."""

    inventory = copy.deepcopy(info)
    changed_items, changed_item_ids, replacements, next_item_id = _consume_one(
        inventory,
        item_id,
        contents.box_template_id,
        next_item_id,
    )
    updated_currency = currency
    currency_changed = False

    for reward in contents.rewards:
        if reward.definition.item_category == "Currency":
            updated_currency = _credit_currency(
                updated_currency,
                reward.definition.template_id,
                reward.amount,
            )
            currency_changed = True
            continue
        mutation = add_purchased_item(
            inventory,
            reward.definition,
            reward.amount,
            next_item_id=next_item_id,
        )
        next_item_id = mutation.next_item_id
        changed_items.extend(mutation.changed_items)
        changed_item_ids.extend(mutation.replaced_item_ids)
        replacements.update(mutation.replacements)

    inventory.changed_item_ids = []
    inventory.field_158 = {}
    inventory.field_1e0 = False

    delta = copy.deepcopy(inventory)
    delta.items = copy.deepcopy(changed_items)
    delta.changed_item_ids = list(changed_item_ids)
    delta.field_158 = dict(replacements)
    delta.field_1e0 = True

    final = copy.deepcopy(inventory)
    final.items = []
    final.changed_item_ids = []
    final.field_158 = {}
    final.field_1e0 = False

    return PaymentPackageOpenMutation(
        contents=contents,
        inventory=inventory,
        currency=updated_currency,
        currency_changed=currency_changed,
        changed_items=tuple(changed_items),
        changed_item_ids=tuple(changed_item_ids),
        replacements=dict(replacements),
        delta=delta,
        final=final,
        next_item_id=next_item_id,
    )
