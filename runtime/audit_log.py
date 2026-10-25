"""Bounded, batched audit logging with one disk writer and rotating files."""

from collections import deque
from pathlib import Path
import threading
import time


class AsyncAuditLog:
    """Never make a packet handler wait for the filesystem.

    Accepted records drain on close. An overloaded logger drops new records,
    counts them, and writes a loss notice when space becomes available. Disk
    errors are observable through last_error and raised by flush/close.
    """

    def __init__(self, path, *, queue_limit=4096, buffer_bytes=4 * 1024 * 1024,
                 max_file_bytes=10 * 1024 * 1024, backups=3):
        self.path = Path(path)
        self.queue_limit = max(1, int(queue_limit))
        self.buffer_bytes = max(1, int(buffer_bytes))
        self.max_file_bytes = max(1, int(max_file_bytes))
        self.backups = max(1, int(backups))
        self._condition = threading.Condition()
        self._queue = deque()
        self._buffered_bytes = 0
        self._inflight = 0
        self._next_sequence = 0
        self._completed_sequence = 0
        self._unreported_drops = 0
        self._closing = False
        self._thread = None
        self._stream = None
        self._file_bytes = 0
        self.dropped_total = 0
        self.failed_total = 0
        self.last_error = None

    def append(self, line: str) -> bool:
        line = str(line).rstrip("\r\n") + "\n"
        size = len(line.encode("utf-8", errors="replace"))
        with self._condition:
            if (self._closing or len(self._queue) + self._inflight >= self.queue_limit
                    or self._buffered_bytes + size > self.buffer_bytes):
                self.dropped_total += 1
                self._unreported_drops += 1
                return False
            self._next_sequence += 1
            self._queue.append((self._next_sequence, line, size))
            self._buffered_bytes += size
            if self._thread is None:
                self._thread = threading.Thread(
                    target=self._run, name="chat-audit-writer", daemon=True,
                )
                self._thread.start()
            self._condition.notify_all()
            return True

    def flush(self, timeout=5.0):
        deadline = time.monotonic() + max(0.0, float(timeout))
        with self._condition:
            target = self._next_sequence
            while self._completed_sequence < target:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("timed out flushing chat audit log")
                self._condition.wait(remaining)
            if self.last_error is not None:
                raise OSError("chat audit records could not be written") from self.last_error

    def close(self, timeout=5.0):
        with self._condition:
            self._closing = True
            thread = self._thread
            self._condition.notify_all()
        if thread is not None:
            thread.join(max(0.0, float(timeout)))
            if thread.is_alive():
                raise TimeoutError("timed out stopping chat audit writer")
        self.flush(timeout=0)

    def _open(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._stream = self.path.open("a", encoding="utf-8", errors="replace", newline="")
        self._file_bytes = self.path.stat().st_size

    def _rotate(self):
        if self._stream is not None:
            self._stream.close()
            self._stream = None
        for index in range(self.backups, 0, -1):
            source = self.path if index == 1 else Path(f"{self.path}.{index - 1}")
            if source.exists():
                source.replace(Path(f"{self.path}.{index}"))
        self._open()

    def _write_batch(self, text):
        if self._stream is None:
            self._open()
        size = len(text.encode("utf-8", errors="replace"))
        if self._file_bytes and self._file_bytes + size > self.max_file_bytes:
            self._rotate()
        self._stream.write(text)
        self._stream.flush()
        self._file_bytes += size

    def _run(self):
        try:
            while True:
                with self._condition:
                    while not self._queue and not self._closing:
                        self._condition.wait()
                    if not self._queue:
                        return
                    batch = [self._queue.popleft() for _ in range(min(128, len(self._queue)))]
                    self._inflight = len(batch)
                    dropped = self._unreported_drops
                    self._unreported_drops = 0
                text = "".join(record[1] for record in batch)
                if dropped:
                    text = f"[AuditLog] dropped {dropped} records: capacity exceeded\n" + text
                try:
                    self._write_batch(text)
                except Exception as exc:
                    with self._condition:
                        self.last_error = exc
                        self.failed_total += len(batch)
                finally:
                    with self._condition:
                        self._buffered_bytes -= sum(record[2] for record in batch)
                        self._inflight = 0
                        self._completed_sequence = batch[-1][0]
                        self._condition.notify_all()
        finally:
            if self._stream is not None:
                try:
                    self._stream.close()
                except OSError as exc:
                    with self._condition:
                        self.last_error = exc
                self._stream = None
