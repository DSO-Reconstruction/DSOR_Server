"""Characters, saved — and what they are keyed on."""

import uuid

import pytest

from dsor import identity
from dsor.store import SCHEMA, Store


# ── who is connecting ────────────────────────────────────────────────────────


def an_auth(account: int, session: bytes, character: int | None = None) -> bytes:
    """A 0x8A of the shape the client sends, short form or long."""
    head = bytes([0x8A, 0x11, 0x00]) + b"DrasaOnlineClient" + bytes([0xAA, 0x04])
    if character is None:
        middle = bytes([0x09, 0x02, 0x01]) + account.to_bytes(4, "little")
    else:
        middle = (
            bytes([0x0A, 0x01, 0x01])
            + character.to_bytes(4, "little")
            + bytes([0x02, 0x01])
            + account.to_bytes(4, "little")
        )
    return head + middle + bytes([0x03, 0x05, 0x10, 0x00]) + session + b"\x05\x03\x83\x00"


def test_the_account_id_is_read_out_of_the_message_the_client_already_sends():
    """Confirmed against the launcher's own arguments: -accid appears as that u32 in
    57 datagrams across eight sessions, and -sid as those 16 bytes in 682."""
    session = uuid.UUID("eac9dbc7-dc3c-466d-98ce-2c945b14fb1b")
    got = identity.parse(an_auth(112130866, session.bytes))
    assert got is not None
    assert got.account == 112130866
    assert got.session == session
    assert got.character is None
    assert got.key == "112130866"


def test_the_long_form_carries_the_chosen_character_too():
    """Sent to a map server and to login once a character has been chosen. One of the
    ids seen in the captures, 111886222, is the name of a file in the client's own
    directory — 481_111886222.xml — which is where that reading was confirmed."""
    session = uuid.uuid4()
    got = identity.parse(an_auth(112130866, session.bytes, character=111886222))
    assert got.account == 112130866
    assert got.character == 111886222
    assert got.key == "112130866:111886222"


def test_anything_that_is_not_an_identity_gives_none_and_not_a_guess():
    """A server that invents an account id writes one player's progress over
    another's."""
    assert identity.parse(b"") is None
    assert identity.parse(bytes([0x84, 0x01, 0x02])) is None, "not a 0x8A"
    assert identity.parse(bytes([0x8A]) + b"nothing useful here") is None
    # The anchor present but the message truncated before the session id.
    assert identity.parse(bytes([0x8A]) + b"\x03\x05\x10\x00" + b"\x00" * 4) is None
    # A zero account is not an account.
    assert identity.parse(an_auth(0, uuid.uuid4().bytes)) is None


def test_the_offset_moves_with_the_form_so_the_anchor_is_what_is_used():
    """25 in the short form and 31 in the long one."""
    session = uuid.uuid4().bytes
    short = an_auth(9, session)
    long = an_auth(9, session, character=7)
    assert len(long) == len(short) + 6
    assert identity.parse(short).account == identity.parse(long).account == 9


# ── the file ─────────────────────────────────────────────────────────────────


@pytest.fixture
def store(tmp_path):
    made = Store(tmp_path / "characters.sqlite")
    yield made
    made.close()


def a_character(store, key="1:2", **over):
    values = dict(
        key=key, account=1, character=2, level=15, experience=55_800,
        health=2000.0, max_health=2700.0, resource=60.0,
        position=(100, 0, 200), map_name="a0001_start_tutorial_dun",
    )
    values.update(over)
    store.save(**values)
    return store.load(key)


def test_a_character_survives_a_round_trip(store):
    saved = a_character(store)
    assert (saved.level, saved.experience) == (15, 55_800)
    assert (saved.health, saved.max_health, saved.resource) == (2000.0, 2700.0, 60.0)
    assert saved.position == (100, 0, 200)
    assert saved.map_name == "a0001_start_tutorial_dun"
    assert saved.saves == 1


def test_saving_again_replaces_and_counts(store):
    a_character(store)
    a_character(store, level=16, experience=70_700)
    saved = store.load("1:2")
    assert (saved.level, saved.experience) == (16, 70_700)
    assert saved.saves == 2, "the count is how a session's length shows"
    assert store.count == 1, "replaced, not duplicated"


def test_an_unknown_key_is_none_rather_than_an_empty_character(store):
    assert store.load("nobody") is None


def test_two_characters_of_one_account_are_kept_apart(store):
    a_character(store, key="1:2", account=1, character=2)
    a_character(store, key="1:3", account=1, character=3, level=30)
    a_character(store, key="9:9", account=9, character=9)
    mine = store.characters_of(1)
    assert [c.character for c in mine] == [2, 3]
    assert [c.level for c in mine] == [15, 30]
    assert store.count == 3


def test_a_character_with_no_position_is_allowed(store):
    saved = a_character(store, position=None)
    assert saved.position is None


def test_a_file_from_a_newer_server_is_refused_rather_than_half_read(tmp_path):
    path = tmp_path / "characters.sqlite"
    made = Store(path)
    made.db.execute("UPDATE meta SET value = ? WHERE key = 'schema'", (SCHEMA + 1,))
    made.close()
    with pytest.raises(RuntimeError, match="newer server"):
        Store(path)


def test_the_file_reopens_with_what_was_in_it(tmp_path):
    path = tmp_path / "characters.sqlite"
    first = Store(path)
    a_character(first)
    first.close()
    again = Store(path)
    assert again.load("1:2").level == 15
    again.close()


# ── the service ──────────────────────────────────────────────────────────────


def test_a_saved_character_wins_over_the_start_level(tmp_path):
    """Which is the point of saving one: --start-level is what a *new* character
    arrives with."""
    import server
    from dsor.gameplay import Position

    service = server.Service(port=30000, name="t", role="map", map_name="a0001")
    service.store = Store(tmp_path / "c.sqlite")
    service.rules.start_level = 1
    sender = ("127.0.0.1", 1)
    service.who[sender] = identity.Identity(account=1, character=2)
    service.store.save(
        key="1:2", account=1, character=2, level=30, experience=1_150_000,
        health=9000.0, max_health=9900.0, resource=25.0,
    )

    assert service.restore(sender)
    player = service.world.player(sender)
    assert player.level == 30
    assert player.experience == 1_150_000
    assert player.max_health == 9900.0
    service.store.close()


def test_a_peer_nobody_has_identified_is_not_saved(tmp_path):
    import server

    service = server.Service(port=30000, name="t", role="map", map_name="a0001")
    service.store = Store(tmp_path / "c.sqlite")
    sender = ("127.0.0.1", 1)
    service.world.player(sender)
    assert not service.persist(sender), "no identity, nothing to key it on"
    assert service.store.count == 0
    assert not service.restore(sender)
    service.store.close()


def test_saving_is_off_when_there_is_no_store():
    import server

    service = server.Service(port=30000, name="t", role="map", map_name="a0001")
    sender = ("127.0.0.1", 1)
    service.who[sender] = identity.Identity(account=1)
    service.world.player(sender)
    assert not service.persist(sender)
    assert not service.restore(sender)


def test_a_round_trip_through_the_service(tmp_path):
    import server
    from dsor.gameplay import Position

    service = server.Service(port=30000, name="t", role="map", map_name="a0001")
    service.store = Store(tmp_path / "c.sqlite")
    sender = ("127.0.0.1", 1)
    service.who[sender] = identity.Identity(account=42, character=7)

    player = service.world.player(sender)
    player.level = 100
    player.experience = 882_229_601
    player.health = 5400.0
    player.max_health = 6000.0
    player.resource = 60.0
    player.position = Position(100, 0, 200)
    assert service.persist(sender)

    # A new service, as if the server had been restarted.
    again = server.Service(port=30000, name="t", role="map", map_name="a0001")
    again.store = Store(tmp_path / "c.sqlite")
    again.who[sender] = identity.Identity(account=42, character=7)
    assert again.restore(sender)
    restored = again.world.player(sender)
    assert restored.level == 100
    assert restored.experience == 882_229_601
    assert restored.resource == 60.0
    service.store.close()
    again.store.close()
