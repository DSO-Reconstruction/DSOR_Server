"""The level command, and why a status effect needs it.

The long search ended here. Everything about the status effect message was right --
the codec reproduces all 313,301 real ones byte for byte, the database is the client's
own file down to its md5, the wire index mapping is uniquely best at rowid-1, the
message is field-for-field the live service's, and the messages provably arrived at the
client -- and nothing appeared.

The client's own binary says why. Its StatusEffectCommand handler looks the actor up in
ClientActorManager, then creates each effect through a function in
statuseffectmanager.cc that asserts:

    causer.isvalid()
    effectTemplate->IsValid()
    0 < causerLevel
    MaxActorLevel >= causerLevel

When the instance is not created the handler advances to the next element and logs
nothing at all. So an actor whose level the client never learned can hold no status
effect, silently, however correct the message.

And the level was the one thing this server never sent. Of everything the live service
sends and this one did not -- measured on captures that are certainly the service's --
two commands were left: 0x007C PlayerLevelUpdateCommand and 0x007B
ActorStatsUpdateCommand.
"""

import pathlib
import struct

from dsor import vitals

ACTOR = bytes.fromhex("15000100")

#: The live service's own PlayerLevelUpdateCommand, for the recorded level-104
#: character. Kept here as bytes because reproducing it is the test.
RECORDED_LEVEL = bytes.fromhex(
    "857c0068000000000000000000000000000000000015000100ff"
)

#: And its ActorStatsUpdateCommand, carrying 75.0.
RECORDED_STATS = bytes.fromhex("857b00d0dd0600000000000000964215000100ff")


def test_the_level_command_is_the_recorded_one():
    assert vitals.player_level(104, ACTOR) == RECORDED_LEVEL


def test_the_stats_command_is_the_recorded_one():
    assert vitals.actor_stats(450000, 75.0, ACTOR) == RECORDED_STATS


def test_the_level_is_where_the_recording_puts_it():
    built = vitals.player_level(37, ACTOR)
    assert struct.unpack_from("<I", built, 3)[0] == 37
    assert built[7 : 7 + vitals.LEVEL_PADDING] == bytes(vitals.LEVEL_PADDING)
    assert built[-5:-1] == ACTOR
    assert built[-1] == vitals.TERMINATOR


def test_the_level_is_clamped_rather_than_trusted():
    """The client asserts ``MaxActorLevel >= causerLevel``, and an assert in a release
    build of Nebula3 is a message box and a dead client -- the operator has seen one."""
    assert struct.unpack_from("<I", vitals.player_level(0, ACTOR), 3)[0] == 1
    assert struct.unpack_from("<I", vitals.player_level(-5, ACTOR), 3)[0] == 1
    highest = struct.unpack_from(
        "<I", vitals.player_level(9999, ACTOR), 3
    )[0]
    assert highest == vitals.HIGHEST_LEVEL


def test_entering_the_world_announces_the_level():
    from dsor.gameplay import Position
    from dsor.world import World

    world = World()
    world.rules.mobs = 3
    world._ready()
    where = ("1.2.3.4", 5)
    player = world.player(where)
    player.in_world = True
    player.position = Position(x=24453, elevation=-31744, y=25595)
    sent = [
        payload
        for _address, payload in world.enter(where)
        if payload[:3] == bytes([vitals.MULTI, 0x7C, 0x00])
    ]
    assert len(sent) == 1, "the level goes out once, before the first tick pair"
    assert sent[0][-5:-1] == player.actor


def test_the_level_is_not_repeated_while_it_has_not_changed():
    """It is state, not a heartbeat."""
    from dsor.gameplay import Position
    from dsor.world import World

    world = World()
    world.rules.mobs = 3
    world._ready()
    where = ("1.2.3.4", 5)
    player = world.player(where)
    player.in_world = True
    player.position = Position(x=24453, elevation=-31744, y=25595)
    world.enter(where)
    again = [
        payload
        for _address, payload in world.enter(where)
        if payload[:3] == bytes([vitals.MULTI, 0x7C, 0x00])
    ]
    assert again == []
    player.level = 12
    world.announce_level(where)
    changed = [
        payload
        for _address, payload in world._drain()
        if payload[:3] == bytes([vitals.MULTI, 0x7C, 0x00])
    ]
    assert len(changed) == 1
    assert struct.unpack_from("<I", changed[0], 3)[0] == 12


# --------------------------------------------------------------- the client's clock


def test_the_client_tick_is_read_from_its_movement_command():
    """The field that only ever rises: 22,648 of 22,648 records of one session, from
    428 to 29,130. Four bytes little-endian at offset 9 of the body."""
    from server import CLIENT_TICK_AT, client_tick

    assert CLIENT_TICK_AT == 9
    assert client_tick(bytes.fromhex("43d8c5fe0117000080ac0100001400")) == 428
    assert client_tick(bytes.fromhex("38e0a8fe8d0b0000d6c17100001400")) == 29121
    assert client_tick(b"\x00" * 8) is None, "a short body states no tick"


def test_the_clock_is_the_client_s_as_it_stands():
    """Taken as it comes, not forward-only.

    Forward-only was tried and it is wrong: the client's clock starts again at nearly
    zero on a new session, and refusing to go back kept the previous session's value --
    35,278 while the client was at 10,000, which schedules every effect a thousand
    seconds ahead and shows nothing at all.

    Implausible values are still refused, because a misread body would otherwise throw
    the clock somewhere absurd.
    """
    from dsor.world import World

    world = World()
    where = ("1.2.3.4", 5)
    world.accept_clock(where, 5000)
    assert world.player(where).server_tick == 5000
    world.accept_clock(where, 400)
    assert world.player(where).server_tick == 400, "a new session starts over"
    for refused in (None, 0, -1, 0x80000000):
        world.accept_clock(where, refused)
        assert world.player(where).server_tick == 400


def test_an_effect_window_is_ahead_of_the_clock_it_was_built_on():
    """The condition the client actually applies, from its own code:

        create if |now - end| <= 5, or now < end, or start == end

    So an element built on the client's clock passes on the second test, and one built
    on a clock 5,561 ticks behind fails all three.
    """
    from dsor import statuseffect as se
    from dsor.gameplay import Position
    from dsor.skills import wire_of
    from dsor.world import World

    world = World()
    world.rules.mobs = 3
    world._ready()
    where = ("1.2.3.4", 5)
    player = world.player(where)
    player.in_world = True
    player.position = Position(x=24453, elevation=-31744, y=25595)
    world.accept_clock(where, 29130)
    world.resolve_attack(where, wire_of("frenzyshout"))
    world.resolve_attack(where, wire_of("frenzyshout"), travelled=True)
    sent = [
        payload
        for _address, payload in world.tick()
        if payload[:3] == bytes([se.MULTI, 0x4F, 0x00])
    ]
    assert sent
    for element in se.decode(sent[0]).elements:
        assert element.start == 29130, "built on the client's clock"
        assert element.end > 29130, "and ending in the client's future"
