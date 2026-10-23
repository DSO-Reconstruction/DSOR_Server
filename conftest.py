"""Allow tests to run directly from the standalone source directory."""

import os
from pathlib import Path
import runpy

# The suite's captured packets come from dro_client64-patch5. Pin its command
# ids and skill/Hit layout before any protocol module resolves them at import;
# the server itself defaults to the release261_203 client.
os.environ.setdefault("DSO_COMMAND_IDS", "builtin")
os.environ.setdefault("DSO_SKILL_HIT_MARKER_BITS", "0")

runpy.run_path(str(Path(__file__).with_name("run.py")))["load_package"]()
