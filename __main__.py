"""Run the production server with ``python -m dso_server``."""

import os
import struct
import sys

# Release defaults; explicit operator settings take precedence.
os.environ.setdefault("DSO_REQUIRE_LOGIN", "1")
os.environ.setdefault("DSO_DEBUG_VENDOR", "0")

from dso_server.app import main
from dso_server.runtime.log_stream import install as install_timestamped_logging


def _check_interpreter() -> None:
    """Refuse to start on a 32-bit interpreter.

    Gameplay deadlines use a bounded worker scheduler rather than one native
    thread per impact. A 64-bit interpreter is still required for the static
    database, navigation meshes, shared encounters and long-running state.
    """

    if struct.calcsize("P") * 8 >= 64:
        return
    sys.exit("\n".join((
        "This server requires a 64-bit Python interpreter.",
        "",
        f"  running: {sys.executable}",
        f"           {struct.calcsize('P') * 8}-bit, "
        f"maxsize=0x{sys.maxsize:x}",
        "",
        "The production runtime keeps the game database, navigation meshes,",
        "shared encounter state and packet caches resident. A 32-bit address",
        "space is not supported.",
        "",
        "On Windows, launch with a 64-bit interpreter, for example:",
        "  py -3.12-64 -m dso_server",
        "",
        "Set DSO_ALLOW_32BIT=1 to override (not recommended).",
    )))


if __name__ == "__main__":
    if not os.environ.get("DSO_ALLOW_32BIT"):
        _check_interpreter()
    # Before anything prints, so the whole log is correlatable.
    install_timestamped_logging()
    main()
