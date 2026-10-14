"""Instrumented locks for the server's mutable state domains.

Strict mode is the production default. Set DSO_CONCURRENCY_GUARDS=observe only
as an emergency aid: violations are then logged and retained, but acquisition
timeouts remain enforced. DSO_LOCK_TIMEOUT_SECONDS controls the maximum wait.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import logging
import math
import os
import sys
import threading
import time
import traceback
from typing import Final, Literal
import weakref


SESSION_FLOW_DOMAIN: Final = "session_flow"
MAP_COMBAT_DOMAIN: Final = "map_combat"
GuardMode = Literal["strict", "observe"]

_LOGGER = logging.getLogger(__name__)
_VALID_MODES: Final = frozenset(("strict", "observe"))
_MAX_TIMEOUT_SECONDS: Final = 300.0
_SLOW_WAIT_SECONDS: Final = max(
    0.001, float(os.environ.get("DSO_SLOW_LOCK_WAIT_SECONDS", "0.040"))
)
_SLOW_WAIT_LOG_INTERVAL_SECONDS: Final = 2.0


class ConcurrencyLockError(RuntimeError):
    """Base class for guarded-lock failures."""


class LockOrderViolation(ConcurrencyLockError):
    """A forbidden lock-domain nesting attempt."""


class LockAcquisitionTimeout(ConcurrencyLockError, TimeoutError):
    """A blocking lock acquisition exceeded its configured bound."""


def _default_mode() -> GuardMode:
    value = os.environ.get("DSO_CONCURRENCY_GUARDS", "strict").strip().lower()
    if value not in _VALID_MODES:
        raise RuntimeError(
            "DSO_CONCURRENCY_GUARDS must be 'strict' or 'observe', "
            f"not {value!r}"
        )
    return value  # type: ignore[return-value]


def _default_timeout() -> float:
    raw = os.environ.get("DSO_LOCK_TIMEOUT_SECONDS", "5.0")
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            "DSO_LOCK_TIMEOUT_SECONDS must be a positive finite number"
        ) from exc
    if not math.isfinite(value) or value <= 0:
        raise RuntimeError(
            "DSO_LOCK_TIMEOUT_SECONDS must be a positive finite number"
        )
    return min(value, _MAX_TIMEOUT_SECONDS)


DEFAULT_GUARD_MODE: Final[GuardMode] = _default_mode()
DEFAULT_LOCK_TIMEOUT_SECONDS: Final[float] = _default_timeout()


@dataclass(frozen=True, slots=True)
class HeldLock:
    lock_id: int
    domain: str
    name: str
    depth: int


@dataclass(slots=True)
class _HeldRecord:
    lock_id: int
    domain: str
    name: str
    depth: int

    def snapshot(self) -> HeldLock:
        return HeldLock(self.lock_id, self.domain, self.name, self.depth)


class _ThreadState:
    __slots__ = ("held", "ident", "name", "token", "__weakref__")

    def __init__(self) -> None:
        self.ident = threading.get_ident()
        self.name = threading.current_thread().name
        self.token = object()
        self.held: dict[int, _HeldRecord] = {}


_local = threading.local()
_registry_lock = threading.Lock()
_registry: weakref.WeakValueDictionary[int, _ThreadState] = (
    weakref.WeakValueDictionary()
)
_violations_lock = threading.Lock()
_violations: deque[str] = deque(maxlen=256)


def _thread_state() -> _ThreadState:
    state = getattr(_local, "state", None)
    if state is None:
        state = _ThreadState()
        _local.state = state
        with _registry_lock:
            _registry[state.ident] = state
    else:
        state.name = threading.current_thread().name
    return state


def _snapshots(state: _ThreadState) -> tuple[HeldLock, ...]:
    try:
        records = tuple(state.held.values())
    except RuntimeError:
        records = tuple(state.held.copy().values())
    return tuple(record.snapshot() for record in records)


def held_locks_for_current_thread() -> tuple[HeldLock, ...]:
    return _snapshots(_thread_state())


def observed_lock_violations() -> tuple[str, ...]:
    with _violations_lock:
        return tuple(_violations)


def _format_held(held: tuple[HeldLock, ...]) -> str:
    if not held:
        return "none"
    return ", ".join(
        f"{item.domain}:{item.name} (depth={item.depth}, id={item.lock_id:#x})"
        for item in held
    )


def all_thread_tracebacks() -> str:
    """Public entry point for the all-thread diagnostic dump."""

    return _all_thread_tracebacks()


def _all_thread_tracebacks() -> str:
    frames = sys._current_frames()
    names = {
        thread.ident: thread.name
        for thread in threading.enumerate()
        if thread.ident is not None
    }
    with _registry_lock:
        held = {
            ident: _snapshots(state)
            for ident, state in tuple(_registry.items())
        }
    sections = []
    for ident, frame in sorted(frames.items()):
        sections.append(
            f"--- thread {names.get(ident, '<unknown>')!r} "
            f"(ident={ident}, guarded={_format_held(held.get(ident, ()))}) ---\n"
            + "".join(traceback.format_stack(frame, limit=64)).rstrip()
        )
    return "\n".join(sections) or "<no Python thread frames available>"


class _InstrumentedLock:
    def __init__(
        self,
        domain: str,
        name: str,
        *,
        reentrant: bool,
        timeout_seconds: float | None,
        guard_mode: GuardMode | None,
    ) -> None:
        if not isinstance(domain, str) or not domain:
            raise ValueError("lock domain must be a non-empty string")
        if not isinstance(name, str) or not name:
            raise ValueError("lock name must be a non-empty string")
        mode = DEFAULT_GUARD_MODE if guard_mode is None else guard_mode
        if mode not in _VALID_MODES:
            raise ValueError("guard_mode must be 'strict' or 'observe'")
        wait = (
            DEFAULT_LOCK_TIMEOUT_SECONDS
            if timeout_seconds is None
            else float(timeout_seconds)
        )
        if not math.isfinite(wait) or wait <= 0:
            raise ValueError("timeout_seconds must be a positive finite number")

        self.domain = domain
        self.name = name
        self.guard_mode: GuardMode = mode
        self.timeout_seconds = min(wait, _MAX_TIMEOUT_SECONDS)
        self._reentrant = bool(reentrant)
        self._primitive = threading.RLock() if reentrant else threading.Lock()
        self._metadata_lock = threading.Lock()
        self._owner: _ThreadState | None = None
        self._owner_token: object | None = None
        self._depth = 0
        self._last_slow_wait_log_at = 0.0
        self._slow_wait_report: tuple | None = None

    def _violation(self, state: _ThreadState) -> str | None:
        for held in tuple(state.held.values()):
            if held.lock_id == id(self) and not self._reentrant:
                return "a non-reentrant lock may not acquire itself"
            if (
                self.domain == SESSION_FLOW_DOMAIN
                and held.domain == MAP_COMBAT_DOMAIN
            ):
                return "session_flow may not nest inside map_combat"
            if (
                self.domain == MAP_COMBAT_DOMAIN
                and held.domain == SESSION_FLOW_DOMAIN
            ):
                return "map_combat may not nest inside session_flow"
            if (
                self.domain == SESSION_FLOW_DOMAIN
                and held.domain == SESSION_FLOW_DOMAIN
                and held.lock_id != id(self)
            ):
                return "a thread may not hold two distinct session_flow locks"
            if (
                self.domain == MAP_COMBAT_DOMAIN
                and held.domain == MAP_COMBAT_DOMAIN
                and held.lock_id != id(self)
            ):
                return "a thread may not hold two distinct map_combat locks"
        return None

    def _check_policy(self, state: _ThreadState) -> None:
        reason = self._violation(state)
        if reason is None:
            return
        message = (
            f"forbidden lock nesting: {reason}; requested "
            f"{self.domain}:{self.name} (id={id(self):#x}) on "
            f"{state.name!r} (ident={state.ident}); currently held: "
            f"{_format_held(_snapshots(state))}\nAcquisition stack:\n"
            + "".join(traceback.format_stack(limit=32)[:-1]).rstrip()
        )
        if self.guard_mode == "strict":
            raise LockOrderViolation(message)
        with _violations_lock:
            _violations.append(message)
        _LOGGER.warning("Concurrency guard observe-mode violation:\n%s", message)

    def _wait_bound(self, blocking: bool, timeout: float) -> float:
        if not blocking:
            if timeout != -1:
                raise ValueError("can't specify a timeout for a non-blocking call")
            return -1.0
        if timeout == -1:
            return self.timeout_seconds
        value = float(timeout)
        if value < 0:
            raise ValueError("timeout value must be positive")
        if not math.isfinite(value):
            raise OverflowError("timeout value is too large")
        return min(value, self.timeout_seconds)

    def _mark_acquired(self, state: _ThreadState) -> None:
        with self._metadata_lock:
            if self._owner_token is state.token:
                self._depth += 1
            else:
                self._owner = state
                self._owner_token = state.token
                self._depth = 1
            held = state.held.get(id(self))
            if held is None:
                state.held[id(self)] = _HeldRecord(
                    id(self), self.domain, self.name, 1
                )
            else:
                held.depth += 1

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        state = _thread_state()
        self._check_policy(state)
        bound = self._wait_bound(blocking, timeout)
        if not blocking:
            acquired = self._primitive.acquire(False)
            if acquired:
                self._mark_acquired(state)
            return acquired
        # Keep the uncontended path cheap and distinguish an actual wait from
        # normal lock use. Successful waits used to be invisible even when
        # they consumed nearly the full five-second timeout.
        if self._primitive.acquire(False):
            self._mark_acquired(state)
            return True
        wait_started = time.perf_counter()
        with self._metadata_lock:
            previous_owner = self._owner
            previous_depth = self._depth
        if not self._primitive.acquire(True, bound):
            with self._metadata_lock:
                owner = self._owner
                depth = self._depth
            owner_description = (
                "unknown"
                if owner is None
                else f"{owner.name!r} (ident={owner.ident}, depth={depth})"
            )
            raise LockAcquisitionTimeout(
                f"timed out after {bound:.3f}s acquiring "
                f"{self.domain}:{self.name} (id={id(self):#x}) on "
                f"{state.name!r} (ident={state.ident}); owner: "
                f"{owner_description}; requester-held locks: "
                f"{_format_held(_snapshots(state))}\n"
                f"All Python thread tracebacks:\n{_all_thread_tracebacks()}"
            )
        waited = time.perf_counter() - wait_started
        self._mark_acquired(state)
        if waited >= _SLOW_WAIT_SECONDS:
            now = time.perf_counter()
            with self._metadata_lock:
                if (
                    now - self._last_slow_wait_log_at
                    >= _SLOW_WAIT_LOG_INTERVAL_SECONDS
                ):
                    self._last_slow_wait_log_at = now
                    self._slow_wait_report = (
                        waited,
                        now,
                        state.name,
                        state.ident,
                        (
                            "unknown"
                            if previous_owner is None
                            else (
                                f"{previous_owner.name!r} "
                                f"(ident={previous_owner.ident}, "
                                f"depth={previous_depth})"
                            )
                        ),
                    )
        return True

    def release(self) -> None:
        slow_report = None
        with self._metadata_lock:
            owner = self._owner
            self._primitive.release()
            if owner is not None:
                held = owner.held.get(id(self))
                if held is not None:
                    if held.depth <= 1:
                        owner.held.pop(id(self), None)
                    else:
                        held.depth -= 1
            self._depth -= 1
            if self._depth <= 0:
                slow_report = self._slow_wait_report
                self._slow_wait_report = None
                self._owner = None
                self._owner_token = None
                self._depth = 0
        # Emit only after the protected primitive has been released. Logging
        # while still owning a hot combat lock would itself extend the stall.
        if slow_report is not None:
            waited, acquired_at, requester_name, requester_ident, owner = (
                slow_report
            )
            held_for = max(0.0, time.perf_counter() - acquired_at)
            print(
                f"[Concurrency] slow successful wait for "
                f"{self.domain}:{self.name}: {waited * 1000.0:.1f}ms; "
                f"requester={requester_name!r} (ident={requester_ident}); "
                f"previous_owner={owner}; subsequent_hold="
                f"{held_for * 1000.0:.1f}ms"
            )

    def locked(self) -> bool:
        with self._metadata_lock:
            return self._depth > 0

    def _is_owned(self) -> bool:
        state = _thread_state()
        with self._metadata_lock:
            return self._owner_token is state.token and self._depth > 0

    def _at_fork_reinit(self) -> None:
        reinitialize = getattr(self._primitive, "_at_fork_reinit", None)
        if reinitialize is not None:
            reinitialize()
        with self._metadata_lock:
            if self._owner is not None:
                self._owner.held.pop(id(self), None)
            self._owner = None
            self._owner_token = None
            self._depth = 0

    def __enter__(self) -> bool:
        return self.acquire()

    def __exit__(self, exc_type, exc_value, traceback_object) -> None:
        self.release()

    def __repr__(self) -> str:
        state = "locked" if self.locked() else "unlocked"
        return (
            f"<{type(self).__name__} {state} domain={self.domain!r} "
            f"name={self.name!r} at {id(self):#x}>"
        )


class InstrumentedLock(_InstrumentedLock):
    """A guarded non-reentrant lock."""

    def __init__(
        self,
        domain: str,
        name: str,
        *,
        timeout_seconds: float | None = None,
        guard_mode: GuardMode | None = None,
    ) -> None:
        super().__init__(
            domain,
            name,
            reentrant=False,
            timeout_seconds=timeout_seconds,
            guard_mode=guard_mode,
        )


class InstrumentedRLock(_InstrumentedLock):
    """A guarded reentrant lock."""

    def __init__(
        self,
        domain: str,
        name: str,
        *,
        timeout_seconds: float | None = None,
        guard_mode: GuardMode | None = None,
    ) -> None:
        super().__init__(
            domain,
            name,
            reentrant=True,
            timeout_seconds=timeout_seconds,
            guard_mode=guard_mode,
        )


def assert_no_state_locks_held(operation: str) -> None:
    """Reject an external side effect while mutable gameplay state is locked.

    Socket writes, database calls, callbacks, and other potentially blocking
    operations must happen after session/map state has been snapshotted and its
    lock released. This boundary is deliberately strict even when guarded
    locks are configured in observe mode: allowing the operation would recreate
    an unbounded state-lock -> external-lock dependency.
    """

    if not isinstance(operation, str) or not operation.strip():
        raise ValueError("operation must be a non-empty string")
    state = _thread_state()
    held = tuple(
        item
        for item in _snapshots(state)
        if item.domain in (SESSION_FLOW_DOMAIN, MAP_COMBAT_DOMAIN)
    )
    if not held:
        return
    raise LockOrderViolation(
        f"{operation} may not run while mutable state locks are held on "
        f"{state.name!r} (ident={state.ident}); currently held: "
        f"{_format_held(held)}\nOperation stack:\n"
        + "".join(traceback.format_stack(limit=32)[:-1]).rstrip()
    )


__all__ = [
    "ConcurrencyLockError",
    "DEFAULT_GUARD_MODE",
    "DEFAULT_LOCK_TIMEOUT_SECONDS",
    "HeldLock",
    "InstrumentedLock",
    "InstrumentedRLock",
    "LockAcquisitionTimeout",
    "LockOrderViolation",
    "MAP_COMBAT_DOMAIN",
    "SESSION_FLOW_DOMAIN",
    "all_thread_tracebacks",
    "assert_no_state_locks_held",
    "held_locks_for_current_thread",
    "observed_lock_violations",
]
