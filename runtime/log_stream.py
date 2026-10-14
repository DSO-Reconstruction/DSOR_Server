"""Timestamped, line-atomic stdout for the server.

Three properties the plain stream does not have, all of which have cost real
diagnostic or gameplay time on this project:

- **Timestamps.** Without them a server log can only be correlated with a
  client log or a player's report by position in the file, which silently
  turns "these happened together" into a guess.
- **Line atomicity.** Several threads print concurrently, and interleaved
  writes produce joined lines like ``...effects[Service] reconciled...``.
  Those are not just ugly: they make counts and greps wrong.
- **Never blocking a gameplay thread.** Production runs inside ``screen`` over
  SSH. When the terminal stops draining -- a stalled SSH link, a paused
  display -- the pty buffer fills and every ``write`` blocks until it drains.
  With synchronous writes that blocked whichever thread printed: the UDP
  receive thread, a map worker, an impact worker. Console output is now handed
  to one writer thread through a bounded queue; when the console cannot keep
  up, lines are dropped and counted rather than stalling the game.
"""

from __future__ import annotations

import atexit
import os
import threading
import time
from collections import deque
from datetime import datetime


DEFAULT_QUEUE_LIMIT = 20_000


def _enabled() -> bool:
    return os.getenv("DSO_LOG_TIMESTAMPS", "1").strip().lower() not in {
        "0", "false", "no", "off",
    }


def _asynchronous_enabled() -> bool:
    return os.getenv("DSO_LOG_ASYNC", "1").strip().lower() not in {
        "0", "false", "no", "off",
    }


def _queue_limit() -> int:
    raw = os.getenv("DSO_LOG_QUEUE_LINES", str(DEFAULT_QUEUE_LIMIT))
    try:
        return max(100, int(raw))
    except (TypeError, ValueError):
        return DEFAULT_QUEUE_LIMIT


class TimestampedStream:
    """Prefix every completed line with a wall-clock timestamp.

    ``asynchronous=True`` moves the actual write to a daemon writer thread.
    Producers only append to a bounded in-memory queue and never wait on the
    underlying stream; a full queue drops the newest line and the count is
    reported once the console accepts output again.
    """

    def __init__(
        self,
        stream,
        *,
        clock=None,
        asynchronous: bool = False,
        queue_limit: int = DEFAULT_QUEUE_LIMIT,
    ) -> None:
        self._stream = stream
        self._clock = clock or (lambda: datetime.now())
        self._lock = threading.Lock()
        self._partial = ""
        self._queue_limit = max(1, int(queue_limit))
        self._queue: deque[str] = deque()
        self._dropped = 0
        self.dropped_total = 0
        self._condition = threading.Condition()
        self._writes_in_progress = 0
        self._closing = False
        self._writer: threading.Thread | None = None
        if asynchronous:
            self._writer = threading.Thread(
                target=self._drain, daemon=True, name="console-writer"
            )
            self._writer.start()

    @property
    def raw(self):
        return self._stream

    @property
    def asynchronous(self) -> bool:
        return self._writer is not None

    def _stamp(self) -> str:
        return self._clock().strftime("%H:%M:%S.%f")[:-3]

    def write(self, text) -> int:
        if not isinstance(text, str):
            text = str(text)
        if not text:
            return 0
        with self._lock:
            self._partial += text
            if "\n" not in self._partial:
                return len(text)
            *lines, self._partial = self._partial.split("\n")
            stamp = self._stamp()
            payload = "".join(f"[{stamp}] {line}\n" for line in lines)
            if self._writer is None:
                self._stream.write(payload)
                self._stream.flush()
                return len(text)
        self._enqueue(payload)
        return len(text)

    def _enqueue(self, payload: str) -> None:
        with self._condition:
            if len(self._queue) >= self._queue_limit:
                self._dropped += 1
                self.dropped_total += 1
            else:
                self._queue.append(payload)
            self._condition.notify()

    def _drain(self) -> None:
        while True:
            with self._condition:
                while not self._queue and not self._closing:
                    self._condition.wait()
                if not self._queue and self._closing:
                    return
                payload = "".join(self._queue)
                self._queue.clear()
                dropped, self._dropped = self._dropped, 0
                self._writes_in_progress += 1
            if dropped:
                payload += (
                    f"[{self._stamp()}] [log] dropped {dropped} console "
                    "line(s) while the console was not accepting output\n"
                )
            try:
                self._stream.write(payload)
                self._stream.flush()
            except (OSError, ValueError):
                # The console went away.  Losing log lines is acceptable;
                # taking the server down with them is not.
                pass
            finally:
                with self._condition:
                    self._writes_in_progress -= 1
                    self._condition.notify_all()

    def flush(self, timeout: float = 1.0) -> None:
        payload = None
        with self._lock:
            # A trailing fragment is emitted as its own line rather than being
            # held: losing the last message of a crash is worse than a stray
            # line break.
            if self._partial:
                payload = f"[{self._stamp()}] {self._partial}\n"
                self._partial = ""
            if self._writer is None:
                if payload:
                    self._stream.write(payload)
                self._stream.flush()
                return
        if payload:
            self._enqueue(payload)
        deadline = time.monotonic() + max(0.0, float(timeout))
        with self._condition:
            while (self._queue or self._writes_in_progress) and (
                self._writer is not None and self._writer.is_alive()
            ):
                remaining = deadline - time.monotonic()
                if remaining <= 0.0:
                    break
                self._condition.wait(remaining)

    def close_writer(self, timeout: float = 1.0) -> None:
        """Flush what is queued and retire the writer thread."""

        self.flush(timeout)
        writer = self._writer
        if writer is None:
            return
        with self._condition:
            self._closing = True
            self._condition.notify_all()
        writer.join(timeout)
        self._writer = None

    def pending_lines(self) -> int:
        with self._condition:
            return len(self._queue)

    def isatty(self) -> bool:
        return getattr(self._stream, "isatty", lambda: False)()

    def __getattr__(self, name):
        return getattr(self._stream, name)


def install(stream=None, *, asynchronous: bool | None = None) -> TimestampedStream | None:
    """Wrap ``sys.stdout`` unless ``DSO_LOG_TIMESTAMPS`` disables it.

    ``DSO_LOG_ASYNC`` (default on) selects the non-blocking writer.
    """

    import sys

    if not _enabled():
        return None
    target = sys.stdout if stream is None else stream
    if isinstance(target, TimestampedStream):
        return target
    if asynchronous is None:
        asynchronous = _asynchronous_enabled()
    wrapper = TimestampedStream(
        target, asynchronous=asynchronous, queue_limit=_queue_limit()
    )
    sys.stdout = wrapper
    if asynchronous:
        atexit.register(wrapper.close_writer)
    return wrapper


__all__ = ["DEFAULT_QUEUE_LIMIT", "TimestampedStream", "install"]
