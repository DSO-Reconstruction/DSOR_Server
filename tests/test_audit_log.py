"""Audit disk stalls must never stall chat producers."""

import threading

import pytest

from dso_server.runtime.audit_log import AsyncAuditLog


def test_blocked_disk_does_not_block_append_and_memory_is_bounded(tmp_path):
    log = AsyncAuditLog(tmp_path / "chat.log", queue_limit=3)
    entered, release = threading.Event(), threading.Event()
    write = log._write_batch

    def blocked(text):
        entered.set()
        assert release.wait(3)
        write(text)

    log._write_batch = blocked
    try:
        assert log.append("first")
        assert entered.wait(2)
        assert log.append("second")
        assert log.append("third")
        assert not log.append("overflow")
        assert log.dropped_total == 1
    finally:
        release.set()
        log.close()
    text = log.path.read_text()
    assert text.index("first") < text.index("second") < text.index("third")
    assert "dropped 1 records" in text


def test_close_drains_records_and_rotates_bounded_files(tmp_path):
    log = AsyncAuditLog(tmp_path / "chat.log", max_file_bytes=12, backups=2)
    for value in ("first line", "second line", "third line", "fourth line"):
        assert log.append(value)
        log.flush()
    log.close()
    assert log.path.read_text() == "fourth line\n"
    assert (tmp_path / "chat.log.1").read_text() == "third line\n"
    assert (tmp_path / "chat.log.2").read_text() == "second line\n"
    assert not (tmp_path / "chat.log.3").exists()
    assert not log._thread.is_alive()


def test_byte_budget_rejects_oversized_record_without_starting_writer(tmp_path):
    log = AsyncAuditLog(tmp_path / "chat.log", buffer_bytes=4)
    assert not log.append("too long")
    log.close()
    assert log.dropped_total == 1
    assert not log.path.exists()


def test_disk_errors_are_observable_without_escaping_producer(tmp_path):
    log = AsyncAuditLog(tmp_path / "chat.log")

    def fail(_text):
        raise OSError("disk full")

    log._write_batch = fail
    assert log.append("accepted")
    with pytest.raises(OSError, match="could not be written"):
        log.close()
    assert log.failed_total == 1
    assert isinstance(log.last_error, OSError)
