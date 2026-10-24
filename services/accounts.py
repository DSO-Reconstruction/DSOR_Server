"""HTTP login/register service for remote launcher clients.

The RakNet services speak the game protocol and never see a password. This is
the front door beside them: launchers register accounts here, log in here, and
receive the session GUID plus the connection parameters they must hand to the
game client. The game services then validate that GUID through the shared
:class:`~account_registry.AccountRegistry`.

Endpoints (JSON in, JSON out):

    GET  /api/status      service description and connection parameters
    GET  /api/characters  roster with the account that claims each character
    POST /api/register    {username, password} -> account
    POST /api/login       {username, password} -> session + launch parameters
    POST /api/refresh     {session_id} -> rotated session
    POST /api/logout      {session_id} -> revoked
    POST /api/password    {username, password, new_password}
    POST /api/validate    {session_id} -> session, for the game server itself
    GET  /api/content     the static database the dsor client must hold
    POST /api/content/static-db  {session_id} -> that database, packed
                          (see :mod:`~dso_server.services.content`)

Run it beside the game services (``python -m dso_server``) or on its own host:

    python -m dso_server.services.accounts --host 0.0.0.0 --port 8080
"""

from __future__ import annotations

import argparse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import sys
import threading
import time
from urllib.parse import urlparse

from dso_server.paths import DEFAULT_ACCOUNT_DATABASE_PATH as DEFAULT_REGISTRY_PATH
from dso_server.paths import DEFAULT_STATIC_DB_PATH
from dso_server.services.content import (
    CONTENT_VERSION,
    DOWNLOAD_WINDOW_SECONDS,
    DOWNLOADS_PER_WINDOW,
    StaticDatabasePublisher,
)

from dso_server.persistence.account_registry import (
    AccountDisabledError,
    AccountRegistry,
    AccountRegistryError,
    CharacterClaimedError,
    InvalidCredentialsError,
    InvalidPasswordError,
    InvalidUsernameError,
    UnknownAccountError,
    UnknownSessionError,
    UsernameTakenError,
)


ACCOUNT_SERVICE_VERSION = 1
DEFAULT_ACCOUNT_PORT = 8080
MAX_REQUEST_BYTES = 8 * 1024

# A public login endpoint gets guessed at. Failures are counted per address.
LOGIN_FAILURE_LIMIT = 10
LOGIN_FAILURE_WINDOW_SECONDS = 300.0


_STATUS_FOR_ERROR = {
    InvalidUsernameError: HTTPStatus.BAD_REQUEST,
    InvalidPasswordError: HTTPStatus.BAD_REQUEST,
    UsernameTakenError: HTTPStatus.CONFLICT,
    CharacterClaimedError: HTTPStatus.CONFLICT,
    UnknownAccountError: HTTPStatus.NOT_FOUND,
    InvalidCredentialsError: HTTPStatus.UNAUTHORIZED,
    AccountDisabledError: HTTPStatus.FORBIDDEN,
    UnknownSessionError: HTTPStatus.UNAUTHORIZED,
}


class FailureThrottle:
    """Per-address failed-credential counter with a sliding window."""

    def __init__(
        self,
        limit: int = LOGIN_FAILURE_LIMIT,
        window_seconds: float = LOGIN_FAILURE_WINDOW_SECONDS,
    ) -> None:
        self.limit = max(1, int(limit))
        self.window_seconds = max(1.0, float(window_seconds))
        self._lock = threading.Lock()
        self._failures: dict[str, list[float]] = {}

    def _recent(self, address: str, now: float) -> list[float]:
        cutoff = now - self.window_seconds
        recent = [
            stamp for stamp in self._failures.get(address, ()) if stamp > cutoff
        ]
        if recent:
            self._failures[address] = recent
        else:
            self._failures.pop(address, None)
        return recent

    def blocked(self, address: str, *, now: float | None = None) -> float:
        """Seconds the caller must wait, or 0 when it may try again."""

        now = time.time() if now is None else now
        with self._lock:
            recent = self._recent(address, now)
            if len(recent) < self.limit:
                return 0.0
            return max(0.0, recent[0] + self.window_seconds - now)

    def record_failure(self, address: str, *, now: float | None = None) -> None:
        now = time.time() if now is None else now
        with self._lock:
            recent = self._recent(address, now)
            recent.append(now)
            self._failures[address] = recent

    def clear(self, address: str) -> None:
        with self._lock:
            self._failures.pop(address, None)


class AccountRequestHandler(BaseHTTPRequestHandler):
    """Routes one request against the owning service."""

    server_version = f"DSOAccountService/{ACCOUNT_SERVICE_VERSION}"
    protocol_version = "HTTP/1.1"
    # HTTP/1.1 keeps the connection open between requests. Without a
    # timeout a scanner that connects and goes quiet pins a thread forever.
    timeout = 30.0

    @property
    def service(self) -> "AccountService":
        return self.server.service  # type: ignore[attr-defined]

    # -- plumbing ----------------------------------------------------------

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        if self.service.verbose:
            print(
                f"[AccountService] {self.client_address[0]} "
                f"{format % args}"
            )

    def _client_ip(self) -> str:
        # A reverse proxy is a normal way to expose this; trust the forwarded
        # address only when the operator says the proxy is in front of us.
        if self.service.trust_forwarded_for:
            forwarded = self.headers.get("X-Forwarded-For", "")
            if forwarded:
                return forwarded.split(",")[0].strip()
        return self.client_address[0]

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > MAX_REQUEST_BYTES:
            raise ValueError("request body is too large")
        raw = self.rfile.read(length)
        try:
            document = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"request body is not valid JSON: {exc}")
        if not isinstance(document, dict):
            raise ValueError("request body must be a JSON object")
        return document

    def _respond(self, status: HTTPStatus, document: dict) -> None:
        body = json.dumps(document).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _fail(self, status: HTTPStatus, message: str, **extra) -> None:
        self._respond(status, {"error": message, **extra})

    # -- routing -----------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path.rstrip("/") or "/"
        if path in ("/", "/api", "/api/status"):
            self._respond(HTTPStatus.OK, self.service.status_document())
        elif path == "/api/characters":
            self._respond(HTTPStatus.OK, self.service.characters_document())
        elif path == "/api/content":
            try:
                document = self.service.content_document()
            except (OSError, ValueError, RuntimeError) as exc:
                print(f"[AccountService] static database unavailable: {exc}")
                self._fail(HTTPStatus.SERVICE_UNAVAILABLE, "the static database cannot be read")
                return
            self._respond(HTTPStatus.OK, document)
        else:
            self._fail(HTTPStatus.NOT_FOUND, f"no endpoint {path}")

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path.rstrip("/") or "/"
        handlers = {
            "/api/register": self._register,
            "/api/login": self._login,
            "/api/refresh": self._refresh,
            "/api/logout": self._logout,
            "/api/password": self._password,
            "/api/validate": self._validate,
            "/api/content/static-db": self._static_database,
        }
        handler = handlers.get(path)
        if handler is None:
            self._fail(HTTPStatus.NOT_FOUND, f"no endpoint {path}")
            return
        try:
            payload = self._read_json()
        except ValueError as exc:
            self._fail(HTTPStatus.BAD_REQUEST, str(exc))
            return
        try:
            handler(payload)
        except AccountRegistryError as exc:
            status = _STATUS_FOR_ERROR.get(
                type(exc), HTTPStatus.BAD_REQUEST
            )
            self._fail(status, str(exc))
        except ValueError as exc:
            self._fail(HTTPStatus.BAD_REQUEST, str(exc))

    # -- endpoints ---------------------------------------------------------

    def _register(self, payload: dict) -> None:
        if not self.service.registration_open:
            self._fail(
                HTTPStatus.FORBIDDEN, "registration is closed on this server"
            )
            return
        address = self._client_ip()
        wait = self.service.throttle.blocked(address)
        if wait > 0:
            self._fail(
                HTTPStatus.TOO_MANY_REQUESTS,
                f"too many attempts; retry in {wait:.0f}s",
                retry_after=wait,
            )
            return
        account = self.service.registry.register(
            str(payload.get("username", "")),
            str(payload.get("password", "")),
            client_ip=address,
        )
        claimed = self.service.claim_free_character(account.username)
        if claimed is not None:
            account = self.service.registry.account_by_id(account.account_id)
        print(
            f"[AccountService] registered {account.username!r} as "
            f"accid {account.account_id} from {address}"
        )
        self._respond(
            HTTPStatus.CREATED,
            {"account": account.to_document(), "claimed_character": claimed},
        )

    def _login(self, payload: dict) -> None:
        address = self._client_ip()
        wait = self.service.throttle.blocked(address)
        if wait > 0:
            self._fail(
                HTTPStatus.TOO_MANY_REQUESTS,
                f"too many failed attempts; retry in {wait:.0f}s",
                retry_after=wait,
            )
            return
        try:
            session = self.service.registry.login(
                str(payload.get("username", "")),
                str(payload.get("password", "")),
                client_ip=address,
            )
        except (InvalidCredentialsError, AccountDisabledError):
            self.service.throttle.record_failure(address)
            raise
        self.service.throttle.clear(address)
        print(
            f"[AccountService] {session.username!r} logged in from {address}; "
            f"session {session.session_id}"
        )
        self._respond(
            HTTPStatus.OK,
            {
                "session": session.to_document(),
                "launch": self.service.launch_document(self.headers.get("Host")),
            },
        )

    def _refresh(self, payload: dict) -> None:
        session = self.service.registry.refresh(
            str(payload.get("session_id", "")), client_ip=self._client_ip()
        )
        self._respond(
            HTTPStatus.OK,
            {
                "session": session.to_document(),
                "launch": self.service.launch_document(self.headers.get("Host")),
            },
        )

    def _logout(self, payload: dict) -> None:
        revoked = self.service.registry.revoke(
            str(payload.get("session_id", ""))
        )
        self._respond(HTTPStatus.OK, {"revoked": bool(revoked)})

    def _password(self, payload: dict) -> None:
        address = self._client_ip()
        wait = self.service.throttle.blocked(address)
        if wait > 0:
            self._fail(
                HTTPStatus.TOO_MANY_REQUESTS,
                f"too many attempts; retry in {wait:.0f}s",
                retry_after=wait,
            )
            return
        try:
            account = self.service.registry.set_password(
                str(payload.get("username", "")),
                str(payload.get("password", "")),
                str(payload.get("new_password", "")),
            )
        except (InvalidCredentialsError, AccountDisabledError):
            self.service.throttle.record_failure(address)
            raise
        self._respond(HTTPStatus.OK, {"account": account.to_document()})

    def _validate(self, payload: dict) -> None:
        # Reserved for the game server; a shared token keeps the session
        # lookup off the public surface when one is configured.
        token = self.service.service_token
        if token and self.headers.get("X-Service-Token", "") != token:
            self._fail(HTTPStatus.FORBIDDEN, "service token required")
            return
        session = self.service.registry.validate(
            str(payload.get("session_id", ""))
        )
        if session is None:
            self._respond(HTTPStatus.OK, {"valid": False})
            return
        self._respond(
            HTTPStatus.OK, {"valid": True, "session": session.to_document()}
        )

    def _static_database(self, payload: dict) -> None:
        # The dsor client's copy of the database this server plays with
        # (services/content.py). Signed-in players only, a few per address.
        publisher = self.service.static_db
        if publisher is None:
            self._fail(HTTPStatus.NOT_FOUND, "this server publishes no static database")
            return
        if self.service.registry.validate(str(payload.get("session_id", ""))) is None:
            self._fail(HTTPStatus.UNAUTHORIZED, "sign in to download the static database")
            return
        address = self._client_ip()
        wait = self.service.static_db_throttle.blocked(address)
        if wait > 0:
            self._fail(
                HTTPStatus.TOO_MANY_REQUESTS,
                f"too many downloads; retry in {wait:.0f}s",
                retry_after=wait,
            )
            return
        try:
            packed = publisher.packed()
        except (OSError, ValueError, RuntimeError) as exc:
            print(f"[AccountService] static database unavailable: {exc}")
            self._fail(HTTPStatus.SERVICE_UNAVAILABLE, "the static database cannot be read")
            return
        if packed is None:
            self._fail(HTTPStatus.NOT_FOUND, "this server publishes no static database")
            return
        self.service.static_db_throttle.record_failure(address)
        sha256, body = packed
        print(f"[AccountService] static database {sha256[:12]} ({len(body)} bytes) to {address}")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-SHA256", sha256)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


# Internet scanners and abandoned launchers reset or drop the socket before
# or during a request. socketserver's default handle_error prints a banner
# and a full traceback for every one of them, which floods the console on a
# public port. Only errors that mean "the peer went away" are silenced;
# anything else is still a bug and keeps the traceback.
_PEER_GONE_ERRORS = (ConnectionError, TimeoutError)


class AccountHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer that logs peer disconnects as one quiet line."""

    daemon_threads = True

    def handle_error(self, request, client_address) -> None:
        exc = sys.exc_info()[1]
        if isinstance(exc, _PEER_GONE_ERRORS):
            service = getattr(self, "service", None)
            if service is not None and service.verbose:
                print(
                    f"[AccountService] {client_address[0]} "
                    f"dropped the connection: {exc!r}"
                )
            return
        super().handle_error(request, client_address)


class AccountService:
    """Threaded HTTP login/register endpoint over an account registry."""

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = DEFAULT_ACCOUNT_PORT,
        registry: AccountRegistry | None = None,
        registry_path: str | os.PathLike[str] | None = None,
        character_selection=None,
        public_host: str | None = None,
        gateway_port: int = 80,
        chat_port: int = 2191,
        chat_instance_id: int = 481,
        registration_open: bool = True,
        service_token: str | None = None,
        trust_forwarded_for: bool | None = None,
        server_name: str | None = None,
        verbose: bool = False,
        static_database_path: str | os.PathLike[str] | None = None,
        publish_static_database: bool | None = None,
    ) -> None:
        self.host = host
        self.port = int(port)
        self.registry = registry or AccountRegistry(
            registry_path
            or os.getenv("DSO_ACCOUNT_REGISTRY", str(DEFAULT_REGISTRY_PATH))
        )
        self._owns_registry = registry is None
        self.character_selection = character_selection
        self.public_host = public_host or os.getenv("DSO_PUBLIC_HOST") or ""
        # The client's primary endpoint (``-ip``/``-port``) is the auth
        # gateway; the chat endpoint (``-bica``/``-bicp``) shares its host.
        self.gateway_port = int(gateway_port)
        self.chat_port = int(chat_port)
        self.chat_instance_id = int(chat_instance_id)
        # Launchers show this instead of an address: a player never needs to
        # see where the server actually lives, and a visible address is an
        # invitation to point traffic at it.
        self.server_name = (
            server_name
            or os.getenv("DSO_SERVER_NAME")
            or "Drakensang Online"
        )
        self.registration_open = bool(registration_open)
        self.service_token = service_token or os.getenv(
            "DSO_ACCOUNT_SERVICE_TOKEN", ""
        )
        self.trust_forwarded_for = (
            bool(trust_forwarded_for)
            if trust_forwarded_for is not None
            else os.getenv("DSO_TRUST_FORWARDED_FOR", "") == "1"
        )
        self.verbose = bool(verbose)
        self.throttle = FailureThrottle()
        # The database the game services load, for dsor clients to copy
        # (GET /api/content); DSO_PUBLISH_STATIC_DB=0 stops publishing it.
        publish = (
            bool(publish_static_database)
            if publish_static_database is not None
            else os.getenv("DSO_PUBLISH_STATIC_DB", "1") != "0"
        )
        self.static_db = (
            StaticDatabasePublisher(static_database_path or DEFAULT_STATIC_DB_PATH)
            if publish else None
        )
        self.static_db_throttle = FailureThrottle(
            limit=DOWNLOADS_PER_WINDOW, window_seconds=DOWNLOAD_WINDOW_SECONDS
        )
        self.service_name = "AccountService"
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # -- documents ---------------------------------------------------------

    def roster(self) -> tuple:
        return tuple(getattr(self.character_selection, "characters", ()) or ())

    def claim_free_character(self, username: str) -> int | None:
        """Give a new account the first roster character nobody owns."""

        roster = self.roster()
        if not roster:
            return None
        claimed = self.registry.claimed_characters()
        for character in roster:
            character_id = int(character.character_id)
            if character_id not in claimed:
                self.registry.claim_character(username, character_id)
                return character_id
        return None

    def resolved_public_host(self, host_header: str | None = None) -> str:
        """The address a remote client should put in ``-ip`` and ``-bica``."""

        if self.public_host:
            return self.public_host
        if host_header:
            # Reuse the address the client already reached us on, which is
            # what a remote player has routable.
            name = host_header.rsplit(":", 1)[0].strip("[]")
            if name and name not in ("0.0.0.0", "::"):
                return name
        if self.host not in ("", "0.0.0.0", "::"):
            return self.host
        try:
            return socket.gethostbyname(socket.gethostname())
        except OSError:
            return "127.0.0.1"

    def launch_document(self, host_header: str | None = None) -> dict:
        return {
            "host": self.resolved_public_host(host_header),
            "gateway_port": self.gateway_port,
            "chat_port": self.chat_port,
            "instance_id": self.chat_instance_id,
        }

    def status_document(self) -> dict:
        return {
            "service": "dso-account-service",
            "name": self.server_name,
            "version": ACCOUNT_SERVICE_VERSION,
            "registration_open": self.registration_open,
            "accounts": self.registry.count_accounts(),
            "active_sessions": self.registry.count_active_sessions(),
            "session_ttl_seconds": self.registry.session_ttl_seconds,
            "launch": self.launch_document(),
        }

    def content_document(self) -> dict:
        """What a dsor client must hold to play here (services/content.py)."""
        return {
            "version": CONTENT_VERSION,
            "static_db": self.static_db.describe() if self.static_db else None,
        }

    def characters_document(self) -> dict:
        owners = self.registry.claimed_characters()
        return {
            "characters": [
                {
                    "character_id": int(character.character_id),
                    "name": str(character.name),
                    "level": int(getattr(character, "level", 1)),
                    "class_id": int(getattr(character, "class_id", 0)),
                    "map_id": str(getattr(character, "map_id", "")),
                    "owner": owners.get(int(character.character_id)),
                }
                for character in self.roster()
            ]
        }

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if self._httpd is not None:
            return
        httpd = AccountHTTPServer(
            (self.host, self.port), AccountRequestHandler
        )
        httpd.daemon_threads = True
        httpd.service = self  # type: ignore[attr-defined]
        self._httpd = httpd
        # Binding to port 0 in tests resolves to a real port only after bind.
        self.port = httpd.server_address[1]
        self._thread = threading.Thread(
            target=httpd.serve_forever,
            kwargs={"poll_interval": 0.2},
            daemon=True,
            name="account-service",
        )
        self._thread.start()

    def stop(self) -> None:
        httpd, self._httpd = self._httpd, None
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        if self._owns_registry:
            self.registry.close()

    @property
    def url(self) -> str:
        host = self.host if self.host not in ("", "0.0.0.0", "::") else "127.0.0.1"
        return f"http://{host}:{self.port}"


def main(argv: list[str] | None = None) -> int:
    """Run the account service on its own, without the game services."""

    parser = argparse.ArgumentParser(
        prog="python -m dso_server.services.accounts",
        description="Login and registration service for DSO launchers.",
    )
    parser.add_argument(
        "--host",
        default=os.getenv("DSO_ACCOUNT_HOST", "127.0.0.1"),
        help="bind address; use 0.0.0.0 to accept remote clients",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("DSO_ACCOUNT_PORT", str(DEFAULT_ACCOUNT_PORT))),
    )
    parser.add_argument(
        "--registry",
        default=os.getenv("DSO_ACCOUNT_REGISTRY", str(DEFAULT_REGISTRY_PATH)),
        help="SQLite account database",
    )
    parser.add_argument(
        "--public-host",
        default=os.getenv("DSO_PUBLIC_HOST"),
        help="address handed to clients for -bica (default: as reached)",
    )
    parser.add_argument(
        "--gateway-port",
        type=int,
        default=int(os.getenv("DSO_GATEWAY_PORT", "80")),
        help="auth gateway UDP port clients are told to dial first (-port)",
    )
    parser.add_argument(
        "--chat-port",
        type=int,
        default=int(os.getenv("DSO_CHAT_PORT", "2191")),
    )
    parser.add_argument(
        "--instance-id",
        type=int,
        default=int(os.getenv("DSO_CHAT_INSTANCE_ID", "481")),
    )
    parser.add_argument(
        "--name",
        default=os.getenv("DSO_SERVER_NAME"),
        help="server name shown by launchers in place of an address",
    )
    parser.add_argument(
        "--closed",
        action="store_true",
        help="reject new registrations",
    )
    parser.add_argument(
        "--static-db",
        default=str(DEFAULT_STATIC_DB_PATH),
        help="static database published to dsor clients (GET /api/content)",
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    service = AccountService(
        host=args.host,
        port=args.port,
        registry_path=args.registry,
        public_host=args.public_host,
        gateway_port=args.gateway_port,
        chat_port=args.chat_port,
        chat_instance_id=args.instance_id,
        registration_open=not args.closed,
        server_name=args.name,
        verbose=args.verbose,
        static_database_path=args.static_db,
    )
    service.start()
    print(f"Account service listening on http://{args.host}:{service.port}")
    print(f"  registry -> {service.registry.path}")
    print(f"  clients are told to reach the game at "
          f"{service.resolved_public_host()}:{service.gateway_port} "
          f"(chat {service.chat_port})")
    print("\nCtrl+C to stop\n")
    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        pass
    finally:
        service.stop()
        print("Stopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
