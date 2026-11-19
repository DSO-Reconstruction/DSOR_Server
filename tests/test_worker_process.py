import subprocess
import sys
import time


def test_worker_process_startup_and_shutdown():
    test_port = 26350
    cmd = [
        sys.executable,
        "worker.py",
        "--host",
        "127.0.0.1",
        "--port",
        str(test_port),
    ]

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    try:
        # Give it a moment to boot
        time.sleep(1.5)
        # Verify process is still alive and didn't crash
        assert proc.poll() is None, "Worker exited prematurely"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=1.0)
