"""Pending map-handoff routes, held between a SwitchMap and the reconnect.

A map transfer tells the client to reconnect, so the destination service has to
remember where that client was going. The only thing it knows when the new
socket appears is the source address.

Keying that memory by IP alone holds exactly one route per address, which is
wrong as soon as two clients share one: the second arm overwrites the first,
so one player is initialized as the other's character and the other finds no
route at all and is dropped to character selection. Two clients on one machine
is the normal way to test group and PvP features, so the table keeps a queue
per address instead.

The reconnect is claimed in two steps, because the two pieces of information
arrive separately:

* ``ID_NEW_INCOMING_CONNECTION`` says a socket exists but carries no identity,
  so bring-up can only ask *whether* this address has a route pending.
* The ``0x8a`` hello carries the session GUID, which the account registry
  resolves to an account. That is the first point at which a specific route can
  be claimed, and it is where both services consume theirs.
"""

from __future__ import annotations

import copy
import threading
import time
from dataclasses import dataclass, field


@dataclass
class GameplayRoute:
    """One armed handoff: where a reconnecting client is going, and as whom."""

    expires_at: float
    map_name: str
    character: object | None = None
    template_row_index: int | None = None
    mount_ride: object | None = None
    spawn_position: tuple[float, ...] | None = None
    login_handoff: bool = False

    @property
    def character_id(self) -> int | None:
        character = self.character
        if character is None:
            return None
        value = getattr(character, "character_id", None)
        return None if value is None else int(value)


class GameplayRouteTable:
    """Per-address queues of armed routes, claimed by account when known."""

    def __init__(self, ttl_seconds: float) -> None:
        self.ttl_seconds = float(ttl_seconds)
        self._lock = threading.RLock()
        self._routes: dict[str, list[GameplayRoute]] = {}

    # -- arming ------------------------------------------------------------

    def arm(self, client_ip: str, map_name: str, *, character=None,
            template_row_index=None, mount_ride=None, spawn_position=None,
            login_handoff: bool = False) -> GameplayRoute:
        route = GameplayRoute(
            expires_at=time.perf_counter() + self.ttl_seconds,
            map_name=map_name,
            character=copy.deepcopy(character),
            template_row_index=template_row_index,
            mount_ride=copy.deepcopy(mount_ride),
            spawn_position=(
                tuple(float(value) for value in spawn_position)
                if spawn_position is not None
                else None
            ),
            login_handoff=bool(login_handoff),
        )
        with self._lock:
            self._expire_locked()
            pending = self._routes.setdefault(client_ip, [])
            # Re-arming the same character replaces its own route rather than
            # queueing a second one behind it; a repeated transfer is a
            # correction, not another player.
            character_id = route.character_id
            if character_id is not None:
                pending[:] = [
                    existing for existing in pending
                    if existing.character_id != character_id
                ]
            pending.append(route)
        return route

    # -- inspection --------------------------------------------------------

    def _expire_locked(self) -> None:
        now = time.perf_counter()
        for ip in list(self._routes):
            kept = [r for r in self._routes[ip] if r.expires_at > now]
            if kept:
                self._routes[ip] = kept
            else:
                del self._routes[ip]

    def is_armed(self, client_ip: str) -> bool:
        with self._lock:
            self._expire_locked()
            return bool(self._routes.get(client_ip))

    @staticmethod
    def _pick(pending: list[GameplayRoute], character_ids,
              identified: bool) -> int | None:
        """Index of the route this connection may take, or ``None``."""

        wanted = {int(value) for value in character_ids or ()}
        for position, route in enumerate(pending):
            if route.character_id in wanted:
                return position
        if not identified:
            # Historical single-client behaviour: the oldest route.
            return 0
        # An identified account never takes a route armed for another
        # account's character: two clients behind one NAT or VPN exit must
        # each get their own. A route armed without a character cannot be
        # attributed to anyone, so it stays claimable.
        for position, route in enumerate(pending):
            if route.character_id is None:
                return position
        return None

    def peek(self, client_ip: str, *, character_ids=(),
             identified: bool = False) -> GameplayRoute | None:
        """The route this client would claim, without consuming it."""

        with self._lock:
            self._expire_locked()
            pending = self._routes.get(client_ip)
            if not pending:
                return None
            index = self._pick(pending, character_ids, identified)
            return None if index is None else pending[index]

    def claim(self, client_ip: str, *, character_ids=(),
              identified: bool = False) -> GameplayRoute | None:
        """Take this client's route out of the queue.

        ``character_ids`` are the characters the connection has proved it
        owns, and ``identified`` whether it resolved to an account at all.
        With them the right route is picked even when several are pending
        for one address, and an account whose characters have no route here
        gets nothing rather than someone else's. An unidentified connection
        takes the oldest, which is the historical single-client behaviour.
        """

        with self._lock:
            self._expire_locked()
            pending = self._routes.get(client_ip)
            if not pending:
                return None
            index = self._pick(pending, character_ids, identified)
            if index is None:
                return None
            route = pending.pop(index)
            if not pending:
                del self._routes[client_ip]
            return route

    def pending(self, client_ip: str) -> int:
        with self._lock:
            self._expire_locked()
            return len(self._routes.get(client_ip, ()))
