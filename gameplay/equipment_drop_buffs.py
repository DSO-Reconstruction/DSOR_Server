"""Helpers for consumable buffs that guarantee equipment loot."""

from __future__ import annotations

import random
import sqlite3
from pathlib import Path

from dso_server.protocol.consumable_protocol import load_consumable_catalog


PREMIUM_EQUIPMENT_DROP_BUFF = "premium_equipment_drop_buff"


def is_event_equipment_chest(database_path, template_id: str) -> bool:
    """Whether an inventory chest has an authored event association."""

    path = Path(database_path)
    try:
        database = sqlite3.connect(
            f"file:{path.as_posix()}?mode=ro", uri=True,
        )
        try:
            row = database.execute(
                "SELECT EventId, ItemCategory FROM _Template_Item WHERE Id=?",
                (str(template_id),),
            ).fetchone()
        finally:
            database.close()
    except sqlite3.Error:
        return False
    return bool(
        row
        and str(row[0] or "").strip()
        and str(row[1] or "") in {
            "Lootbox", "LootBox", "LootGenItem", "LootBoxItem",
            "Chest", "Container",
        }
    )


def premium_equipment_drop_buff_active(database_path, status_effects) -> bool:
    """Whether the player currently has the Premium Equipment Elixir active."""

    definition = load_consumable_catalog(database_path).definition(
        PREMIUM_EQUIPMENT_DROP_BUFF
    )
    return bool(
        definition is not None
        and any(
            int(effect.effect_id) == int(definition.effect_id)
            for effect in status_effects
        )
    )


def premium_equipment_drop_buff_active_for_session(database_path, session) -> bool:
    """Resolve the consumable buff from a session's current status snapshot."""

    from dso_server.gameplay.status_modifier_runtime import (
        snapshot_active_status_entries,
    )

    with session.flow_lock:
        effects = snapshot_active_status_entries(
            session.gameplay_persistent_status_effects,
            session.gameplay_timed_status_effect_expiries,
            session.gameplay_runtime_status_effects,
            session.gameplay_item_set_effects,
            session.gameplay_socket_effects,
        )
    return premium_equipment_drop_buff_active(database_path, effects)


def roll_premium_equipment_reward(
    database_path,
    *,
    class_name: str,
    player_level: int,
    rng: random.Random,
):
    """Roll one level- and class-eligible Unique or Set equipment reward."""

    from dso_server.protocol.world_loot_protocol import roll_equipment_reward

    rarities = ["Unique", "Set"]
    rng.shuffle(rarities)
    last_error = None
    for rarity in rarities:
        try:
            return roll_equipment_reward(
                database_path,
                class_name=class_name,
                item_level=max(1, int(player_level)),
                rarity=rarity,
                rng=rng,
            )
        except ValueError as exc:
            last_error = exc
    raise ValueError(
        f"no Unique or Set equipment is available for {class_name!r} "
        f"at level {player_level}"
    ) from last_error
