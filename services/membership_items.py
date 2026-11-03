"""Premium and Deluxe teaser items: using one extends that membership.

The items are ``PaymentItem`` / ``ConsumablePaymentItem`` rows
``<premium|deluxe>_teaser_<duration>[_consumable][_7d]`` ("Deluxe (1 day)"),
whose description reads "Use this item and enjoy a Deluxe account for a day.
(If you already have a Deluxe account, it will be extended by a day for
free.)" (db.paymentitem.table_deluxe_teaser_one_dayDesc).

CONTRACT: one use consumes one item and adds its duration to that plan
(protocol/membership_protocol.py ``extend_membership``): Premium time is
paused while Deluxe runs and resumes when Deluxe ends, so a Premium teaser
can be used under Deluxe too. The client gets the inventory change, both
plans' MembershipUpdateCommands (the current plan last) and the current
plan's buff (added, or the running one renewed) in the same packet, without a
map change; premium bags follow (PremiumBagService).
FAILURE: "le deluxe ... je peux pas utiliser"; the buff and its time only
changed after re-entering a map; Premium teasers did nothing.
"""

from __future__ import annotations

from dataclasses import replace
import re

from dso_server.gameplay.membership_effects import (
    membership_status_effect, membership_status_effect_ids,
)
from dso_server.protocol.gameplay_commands import LOCAL_PLAYER_ACTOR_ID
from dso_server.protocol.gameplay_protocol import STATUS_EFFECT_COMMAND, StatusEffect
from dso_server.protocol.inventory_economy_protocol import InventoryMutationBuilder
from dso_server.protocol.membership_protocol import (
    DELUXE_MEMBERSHIP,
    MEMBERSHIP_UPDATE_COMMAND,
    PREMIUM_MEMBERSHIP,
    extend_membership,
    membership_is_active,
    membership_updates,
)
from dso_server.protocol.player_init_protocol import (
    INVENTORY_INFO_COMMAND, ServerCommand, encode_server_command_batch,
)
from dso_server.runtime.context import server_time
from dso_server.services.base import CharacterService

TEASER = re.compile(
    r"^(premium|deluxe)_teaser_(one_day|three_days|one_week|two_weeks|one_month|one_year)(?:_|$)"
)
DAYS = {"one_day": 1, "three_days": 3, "one_week": 7, "two_weeks": 14, "one_month": 30, "one_year": 365}
TIERS = {"premium": PREMIUM_MEMBERSHIP, "deluxe": DELUXE_MEMBERSHIP}


def teaser_membership(template_id: str) -> tuple[int, int] | None:
    """(membership type, days) of a teaser item, None for anything else."""
    match = TEASER.match(str(template_id))
    return (TIERS[match.group(1)], DAYS[match.group(2)]) if match else None


def teaser_days(template_id: str) -> int | None:
    teaser = teaser_membership(template_id)
    return teaser[1] if teaser else None


def sync_membership_effect(session, database_path) -> tuple:
    """Match the running Premium/Deluxe buff to the session's membership.

    Call under the flow lock. Returns the StatusEffect entries to send: the
    current plan's buff renewed in place when it already runs (the client
    refuses a second instance at MaxStackSize 1), otherwise added, then any
    other membership buff removed.
    """

    last_move = session.gameplay_last_move
    server_tick = ((last_move.start_tick + 1) if last_move is not None
                   else server_time() // 40) & 0xFFFFFFFF
    effect_ids = membership_status_effect_ids(database_path)
    effects = session.gameplay_persistent_status_effects
    running = [effect for effect in effects.values() if int(effect.effect_id) in effect_ids]
    wanted = None
    if membership_is_active(session.gameplay_membership_type, session.gameplay_membership_active,
                            session.gameplay_membership_expires_at):
        wanted = membership_status_effect(
            database_path, session.gameplay_membership_type,
            instance_id=session.gameplay_next_status_effect_instance_id,
            actor_id=LOCAL_PLAYER_ACTOR_ID,
            expires_at=session.gameplay_membership_expires_at,
            server_tick=server_tick,
        )
    current = next((effect for effect in running
                    if wanted is not None and effect.effect_id == wanted.effect_id), None)
    entries = []
    if current is not None:
        renewed = replace(wanted, instance_id=current.instance_id)
        effects[renewed.instance_id] = renewed
        entries.append(replace(renewed, update_timing=True, has_details=False, values=()))
    elif wanted is not None:
        effects[wanted.instance_id] = wanted
        session.gameplay_next_status_effect_instance_id += 1
        entries.append(wanted)
    for effect in running:
        if effect is current:
            continue
        effects.pop(effect.instance_id, None)
        session.gameplay_timed_status_effect_expiries.pop(effect.instance_id, None)
        entries.append(replace(
            effect, has_details=False, values=(), update_timing=False, permanent=False,
            field_38=False, field_3c=0xFF, shape_values=(),
        ))
    return tuple(entries)


def handle_membership_item_use(server, session, item_id: int) -> bool:
    """Use one Premium or Deluxe teaser; False when the item is not one."""
    with session.flow_lock:
        inventory = session.gameplay_inventory
        item = next((i for i in inventory.items if int(i.item_id) == int(item_id)), None) if inventory else None
        teaser = teaser_membership(item.template_id) if item is not None else None
        if teaser is None:
            return False
        tier, days = teaser
        builder = InventoryMutationBuilder(inventory, session.gameplay_next_item_id)
        builder.consume(item_id)
        mutation = builder.finish()
        plan = extend_membership(
            session.gameplay_membership_type, session.gameplay_membership_expires_at,
            session.gameplay_membership_active, session.gameplay_membership_premium_expires_at,
            tier, days,
        )
        session.gameplay_inventory = mutation.inventory
        session.gameplay_next_item_id = mutation.next_item_id
        (session.gameplay_membership_type, session.gameplay_membership_expires_at,
         session.gameplay_membership_active, session.gameplay_membership_premium_expires_at) = plan
        effect_entries = sync_membership_effect(session, server.static_database_path)
        commands = [
            ServerCommand(INVENTORY_INFO_COMMAND, mutation.delta, LOCAL_PLAYER_ACTOR_ID),
            ServerCommand(INVENTORY_INFO_COMMAND, mutation.final, LOCAL_PLAYER_ACTOR_ID),
            *(ServerCommand(MEMBERSHIP_UPDATE_COMMAND, update, LOCAL_PLAYER_ACTOR_ID)
              for update in membership_updates(*plan)),
        ]
        status = StatusEffect(False, effect_entries) if effect_entries else None
        if status is not None:
            commands.append(ServerCommand(STATUS_EFFECT_COMMAND, status, LOCAL_PLAYER_ACTOR_ID))
        packet, bits = encode_server_command_batch(tuple(commands), include_terminator=True)
        template_id = item.template_id
    CharacterService._request_account_save(session, "membership teaser used", flush=True)
    CharacterService._send_world_loot_state_batch(session, packet, bits, label="membership-teaser-used")
    if status is not None and session.gameplay_presence_callback is not None:
        session.gameplay_presence_callback(session, ("status_effects", (status,)))
    CharacterService._refresh_premium_bags(session)
    print(f"[membership] {template_id}: current {plan[0]} until {plan[1]}, Premium until {plan[3] or '-'}")
    return True
