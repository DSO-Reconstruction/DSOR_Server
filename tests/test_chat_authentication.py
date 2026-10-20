"""The chat service admits a character only to the account that owns it."""

import unittest
from types import SimpleNamespace

from dso_server.services.chat import ChatServer


class _Registry:
    def __init__(self, character_ids):
        self.record = SimpleNamespace(
            account_id=7, username="someone", character_ids=tuple(character_ids)
        )

    def validate(self, session_id):
        return self.record


def _service(character_ids):
    dropped = []
    service = SimpleNamespace(
        account_registry=_Registry(character_ids),
        _audit=lambda s, event, detail: None,
        drop_session=lambda s, reason: dropped.append(reason),
    )
    return service, dropped


def _hello(user_id):
    return SimpleNamespace(user_id=user_id, session_id=b"\x01" * 16)


class ChatAuthenticationTests(unittest.TestCase):
    def test_an_account_without_characters_cannot_chat_as_one(self):
        # It could, as any character id it named (an admin's included).
        service, dropped = _service(())
        session = SimpleNamespace()
        self.assertFalse(ChatServer._authenticate_hello(service, session, _hello(200000001)))
        self.assertEqual(len(dropped), 1)

    def test_a_character_of_another_account_is_refused(self):
        service, dropped = _service((200000002,))
        self.assertFalse(ChatServer._authenticate_hello(service, SimpleNamespace(), _hello(200000001)))
        self.assertEqual(len(dropped), 1)

    def test_an_owned_character_is_admitted(self):
        service, dropped = _service((200000001,))
        session = SimpleNamespace()
        self.assertTrue(ChatServer._authenticate_hello(service, session, _hello(200000001)))
        self.assertEqual(session.chat_account_id, 7)
        self.assertEqual(dropped, [])


if __name__ == "__main__":
    unittest.main()
