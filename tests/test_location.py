"""The commands that draw an effect on the ground.

Fury of the Dragon and Banner of War grant no status effect at all -- the client's own
``granted_by`` and ``inflicted_by`` columns are empty for both -- so the 0x004F path had
nothing to say about them, which is why one showed "aucun VFX" and the other "pas de
vfx juste le drapeau". What they produce is a location effect, and this is its wire
format.
"""

import pathlib

import pytest

from dsor import location
from dsor.chain import walk
from dsor.skills import wire_of

DATA = pathlib.Path(__file__).resolve().parent.parent / "dsor/data"


def recorded(name: str) -> bytes:
    return (DATA / name).read_bytes()


@pytest.mark.parametrize(
    "name,expected",
    [
        (
            "location_effect_003e.bin",
            [
                "skill_earthquake_graphics",
                "skill_earthquake_aura",
                "skill_earthquake_shockwave_aura",
                "skill_earthquake_loop",
                "skill_earthquake_end",
                "talent_warrior_skill_earthquake_rain_aura",
                "talent_warrior_skill_earthquake_rain_impact",
            ],
        ),
    ],
)
def test_a_recorded_list_decodes_to_its_names(name, expected):
    got = location.decode(recorded(name))
    assert [entry.name for entry in got.entries] == expected
    assert all(entry.shape == location.SPHERE for entry in got.entries)
    assert [entry.index for entry in got.entries] == list(range(len(expected)))


def test_the_recorded_list_round_trips_to_the_byte():
    """The proof that the grammar is right rather than merely plausible.

    A BitStream carries no lengths, so a layout that is 32 bits out still reads names
    and still looks sensible -- the first draft here put 460 bits behind the name
    instead of splitting 965 into 345 and 460, and it ended 32 bits past the actor. Only
    re-encoding catches that, because only re-encoding has to reproduce every bit
    including the ones the decoder does not understand.
    """
    raw = recorded("location_effect_003e.bin")
    assert location.encode(location.decode(raw)) == raw


def test_the_short_command_is_a_pair_of_fields_and_a_tail():
    raw = recorded("location_effect_003d.bin")
    got = location.decode(raw)
    assert got.id == location.REMOVE_LOCATION_EFFECT
    assert got.entries == []
    assert len(got.fields) == 2
    # It is followed in the recording by a 0x003E, which is what made the chained
    # command id's byte order matter.
    found, leftover = walk(raw, most=8)
    assert leftover == 0
    assert [c.id for c in found] == [0x003D, 0x003E]


def test_a_single_effect_command_carries_no_count():
    """0x003C's leading 16 bits are the entry's index, not a length.

    Reading them as a count asked for a 25,960-byte string. The recorded payload holds
    two of them chained, indices 5 and 6, continuing the numbering of the 0x003E.
    """
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
    """The check that ``LocationEffects`` is the right column.

    The database's C:1.0 entries for earthquake are the recording's first five names in
    the recording's order. The recording's two extra entries are the talent-gated rain
    effects, which the database marks C:0.0 -- the recorded character had the talent.
    """
    recorded_names = [e.name for e in location.decode(recorded("location_effect_003e.bin")).entries]
    from_database = location.placed_by(wire_of("earthquake"))
    assert from_database == recorded_names[: len(from_database)]
    assert len(from_database) == 5
    # The recording's two extra names are *not* in the skill's column at all, gated or
    # not: they come from the talent, which has its own LocationEffects. So the column
    # is the right source for what this server places, and the difference between the
    # recording and this server is exactly the recorded character's talent.
    everything = location.placed_by(wire_of("earthquake"), certain_only=False)
    assert set(recorded_names) - set(everything) == {
        "talent_warrior_skill_earthquake_rain_aura",
        "talent_warrior_skill_earthquake_rain_impact",
    }


def test_a_built_message_is_read_back_as_what_went_in():
    for skill in ("earthquake", "defiance"):
        built = location.for_skill(wire_of(skill))
        assert built is not None, skill
        got = location.decode(built)
        assert [e.name for e in got.entries] == location.placed_by(wire_of(skill))
        assert [e.index for e in got.entries] == list(range(len(got.entries)))
        assert location.encode(got) == built
        found, leftover = walk(built, most=8)
        assert leftover == 0, skill
        assert [c.id for c in found] == [0x003E], skill


def test_a_skill_that_places_nothing_sends_nothing():
    """Most skills. Sending an empty list would be a message that says nothing."""
    for skill in ("frenzyshout", "spikedShield", "warshout", "seismicslam", "battlecry"):
        assert location.placed_by(wire_of(skill)) == []
        assert location.for_skill(wire_of(skill)) is None


def test_casting_the_two_skills_puts_the_command_on_the_wire():
    from tests.test_effects import a_player

    for skill, count in (("earthquake", 5), ("defiance", 2)):
        world, sender = a_player()
        world._drain()
        # travelled=True is the deferred half. Both skills have a HitFrame -- 9 ticks
        # for earthquake, 6 for defiance -- so resolve_attack schedules and returns,
        # and the auras are laid when the blow lands.
        world.resolve_attack(sender, wire_of(skill), travelled=True)
        sent = [p for _a, p in world._drain() if p[:3] == b"\x85\x3e\x00"]
        assert len(sent) == 1, f"{skill}: {len(sent)} location commands"
        assert len(location.decode(sent[0]).entries) == count, skill


def test_the_switch_silences_the_ground_too():
    from tests.test_effects import a_player

    world, sender = a_player()
    world.rules.status_effects = False
    world._drain()
    world.resolve_attack(sender, wire_of("earthquake"), travelled=True)
    assert not [p for _a, p in world._drain() if p[:3] == b"\x85\x3e\x00"]
