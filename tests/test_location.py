"""The commands that draw an effect on the ground.

Nine skills of the four playable classes place *only* ground effects, so without this
they do nothing at all: the warrior's Fury of the Dragon and Banner of War, the mage's
arcanevortex, and five of the dwarf's. Fourteen use one at all.

The codec was written, then deleted with the rest of the effects code when that was
rebuilt, then restored -- and it survived the rebuild unharmed because it writes effect
*names*, not the wire indices that turned out to be off by sixteen.
"""

import pathlib

from dsor import location
from dsor.chain import walk
from dsor.skills import wire_of

DATA = pathlib.Path(__file__).resolve().parent.parent / "dsor/data"


def recorded(name: str) -> bytes:
    return (DATA / name).read_bytes()


def test_the_recorded_list_round_trips_to_the_byte():
    """The proof that the grammar is right rather than merely plausible.

    A BitStream carries no lengths, so a layout that is 32 bits out still reads names and
    still looks sensible. The first draft here put 460 bits behind the name instead of
    splitting 965 into 345 and 460, and it ended 32 bits past the actor. Only re-encoding
    catches that, because only re-encoding has to reproduce the bits it does not
    understand.
    """
    raw = recorded("location_effect_003e.bin")
    assert location.encode(location.decode(raw)) == raw


def test_the_recorded_list_names_earthquake_s_own_effects():
    got = location.decode(recorded("location_effect_003e.bin"))
    assert [entry.name for entry in got.entries][:5] == [
        "skill_earthquake_graphics",
        "skill_earthquake_aura",
        "skill_earthquake_shockwave_aura",
        "skill_earthquake_loop",
        "skill_earthquake_end",
    ]
    assert all(entry.shape == location.SPHERE for entry in got.entries)


def test_the_short_command_is_followed_by_a_list():
    raw = recorded("location_effect_003d.bin")
    got = location.decode(raw)
    assert got.id == location.REMOVE_LOCATION_EFFECT
    assert got.entries == []
    found, leftover = walk(raw, most=8)
    assert leftover == 0
    assert [c.id for c in found] == [0x003D, 0x003E]


def test_a_single_effect_command_carries_no_count():
    """0x003C's leading 16 bits are the entry's index, not a length. Reading them as a
    count asked for a 25,960-byte string."""
    raw = recorded("location_effect_003c.bin")
    found, leftover = walk(raw, most=8)
    assert leftover == 0
    assert [c.id for c in found] == [0x003C, 0x003C]
    seen = []
    for command in found:
        got = location.decode(raw, at=24 + command.start, known=command.id)
        assert len(got.entries) == 1
        seen.append((got.entries[0].index, got.entries[0].name))
        assert got.actor == 0x00010008
    assert seen == [
        (5, "talent_warrior_skill_earthquake_rain_aura"),
        (6, "talent_warrior_skill_earthquake_rain_impact"),
    ]


def test_the_database_names_the_same_effects_as_the_recording():
    recorded_names = [
        e.name for e in location.decode(recorded("location_effect_003e.bin")).entries
    ]
    from_database = location.placed_by(wire_of("earthquake"))
    assert from_database == recorded_names[: len(from_database)]
    assert len(from_database) == 5


def test_every_skill_that_places_something_builds_a_readable_message():
    """The nine that place only ground effects, and one that places none."""
    for skill, count in (
        ("earthquake", 5),
        ("defiance", 2),
        ("true_earthquake", 5),
        ("arcanevortex", 1),
        ("HeavyShot", 1),
        ("Grenade", 1),
        ("Trail", 1),
        ("HoverJump", 1),
        ("ShrapnelShot", 1),
    ):
        wire = wire_of(skill)
        built = location.for_skill(wire)
        assert built is not None, skill
        got = location.decode(built)
        assert [e.name for e in got.entries] == location.placed_by(wire), skill
        assert len(got.entries) == count, skill
        assert location.encode(got) == built, skill
        found, leftover = walk(built, most=8)
        assert leftover == 0 and [c.id for c in found] == [0x003E], skill


def test_a_skill_that_places_nothing_sends_nothing():
    for skill in ("frenzyshout", "spikedShield", "warshout", "angrystrike"):
        assert location.placed_by(wire_of(skill)) == []
        assert location.for_skill(wire_of(skill)) is None


def test_casting_puts_the_ground_command_on_the_wire():
    from dsor.gameplay import Position
    from dsor.world import World

    for skill, count in (("earthquake", 5), ("defiance", 2)):
        world = World()
        world.rules.mobs = 3
        world._ready()
        where = ("1.2.3.4", 5)
        player = world.player(where)
        player.in_world = True
        player.position = Position(x=24453, elevation=-31744, y=25595)
        world.resolve_attack(where, wire_of(skill))
        sent = [
            payload
            for _address, payload in world.tick()
            if payload[:3] == bytes([location.MULTI, 0x3E, 0x00])
        ]
        assert len(sent) == 1, f"{skill}: {len(sent)}"
        assert len(location.decode(sent[0]).entries) == count, skill


def test_the_switch_silences_the_ground():
    from dsor.gameplay import Position
    from dsor.world import World

    world = World()
    world.rules.mobs = 3
    world._ready()
    world.rules.location_effects = False
    where = ("1.2.3.4", 5)
    world.player(where).in_world = True
    world.player(where).position = Position(x=24453, elevation=-31744, y=25595)
    world.resolve_attack(where, wire_of("earthquake"))
    assert not [
        p for _a, p in world.tick() if p[:3] == bytes([location.MULTI, 0x3E, 0x00])
    ]
