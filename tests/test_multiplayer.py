"""Two players in one world, each seeing the other.

The route is the one the creatures already take, and the live service walks it for
players too -- ``officiel4`` had strangers standing around the hub:

    server   0x0074   ActorsEnterVicinityCommand -- an actor came into range
    client   0x001C   ActorRequestCommand -- who is that?
    server   0x0021   NewRemotePlayerCommand -- this is who
    server   0x005F   MoveCommand, from then on
    server   0x001E   DiscardPlayerCommand -- and they are gone

Four messages before one of those 0x0021 in the capture sit the vicinity announcement
and the client's own question, which is how the order was established rather than
guessed.
"""

import pytest

from dsor import remote
from dsor.chain import walk
from dsor.gameplay import Position
from dsor.world import World
from raknet.bitstream import BitReader
from raknet.payload import bits_of

HERE = ("10.0.0.1", 1000)
THERE = ("10.0.0.2", 1001)


def a_world():
    world = World()
    world.rules.mobs = 0
    world.rules.enforce = False
    world._ready()
    for index, address in enumerate((HERE, THERE)):
        player = world.player(address)
        player.in_world = True
        player.position = Position(x=100 + index * 10, elevation=0, y=200)
        player.server_tick = 500
        player.level = 100
    return world


def test_two_players_get_actors_of_their_own():
    """The first keeps the one the recorded description names."""
    world = a_world()
    mine = world.player(HERE).actor
    theirs = world.player(THERE).actor
    assert mine != theirs
    assert len(mine) == len(theirs) == 4


def test_each_is_announced_to_the_other():
    world = a_world()
    world._outbox.clear()
    world.introduce(HERE)
    sent = world._drain()
    for address in (HERE, THERE):
        announced = [
            payload
            for to, payload in sent
            if to == address and payload[:3] == bytes([0x85, 0x74, 0x00])
        ]
        assert len(announced) == 1, f"{address}: {len(announced)}"


def test_a_player_asks_who_the_other_is_and_gets_both_messages():
    """A real ``NewRemotePlayerCommand`` named after them, and the look behind it.

    Two messages and not one. The description alone was accepted, acknowledged and drew
    nothing; the captures count 27 of the first against 62 of the second, and this sent
    none of the second.
    """
    world = a_world()
    theirs = world.player(THERE).actor
    described = world.describe_player(theirs)
    assert described is not None
    assert [
        int.from_bytes(bytes(message)[1:3], "little") for message in described
    ] == [remote.NEW_REMOTE_PLAYER, remote.REMOTE_PLAYER_INFO]
    for message in described:
        assert bytes(message)[0] == remote.MULTI
        # Each ends on its own actor and terminator, at its declared length.
        end = bits_of(message) - remote.TAIL_BITS
        assert BitReader(bytes(message), end).read_uint(32) == int.from_bytes(
            theirs, "little"
        )
        assert BitReader(bytes(message), bits_of(message) - 8).read_uint(8) == 0xFF
    found, leftover = walk(bytes(described[0]), most=4)
    assert leftover == 0
    assert [command.id for command in found] == [remote.NEW_REMOTE_PLAYER]
    assert remote.named(bytes(described[0])) == world.player_name(
        world.player(THERE)
    )
    # Nobody at that actor, nothing described.
    assert world.describe_player(bytes([0xEE, 0xEE, 0x01, 0x00])) is None


def test_the_look_is_not_byte_aligned_and_keeps_its_declared_length():
    """4,074 bits in 510 bytes. A 509-byte extraction truncated it by two, and the only
    thing that said so was the actor sitting at 4,034 where a byte-aligned tail would
    have put it at 4,032."""
    got = remote.info()
    assert bits_of(got) == remote.INFO_BITS == 4074
    assert len(got) == 510
    assert bits_of(got) % 8 == 2
    out = remote.appearance(b"\x15\x00\x01\x00")
    assert bits_of(out) == remote.INFO_BITS
    assert remote.occurrences(out, 0x00010015) == [4034]
    assert remote.actor_of(out) == 0x00010015


def test_two_players_on_one_account_are_told_apart():
    """Both sessions choose the same character, so the fallback name has to differ."""
    world = a_world()
    names = {world.player_name(world.player(a)) for a in (HERE, THERE)}
    assert len(names) == 2, names
    # A real name, once something knows one, wins.
    world.player(HERE).name = "Username"
    assert world.player_name(world.player(HERE)) == "Username"


def test_each_position_update_carries_the_other_player():
    world = a_world()
    world._outbox.clear()
    sent = dict(
        (address, payload)
        for address, payload in world.tick()
        if payload[:3] == bytes([0x85, 0x5F, 0x00])
    )
    assert set(sent) == {HERE, THERE}
    for address, other in ((HERE, THERE), (THERE, HERE)):
        payload = sent[address]
        assert world.player(other).actor in payload, "the other player's actor"
        # Two records of twenty bytes, their own and the other's, with the two-byte
        # separator the group format chains them by.
        assert len(payload) == 3 + 20 + 2 + 20


def test_a_lone_player_carries_nobody():
    world = a_world()
    world.forget(THERE)
    world._outbox.clear()
    sent = [p for _a, p in world.tick() if p[:3] == bytes([0x85, 0x5F, 0x00])]
    assert len(sent) == 1
    assert len(sent[0]) == 3 + 20


def test_leaving_says_goodbye_to_everybody_still_here():
    world = a_world()
    leaving = world.player(THERE).actor
    world._outbox.clear()
    world.forget(THERE)
    sent = world._drain()
    assert [(to, payload) for to, payload in sent] == [(HERE, remote.left(leaving))]
    assert len(remote.left(leaving)) == 8


def test_the_switch_makes_them_invisible_again():
    world = a_world()
    world.rules.remote_players = False
    world._outbox.clear()
    world.introduce(HERE)
    assert world._drain() == []
    assert world.visitor_records(HERE, 500) == []
    assert world.describe_player(world.player(THERE).actor) is None


def test_a_crowd_is_bounded_and_nobody_sees_themselves():
    """Past the bound everybody is shown the same block -- except the players in it."""
    world = World()
    world.rules.mobs = 0
    world._ready()
    world.rules.visible_players = 3
    addresses = [("10.0.1.%d" % index, 2000 + index) for index in range(10)]
    for index, address in enumerate(addresses):
        player = world.player(address)
        player.in_world = True
        player.position = Position(x=index, elevation=0, y=0)
    for address in addresses:
        got = world.visitor_records(address, 700)
        assert len(got) <= 3, len(got)
        mine = world.player(address).actor
        assert not any(mine == record[15:19] for record in got), address


@pytest.mark.parametrize("name", ["", "x" * (remote.LONGEST_NAME + 1)])
def test_a_name_that_will_not_fit_is_refused(name):
    with pytest.raises(ValueError):
        remote.with_name(remote.description(), name)


def test_the_recorded_description_is_what_the_service_sent():
    raw = remote.description()
    assert remote.named(raw) == remote.RECORDED_NAME
    assert remote.actor_of(raw) == remote.RECORDED_ACTOR
    found, leftover = walk(raw, most=4)
    assert leftover == 0 and [c.id for c in found] == [remote.NEW_REMOTE_PLAYER]
    # It carries an appearance, which is why it is replayed rather than built.
    assert b"ranger" in raw or b"mage" in raw or b"warrior" in raw


def test_the_trimmed_arrival_carries_no_strangers_at_all():
    """They used to be evicted one by one; now they are simply not sent.

    The recorded Kingshill batch holds 38 NewRemotePlayerCommand -- M3M3M3, Macdoe,
    Eredina, ThorinOakenshiel and thirty more, each confirmed by reading a
    length-prefixed name out of the 0x0021 body. Trimming the arrival to the player's
    own command leaves none of them, so there is nothing to discard.
    """
    from dsor.recorded import map_arrival

    content = dict(map_arrival("a0200_kingscity"))["content"]
    # The run of stranger actors, 0x00021D7D to 0x00022154, all below the actor the
    # state was recorded for.
    for actor in (0x00021D7D, 0x00021E71, 0x00022058, 0x00022152):
        assert actor.to_bytes(4, "little") not in content, hex(actor)
    assert len(content) < 1500, "the player's command, not the session"


def test_a_discard_is_eight_bytes_and_names_the_actor():
    for actor in (0x00021D7D, 0x00022152):
        message = remote.left(actor.to_bytes(4, "little"))
        assert len(message) == 8
        assert message[:3] == bytes([0x85, 0x1E, 0x00])
        assert message[3:7] == actor.to_bytes(4, "little")
        assert message[7] == 0xFF


# --- the description's transcribed grammar --------------------------------------


def test_the_body_starts_where_the_transcribed_widths_say_it_does():
    """Two independent routes to bit 190, which is what makes the transcription real.

    ``NewRemotePlayerCommand::Decode`` reads 5 bools, a 32-bit enum, a bool, two floats,
    a float and then the name's length prefix -- 134 bits of fields. The name was
    already known to sit at bit 190 from the wire. 190 - 134 = 56, and no other start
    puts them together.
    """
    assert remote.BODY_AT + 5 + 32 + 1 + 64 + 32 == remote.NAME_LENGTH_AT


def test_the_guild_is_the_string_behind_the_name():
    assert remote.guild_of(remote.description()).endswith("rder")


def test_the_template_row_is_found_by_walking_and_can_be_moved():
    blob = remote.description()
    assert remote.template_row_at(blob) == 398
    assert remote.template_row(blob) == 1
    moved = bytes(remote.with_template_row(blob, 7))
    assert remote.template_row(moved) == 7
    # And nothing else moved with it.
    assert remote.named(moved) == remote.named(blob)
    assert remote.actor_of(moved) == remote.actor_of(blob)
    assert bits_of(moved) == bits_of(blob)


def test_the_row_is_still_found_after_the_name_changes_length():
    """It sits behind two length-prefixed strings, so it has no fixed offset."""
    for name in ("Yx", "Y" * 24):
        renamed = bytes(remote.with_name(remote.description(), name))
        assert remote.named(renamed) == name
        assert remote.template_row(renamed) == 1
        assert remote.template_row_at(renamed) == 398 + (len(name) - 7) * 8


# --- the actor is written twice --------------------------------------------------


def test_the_description_writes_its_actor_twice():
    """Once in the body, once in the chained tail. A 32-bit window matching by chance
    is about one in a million, so twice means written twice."""
    blob = remote.description()
    assert remote.occurrences(blob, remote.RECORDED_ACTOR) == [3918, 4432]
    assert 4432 == bits_of(blob) - remote.TAIL_BITS


def test_rebinding_rewrites_every_occurrence():
    """Rewriting only the tail is what made a second player invisible: the description
    announced the recording's actor while the vicinity announcement and every
    MoveCommand spoke about the new one, so nothing bound and nothing was drawn."""
    blob = remote.description()
    out = bytes(remote.with_actor(blob, 0x00010015))
    assert remote.occurrences(out, remote.RECORDED_ACTOR) == []
    assert remote.occurrences(out, 0x00010015) == [3918, 4432]
    assert remote.actor_of(out) == 0x00010015
    assert bits_of(out) == bits_of(blob)


def test_rebinding_leaves_everything_else_alone():
    blob = remote.description()
    out = bytes(remote.with_actor(blob, 0x00010015))
    assert remote.named(out) == remote.named(blob)
    assert remote.guild_of(out) == remote.guild_of(blob)
    assert remote.template_row(out) == remote.template_row(blob)


def test_a_built_player_carries_its_actor_everywhere():
    """Which is what world.describe_player hands the client.

    Both offsets are checked relative to the message rather than written down: the name
    is length-prefixed, so a name four bytes longer than the recording's moves the body
    field and the tail alike. That is the reason with_actor searches for the value
    instead of seeking to a number.
    """
    for raw, actor in ((b"\x15\x00\x01\x00", 0x00010015), (b"\x00\x01\x01\x00", 0x00010100)):
        out = bytes(remote.player(raw, "Username"))
        moved = 8 * (len("Username") - len(remote.RECORDED_NAME))
        assert remote.occurrences(out, actor) == [3918 + moved, 4432 + moved]
        assert remote.occurrences(out, actor)[1] == bits_of(out) - remote.TAIL_BITS
        assert remote.named(out) == "Username"
        assert remote.actor_of(out) == actor


def test_rebinding_to_the_actor_it_already_has_changes_nothing():
    blob = remote.description()
    out = bytes(remote.with_actor(blob, remote.RECORDED_ACTOR))
    assert out == blob
