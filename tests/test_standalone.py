"""Check project boundaries and startup with no parent repository or prototypes."""

import ast
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_PACKAGES = ("services", "protocol", "gameplay", "persistence", "runtime")
# The Windows launcher (tkinter/ctypes) is a separate program run beside the
# server, importing its settings module directly; it is not part of the package.
LAUNCHER_SOURCES = frozenset({"launcher_gui.py", "client_settings.py"})


def _package_root_sources():
    return [path for path in PROJECT_ROOT.glob("*.py") if path.name not in LAUNCHER_SOURCES]


class StandaloneProjectTests(unittest.TestCase):
    def test_source_imports_are_owned_by_the_project(self):
        sources = _package_root_sources()
        for folder in (*RUNTIME_PACKAGES, "tests"):
            sources.extend((PROJECT_ROOT / folder).rglob("*.py"))
        for source in sources:
            relative = source.relative_to(PROJECT_ROOT)
            is_test = relative.parts[0] == "tests"
            package = ".".join(("dso_server", *relative.parts[:-1]))
            allowed = sys.stdlib_module_names | {"dso_server"}
            if is_test:
                allowed |= {"pytest"}
            for node in ast.walk(ast.parse(source.read_text(encoding="utf-8-sig"))):
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    module = importlib.util.resolve_name(
                        "." * node.level + (node.module or ""), package,
                    ) if node.level else node.module
                    names = [module]
                    if module == "dso_server":
                        names += [module + "." + alias.name for alias in node.names]
                else:
                    continue
                for name in names:
                    with self.subTest(source=str(relative), line=node.lineno):
                        self.assertIn(name.split(".")[0], allowed)
                        if not is_test:
                            self.assertFalse(name.startswith("dso_server.tests"))
                        if name.startswith("dso_server."):
                            target = PROJECT_ROOT.joinpath(*name.split(".")[1:])
                            self.assertTrue(
                                target.with_suffix(".py").is_file() or target.is_dir(),
                                f"Missing local module: {name}",
                            )

    def test_package_metadata_contains_runtime_packages_and_no_prototypes(self):
        config = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text())
        package = config["tool"]["setuptools"]
        self.assertEqual(set(package["packages"]), {
            "dso_server", *("dso_server." + name for name in RUNTIME_PACKAGES),
        })
        assets = package["package-data"]["dso_server"]
        self.assertNotIn("characters.json", assets)
        self.assertFalse((PROJECT_ROOT / "characters.json").exists())

    def test_relocated_application_uses_only_local_code_and_default_files(self):
        with tempfile.TemporaryDirectory(prefix="dso-standalone-") as directory:
            project = Path(directory) / "server"
            project.mkdir()
            for source in _package_root_sources():
                shutil.copy2(source, project / source.name)
            for folder in RUNTIME_PACKAGES:
                shutil.copytree(
                    PROJECT_ROOT / folder, project / folder,
                    ignore=shutil.ignore_patterns("__pycache__"),
                )
            for name in (
                "data/db/static.db4", "data/events.json",
                "data/catalogs/chest_contents.json",
                "data/navigation/navshapes.bin",
            ):
                target = project / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(PROJECT_ROOT / name, target)
            # Old configuration must not be read even when an inherited override
            # and a file with the historical name are both present.
            obsolete = project / "characters.json"
            obsolete.write_text("not valid JSON", encoding="utf-8")
            script = r'''
import importlib
from pathlib import Path
import runpy
import sys

project = Path(sys.argv[1]).resolve()
original = Path(sys.argv[2]).resolve()
assert all(not Path(p).resolve().is_relative_to(original.parent) for p in sys.path)
runpy.run_path(str(project / "run.py"))["load_package"]()
for path in sorted(project.rglob("*.py")):
    if path.stem != "conftest":
        name = ".".join(path.relative_to(project).with_suffix("").parts)
        importlib.import_module("dso_server." + name)

from dso_server.app import ServerApplication
from dso_server.paths import DEFAULT_STATIC_DB_PATH, DEFAULT_CHEST_CONTENTS_MAP
from dso_server.runtime.context import EVENT_SCHEDULE_PATH
from dso_server.persistence.account_state import CharacterProfile

app = ServerApplication(autosave_interval=0)
try:
    paths = (
        DEFAULT_STATIC_DB_PATH, DEFAULT_CHEST_CONTENTS_MAP, EVENT_SCHEDULE_PATH,
        app.account_store.path, app.social_store.path, app.account_registry.path,
        app.chat.audit_path, app.characters.static_database_path,
        app.login.static_database_path,
    )
    for path in paths:
        assert Path(path).resolve().is_relative_to(project), path
    assert app.characters.character_selection.characters == []
    assert app.characters.character_selection.equipment == []
    assert app.account_store.profiles() == ()
    app.account_store.put_profile(CharacterProfile(
        character_id=42, name="PersistedPlayer", map_id="a0200_kingscity",
        class_id=1, gender=0, hair_style=0, beard_style=0, body_style=0,
        body_stature=0, premium_currency=123,
    ), flush=True)
finally:
    app.account_store.close()
    app.account_registry.close()

app = ServerApplication(autosave_interval=0)
try:
    assert [p.character_id for p in app.account_store.profiles()] == [42]
    roster = app.characters.character_selection.characters
    assert [(c.character_id, c.name, c.premium_currency) for c in roster] == [
        (42, "PersistedPlayer", 123),
    ]
    for name, module in tuple(sys.modules.items()):
        assert not name.startswith("dso_server.tests"), name
        filename = getattr(module, "__file__", None)
        if filename is None:
            continue
        path = Path(filename).resolve()
        assert not path.is_relative_to(original.parent), (name, path)
        if name == "dso_server" or name.startswith("dso_server."):
            assert path.is_relative_to(project), (name, path)
finally:
    app.account_store.close()
    app.account_registry.close()
print("Standalone application initialized successfully")
'''
            environment = {
                key: value for key, value in os.environ.items()
                if not key.upper().startswith("DSO_")
            }
            environment["DSO_CHARACTERS_FILE"] = str(obsolete)
            environment["DSO_ACCOUNT_STATE_FILE"] = str(obsolete)
            result = subprocess.run(
                [sys.executable, "-I", "-S", "-c", script,
                 str(project), str(PROJECT_ROOT)],
                cwd=directory, env=environment, capture_output=True,
                text=True, timeout=60,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("Standalone application initialized successfully", result.stdout)
