"""Allow tests to run directly from the standalone source directory."""

from pathlib import Path
import runpy


runpy.run_path(str(Path(__file__).with_name("run.py")))["load_package"]()
