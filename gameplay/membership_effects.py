"""Membership status effects shown in the player's buff bar."""

from datetime import datetime, timezone
import math
import os

from dso_server.gameplay.status_runtime_catalog import load_status_runtime_catalog
from dso_server.protocol.gameplay_protocol import StatusEffectEntry
from dso_server.protocol.membership_protocol import (
    DELUXE_MEMBERSHIP,
    PREMIUM_MEMBERSHIP,
    parse_membership_expiry,
)


# Off unless DSO_MEMBERSHIP_XP_BUFF=1: every new character is Deluxe for five
# years (services/player_init_state.py), so the buff would double everyone's XP.
MEMBERSHIP_XP_BUFF = os.getenv("DSO_MEMBERSHIP_XP_BUFF", "0").strip().lower() in {
    "1", "true", "yes",
}

_MEMBERSHIP_EFFECTS = {
    PREMIUM_MEMBERSHIP: ("Premium", 0.5),
    DELUXE_MEMBERSHIP: ("Deluxe", 1.0),
}


def membership_status_effect_ids(database_path):
    """Return the wire ids for all membership tiers modeled by this server."""

    catalog = load_status_runtime_catalog(database_path)
    return frozenset(
        catalog.effect_ids[resource_id]
        for resource_id, _bonus in _MEMBERSHIP_EFFECTS.values()
        if resource_id in catalog.effect_ids
    )


def membership_status_effect(
    database_path,
    membership_type,
    *,
    instance_id,
    actor_id,
    expires_at,
    server_tick=0,
    now=None,
):
    """Build the membership UI effect and optionally grant its XP bonus."""

    definition = _MEMBERSHIP_EFFECTS.get(int(membership_type))
    if definition is None:
        return None
    resource_id, configured_bonus = definition
    bonus = configured_bonus if MEMBERSHIP_XP_BUFF else 0.0
    effect_id = load_status_runtime_catalog(database_path).effect_ids.get(resource_id)
    if effect_id is None:
        return None
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    remaining_seconds = max(
        0.0,
        (parse_membership_expiry(expires_at) - current.astimezone(timezone.utc)).total_seconds(),
    )
    duration_ticks = min(0xFFFFFFFF, max(1, math.ceil(remaining_seconds * 25)))
    start_tick = int(server_tick) & 0xFFFFFFFF
    end_tick = (start_tick + duration_ticks) & 0xFFFFFFFF
    # Zero is reserved by the client for a permanent status effect.
    if end_tick == 0:
        duration_ticks -= 1
        end_tick = (start_tick + duration_ticks) & 0xFFFFFFFF
    return StatusEffectEntry(
        effect_id=int(effect_id),
        field_04=int(instance_id),
        field_0c=end_tick,
        field_14=25,
        field_08=start_tick,
        field_18=0,
        field_10=duration_ticks,
        field_1c=100,
        field_20=int(actor_id),
        field_25=False,
        field_26=False,
        field_39=True,
        has_details=True,
        # The static templates map these values to QuestExperience and XPGain.
        values=(bonus, bonus),
    )


__all__ = ["membership_status_effect", "membership_status_effect_ids"]
