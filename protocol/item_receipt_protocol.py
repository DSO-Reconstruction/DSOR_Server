"""Retail item-received presentation messages for inventory grants."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from dso_server.protocol.gameplay_protocol import Message, MessageArgument
from dso_server.protocol.inventory_codec import InventoryInfo


ITEM_RECEIVED_MESSAGE_TYPE = 2
ITEM_RECEIVED_CATEGORY = 7
ITEM_RECEIVED_TEMPLATE_CATEGORIES = frozenset({
    "Currency",
    "ItemCurrency",
    "Key",
    "NoAutoUseCurrency",
})


@dataclass(frozen=True)
class ItemReceiptGrant:
    """One authoritative reward before it is mapped to a receipt item id."""

    template_id: str
    amount: int
    materialized: bool = True
    include_template_id: bool = False


@dataclass(frozen=True)
class ItemReceipt:
    item_id: int
    template_id: str
    amount: int
    include_template_id: bool = False


def item_received_message(receipt: ItemReceipt) -> Message:
    """Build the retail ``MessageCommand`` type-2 item toast body."""

    item_id = int(receipt.item_id)
    amount = int(receipt.amount)
    template_id = str(receipt.template_id)
    if not 0 < item_id <= 0xFFFFFFFF:
        raise ValueError("receipt item id is outside uint32 range")
    if not 0 < amount <= 0xFFFFFFFF:
        raise ValueError("receipt amount is outside uint32 range")
    if not template_id:
        raise ValueError("receipt template id is empty")
    arguments = [
        MessageArgument(1, ITEM_RECEIVED_CATEGORY),
        MessageArgument(1, amount),
        MessageArgument(4, item_id),
    ]
    if receipt.include_template_id:
        arguments.append(
            MessageArgument(2, string_value=template_id)
        )
    return Message(ITEM_RECEIVED_MESSAGE_TYPE, tuple(arguments))


def _original_item_amount(
    item_id: int,
    before_by_id: Mapping[int, object],
    replacements: Mapping[int, int],
) -> int:
    """Follow new->old rekeys to the stack that existed before the grant."""

    current = int(item_id)
    visited: set[int] = set()
    while current in replacements and current not in visited:
        visited.add(current)
        current = int(replacements[current])
    original = before_by_id.get(current)
    return max(0, int(getattr(original, "amount", 0)))


def build_item_receipts(
    before: InventoryInfo,
    after: InventoryInfo,
    changed_item_ids: Iterable[int],
    replacements: Mapping[int, int],
    grants: Iterable[ItemReceiptGrant],
    *,
    next_item_id: int,
) -> tuple[tuple[ItemReceipt, ...], int]:
    """Resolve reward amounts to final item ids used by the client.

    A stack update is published under a new item id and contains the final
    stack amount.  Receipt text must show only the awarded delta.  Virtual
    currencies have no inventory object, so retail reserves a transient item
    id and includes the template string for icon resolution.
    """

    before_by_id = {
        int(item.item_id): item for item in before.items
    }
    final_ids = {int(item.item_id) for item in after.items}
    changed_ids = {
        int(item_id) for item_id in changed_item_ids
    }
    available: dict[str, list[list[int]]] = defaultdict(list)
    for item in after.items:
        item_id = int(item.item_id)
        if item_id not in final_ids or item_id not in changed_ids:
            continue
        added_amount = max(
            0,
            int(item.amount)
            - _original_item_amount(item_id, before_by_id, replacements),
        )
        if added_amount:
            available[str(item.template_id)].append(
                [item_id, added_amount]
            )

    receipts: list[ItemReceipt] = []
    transient_id = int(next_item_id)
    for grant in grants:
        template_id = str(grant.template_id)
        remaining = int(grant.amount)
        if not template_id or remaining <= 0:
            raise ValueError("item receipt grant is invalid")
        if not grant.materialized:
            receipts.append(
                ItemReceipt(
                    transient_id,
                    template_id,
                    remaining,
                    True,
                )
            )
            transient_id += 1
            continue
        for candidate in available.get(template_id, ()):
            if remaining <= 0:
                break
            item_id, unreported = candidate
            if unreported <= 0:
                continue
            amount = min(remaining, unreported)
            receipts.append(
                ItemReceipt(
                    item_id,
                    template_id,
                    amount,
                    bool(grant.include_template_id),
                )
            )
            candidate[1] -= amount
            remaining -= amount
        if remaining:
            raise ValueError(
                f"inventory delta is missing {remaining}x {template_id!r}"
            )
    return tuple(receipts), transient_id


__all__ = [
    "ITEM_RECEIVED_CATEGORY",
    "ITEM_RECEIVED_MESSAGE_TYPE",
    "ITEM_RECEIVED_TEMPLATE_CATEGORIES",
    "ItemReceipt",
    "ItemReceiptGrant",
    "build_item_receipts",
    "item_received_message",
]
