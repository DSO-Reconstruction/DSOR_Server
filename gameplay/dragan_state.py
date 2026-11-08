"""Server-only, per-instance choices for Dragan event encounters."""

from __future__ import annotations

import hashlib


# Keep this mapping aligned with the installed event-map spawn records; these
# are placement node ids, not DB4 edits.
_CADOC_PARENT_NODES = {
    "e_dra_03_dun_gloomypastures_01": (9738, 9747, 9756, 9765),
    "e_dra_03_dun_ironforestnight_01": (12241, 12250, 12259, 12268),
    "e_dra_03_dun_jail_01": (14979, 14988, 14997, 15006),
    "e_dra_03_dun_storage_01": (12911, 12920, 12929, 12938),
}
_CADOC_SECRET_TARGET_PREFIXES = {
    "grave": "e_dra_03_dun_grave_",
    "wildforest": "e_dra_03_dun_wildforestnight_",
    "mystra": "e_dra_03_dun_mystranight_",
}

DRAGAN_RARE_BOSS_ABSENT_FLAG = "dragan_rare_boss_absent"
DRAGAN_RARE_BOSS_MAPS = frozenset({
    *_CADOC_PARENT_NODES,
    "e_dra_03_dun_mystranight_01",
    "e_dra_03_dun_wildforestnight_01",
})


def dragan_rare_boss_spawn_flag(
    map_id: str, instance_key: object,
) -> str | None:
    """Return a stable 15% rare Dragan boss roll for this map instance.

    The selected candidate is resolved against the map's authored spawn rows
    by local_monster_state, so the same roll also covers Cadoc placements.
    """

    normalized_map = str(map_id or "").strip().casefold()
    if normalized_map not in DRAGAN_RARE_BOSS_MAPS:
        return None
    digest = hashlib.sha256(
        f"dragan-rare-boss:{normalized_map}:{instance_key!r}".encode("utf-8")
    ).digest()
    if int.from_bytes(digest[:8], "big") % 100 >= 15:
        return DRAGAN_RARE_BOSS_ABSENT_FLAG
    return f"dragan_rare_boss_choice_{digest[8:24].hex()}"


def dragan_secret_portal_target_flag(
    map_id: str, instance_key: object,
) -> str | None:
    """Return one shared secret-dungeon destination for this map instance."""

    normalized_map = str(map_id or "").strip().casefold()
    if normalized_map not in _CADOC_PARENT_NODES:
        return None
    digest = hashlib.sha256(
        f"dragan-secret-portal:{normalized_map}:{instance_key!r}".encode("utf-8")
    ).digest()
    target = tuple(_CADOC_SECRET_TARGET_PREFIXES)[
        int.from_bytes(digest[:8], "big") % len(_CADOC_SECRET_TARGET_PREFIXES)
    ]
    return f"dragan_secret_portal_target_{target}"


def filter_dragan_secret_portal_exits(map_id, exits, active_event_flags):
    """Keep only the one authored secret portal destination selected per run."""

    if str(map_id or "").strip().casefold() not in _CADOC_PARENT_NODES:
        return tuple(exits)
    target = next(
        (
            str(flag).casefold().removeprefix(
                "dragan_secret_portal_target_"
            )
            for flag in active_event_flags
            if str(flag).casefold().startswith(
                "dragan_secret_portal_target_"
            )
        ),
        None,
    )
    prefix = _CADOC_SECRET_TARGET_PREFIXES.get(target)
    if prefix is None:
        return tuple(exits)
    secret_prefixes = tuple(_CADOC_SECRET_TARGET_PREFIXES.values())
    return tuple(
        definition for definition in exits
        if (
            not str(definition.destination_map_id or "").casefold().startswith(
                secret_prefixes
            )
            or str(definition.destination_map_id or "").casefold().startswith(
                prefix
            )
        )
    )
