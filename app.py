"""Production application composition and lifecycle management."""

from __future__ import annotations

import os
import signal
import socket
import threading
import time
import traceback
from pathlib import Path

from dso_server.services.character_server import CharacterServer
from dso_server.runtime.context import (
    ACCOUNT_STATE_SAVE_INTERVAL_SECONDS,
    CHEST_CONTENTS_MAP_PATH,
    EVENT_SCHEDULE_PATH,
    HOST,
    STATUS_EFFECTS_DISABLED,
    VICTIM_STATUS_EFFECTS_DISABLED,
    AccountStateStore,
    SocialStateStore,
)
from dso_server.services.accounts import (
    DEFAULT_ACCOUNT_PORT,
    DEFAULT_REGISTRY_PATH,
    AccountService,
)
from dso_server.services.admin import AdminService, DEFAULT_ADMIN_PORT
from dso_server.services.chat import ChatServer
from dso_server.services.gateway import GatewayLoginServer
from dso_server.services.login_game import LoginGameServer
from dso_server.runtime.stall_watchdog import StallWatchdog
from dso_server.runtime.gc_tuning import ManagedGarbageCollector
from dso_server.runtime.timer_resolution import raise_timer_resolution
from dso_server.runtime.monster_runtime_diagnostics import worker_snapshot
from dso_server.runtime.maintenance import MaintenanceShutdown
from dso_server.runtime import resident_content
from dso_server.paths import (
    DEFAULT_PLAYER_DATABASE_PATH,
    DEFAULT_QUESTS_PATH,
    DEFAULT_SOCIAL_STATE_PATH,
    DEFAULT_STATIC_DB_PATH,
)
from dso_server.runtime.transport import (
    UNSPECIFIED_HOSTS,
    FrameBuilder,
    LoginServer,
    RakNetServer,
    Session,
)

from dso_server.persistence.account_registry import AccountRegistry


def _environment_flag(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _map_workers_busy() -> bool:
    """Whether any map simulation worker is currently advancing monsters.

    The managed collector prefers to run its full collection while no map is
    simulating, because that collection pauses every thread for as long as it
    takes to walk the heap.
    """

    for worker in worker_snapshot():
        thread = getattr(worker, "_thread", None)
        if (
            thread is not None
            and thread.is_alive()
            and getattr(worker, "diagnostic_last_result", None) is True
        ):
            return True
    return False


def resolve_advertised_host(bind_host: str, public_host: str | None = None) -> str:
    """The address clients are redirected to between services.

    Every hop after the auth gateway (login on 2190, the map service on
    2192, and each SwitchMapCommand) names the next server explicitly, so a
    server bound to ``0.0.0.0`` must not advertise its bind address: the
    client cannot connect to ``0.0.0.0:2190``. ``DSO_PUBLIC_HOST`` is what
    the operator knows is routable; a specific bind address is its own
    answer; a wildcard bind without either falls back to the host's primary
    address, which is right on a server with a public interface and wrong
    behind NAT, where the variable must be set.
    """

    public_host = (public_host or os.getenv("DSO_PUBLIC_HOST") or "").strip()
    if public_host:
        return public_host
    if bind_host not in UNSPECIFIED_HOSTS:
        return bind_host
    try:
        return socket.gethostbyname(socket.gethostname())
    except OSError:
        return "127.0.0.1"


class PeriodicAccountSaver:
    """Publish active gameplay snapshots at a fixed interval."""

    def __init__(
        self,
        store: AccountStateStore,
        servers: tuple[RakNetServer, ...],
        interval_seconds: float,
    ) -> None:
        self.store = store
        self.servers = servers
        self.interval_seconds = max(0.0, float(interval_seconds))
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self.interval_seconds <= 0:
            print("[AccountState] periodic saving disabled")
            return
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name="account-state-autosave",
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        self._thread = None

    def save_once(self) -> tuple[int, bool]:
        saved = 0
        for server in self.servers:
            # Copy the registry under its dedicated lock, then release it
            # before persistence callbacks acquire session state or enqueue
            # database work.
            try:
                sessions = server.session_snapshot()
            except Exception as exc:
                # A damaged service registry must not prevent snapshots from
                # the other gameplay service, nor kill the periodic worker.
                service_label = getattr(
                    server, 'service_name', type(server).__name__
                )
                print(
                    '[AccountState] periodic session snapshot failed for '
                    f'{service_label}: {type(exc).__name__}: {exc}'
                )
                traceback.print_exc()
                continue
            for session in sessions:
                callback = session.gameplay_persist_callback
                if callback is None:
                    continue
                try:
                    saved += int(bool(callback(session, "interval", False)))
                except Exception as exc:
                    print(
                        "[AccountState] periodic snapshot failed for "
                        f"{session.addr}: {exc}"
                    )
                    traceback.print_exc()
        try:
            queued = self.store.request_flush()
        except Exception as exc:
            print(f"[PlayerDatabase] periodic write request failed: {exc}")
            traceback.print_exc()
            return saved, False
        if saved and queued:
            print(
                f"[PlayerDatabase] queued {saved} active "
                "gameplay session(s)"
            )
        return saved, queued

    def _run(self) -> None:
        consecutive_failures = 0
        while not self._stop_event.wait(self.interval_seconds):
            try:
                self.save_once()
            except Exception as exc:
                # Per-session isolation above handles expected failures. This
                # final boundary keeps a future orchestration regression from
                # permanently killing the long-lived worker. Process-control
                # exceptions intentionally remain fatal.
                consecutive_failures += 1
                print(
                    '[AccountState] autosave iteration failed: '
                    f'{type(exc).__name__}: {exc}'
                )
                traceback.print_exc()
                retry_delay = min(
                    5.0,
                    0.05 * (2 ** min(consecutive_failures - 1, 7)),
                )
                if self._stop_event.wait(retry_delay):
                    return
                continue
            consecutive_failures = 0


class ServerApplication:
    """Own all server services and their shared persistent stores."""

    def __init__(
        self,
        *,
        host: str = HOST,
        player_database_file: str | os.PathLike[str] | None = None,
        social_state_file: str | os.PathLike[str] | None = None,
        autosave_interval: float = ACCOUNT_STATE_SAVE_INTERVAL_SECONDS,
        account_registry_file: str | os.PathLike[str] | None = None,
        account_service_host: str | None = None,
        account_service_port: int | None = None,
        account_service_enabled: bool | None = None,
        admin_service_host: str | None = None,
        admin_service_port: int | None = None,
        admin_service_enabled: bool | None = None,
        require_login: bool | None = None,
        public_host: str | None = None,
    ) -> None:
        self.host = host
        self.advertised_host = resolve_advertised_host(host, public_host)
        self.login_address = f"{self.advertised_host}:2190"
        self.character_address = f"{self.advertised_host}:2192"
        self.account_store = AccountStateStore(
            player_database_file
            or os.getenv(
                "DSO_PLAYER_DATABASE",
                str(DEFAULT_PLAYER_DATABASE_PATH),
            ),
            static_database_path=DEFAULT_STATIC_DB_PATH,
        )
        self.social_store = SocialStateStore(
            social_state_file
            or os.getenv("DSO_SOCIAL_STATE_FILE", str(DEFAULT_SOCIAL_STATE_PATH))
        )
        if account_service_enabled is None:
            account_service_enabled = _environment_flag(
                "DSO_ACCOUNT_SERVICE", True
            )
        self.account_service_enabled = bool(account_service_enabled)
        self.admin_service_enabled = (
            _environment_flag("DSO_ADMIN_SERVICE", self.account_service_enabled)
            if admin_service_enabled is None else bool(admin_service_enabled)
        )
        self.account_registry = (
            AccountRegistry(
                account_registry_file
                or os.getenv(
                    "DSO_ACCOUNT_REGISTRY", str(DEFAULT_REGISTRY_PATH)
                )
            )
            if self.account_service_enabled or self.admin_service_enabled
            else None
        )
        self.account_store.account_registry = self.account_registry
        # Enforcing login by default on a loopback bind would break the
        # existing launch-client.ps1 workflow, which never logs in. A server
        # published to a network is the case where it must be on.
        if require_login is None:
            require_login = _environment_flag(
                "DSO_REQUIRE_LOGIN",
                self.account_registry is not None
                and host not in ("127.0.0.1", "localhost", "::1"),
            )
        self.require_login = bool(require_login) and self.account_registry is not None

        self.gateway = GatewayLoginServer(host, self.login_address)
        self.login = LoginGameServer(
            host,
            self.character_address,
            2190,
            account_store=self.account_store,
            social_store=self.social_store,
        )
        self.characters = CharacterServer(
            host,
            gameplay_router=self.login,
            account_store=self.account_store,
            social_store=self.social_store,
        )
        # The legacy direct-map route on 2190 shares the production roster.
        self.login.character_selection = self.characters.character_selection
        self.login.player_template_rows = self.characters.player_template_rows
        self.chat = ChatServer(
            host,
            character_selection=self.characters.character_selection,
            gameplay_servers=(self.login, self.characters),
            social_store=self.social_store,
            static_database=self.characters.static_database,
            account_registry=self.account_registry,
        )
        self.login.social_chat_callback = self.chat.refresh_social_memberships
        self.characters.social_chat_callback = (
            self.chat.refresh_social_memberships
        )
        self.login.system_chat_callback = self.chat.send_system_message_to_user
        self.characters.system_chat_callback = (
            self.chat.send_system_message_to_user
        )
        self.services: tuple[RakNetServer, ...] = (
            self.gateway,
            self.login,
            self.characters,
            self.chat,
        )
        for service in self.services:
            service.advertised_host = self.advertised_host
        self.accounts: AccountService | None = None
        if self.account_registry is not None and self.account_service_enabled:
            self.accounts = AccountService(
                host=account_service_host
                or os.getenv("DSO_ACCOUNT_HOST")
                or ("0.0.0.0" if host not in ("127.0.0.1", "localhost") else host),
                port=int(
                    account_service_port
                    if account_service_port is not None
                    else os.getenv("DSO_ACCOUNT_PORT", str(DEFAULT_ACCOUNT_PORT))
                ),
                registry=self.account_registry,
                character_selection=self.characters.character_selection,
                # The service reads DSO_PUBLIC_HOST itself and otherwise
                # answers with the address the launcher reached it on.
                gateway_port=80,
                chat_port=2191,
                chat_instance_id=self.chat.instance_id,
                # The database the game services loaded, for dsor clients to
                # copy (services/content.py).
                static_database_path=self.characters.static_database_path,
            )
        # Every service resolves the hello's session GUID, so the
        # character list and creation can be scoped to one account.
        for service in self.services:
            service.account_registry = self.account_registry

        if self.require_login:
            for service in self.services:
                # The address gate runs before the RakNet handshake, where
                # the client has not identified itself yet. It only proves
                # that *someone* at that address logged in, so on its own it
                # admits everyone behind the same NAT or VPN exit.
                service.connection_filter = self.account_registry.authorized_ip
                # The hello carries the session GUID. A client that cannot
                # present a live one is dropped there instead of being served
                # the whole roster like an unidentified local client.
                service.require_account_identity = True
        self.autosaver = PeriodicAccountSaver(
            self.account_store,
            (self.login, self.characters),
            autosave_interval,
        )
        # Observe map-worker progress independently of both the simulation
        # locks and console output; a healthy process can have a blocked map.
        self.stall_watchdog = StallWatchdog(
            monitor_monsters=True,
            report=None,
            diagnostic_path=(
                Path(__file__).resolve().parent
                / "data" / "diagnostics" / "monster-stalls.log"
            ),
            # Sampling sees only workers that exist; this restarts the
            # simulation of a map instance that lost its worker altogether.
            supervisor=self._supervise_monster_worlds,
        )
        # A full cyclic collection freezes the whole interpreter for as long
        # as it takes to walk the heap (70-100 ms with dense maps cached).
        # Freeze the startup heap and run full collections on our schedule,
        # preferring moments when no map is simulating.
        self.garbage_collector = ManagedGarbageCollector(
            busy_check=_map_workers_busy,
        )
        self._stop_event = threading.Event()
        self._started = False
        self.maintenance = MaintenanceShutdown(
            (self.login, self.characters), self._stop_event.set,
        )
        self.chat.shutdown_callback = self.maintenance.schedule
        self.login.maintenance = self.maintenance
        self.characters.maintenance = self.maintenance
        self.admin: AdminService | None = None
        if self.admin_service_enabled:
            self.admin = AdminService(
                registry=self.account_registry, store=self.account_store,
                static_database_path=self.characters.static_database_path,
                host=admin_service_host or os.getenv("DSO_ADMIN_HOST", "127.0.0.1"),
                port=int(admin_service_port if admin_service_port is not None
                         else os.getenv("DSO_ADMIN_PORT", str(DEFAULT_ADMIN_PORT))),
                application=self,
                secure_cookie=_environment_flag("DSO_ADMIN_SECURE_COOKIE", False),
            )
            if self.accounts is not None and self.admin.port != 0 and self.admin.port == self.accounts.port:
                raise ValueError("admin and account HTTP services require different ports")
            self.login.admin_booking_delivery = self.admin.delivery
            self.characters.admin_booking_delivery = self.admin.delivery

    def start(self) -> None:
        if self._started:
            return
        # Clear before accepting traffic: a zero-minute maintenance request
        # arriving during startup must not be erased after services start.
        self._stop_event.clear()
        for warning in self.account_store.warnings:
            print(f"[AccountState] WARNING: {warning}")
        if STATUS_EFFECTS_DISABLED:
            print(
                "[Diagnostic] ALL status effects are disabled "
                "(DSO_DISABLE_STATUS_EFFECTS). Damage, deaths and loot are "
                "unaffected; buffs, debuffs and crowd control are not sent."
            )
        elif VICTIM_STATUS_EFFECTS_DISABLED:
            print(
                "[Diagnostic] victim status effects are disabled "
                "(DSO_DISABLE_VICTIM_STATUS_EFFECTS). Monsters take damage "
                "and die normally but receive no debuffs or crowd control."
            )
        started: list[RakNetServer] = []
        try:
            for service in self.services:
                service.start()
                started.append(service)
            if self.accounts is not None:
                self.accounts.start()
            if self.admin is not None:
                self.admin.start()
            self.autosaver.start()
            self.stall_watchdog.start()
            self.garbage_collector.install()
        except Exception:
            if self.admin is not None:
                self.admin.stop()
            self.autosaver.stop()
            self.maintenance.close()
            collector = getattr(self, "garbage_collector", None)
            if collector is not None:
                collector.stop()
            self.stall_watchdog.stop()
            if self.accounts is not None:
                self.accounts.stop()
            for service in reversed(started):
                service.stop()
            raise
        self._started = True
        self._print_banner()

    def stop(self) -> None:
        self.maintenance.close()
        if not self._started:
            return
        if getattr(self, "admin", None) is not None:
            self.admin.stop()
        self.stall_watchdog.stop()
        statistics = self.stall_watchdog.statistics()
        if statistics["stall_count"]:
            print(
                f"[StallWatchdog] {statistics['stall_count']} stall(s) this "
                f"session; worst {statistics['worst_stall_seconds']:.3f}s"
            )
        # Tests build partially constructed applications; a missing
        # collector must not turn shutdown into a second failure.
        collector = getattr(self, "garbage_collector", None)
        if collector is not None:
            collector.stop()
            collections = collector.statistics()
            if collections["collections"]:
                print(
                    f"[GC] {collections['collections']} managed "
                    f"collection(s) this session "
                    f"({collections['full_collections']} whole-heap); worst "
                    f"{collections['worst_collection_seconds'] * 1000:.0f}ms"
                )
        self.autosaver.stop()
        if self.accounts is not None:
            self.accounts.stop()
        for service in reversed(self.services):
            service.stop()
        close_errors = []
        for store in (getattr(self, "social_store", None), self.account_store,
                      self.account_registry):
            if store is not None:
                try:
                    store.close()
                except Exception as exc:
                    close_errors.append(exc)
                    print(f"[Shutdown] {type(store).__name__} close failed: {exc}")
        if close_errors:
            # Let a later stop retry a failed writer, but always give the other
            # stores a chance to drain on this attempt.
            raise close_errors[0]
        self._started = False
        self._stop_event.set()
        print("Stopped.")

    def serve_forever(self) -> None:
        self.start()
        try:
            while not self._stop_event.wait(1.0):
                pass
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()

    def _supervise_monster_worlds(self) -> list[dict]:
        """Restart orphaned map simulations on every gameplay service."""

        events = []
        for service in (self.login, self.characters):
            events.extend(
                CharacterServer._supervise_monster_worlds(service)
            )
        return events

    def _print_banner(self) -> None:
        chat_region = (
            "region override"
            if self.chat.region_id_is_override
            else "dynamic DB region; fallback"
        )
        print(f"\nDSO local server running on {self.host}")
        if self.advertised_host != self.host:
            print(
                f"  clients are redirected to {self.advertised_host} "
                + (
                    "(DSO_PUBLIC_HOST)"
                    if os.getenv("DSO_PUBLIC_HOST")
                    else "(guessed from this host; set DSO_PUBLIC_HOST "
                    "if players cannot reach it)"
                )
            )
        print(f"  80   -> auth gateway, redirects to {self.login_address}")
        print("  2190 -> login gateway / armed map handoff")
        print(
            "  2191 -> built-in chat "
            f"(instance {self.chat.instance_id}, {chat_region} "
            f"{self.chat.region_id}, user {self.chat.user_name!r})"
        )
        print(
            "  2192 -> chars / armed gameplay "
            f"(selection: {len(self.characters.character_selection_payload)}B "
            "serialized)"
        )
        if self.accounts is not None:
            print(
                f"  {self.accounts.port:<4} -> login/register HTTP service on "
                f"{self.accounts.host} "
                f"({self.account_registry.count_accounts()} account(s), "
                f"{self.account_registry.count_active_sessions()} active "
                "session(s))"
            )
            print(
                "          clients reach the game at "
                f"{self.accounts.resolved_public_host()}:"
                f"{self.accounts.gateway_port} "
                f"(chat {self.accounts.chat_port})"
            )
        else:
            print("  login/register service disabled (DSO_ACCOUNT_SERVICE=0)")
        print(
            f"  player DB -> {Path(self.account_store.path)} "
            f"(every {self.autosaver.interval_seconds:g}s + transitions)"
        )
        if self.account_registry is not None:
            print(f"  account DB -> {Path(self.account_registry.path)}")
        print(
            "  login enforcement: "
            + (
                "ON - clients must present a live login session; "
                "unidentified hellos are dropped"
                if self.require_login
                else "OFF - any address may connect (DSO_REQUIRE_LOGIN=1 to "
                "enforce)"
            )
        )
        if self.admin is not None:
            print(f"  {self.admin.port:<4} -> admin CMS: {self.admin.url}")
        print("\nCtrl+C to stop\n")


def start_account_state_autosave(
    store: AccountStateStore,
    servers: tuple[RakNetServer, ...],
    interval_seconds: float = ACCOUNT_STATE_SAVE_INTERVAL_SECONDS,
) -> PeriodicAccountSaver:
    """Compatibility helper returning the managed periodic saver."""

    saver = PeriodicAccountSaver(store, servers, interval_seconds)
    saver.start()
    return saver


def import_server_modules() -> None:
    """Import every server module before the first player arrives.

    Many handlers import their collaborators inside the function. The first
    such call would otherwise read and compile modules from disk while a
    player waits on it.
    """

    import importlib
    import pkgutil

    import dso_server

    started = time.perf_counter()
    imported = 0
    for package in ("gameplay", "persistence", "protocol", "runtime", "services"):
        root = Path(dso_server.__file__).resolve().parent / package
        for module in pkgutil.iter_modules([str(root)]):
            name = f"dso_server.{package}.{module.name}"
            try:
                importlib.import_module(name)
            except Exception as exc:
                print(f"[Startup] could not preload {name}: {type(exc).__name__}: {exc}")
            else:
                imported += 1
    # Standard-library modules that load on first use (datetime.strptime).
    import _strptime  # noqa: F401

    print(
        f"[Startup] {imported} server modules imported in "
        f"{time.perf_counter() - started:.1f}s"
    )


def main() -> None:
    # Before any worker starts waiting: on Windows every timed wait otherwise
    # rounds up to 15.6 ms, which made the 25 Hz map tick uneven.
    raise_timer_resolution()
    # Before any service reads a table: every reader then gets the copy held
    # in memory (runtime/resident_content.py).
    resident_content.install(
        DEFAULT_STATIC_DB_PATH,
        extra_files=(
            DEFAULT_QUESTS_PATH, EVENT_SCHEDULE_PATH, CHEST_CONTENTS_MAP_PATH,
        ),
    )
    import_server_modules()
    application = ServerApplication()

    def request_stop(_signum, _frame) -> None:
        application._stop_event.set()

    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, request_stop)
        signal.signal(signal.SIGINT, request_stop)
    application.serve_forever()


__all__ = [
    "CharacterServer",
    "ChatServer",
    "FrameBuilder",
    "GatewayLoginServer",
    "LoginGameServer",
    "LoginServer",
    "PeriodicAccountSaver",
    "RakNetServer",
    "ServerApplication",
    "Session",
    "StallWatchdog",
    "main",
    "start_account_state_autosave",
]
