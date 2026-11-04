#!/usr/bin/env bash
set -Eeuo pipefail

SERVER_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$SERVER_DIR"

PYTHON_BIN="${DSO_PYTHON:-python3}"
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "Python was not found: $PYTHON_BIN (install 64-bit Python 3.12 or newer)." >&2
  exit 2
fi
if ! "$PYTHON_BIN" -c 'import struct,sys; sys.exit(0 if sys.version_info >= (3,12) and struct.calcsize("P") == 8 else 1)'; then
  echo "This CMS requires 64-bit Python 3.12 or newer. Found: $("$PYTHON_BIN" --version 2>&1)." >&2
  exit 2
fi

echo "Starting the CMS only. Game and account services will stay stopped."
exec "$PYTHON_BIN" -u run_cms.py
