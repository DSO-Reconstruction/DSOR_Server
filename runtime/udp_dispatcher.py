"""High-performance loopback UDP Dispatcher for multi-process game instances.

Listens on the public game port (e.g. UDP 2192) and transparently multiplexes
incoming client datagrams to internal worker processes listening on local
virtual loopback ports (127.0.0.1:PORT).

This allows:
1. Running instance / dungeon simulations in completely independent OS processes (PIDs),
   bypassing the Python GIL and isolating map stalls/crashes.
2. Zero additional external firewall/modem ports: clients only connect to the single
   public port.
"""

from __future__ import annotations

import logging
import os
import socket
import threading
import time
from typing import Callable

_LOGGER = logging.getLogger("dso_server.udp_dispatcher")


class ClientTunnel:
    """A virtual loopback tunnel representing a single client connection."""

    __slots__ = (
        "client_addr",
        "tunnel_sock",
        "worker_port",
        "last_active",
        "local_port",
    )

    def __init__(self, client_addr: tuple[str, int], worker_port: int) -> None:
        self.client_addr = client_addr
        self.worker_port = worker_port
        self.last_active = time.monotonic()
        self.tunnel_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # Bind to loopback on an OS-assigned ephemeral port
        self.tunnel_sock.bind(("127.0.0.1", 0))
        self.tunnel_sock.setblocking(False)
        self.local_port = self.tunnel_sock.getsockname()[1]

    def forward_to_worker(self, data: bytes) -> None:
        self.last_active = time.monotonic()
        try:
            self.tunnel_sock.sendto(data, ("127.0.0.1", self.worker_port))
        except OSError as exc:
            _LOGGER.debug("Failed sending to worker 127.0.0.1:%s: %s", self.worker_port, exc)

    def close(self) -> None:
        try:
            self.tunnel_sock.close()
        except OSError:
            pass


class UdpDispatcher:
    """Transparent bidirectional UDP router between public clients and local workers."""

    def __init__(
        self,
        public_host: str,
        public_port: int,
        default_worker_port: int,
        *,
        idle_timeout_seconds: float = 120.0,
    ) -> None:
        self.public_host = public_host
        self.public_port = public_port
        self.default_worker_port = default_worker_port
        self.idle_timeout_seconds = idle_timeout_seconds

        self._public_sock: socket.socket | None = None
        self._tunnels: dict[tuple[str, int], ClientTunnel] = {}
        self._tunnel_by_fileno: dict[int, ClientTunnel] = {}
        self._tunnel_by_local_port: dict[int, ClientTunnel] = {}
        self._route_overrides: dict[tuple[str, int], int] = {}
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._worker_thread: threading.Thread | None = None

    def get_real_client_addr(self, transport_addr: tuple[str, int]) -> tuple[str, int]:
        """Map a local loopback worker address (127.0.0.1:tunnel_port) back to the real client (IP:Port)."""
        with self._lock:
            tunnel = self._tunnel_by_local_port.get(transport_addr[1])
            if tunnel is not None:
                return tunnel.client_addr
        return transport_addr

    def set_client_route(self, client_addr: tuple[str, int], target_worker_port: int) -> None:
        """Dynamically route a client's traffic to a specific worker process."""
        with self._lock:
            self._route_overrides[client_addr] = target_worker_port
            tunnel = self._tunnels.get(client_addr)
            if tunnel is not None:
                tunnel.worker_port = target_worker_port

    def remove_client_route(self, client_addr: tuple[str, int]) -> None:
        with self._lock:
            self._route_overrides.pop(client_addr, None)
            tunnel = self._tunnels.get(client_addr)
            if tunnel is not None:
                tunnel.worker_port = self.default_worker_port

    def start(self) -> None:
        if self._public_sock is not None:
            raise RuntimeError("Dispatcher is already running")

        self._stop_event.clear()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # Windows SO_EXCLUSIVEADDRUSE, Unix SO_REUSEADDR
        address_option = (
            socket.SO_EXCLUSIVEADDRUSE if os.name == "nt" else socket.SO_REUSEADDR
        )
        sock.setsockopt(socket.SOL_SOCKET, address_option, 1)

        # Buffer sizes (1MB)
        for opt in (socket.SO_RCVBUF, socket.SO_SNDBUF):
            try:
                sock.setsockopt(socket.SOL_SOCKET, opt, 1 << 20)
            except OSError:
                pass

        sock.bind((self.public_host, self.public_port))
        sock.setblocking(False)
        self._public_sock = sock

        self._worker_thread = threading.Thread(
            target=self._run_loop,
            daemon=True,
            name=f"udp-dispatcher-{self.public_port}",
        )
        self._worker_thread.start()
        _LOGGER.info(
            "UdpDispatcher listening on %s:%d (default worker 127.0.0.1:%d)",
            self.public_host,
            self.public_port,
            self.default_worker_port,
        )

    def stop(self) -> None:
        self._stop_event.set()
        if self._worker_thread is not None:
            self._worker_thread.join(timeout=1.0)
            self._worker_thread = None

        with self._lock:
            for tunnel in self._tunnels.values():
                tunnel.close()
            self._tunnels.clear()
            self._tunnel_by_fileno.clear()
            self._tunnel_by_local_port.clear()

        if self._public_sock is not None:
            try:
                self._public_sock.close()
            except OSError:
                pass
            self._public_sock = None

    def _get_or_create_tunnel(self, client_addr: tuple[str, int]) -> ClientTunnel:
        tunnel = self._tunnels.get(client_addr)
        if tunnel is None:
            worker_port = self._route_overrides.get(client_addr, self.default_worker_port)
            tunnel = ClientTunnel(client_addr, worker_port)
            self._tunnels[client_addr] = tunnel
            self._tunnel_by_fileno[tunnel.tunnel_sock.fileno()] = tunnel
            self._tunnel_by_local_port[tunnel.local_port] = tunnel
        return tunnel

    def _cleanup_stale_tunnels(self, now: float) -> None:
        stale = [
            addr
            for addr, tunnel in self._tunnels.items()
            if now - tunnel.last_active > self.idle_timeout_seconds
        ]
        for addr in stale:
            tunnel = self._tunnels.pop(addr, None)
            if tunnel is not None:
                self._tunnel_by_fileno.pop(tunnel.tunnel_sock.fileno(), None)
                self._tunnel_by_local_port.pop(tunnel.local_port, None)
                tunnel.close()

    def _run_loop(self) -> None:
        import select

        last_cleanup = time.monotonic()
        public_sock = self._public_sock
        if public_sock is None:
            return

        while not self._stop_event.is_set():
            now = time.monotonic()
            if now - last_cleanup >= 10.0:
                with self._lock:
                    self._cleanup_stale_tunnels(now)
                last_cleanup = now

            with self._lock:
                readable_sockets = [public_sock] + [
                    t.tunnel_sock for t in self._tunnels.values()
                ]

            try:
                # 20ms poll interval
                rlist, _, _ = select.select(readable_sockets, [], [], 0.020)
            except (OSError, ValueError):
                continue

            for s in rlist:
                if s is public_sock:
                    # Inbound from public internet/client -> send to local worker
                    while True:
                        try:
                            data, client_addr = public_sock.recvfrom(4096)
                        except (BlockingIOError, InterruptedError):
                            break
                        except OSError:
                            break

                        with self._lock:
                            tunnel = self._get_or_create_tunnel(client_addr)
                        tunnel.forward_to_worker(data)
                else:
                    # Outbound from local worker -> send to public client
                    fileno = s.fileno()
                    with self._lock:
                        tunnel = self._tunnel_by_fileno.get(fileno)
                    if tunnel is None:
                        continue

                    while True:
                        try:
                            data, _ = s.recvfrom(4096)
                        except (BlockingIOError, InterruptedError):
                            break
                        except OSError:
                            break

                        tunnel.last_active = now
                        try:
                            public_sock.sendto(data, tunnel.client_addr)
                        except OSError as exc:
                            _LOGGER.debug(
                                "Failed delivering reply to %s: %s",
                                tunnel.client_addr,
                                exc,
                            )
