"""Accounts, credentials and one session per account.

The capture is the reason these tests are shaped the way they are: **no password ever
crosses the wire**. Twelve 0x8A messages, one per tier per connection, each carrying an
account id and a 16-byte session GUID and nothing else, byte-identical across all
twelve. So the session id *is* the credential a game server checks, the password is
checked somewhere off the wire, and what a test can assert about the game side is
exactly this: the right session gets in, a wrong one does not, and a second client on
one account does not.
"""

from __future__ import annotations

import urllib.error
import urllib.parse
import urllib.request
import uuid

import pytest

import server as srv
from dsor import portal
from dsor.store import Store
from dsor.world import Rules


@pytest.fixture
def store(tmp_path):
    kept = Store(tmp_path / "characters.sqlite")
    yield kept
    kept.close()


# --- the store's own rules ------------------------------------------------------


def test_an_account_is_created_and_found_by_name_and_by_id(store):
    made = store.add_account("Username", "hunter2")
    assert store.account("Username").id == made.id
    assert store.account_by_id(made.id).name == "Username"
    assert store.account("nobody") is None


def test_the_password_is_not_stored(store):
    store.add_account("Username", "hunter2")
    row = store.db.execute("SELECT secret FROM accounts").fetchone()
    assert "hunter2" not in row["secret"]
    # salt$hash, and the salt is what makes two of the same password differ.
    assert row["secret"].count("$") == 1
    store.add_account("other", "hunter2")
    secrets = [r["secret"] for r in store.db.execute("SELECT secret FROM accounts")]
    assert secrets[0] != secrets[1]


def test_the_name_is_taken_only_once(store):
    store.add_account("Username", "hunter2")
    with pytest.raises(ValueError, match="already an account"):
        store.add_account("Username", "something else")


def test_signing_in_needs_the_password(store):
    store.add_account("Username", "hunter2")
    assert store.sign_in("Username", "wrong") is None
    assert store.sign_in("nobody", "hunter2") is None
    assert store.sign_in("Username", "hunter2") is not None


def test_signing_in_again_invalidates_the_line_the_last_login_handed_out(store):
    store.add_account("Username", "hunter2")
    _, first = store.sign_in("Username", "hunter2")
    _, second = store.sign_in("Username", "hunter2")
    assert first != second
    account = store.account("Username").id
    assert not store.session_matches(account, uuid.UUID(first))
    assert store.session_matches(account, uuid.UUID(second))


def test_a_session_matches_nothing_before_the_first_login(store):
    made = store.add_account("Username", "hunter2")
    assert not store.session_matches(made.id, uuid.uuid4())
    assert not store.session_matches(made.id, None)
    assert not store.session_matches(999999, uuid.uuid4())


def test_a_character_is_created_with_its_account_and_read_back(store):
    made = store.add_account("Username", "hunter2")
    store.add_character(made.id, "Balrog", "warrior", level=7, map_name="a0200_kingscity")
    (only,) = store.characters_of(made.id)
    assert (only.name, only.character_class, only.level) == ("Balrog", "warrior", 7)
    assert only.map_name == "a0200_kingscity"
    assert store.characters_of(made.id + 1) == []


# --- exclusivity ---------------------------------------------------------------

FIRST = b"\x01" * 8
SECOND = b"\x02" * 8


def test_one_account_is_claimed_by_one_client():
    sessions = srv.Sessions()
    assert sessions.claim(42, FIRST)
    assert not sessions.claim(42, SECOND)
    # And the one holding it may say so again as often as it likes: a client crosses
    # three tiers on one GUID and reclaims on each.
    assert sessions.claim(42, FIRST)
    assert sessions.owner(42) == FIRST


def test_another_account_is_not_affected():
    sessions = srv.Sessions()
    assert sessions.claim(42, FIRST)
    assert sessions.claim(43, SECOND)


def test_a_released_claim_is_free_again():
    sessions = srv.Sessions()
    sessions.claim(42, FIRST)
    # Not the holder, so nothing happens: a disconnection cannot free someone else.
    sessions.release(42, SECOND)
    assert not sessions.claim(42, SECOND)
    sessions.release(42, FIRST)
    assert sessions.claim(42, SECOND)


def test_a_claim_a_killed_client_never_released_expires():
    """A client that is killed sends no disconnection, and must not lock the account."""
    sessions = srv.Sessions()
    assert sessions.claim(42, FIRST, now=1000.0)
    assert not sessions.claim(42, SECOND, now=1000.0 + sessions.linger)
    assert sessions.claim(42, SECOND, now=1000.0 + sessions.linger + 0.1)
    assert sessions.owner(42) == SECOND


# --- what the server does with a 0x8A ------------------------------------------


class _Connection:
    """Just enough of raknet.connection.Connection for _admitted."""

    def __init__(self, guid: bytes = FIRST) -> None:
        self.client_guid = guid
        self.state = None
        self.sent: list[bytes] = []


def _service(store, **rules) -> srv.Service:
    service = srv.Service(0, "test", role="map")
    service.store = store
    for name, value in rules.items():
        setattr(service.rules, name, value)
    service._queue = lambda connection, payload, sender, **kw: connection.sent.append(
        payload
    )
    return service


def _identity(account: int, session) -> object:
    from dsor.identity import Identity

    return Identity(account=account, session=session)


def test_an_account_this_server_never_issued_gets_in_by_default(store):
    """Which is what lets the recorded launcher line keep working."""
    service = _service(store)
    connection = _Connection()
    assert service._admitted(connection, _identity(112149298, uuid.uuid4()), ("a", 1))


def test_and_is_turned_away_when_the_server_says_accounts_only(store):
    service = _service(store, require_account=True)
    connection = _Connection()
    assert not service._admitted(
        connection, _identity(112149298, uuid.uuid4()), ("a", 1)
    )
    assert connection.sent == [bytes([0x15])]


def test_an_account_the_server_knows_must_present_the_session_it_issued(store):
    """Always, rule or no rule: an account anyone could name is no account at all."""
    made = store.add_account("Username", "hunter2")
    _, issued = store.sign_in("Username", "hunter2")
    service = _service(store)
    assert service._admitted(
        _Connection(), _identity(made.id, uuid.UUID(issued)), ("a", 1)
    )
    refused = _Connection()
    assert not service._admitted(refused, _identity(made.id, uuid.uuid4()), ("a", 1))
    assert refused.sent == [bytes([0x15])]
    assert not service._admitted(_Connection(), _identity(made.id, None), ("a", 1))


def test_a_second_client_on_one_account_is_refused(store):
    made = store.add_account("Username", "hunter2")
    _, issued = store.sign_in("Username", "hunter2")
    who = _identity(made.id, uuid.UUID(issued))
    service = _service(store)
    assert service._admitted(_Connection(FIRST), who, ("a", 1))
    second = _Connection(SECOND)
    assert not service._admitted(second, who, ("b", 2))
    assert second.sent == [bytes([0x15])]
    # Sharing is a decision, not an accident, and it has a flag.
    shared = _service(store, one_session_per_account=False)
    shared.sessions = service.sessions
    assert shared._admitted(_Connection(SECOND), who, ("b", 2))


def test_a_message_that_is_not_an_identity_is_let_through_unless_accounts_are_required(
    store,
):
    service = _service(store)
    assert service._admitted(_Connection(), None, ("a", 1))
    strict = _service(store, require_account=True)
    refused = _Connection()
    assert not strict._admitted(refused, None, ("a", 1))
    assert refused.sent == [bytes([0x15])]


def test_a_refused_client_is_dropped_rather_than_left_pinging(store):
    """Silence is worse: an ignored client reconnects for ever."""
    service = _service(store, require_account=True)
    connection = _Connection()
    sender = ("1.2.3.4", 5)
    service.connections[sender] = connection
    service.who[sender] = _identity(1, uuid.uuid4())
    service.record_sent.add(sender)
    service._admitted(connection, _identity(112149298, uuid.uuid4()), sender)
    assert sender not in service.connections
    assert sender not in service.who
    assert sender not in service.record_sent


# --- the portal ----------------------------------------------------------------


@pytest.fixture
def page(tmp_path):
    http = portal.serve(characters=str(tmp_path / "characters.sqlite"), port=0)
    yield f"http://127.0.0.1:{http.server_address[1]}"
    http.shutdown()
    http.server_close()


def _post(where: str, **fields) -> tuple[int, str]:
    data = urllib.parse.urlencode(fields).encode()
    try:
        with urllib.request.urlopen(where, data, timeout=5) as answer:
            return answer.status, answer.read().decode()
    except urllib.error.HTTPError as refused:
        return refused.code, refused.read().decode()


def test_the_portal_serves_a_form(page):
    with urllib.request.urlopen(page, timeout=5) as answer:
        body = answer.read().decode()
    assert 'action="/register"' in body
    assert 'action="/login"' in body


def test_registering_hands_back_a_launcher_line_with_the_credential_in_it(page, tmp_path):
    code, body = _post(
        page + "/register", name="Username", password="hunter2",
        character="Balrog", **{"class": "warrior"},
    )
    assert code == 200
    kept = Store(tmp_path / "characters.sqlite")
    try:
        account = kept.account("Username")
        assert account is not None
        assert f"-accid {account.id} " in body
        assert f"-sid {account.session}" in body
        (only,) = kept.characters_of(account.id)
        assert only.name == "Balrog"
    finally:
        kept.close()


def test_the_portal_refuses_a_name_that_is_taken(page):
    _post(page + "/register", name="Username", password="hunter2", character="Balrog")
    code, body = _post(
        page + "/register", name="Username", password="other", character="Other"
    )
    assert code == 400
    assert "already an account" in body


def test_the_portal_wants_a_character_with_the_account(page):
    code, body = _post(page + "/register", name="Username", password="hunter2")
    assert code == 400
    assert "character name" in body.lower()


def test_signing_in_through_the_portal_issues_a_new_session(page):
    _, first = _post(
        page + "/register", name="Username", password="hunter2", character="Balrog"
    )
    code, second = _post(page + "/login", name="Username", password="hunter2")
    assert code == 200
    assert "-sid " in second
    assert second.split("-sid ")[1] != first.split("-sid ")[1]


def test_a_wrong_password_says_nothing_about_which_accounts_exist(page):
    _post(page + "/register", name="Username", password="hunter2", character="Balrog")
    wrong = _post(page + "/login", name="Username", password="nope")
    missing = _post(page + "/login", name="nobody", password="nope")
    assert wrong[0] == missing[0] == 400
    assert "No account of that name with that password." in wrong[1]
    assert wrong[1] == missing[1]


def test_a_name_from_a_form_is_escaped_before_it_is_put_on_a_page(page):
    code, body = _post(
        page + "/register", name="a", password="b", character="<script>x</script>"
    )
    assert code == 200
    assert "<script>x</script>" not in body
    assert "&lt;script&gt;" in body
