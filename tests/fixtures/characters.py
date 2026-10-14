"""Explicit persisted characters for tests that exercise composed services."""

from pathlib import Path
import tempfile

from dso_server.persistence.account_state import AccountStateStore, CharacterProfile
from dso_server.persistence.social_state import SocialStateStore
from dso_server.services.character_server import CharacterServer


def character_profile(character_id=200_000_001, name="TestPlayer", **overrides):
    values = dict(
        character_id=character_id, name=name, map_id="a0200_kingscity",
        class_id=1, gender=0, hair_style=0, beard_style=0,
        body_style=0, body_stature=0, level=100,
    )
    values.update(overrides)
    return CharacterProfile(**values)


def character_server(test, profiles=None):
    directory = tempfile.TemporaryDirectory(prefix="dso-character-test-")
    test.addCleanup(directory.cleanup)
    store = AccountStateStore(Path(directory.name) / "players.sqlite3")
    test.addCleanup(store.close)
    for profile in ((character_profile(),) if profiles is None else profiles):
        store.put_profile(profile, flush=True)
    return CharacterServer(
        "127.0.0.2", account_store=store,
        social_store=SocialStateStore(Path(directory.name) / "social.json"),
    )
