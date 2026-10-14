"""Console output must never block a gameplay thread."""

from __future__ import annotations

import io
import threading
import time
import unittest
from datetime import datetime

from dso_server.runtime.log_stream import TimestampedStream


class BlockingSink(io.StringIO):
    """A console that stops accepting output until released."""

    def __init__(self) -> None:
        super().__init__()
        self.release = threading.Event()
        self.release.set()
        self.writes = 0

    def write(self, text: str) -> int:
        self.release.wait()
        self.writes += 1
        return super().write(text)


def fixed_clock():
    return datetime(2026, 9, 24, 12, 0, 0, 123000)


class SynchronousStreamTests(unittest.TestCase):
    def test_default_stream_writes_immediately_with_a_timestamp(self):
        sink = io.StringIO()
        stream = TimestampedStream(sink, clock=fixed_clock)
        stream.write("hello\n")
        self.assertEqual(sink.getvalue(), "[12:00:00.123] hello\n")
        self.assertFalse(stream.asynchronous)


class AsynchronousStreamTests(unittest.TestCase):
    def test_writers_do_not_wait_for_a_blocked_console(self):
        sink = BlockingSink()
        stream = TimestampedStream(
            sink, clock=fixed_clock, asynchronous=True, queue_limit=50
        )
        self.addCleanup(stream.close_writer, 0.5)
        sink.release.clear()
        started = time.perf_counter()
        for index in range(500):
            stream.write(f"line {index}\n")
        elapsed = time.perf_counter() - started
        self.assertLess(elapsed, 0.5, "a blocked console stalled the writer")
        # The queue is bounded: the surplus was dropped, not buffered forever.
        self.assertGreater(stream.dropped_total, 0)
        self.assertLessEqual(stream.pending_lines(), 50)
        sink.release.set()
        stream.flush(2.0)
        output = sink.getvalue()
        self.assertIn("[12:00:00.123] line 0\n", output)
        self.assertIn("dropped", output)
        self.assertIn("console line(s)", output)

    def test_flush_delivers_everything_queued(self):
        sink = BlockingSink()
        stream = TimestampedStream(sink, clock=fixed_clock, asynchronous=True)
        self.addCleanup(stream.close_writer, 0.5)
        for index in range(20):
            stream.write(f"entry {index}\n")
        stream.write("partial without newline")
        stream.flush(2.0)
        output = sink.getvalue()
        for index in range(20):
            self.assertIn(f"entry {index}\n", output)
        self.assertIn("partial without newline\n", output)
        self.assertEqual(stream.pending_lines(), 0)

    def test_lines_stay_atomic_across_threads(self):
        sink = BlockingSink()
        stream = TimestampedStream(sink, clock=fixed_clock, asynchronous=True)
        self.addCleanup(stream.close_writer, 0.5)

        def worker(tag):
            for _ in range(200):
                stream.write(f"{tag} says hello\n")

        threads = [threading.Thread(target=worker, args=(f"t{i}",)) for i in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        stream.flush(2.0)
        lines = sink.getvalue().splitlines()
        self.assertEqual(len(lines), 800)
        self.assertTrue(all(line.endswith(" says hello") for line in lines))


if __name__ == "__main__":
    unittest.main()
