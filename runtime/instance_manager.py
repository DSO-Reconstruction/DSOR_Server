"""Instance and Worker Process Manager for DSO Server.

Coordinates multi-process map simulation:
1. Allocates loopback virtual ports (127.0.0.1:21920+) for worker instances.
2. Tracks running worker processes (PIDs).
3. Connects map transitions with the UdpDispatcher so clients are seamlessly
   routed to the appropriate worker without changing public ports.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from dso_server.runtime.udp_dispatcher import UdpDispatcher

_LOGGER = logging.getLogger("dso_server.instance_manager")


@dataclass
class InstanceWorkerRecord:
    map_id: str
    port: int
    process: subprocess.Popen | None
    pid: int | None
    created_at: float
    last_active: float
    player_count: int = 0


class InstanceManager:
    """Manages separate OS processes (PIDs) for map and dungeon instances."""

    def __init__(
        self,
        dispatcher: UdpDispatcher,
        *,
        base_worker_port: int = 21921,
        max_workers: int = 32,
    ) -> None:
        self.dispatcher = dispatcher
        self.base_worker_port = base_worker_port
        self.max_workers = max_workers

        self._lock = threading.Lock()
        self._workers: dict[int, InstanceWorkerRecord] = {}  # port -> record
        self._map_to_port: dict[str, int] = {}
        self._next_port_offset = 0

    def get_or_spawn_worker(self, map_id: str) -> int:
        """Return the internal loopback port for a map, spawning a new worker process if needed."""
        with self._lock:
            # Check if map is already running in a worker
            existing_port = self._map_to_port.get(map_id)
            if existing_port is not None:
                record = self._workers.get(existing_port)
                if record is not None and (record.process is None or record.process.poll() is None):
                    record.last_active = time.monotonic()
                    return existing_port

            # Allocate next available internal loopback port
            port = self._allocate_port_locked()
            _LOGGER.info(
                "Allocating new worker PID for map %s on 127.0.0.1:%d",
                map_id,
                port,
            )

            # Spawn child process
            # In production, worker runs standalone instance mode:
            # python -m dso_server.worker --port <port> --map <map_id>
            cmd = [
                sys.executable,
                "-m",
                "dso_server.worker",
                "--port",
                str(port),
                "--map-id",
                str(map_id),
            ]

            proc = None
            pid = None
            try:
                # We start with creationflags / detached if on windows to ensure independence
                creationflags = 0
                if os.name == "nt":
                    creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    creationflags=creationflags,
                )
                pid = proc.pid
                _LOGGER.info("Spawned worker process PID %s for map %s", pid, map_id)
            except Exception as exc:
                _LOGGER.error("Failed to spawn worker process for %s: %s", map_id, exc)

            record = InstanceWorkerRecord(
                map_id=map_id,
                port=port,
                process=proc,
                pid=pid,
                created_at=time.monotonic(),
                last_active=time.monotonic(),
            )
            self._workers[port] = record
            self._map_to_port[map_id] = port
            return port

    def route_player_to_map(
        self,
        client_addr: tuple[str, int],
        map_id: str,
    ) -> int:
        """Route a player to the worker process hosting map_id."""
        port = self.get_or_spawn_worker(map_id)
        with self._lock:
            record = self._workers.get(port)
            if record is not None:
                record.player_count += 1
                record.last_active = time.monotonic()
        self.dispatcher.set_client_route(client_addr, port)
        return port

    def release_player(self, client_addr: tuple[str, int], map_id: str) -> None:
        """Record player leaving a map and revert client route to default hub."""
        self.dispatcher.remove_client_route(client_addr)
        with self._lock:
            port = self._map_to_port.get(map_id)
            if port is not None:
                record = self._workers.get(port)
                if record is not None and record.player_count > 0:
                    record.player_count -= 1
                    record.last_active = time.monotonic()

    def terminate_all(self) -> None:
        with self._lock:
            for record in self._workers.values():
                if record.process is not None and record.process.poll() is None:
                    try:
                        record.process.terminate()
                    except OSError:
                        pass
            self._workers.clear()
            self._map_to_port.clear()

    def _allocate_port_locked(self) -> int:
        for _ in range(self.max_workers):
            port = self.base_worker_port + self._next_port_offset
            self._next_port_offset = (self._next_port_offset + 1) % self.max_workers
            record = self._workers.get(port)
            if record is None or (record.process is not None and record.process.poll() is not None):
                return port
        return self.base_worker_port
