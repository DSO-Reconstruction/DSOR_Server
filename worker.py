"""Standalone Map/Instance Worker process for DSO Server.

Runs a CharacterServer instance in an isolated OS process (own PID and GIL)
bound to a local loopback virtual port (127.0.0.1:PORT).
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
import time
from pathlib import Path

from dso_server.paths import (
    DEFAULT_PLAYER_DATABASE_PATH,
    DEFAULT_SOCIAL_STATE_PATH,
    DEFAULT_STATIC_DB_PATH,
)
from dso_server.persistence.account_state import AccountStateStore
from dso_server.runtime.context import SocialStateStore
from dso_server.services.character_server import CharacterServer

_LOGGER = logging.getLogger("dso_server.worker")


def main() -> int:
    parser = argparse.ArgumentParser(description="DSO Map Instance Worker Process")
    parser.add_argument("--host", default="127.0.0.1", help="Loopback bind host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, required=True, help="Loopback bind port (e.g. 21921)")
    parser.add_argument("--map-id", default=None, help="Assigned map or dungeon ID")
    parser.add_argument(
        "--player-db",
        default=os.getenv("DSO_PLAYER_DATABASE", str(DEFAULT_PLAYER_DATABASE_PATH)),
        help="Path to player database",
    )
    parser.add_argument(
        "--social-db",
        default=os.getenv("DSO_SOCIAL_STATE_FILE", str(DEFAULT_SOCIAL_STATE_PATH)),
        help="Path to social state store",
    )
    parser.add_argument(
        "--static-db",
        default=os.getenv("DSO_STATIC_DB", str(DEFAULT_STATIC_DB_PATH)),
        help="Path to static game database",
    )
    args = parser.parse_args()

    pid = os.getpid()
    print(f"[Worker PID {pid}] Starting instance worker on {args.host}:{args.port} (Map: {args.map_id or 'General'})")

    account_store = AccountStateStore(
        args.player_db,
        static_database_path=args.static_db,
    )
    social_store = SocialStateStore(args.social_db)

    server = CharacterServer(
        host=args.host,
        port=args.port,
        account_store=account_store,
        social_store=social_store,
        static_database=args.static_db,
    )

    stop_event = threading.Event()

    def _signal_handler(signum, frame):
        print(f"[Worker PID {pid}] Stopping on signal {signum}...")
        stop_event.set()

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    try:
        server.start()
        print(f"[Worker PID {pid}] Ready and listening on {args.host}:{args.port}")
        while not stop_event.is_set():
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        print(f"[Worker PID {pid}] Error: {exc}", file=sys.stderr)
        return 1
    finally:
        print(f"[Worker PID {pid}] Shutting down...")
        try:
            server.stop()
        except Exception:
            pass
        try:
            account_store.close()
        except Exception:
            pass
        print(f"[Worker PID {pid}] Exited.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
