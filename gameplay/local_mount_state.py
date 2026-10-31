"""Editable local mount state reconstructed from the retail protocol.

Mounts are sticker-book items, not separately spawned actors.  Activating a
mount sends ``UseStickerBookItemCommand`` (0x0135); the map server applies a
short cast status effect and, when it completes, the paired ride effect.  The
ride effect's static ``Mounted`` modifier creates the visual mount and changes
the player's movement speed.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
import sqlite3

from dso_server.protocol.gameplay_protocol import StatusEffect, StatusEffectEntry


@dataclass(frozen=True)
class MountDefinition:
    stickerbook_resource_id: str
    mount_resource_id: str
    cast_effect_id: int
    ride_effect_id: int
    cast_duration_seconds: float
    cast_duration_ticks: int
    ride_duration_ticks: int
    stickerbook_state: int
    stickerbook_favorite_position: int


@dataclass(frozen=True)
class MountEffectState:
    definition: MountDefinition
    effect_id: int
    instance_id: int
    start_tick: int
    duration_ticks: int


# Status-effect wire numbers are zero-based FixedArray indexes, while SQLite
# rowids are one-based. These ids are an older database's (cast row 4101, ride
# 4100); they move with each client build, so find_mount takes them from the
# active static.db4 and this entry only serves when that database lacks the
# mount. Its timings are the item's and the ride effect's, before
# with_duration_scale.
BLACK_BEAR_MOUNT = MountDefinition(
    stickerbook_resource_id="epicmount_black_bear_01",
    mount_resource_id="mount_black_bear_01",
    cast_effect_id=4100,
    ride_effect_id=4099,
    cast_duration_seconds=3.5,
    cast_duration_ticks=88,
    ride_duration_ticks=900_000,
    stickerbook_state=3,
    # The 2026-09-09 retail capture proves favorite state is encoded by
    # stickerbook_state (2=owned, 3=favorite).  The following wire field is
    # zero even for favorites; older builds incorrectly wrote slot number 6.
    stickerbook_favorite_position=0,
)

LOCAL_MOUNTS = {
    BLACK_BEAR_MOUNT.stickerbook_resource_id: BLACK_BEAR_MOUNT,
}


def with_duration_scale(definition: MountDefinition, scale: float) -> MountDefinition:
    """Apply the rider's StatusEffectGroupDuration on the Mount group.

    Cast and ride are both in that group. EVIDENCE: in the 2026-10-28 and
    2026-10-29 retail captures, players with Wisdom's mount talent at 15
    (-31%) cast in 60 ticks (3.5 s x 0.69) and ride for 621000 (900000 x 0.69);
    players without it ride for 900000.
    """

    scale = max(0.0, float(scale))
    cast_seconds = definition.cast_duration_seconds * scale
    return replace(
        definition,
        cast_duration_seconds=cast_seconds,
        cast_duration_ticks=max(1, round(cast_seconds * 25)),
        ride_duration_ticks=max(1, round(definition.ride_duration_ticks * scale)),
    )


def find_mount(
    resource_id: str,
    static_database_path: str | Path | None = None,
) -> MountDefinition | None:
    """The mount an item activates, its effect ids from the static database."""

    resource_id = str(resource_id)
    local = LOCAL_MOUNTS.get(resource_id)
    # Imported here: stickerbook_state imports this module.
    from dso_server.gameplay.stickerbook_state import (
        load_inventory_mount_definition,
    )
    from dso_server.paths import DEFAULT_STATIC_DB_PATH

    try:
        definition = load_inventory_mount_definition(
            static_database_path or DEFAULT_STATIC_DB_PATH, resource_id
        )
    except (sqlite3.Error, OSError):
        definition = None
    if definition is None:
        return local
    if local is not None:
        definition = replace(
            definition,
            stickerbook_state=local.stickerbook_state,
            stickerbook_favorite_position=local.stickerbook_favorite_position,
        )
    return definition


def begin_mount_cast(
    definition: MountDefinition,
    *,
    instance_id: int,
    start_tick: int,
) -> MountEffectState:
    return MountEffectState(
        definition=definition,
        effect_id=definition.cast_effect_id,
        instance_id=int(instance_id),
        start_tick=int(start_tick) & 0xFFFFFFFF,
        duration_ticks=definition.cast_duration_ticks,
    )


def begin_mount_ride(
    cast: MountEffectState,
    *,
    instance_id: int,
) -> MountEffectState:
    return MountEffectState(
        definition=cast.definition,
        effect_id=cast.definition.ride_effect_id,
        instance_id=int(instance_id),
        start_tick=(cast.start_tick + cast.duration_ticks) & 0xFFFFFFFF,
        duration_ticks=cast.definition.ride_duration_ticks,
    )


def mount_status_effect(
    state: MountEffectState,
    *,
    actor_id: int,
    active: bool,
) -> StatusEffect:
    """Build the generated StatusEffect body used to add/remove a mount state.

    ``active=False`` selects the generated removal branch for the same
    status-effect instance.
    """

    entry = _mount_status_effect_entry(state, actor_id=actor_id, active=active)
    return StatusEffect(False, (entry,))


def mount_transition_status_effects(
    cast: MountEffectState,
    ride: MountEffectState,
    *,
    actor_id: int,
) -> tuple[StatusEffect, StatusEffect]:
    """Build the ordered ride-add/cast-remove commands seen in retail."""

    if cast.definition != ride.definition:
        raise ValueError("mount cast and ride must use the same definition")
    ride_add = StatusEffect(
        False,
        (_mount_status_effect_entry(ride, actor_id=actor_id, active=True),),
    )
    cast_removal = _mount_status_effect_entry(
        cast, actor_id=actor_id, active=False
    )
    # Retail includes the completed cast removal twice in the second command.
    return ride_add, StatusEffect(False, (cast_removal, cast_removal))


def _mount_status_effect_entry(
    state: MountEffectState,
    *,
    actor_id: int,
    active: bool,
) -> StatusEffectEntry:
    duration = int(state.duration_ticks)
    start_tick = int(state.start_tick) & 0xFFFFFFFF
    is_ride = state.effect_id == state.definition.ride_effect_id
    end_tick = 0 if is_ride else (start_tick + duration) & 0xFFFFFFFF
    return StatusEffectEntry(
        effect_id=int(state.effect_id),
        field_04=int(state.instance_id),
        field_0c=end_tick,
        field_14=25 if is_ride else 0,
        field_08=start_tick,
        field_18=0,
        field_10=duration,
        field_1c=100,
        field_20=int(actor_id),
        field_25=False,
        field_26=False,
        field_39=is_ride,
        has_details=bool(active),
        values=(0.0, 0.0, 0.0, 0.0, 0.0) if active else (),
    )
