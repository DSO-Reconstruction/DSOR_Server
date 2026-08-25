"""What the tutorial dungeon actually contains, from the client's own map data.

``db/maps_a0001_start_tutorial_dun.sqlite`` carries the map's furniture:
``_Instance_SpawnPoint``, ``_Instance_NPC``, ``_Instance_TriggerVolume`` and
``_Instance_InteractExit``. Each row holds a 4x4 transform whose last column is the
position, in the same frame a creature's *description* uses — spawn point 5 sits at
(-62.82, 0.00, 53.48), which is bit-for-bit the position the recorded description of
actor 08 carries. That is what makes this usable rather than merely interesting.

Copied here rather than read at run time, so the server needs no game files, the
same way the experience and damage curves are.

Twenty-five spawn points, against the six creatures this server served from one
recorded batch. Three of them are champions and one is a health globe.
"""

from __future__ import annotations

#: (blueprint, x, elevation, y) in the description frame.
SPAWN_POINTS: list[tuple[str, float, float, float]] = [
    ('a0001_gen_anderworld_creature', 49.0, 5.0, 81.0),
    ('a0001_gen_anderworld_creature', 50.0, 5.0, 77.0),
    ('a0001_gen_anderworld_creature', 41.0, 8.0, 42.0),
    ('a0001_gen_anderworld_creature', 50.0, 8.0, 45.56),
    ('a0001_gen_anderworld_creature', 46.0, 9.0, 29.0),
    ('a0001_gen_anderworld_creature_1st_encounter', -62.82, 0.0, 53.48),
    ('a0001_champion_anderworld_creature_healing', 12.0, 3.0, 76.0),
    ('a0001_gen_anderworld_creature_1st_encounter', -47.83, 0.34, 53.02),
    ('a0001_gen_anderworld_creature_1st_encounter', -45.68, 0.34, 54.63),
    ('a0001_gen_anderworld_creature_2nd_loot', -11.0, 0.0, 68.0),
    ('a0001_gen_anderworld_creature', -9.0, 0.0, 55.0),
    ('a0001_gen_anderworld_creature', -14.0, 0.0, 58.0),
    ('a0001_gen_anderworld_creature', -31.14, 0.36, 52.48),
    ('a0001_gen_anderworld_creature', -30.91, 0.36, 55.34),
    ('a0001_gen_anderworld_creature', -8.0, 0.0, 61.0),
    ('a0001_gen_anderworld_creature_1st_loot', -28.59, 0.36, 53.8),
    ('a0001_normal_anderworld_minion_01', 51.0, 5.0, 72.0),
    ('a0001_tutorial_movement_target', -85.59, 1.08, 69.53),
    ('a0001_champion_undead_mage_01', 47.0, 9.0, -11.0),
    ('a0001_champion_anderworld_creature_openexit', 47.0, 9.0, -10.0),
    ('a0001_normal_anderworld_minion_01', 40.0, 9.0, 1.0),
    ('a0001_normal_anderworld_minion_01', 52.0, 9.0, 1.0),
    ('a0001_gen_anderworld_creature_healthglobe', 45.0, 9.0, -4.0),
    ('a0001_gen_anderworld_creature', 43.0, 9.0, -2.0),
    ('a0001_gen_anderworld_creature', 50.0, 9.0, -1.0),
]

#: The tutorial's script: each trigger volume, the event it fires and its category.
#: One is a Quest; the rest are Hints, which is what the tutorial's prompts are.
TRIGGERS: list[tuple[str, str, str]] = [
    ('a0001_tutorial_movement_main_end_trigger', '', 'Hint'),
    ('a0001_tutorial_attack_main_start_signal_trigger', '', 'Hint'),
    ('a0001_tutorial_experience_main_end_signal_trigger', '', 'Hint'),
    ('a0001_tutorial_NPCs_main_start_signal_trigger', '', 'Hint'),
    ('a0001_start_tutorial_02_explore_01', 'a0001explore01', 'Quest'),
    ('a0001_tutorial_cultist_sequence_start_trigger_01', '', 'Hint'),
    ('a0001_tutorial_cultist_sequence_start_trigger', '', 'Hint'),
    ('a0001_start_tutorial_healthglobe_trigger', '', 'Hint'),
    ('a0001_tutorial_exclamationmark_main_trigger', '', 'Hint'),
    ('a0001_enter_map_quest_signal_trigger', '', 'Hint'),
    ('a0001_start_tutorial_healthpoints_trigger', '', 'Hint'),
]

#: The four NPCs the map holds, and where they stand.
NPCS: list[tuple[str, float, float, float]] = [
    ('a0002_china_invisible_npc', 47.0, 5.11, -31.39),
    ('a0001_start_ranger', -13.49, 0.0, 79.11),
    ('a0001_start_starter', -80.05, 0.0, 54.59),
    ('a0001_start_spellweaver', 74.23, 5.0, -5.32),
]


def blueprints() -> set[str]:
    """Every creature blueprint the map wants spawned."""
    return {name for name, *_ in SPAWN_POINTS}
