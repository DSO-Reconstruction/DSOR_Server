"""Start only the admin CMS from this standalone server directory."""

from importlib.util import module_from_spec, spec_from_file_location
import os
from pathlib import Path
import signal
import sys
import threading


def load_package():
    """Load the local package without adding its parent directory to sys.path."""
    root = Path(__file__).resolve().parent
    spec = spec_from_file_location(
        "dso_server", root / "__init__.py", submodule_search_locations=[str(root)]
    )
    package = module_from_spec(spec)
    sys.modules[spec.name] = package
    spec.loader.exec_module(package)


def load_dotenv():
    """Load simple KEY=VALUE settings without replacing process variables."""
    env_path = Path(__file__).resolve().parent / ".env"
    if not env_path.is_file():
        return
    for raw_line in env_path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name, value = name.strip(), value.strip()
        if name:
            os.environ.setdefault(name, value)


def main():
    load_dotenv()
    os.environ["DSO_CMS_MANAGER"] = "1"
    from dso_server.app import ServerApplication

    # The CMS shares the normal persistent stores, but does not bind the game
    # sockets or the launcher account HTTP endpoint.
    application = ServerApplication(
        account_service_enabled=False,
        admin_service_enabled=True,
    )
    application._cms_manager_mode = True
    stop_event = threading.Event()

    def request_stop(_signum, _frame):
        stop_event.set()

    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGINT, request_stop)
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, request_stop)

    admin = application.admin
    try:
        admin.start()
        print(f"Admin CMS running at {admin.url}. Start the game server from Server control.")
        print("Press Ctrl+C to stop the CMS manager.")
        while not stop_event.wait(0.25):
            if application._start_server_requested.is_set():
                print("Starting the game server in this CMS process.")
                admin.stop()
                for store in (application.social_store, application.account_store,
                              application.account_registry):
                    if store is not None:
                        store.close()
                os.environ["DSO_ADMIN_SERVICE"] = "1"
                run_script = Path(__file__).resolve().parent / "run.py"
                os.execv(sys.executable, [sys.executable, "-u", str(run_script)])
    finally:
        if admin is not None:
            admin.stop()
        for store in (application.social_store, application.account_store,
                      application.account_registry):
            if store is not None:
                store.close()
        print("CMS stopped.")


if __name__ == "__main__":
    load_package()
    main()
