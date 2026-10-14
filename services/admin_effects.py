"""Dedicated, reversible admin buffs and migration of legacy saved toggles."""

from dataclasses import replace
import struct


ADMIN_EFFECTS = {
    "speed": {
        "label": "Movement speed +100%",
        "resource": "admin_movement_speed",
        "description": "Adds up to 100% of base movement speed, limited by the client movement cap when combined with mounts or other bonuses.",
        # All shipped player classes have RunSpeed 5. MovementBonus is added
        # after capped MovementSpeed; relative MovementBonus is not supported.
        "values": (5.0, 0.0, 0.0, 0.0, 0.0),
        "modifiers": "Speed:$0,absolute,MovementBonus;",
        "icon_template": "item_undefeatables_set_travelspeed_buff",
        "legacy_resource": "pvp_talent_speed",
        "legacy_values": (1.0, 0.0, 0.0, 0.0, 0.0),
    },
    "attack_speed": {
        "label": "Attack speed +100%",
        "resource": "admin_attack_speed",
        "description": "Adds 100% of base attack speed through a native status effect.",
        "values": (1.0, 0.0, 0.0, 0.0, 0.0),
        "modifiers": "Speed:$0,relative,Skills;",
        "icon_template": "buff_attackspeed_relative",
        "legacy_resource": "pvp_talent_attackspeed",
        "legacy_values": (1.0, 0.0, 0.0, 0.0, 0.0),
    },
    "stun_immunity": {
        "label": "Stun immunity",
        "resource": "admin_stun_immunity",
        "description": "Immunity to effects in the Disabled status group.",
        "values": (0.0, 0.0, 0.0, 0.0, 0.0),
        "modifiers": "StatusEffectImmunityGroup:Disabled;",
        "icon_template": "pvp_talent_stunimmunity",
        "legacy_resource": "pvp_talent_stunimmunity",
        "legacy_values": (0.0, 0.0, 0.0, 0.0, 0.0),
    },
}


def matching_effects(entries, effect_id, values):
    return tuple(entry for entry in entries
                 if entry.effect_id == effect_id and entry.has_details
                 and entry.field_10 == 0 and entry.field_0c == 0
                 and not entry.field_26 and tuple(entry.values) == values)


def matching_admin_effects(entries, effect_id, preset):
    """Recognize full toggles and explicitly tagged, speed-limited instances."""
    entries = tuple(entries)
    full = matching_effects(entries, effect_id, preset["values"])
    if preset is not ADMIN_EFFECTS["speed"]:
        return full
    # Parameter 4 is unused by Speed:$0. Tag adjusted admin instances with
    # their requested +5 bonus, preserving unrelated custom/timed effects.
    limited = tuple(entry for entry in entries
                    if len(entry.values) == 5 and 0 <= entry.values[0] < 5.0
                    and tuple(entry.values[1:]) == (0.0, 0.0, 0.0, 5.0))
    return full + tuple(entry for entry in limited if matching_effects(
        (entry,), effect_id, tuple(entry.values)))


def limited_admin_speed_entries(entries, catalog, stats):
    """Return only admin entries whose bonus exceeds the native Move limit."""
    preset = ADMIN_EFFECTS["speed"]
    current = matching_admin_effects(entries, catalog.effect_ids.get(preset["resource"]), preset)
    if not current:
        return ()
    other_bonus = max(0.0, min(50.0, stats.movement_bonus_speed
                              - sum(entry.values[0] for entry in current)))
    headroom = 4.0 * stats.base_movement_speed - stats.movement_speed - other_bonus
    bonus = max(0.0, min(preset["values"][0], headroom))
    # Round down to a wire-representable float without exceeding the cap.
    # A small fixed grid also prevents repeated updates from float noise.
    bonus = int(bonus * 10000) / 10000
    wire = struct.pack("<f", bonus)
    rounded = struct.unpack("<f", wire)[0]
    if rounded > bonus:
        rounded = struct.unpack("<f", struct.pack("<I", struct.unpack("<I", wire)[0] - 1))[0]
    bonus = rounded
    values = preset["values"] if bonus == 5.0 else (bonus, 0.0, 0.0, 0.0, 5.0)
    return tuple(replace(entry, values=values) for entry in current if tuple(entry.values) != values)


def restore_admin_effects(entries, catalog):
    """Migrate saved admin toggles and rebase them to the new map's clock.

    Native timed buffs, talent-stack instances, and other parameter values
    never qualify as a legacy admin toggle. Preserve instance IDs so removal
    still targets the exact effect that initialization sent to the client.
    """
    restored = dict(entries)
    for preset in ADMIN_EFFECTS.values():
        effect_id = catalog.effect_ids.get(preset["resource"])
        if effect_id is None:
            continue
        current = matching_admin_effects(entries.values(), effect_id, preset)
        legacy = matching_effects(entries.values(),
                                  catalog.effect_ids.get(preset["legacy_resource"]),
                                  preset["legacy_values"])
        # Prefer the dedicated instance if a save contains both versions.
        for index, entry in enumerate(current + legacy):
            if index:
                restored.pop(entry.field_04, None)
            else:
                restored[entry.field_04] = replace(
                    entry, effect_id=effect_id, values=preset["values"],
                    field_08=0, field_0c=0, field_14=25, field_20=1,
                )
    return restored
