"""Launch this directory directly with a 64-bit Python: ``python run.py``."""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import runpy
import sys


def load_package():
    """Load the local package without adding its parent directory to sys.path."""
    root = Path(__file__).resolve().parent
    spec = spec_from_file_location(
        "dso_server", root / "__init__.py", submodule_search_locations=[str(root)]
    )
    package = module_from_spec(spec)
    sys.modules[spec.name] = package
    spec.loader.exec_module(package)
    return package


if __name__ == "__main__":
    load_package()
    runpy.run_module("dso_server", run_name="__main__")
