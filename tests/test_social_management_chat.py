"""Persisted ignores are enforced by the built-in chat service."""

from __future__ import annotations

import threading

import pytest

from dso_server.persistence.social_state import SocialStateStore
from dso_server.protocol.chat_protocol import decode_chat_text, encode_chat_text
from dso_server.runtime.transport import Session, SessionRegistry
from dso_server.services.chat import ChatServer


class _Socket:
    def sendto(self, data, addr):
        pass


@pytest.fixture
def chat(tmp_path):
    store = SocialStateStore(tmp_path / "social.json")
    server = ChatServer.__new__(ChatServer)
    server.sessions = SessionRegistry(threading.Lock())
    server.social_store = store
    server.gameplay_servers = ()
    server.region_id_is_override = True
    server.region_id = 5
    server.instance_id = 481
    server._audit_writer = None
    audit = []
    server._audit = lambda s, event, detail: audit.append((s.chat_user_name, event, detail))

    def login(user_id, name):
        session = Session(("127.0.0.1", 41000 + user_id), _Socket())
        session.connected = True
        session.chat_user_id = user_id
        session.chat_user_name = name
        session.chat_admin_name_color = False
        session.chat_is_admin = False
        session.chat_joined_channels.update(("map", "region"))
        received = []
        session.send_reliable_ordered = (
            lambda packet, *, track=True, label="", sink=received: sink.append(
                decode_chat_text(packet, from_client=False)
            )
        )
        session.received = received
        server.sessions[session.addr] = session
        return session

    yield server, store, login, audit
    store.close()


def say(server, session, text):
    server._on_char_op(session, encode_chat_text(text, from_client=True))


def test_a_whisper_to_someone_ignoring_the_sender_is_dropped(chat):
    server, store, login, audit = chat
    pest = login(1, "Pest")
    victim = login(2, "Victim")
    say(server, pest, "sw#Victim#ha#hello")
    assert victim.received == ["rw#Pest#ha#hello"]
    store.set_ignored(2, 1, "Pest")
    say(server, pest, "sw#Victim#ha#still there?")
    assert victim.received == ["rw#Pest#ha#hello"]
    assert ("Pest", "blocked", "whisper to user_id=2 (ignored)") in audit
    # Ignoring is recipient-side: the victim can still answer.
    say(server, victim, "sw#Pest#ha#go away")
    assert pest.received == ["rw#Victim#ha#go away"]


def test_channel_messages_skip_recipients_ignoring_the_sender(chat):
    server, store, login, _audit = chat
    pest = login(1, "Pest")
    victim = login(2, "Victim")
    bystander = login(3, "Bystander")
    store.set_ignored(2, 1, "Pest")
    say(server, pest, "sm#region#en#ha#hi all")
    assert victim.received == []
    assert bystander.received == ["rm#region#en#Pest#ha#hi all"]
    assert pest.received == ["rm#region#en#Pest#ha#hi all"]
    say(server, victim, "sm#region#en#ha#hello")
    assert pest.received[-1] == "rm#region#en#Victim#ha#hello"


def test_removing_the_ignore_restores_delivery(chat):
    server, store, login, _audit = chat
    pest = login(1, "Pest")
    victim = login(2, "Victim")
    store.set_ignored(2, 1, "Pest")
    store.set_ignored(2, 1, "", remove=True)
    say(server, pest, "sw#Victim#ha#sorry")
    assert victim.received == ["rw#Pest#ha#sorry"]
