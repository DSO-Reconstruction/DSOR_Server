#!/usr/bin/env bash
set -Eeuo pipefail

SERVER_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$SERVER_DIR"

if [[ -f "$SERVER_DIR/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$SERVER_DIR/.env"
  set +a
fi

export DSO_HOST="${DSO_HOST:-0.0.0.0}"
if [[ -z "${DSO_PUBLIC_HOST:-}" ]]; then
  echo "DSO_PUBLIC_HOST is missing. Set it in $SERVER_DIR/.env." >&2
  exit 2
fi

PYTHON_BIN="${DSO_PYTHON:-python3}"
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "Python was not found: $PYTHON_BIN (install 64-bit Python 3.12 or newer)." >&2
  exit 2
fi
if ! "$PYTHON_BIN" -c 'import struct,sys; sys.exit(0 if sys.version_info >= (3,12) and struct.calcsize("P") == 8 else 1)'; then
  echo "This server requires 64-bit Python 3.12 or newer. Found: $("$PYTHON_BIN" --version 2>&1)." >&2
  exit 2
fi

required_files=(
  "run.py"
  "data/db/static.db4"
  "data/events.json"
  "data/quests.xml"
  "data/catalogs/chest_contents.json"
  "DSOClient/__root.txt"
)
for relative_path in "${required_files[@]}"; do
  if [[ ! -f "$SERVER_DIR/$relative_path" ]]; then
    echo "Required server file is missing: $relative_path" >&2
    exit 2
  fi
done
for required_directory in "data/maps" "data/navigation"; do
  if [[ ! -d "$SERVER_DIR/$required_directory" ]]; then
    echo "Required server directory is missing: $required_directory" >&2
    exit 2
  fi
done

mkdir -p data/db data/logs data/diagnostics data/state
echo "Starting DSO server on $(hostname) and advertising $DSO_PUBLIC_HOST."
exec "$PYTHON_BIN" -u run.py "$@"
