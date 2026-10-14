"""Peer disconnects on the public account port must not flood the console.

Internet scanners open a TCP connection to port 8080 and reset it before or
during the request. socketserver's default ``handle_error`` prints a banner
and a full traceback for each one; the live server console filled up with
``ConnectionResetError: [Errno 104] Connection reset by peer``.
"""

from contextlib import redirect_stderr
import io
import json
import socket
import struct
import threading
import time
import urllib.request

import pytest

from dso_server.persistence.account_registry import AccountRegistry
from dso_server.services.accounts import (
    AccountRequestHandler,
    AccountService,
)


@pytest.fixture
def service(tmp_path):
    registry = AccountRegistry(tmp_path / "accounts.sqlite3")
    svc = AccountService(host="127.0.0.1", port=0, registry=registry)
    svc.start()
    try:
        yield svc
    finally:
        svc.stop()
        registry.close()


def _handle_error_stderr(server, exc: BaseException) -> str:
    """Run ``handle_error`` the way socketserver does: inside the except."""

    buf = io.StringIO()
    try:
        raise exc
    except type(exc):
        with redirect_stderr(buf):
            server.handle_error(None, ("203.0.113.9", 4242))
    return buf.getvalue()


def _wait_for_request_threads() -> None:
    deadline = time.monotonic() + 2.0
    time.sleep(0.05)
    while time.monotonic() < deadline and any(
        "process_request_thread" in t.name for t in threading.enumerate()
    ):
        time.sleep(0.02)


def test_peer_reset_is_silent(service, capsys):
    err = _handle_error_stderr(
        service._httpd, ConnectionResetError(104, "Connection reset by peer")
    )
    assert err == ""
    assert capsys.readouterr().out == ""


def test_peer_reset_is_one_line_when_verbose(service, capsys):
    service.verbose = True
    err = _handle_error_stderr(service._httpd, BrokenPipeError(32, "Broken pipe"))
    assert err == ""
    printed = capsys.readouterr().out
    assert printed.count("\n") == 1
    assert printed.startswith("[AccountService] 203.0.113.9 ")
    assert "Traceback" not in printed


def test_other_errors_keep_the_traceback(service):
    err = _handle_error_stderr(service._httpd, RuntimeError("real bug"))
    assert "Traceback" in err
    assert "real bug" in err


def test_reset_before_request_line_leaves_console_clean(service):
    buf = io.StringIO()
    with redirect_stderr(buf):
        sock = socket.create_connection(("127.0.0.1", service.port))
        # SO_LINGER with a zero timeout turns close() into a RST, which is
        # what the scanners in the live console send.
        sock.setsockopt(
            socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0)
        )
        sock.close()
        _wait_for_request_threads()
        # The service is still healthy afterwards.
        with urllib.request.urlopen(f"{service.url}/api/status", timeout=3) as r:
            assert json.load(r)["service"] == "dso-account-service"
    assert buf.getvalue() == ""


def test_idle_connection_is_closed_quietly(service, monkeypatch):
    # HTTP/1.1 keep-alive would otherwise hold the thread open forever for a
    # client that connects and never speaks.
    monkeypatch.setattr(AccountRequestHandler, "timeout", 0.2)
    buf = io.StringIO()
    with redirect_stderr(buf):
        sock = socket.create_connection(("127.0.0.1", service.port))
        sock.settimeout(3.0)
        assert sock.recv(1) == b""
        sock.close()
    assert buf.getvalue() == ""


def test_default_idle_timeout_is_bounded():
    assert AccountRequestHandler.timeout is not None
    assert 0 < AccountRequestHandler.timeout <= 60
