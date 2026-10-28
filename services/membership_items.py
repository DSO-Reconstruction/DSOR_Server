"""Deluxe teaser items: using one extends the Deluxe membership.

The items are ``PaymentItem`` / ``ConsumablePaymentItem`` rows
``deluxe_teaser_<duration>[_consumable][_7d]`` ("Deluxe (1 day)"), whose
description reads "Use this item and enjoy a Deluxe account for a day. (If
you already have a Deluxe account, it will be extended by a day for free.)"
(db.paymentitem.table_deluxe_teaser_one_dayDesc).

CONTRACT: one use consumes one item, sets the membership to Deluxe and moves
its expiry to the later of now and the current Deluxe expiry, plus the
item's duration; the client gets the inventory change and a
MembershipUpdateCommand; premium bags follow (PremiumBagService).
FAILURE: "le deluxe ... je peux pas utiliser".
UNVERIFIED: an active Premium (tier 1) membership is replaced by Deluxe from
now, not converted; the original's handling of that case is unknown.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import re

from dso_server.protocol.gameplay_commands import LOCAL_PLAYER_ACTOR_ID
from dso_server.protocol.inventory_economy_protocol import InventoryMutationBuilder
from dso_server.protocol.membership_protocol import (
    DELUXE_MEMBERSHIP,
    MEMBERSHIP_UPDATE_COMMAND,
    membership_is_active,
    membership_update_from_expiry,
    parse_membership_expiry,
)
from dso_server.protocol.player_init_protocol import (
    INVENTORY_INFO_COMMAND, ServerCommand, encode_server_command_batch,
)
from dso_server.services.base import CharacterService

TEASER = re.compile(r"^deluxe_teaser_(one_day|three_days|one_week|two_weeks|one_month|one_year)(?:_|$)")
DAYS = {"one_day": 1, "three_days": 3, "one_week": 7, "two_weeks": 14, "one_month": 30, "one_year": 365}


def teaser_days(template_id: str) -> int | None:
    match = TEASER.match(str(template_id))
    return DAYS[match.group(1)] if match else None


def extended_expiry(membership_type: int, active: bool, expires_at: str, days: int,
                    now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    start = now
    if membership_type == DELUXE_MEMBERSHIP and membership_is_active(membership_type, active, expires_at, now=now):
        start = max(now, parse_membership_expiry(expires_at))
    return (start + timedelta(days=days)).isoformat()


def handle_membership_item_use(server, session, item_id: int) -> bool:
    """Use one Deluxe teaser; False when the item is not one."""
    with session.flow_lock:
        inventory = session.gameplay_inventory
        item = next((i for i in inventory.items if int(i.item_id) == int(item_id)), None) if inventory else None
        days = teaser_days(item.template_id) if item is not None else None
        if days is None:
            return False
        builder = InventoryMutationBuilder(inventory, session.gameplay_next_item_id)
        builder.consume(item_id)
        mutation = builder.finish()
        expires_at = extended_expiry(
            session.gameplay_membership_type, session.gameplay_membership_active,
            session.gameplay_membership_expires_at, days,
        )
        session.gameplay_inventory = mutation.inventory
        session.gameplay_next_item_id = mutation.next_item_id
        session.gameplay_membership_type = DELUXE_MEMBERSHIP
        session.gameplay_membership_expires_at = expires_at
        session.gameplay_membership_active = True
        packet, bits = encode_server_command_batch((
            ServerCommand(INVENTORY_INFO_COMMAND, mutation.delta, LOCAL_PLAYER_ACTOR_ID),
            ServerCommand(INVENTORY_INFO_COMMAND, mutation.final, LOCAL_PLAYER_ACTOR_ID),
            ServerCommand(MEMBERSHIP_UPDATE_COMMAND, membership_update_from_expiry(
                expires_at, membership_type=DELUXE_MEMBERSHIP, active=True,
            ), LOCAL_PLAYER_ACTOR_ID),
        ), include_terminator=True)
        template_id = item.template_id
    CharacterService._request_account_save(session, "deluxe teaser used", flush=True)
    CharacterService._send_world_loot_state_batch(session, packet, bits, label="deluxe-teaser-used")
    CharacterService._refresh_premium_bags(session)
    print(f"[membership] {template_id}: Deluxe until {expires_at}")
    return True
