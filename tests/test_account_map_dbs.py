"""The account service publishes the map databases to dsor clients.

NewNPC, PropInfo and ExitInfo name level instances by Guid, and a new client
build gives every map row a new one: the dsor client converts its maps from
this server's (services/content.py, MapDatabasesPublisher).
"""

import hashlib
import json
import struct
import urllib.error
import urllib.request
import zlib

import pytest

from dso_server.persistence.account_registry import AccountRegistry
from dso_server.services.accounts import AccountService
from dso_server.services.content import MAP_BUNDLE_MAGIC, MAP_DOWNLOADS_PER_WINDOW, map_bundle_sha256


@pytest.fixture
def data(tmp_path):
    (tmp_path / "db").mkdir()
    (tmp_path / "maps").mkdir()
    (tmp_path / "db" / "static.db4").write_bytes(b"SQLite format 3\x00" + b"static" * 64)
    for name, fill in (("a0200_kingscity", b"k"), ("a0000_char", b"c")):
        (tmp_path / "maps" / f"{name}.db4").write_bytes(b"SQLite format 3\x00" + fill * 4096)
    (tmp_path / "maps" / "a0200_kingscity.map").write_bytes(b"not a database")
    return tmp_path


def start(tmp_path, data, **options):
    registry = AccountRegistry(tmp_path / "accounts.sqlite3", password_iterations=10000)
    service = AccountService(host="127.0.0.1", port=0, registry=registry,
                             static_database_path=data / "db" / "static.db4", **options)
    service.start()
    return service


@pytest.fixture
def service(tmp_path, data):
    svc = start(tmp_path, data)
    try:
        yield svc
    finally:
        svc.stop()


def get(service, path):
    with urllib.request.urlopen(service.url + path, timeout=10) as response:
        return json.loads(response.read())


def post(service, path, document):
    request = urllib.request.Request(
        service.url + path, data=json.dumps(document).encode(), method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.headers, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.headers, error.read()


def session(service):
    service.registry.register("Hero", "secret123")
    return service.registry.login("Hero", "secret123").session_id


def read_bundle(blob):
    """name -> database, checking every file against its sha256."""
    assert blob[:8] == MAP_BUNDLE_MAGIC
    count, at = struct.unpack_from("<I", blob, 8)[0], 12
    files = {}
    for _ in range(count):
        length = struct.unpack_from("<H", blob, at)[0]
        name = blob[at + 2:at + 2 + length].decode()
        at += 2 + length
        digest = blob[at:at + 32]
        size, packed = struct.unpack_from("<II", blob, at + 32)
        at += 40
        part = blob[at:at + packed]
        at += packed
        assert part[:4] == b"__ZN" and struct.unpack_from("<I", part, 4)[0] == size
        database = zlib.decompress(part[8:])
        assert len(database) == size and hashlib.sha256(database).digest() == digest
        files[name] = database
    assert at == len(blob)
    return files


def test_content_names_the_map_bundle(service, data):
    entry = get(service, "/api/content")["maps"]
    databases = {p.name: p.read_bytes() for p in (data / "maps").glob("*.db4")}
    assert entry["files"] == 2
    assert entry["size"] == sum(map(len, databases.values()))
    assert entry["sha256"] == map_bundle_sha256([(n, hashlib.sha256(d).hexdigest()) for n, d in databases.items()])
    assert 0 < entry["packed_size"] < entry["size"]
    assert get(service, "/api/content")["static_db"] is not None


def test_only_a_signed_in_player_downloads_them(service):
    status, _, _ = post(service, "/api/content/maps", {"session_id": "not-a-session"})
    assert status == 401


def test_the_bundle_holds_every_map_database_and_follows_their_changes(service, data):
    sid = session(service)
    status, headers, body = post(service, "/api/content/maps", {"session_id": sid})
    assert status == 200
    files = read_bundle(body)
    assert sorted(files) == ["a0000_char.db4", "a0200_kingscity.db4"]
    assert files["a0200_kingscity.db4"] == (data / "maps" / "a0200_kingscity.db4").read_bytes()
    assert headers["X-Content-SHA256"] == get(service, "/api/content")["maps"]["sha256"]
    before = get(service, "/api/content")["maps"]["sha256"]
    (data / "maps" / "a0000_char.db4").write_bytes(b"SQLite format 3\x00" + b"x" * 5000)
    after = get(service, "/api/content")["maps"]
    assert after["sha256"] != before and after["size"] == 4112 + 5016
    _, _, body = post(service, "/api/content/maps", {"session_id": sid})
    assert read_bundle(body)["a0000_char.db4"].endswith(b"x" * 5000)


def test_downloads_are_limited_per_address(service):
    sid = session(service)
    for _ in range(MAP_DOWNLOADS_PER_WINDOW):
        assert post(service, "/api/content/maps", {"session_id": sid})[0] == 200
    status, _, body = post(service, "/api/content/maps", {"session_id": sid})
    assert status == 429
    assert json.loads(body)["retry_after"] > 0
    # The static database keeps its own allowance.
    assert post(service, "/api/content/static-db", {"session_id": sid})[0] == 200


def test_without_maps_or_with_publishing_off_nothing_is_offered(tmp_path, data, monkeypatch):
    for path in (data / "maps").glob("*.db4"):
        path.unlink()
    svc = start(tmp_path, data)
    try:
        assert get(svc, "/api/content")["maps"] is None
        assert post(svc, "/api/content/maps", {"session_id": session(svc)})[0] == 404
    finally:
        svc.stop()
    (data / "maps" / "a0000_char.db4").write_bytes(b"SQLite format 3\x00")
    monkeypatch.setenv("DSO_PUBLISH_MAPS", "0")
    off = tmp_path / "off"
    off.mkdir()
    svc = start(off, data)
    try:
        assert get(svc, "/api/content")["maps"] is None
        assert get(svc, "/api/content")["static_db"] is not None
    finally:
        svc.stop()
