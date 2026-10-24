"""The account service publishes the static database to dsor clients.

The official client and its launcher never call these endpoints, and the
status document they read stays as it was (services/content.py).
"""

import hashlib
import json
import os
import struct
import urllib.error
import urllib.request
import zlib

import pytest

from dso_server.persistence.account_registry import AccountRegistry
from dso_server.services.accounts import AccountService
from dso_server.services.content import DOWNLOADS_PER_WINDOW


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "static.db4"
    path.write_bytes(b"SQLite format 3\x00" + bytes(range(256)) * 512)
    return path


def start(tmp_path, **options):
    registry = AccountRegistry(tmp_path / "accounts.sqlite3", password_iterations=10000)
    service = AccountService(host="127.0.0.1", port=0, registry=registry, **options)
    service.start()
    return service


@pytest.fixture
def service(tmp_path, database):
    svc = start(tmp_path, static_database_path=database)
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


def unpack(blob):
    assert blob[:4] == b"__ZN"
    data = zlib.decompress(blob[8:])
    assert len(data) == struct.unpack_from("<I", blob, 4)[0]
    return data


def test_content_names_the_database_and_the_status_document_is_unchanged(service, database):
    entry = get(service, "/api/content")["static_db"]
    assert entry["sha256"] == hashlib.sha256(database.read_bytes()).hexdigest()
    assert entry["size"] == database.stat().st_size
    assert 0 < entry["packed_size"] < entry["size"]
    assert "static_db" not in get(service, "/api/status")


def test_only_a_signed_in_player_downloads_it(service):
    for document in ({}, {"session_id": "00000000-0000-0000-0000-000000000000"}):
        status, _, body = post(service, "/api/content/static-db", document)
        assert status == 401, body
    sid = session(service)
    service.registry.revoke(sid)
    assert post(service, "/api/content/static-db", {"session_id": sid})[0] == 401


def test_the_download_is_the_database_and_follows_its_changes(service, database):
    sid = session(service)
    status, headers, body = post(service, "/api/content/static-db", {"session_id": sid})
    assert status == 200
    assert unpack(body) == database.read_bytes()
    assert headers["X-Content-SHA256"] == hashlib.sha256(database.read_bytes()).hexdigest()
    before = get(service, "/api/content")["static_db"]["sha256"]
    database.write_bytes(b"SQLite format 3\x00" + b"admin buffs" * 999)
    stat = database.stat()
    os.utime(database, ns=(stat.st_atime_ns, stat.st_mtime_ns + 2_000_000_000))
    after = get(service, "/api/content")["static_db"]
    assert after["sha256"] != before
    assert after["sha256"] == hashlib.sha256(database.read_bytes()).hexdigest()
    status, _, body = post(service, "/api/content/static-db", {"session_id": sid})
    assert status == 200 and unpack(body) == database.read_bytes()


def test_downloads_are_limited_per_address(service):
    sid = session(service)
    for _ in range(DOWNLOADS_PER_WINDOW):
        assert post(service, "/api/content/static-db", {"session_id": sid})[0] == 200
    status, _, body = post(service, "/api/content/static-db", {"session_id": sid})
    assert status == 429
    assert json.loads(body)["retry_after"] > 0


def test_without_the_file_or_with_publishing_off_nothing_is_offered(tmp_path, monkeypatch):
    missing = start(tmp_path / "a", static_database_path=tmp_path / "missing.db4")
    try:
        assert get(missing, "/api/content")["static_db"] is None
        assert post(missing, "/api/content/static-db", {"session_id": session(missing)})[0] == 404
    finally:
        missing.stop()
    monkeypatch.setenv("DSO_PUBLISH_STATIC_DB", "0")
    off = start(tmp_path / "b", static_database_path=tmp_path / "missing.db4")
    try:
        assert off.static_db is None
        assert get(off, "/api/content")["static_db"] is None
    finally:
        off.stop()
