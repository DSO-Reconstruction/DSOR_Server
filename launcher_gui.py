"""TEST game launcher.

Login with username/password (or register a new account), then start
dro_client64.exe with an authenticated session. One 20-language selector controls the launcher and game client.
"""

from __future__ import annotations

import json
import ipaddress
import os
import subprocess
import sys
import tempfile
import threading
import ctypes
import tkinter as tk
from ctypes import wintypes
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from urllib.parse import urlsplit
from urllib import error as urlerror
from urllib import request as urlrequest

from client_inventory_vfx import create_inventory_vfx_client
from client_settings import (
    OldItemVisuals,
    apply_old_item_visuals,
    discard_old_item_visuals,
    enable_all_cooldown_timers,
    remember_client_process,
    restore_old_item_visuals,
)
from launcher_i18n import LANGUAGE_NAMES, STRINGS
from launcher_updates import (
    apply_server_update,
    is_server_root,
    latest_server_commit,
    local_head,
)

def app_dir() -> Path:
    """Directory of the launcher (exe folder when frozen, not PyInstaller temp)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


APP_TITLE = "DSOR Launcher"
DEFAULT_SERVER_HOST = "127.0.0.1"
DEFAULT_API_PORT = 8080
ICON_PATH = app_dir() / "icon.ico"


def _default_client_folder() -> Path:
    temp_dir = Path(os.environ.get("TEMP") or tempfile.gettempdir())
    return temp_dir / "DSOClient" / "dlcache"


def _client_cache_root(client_folder: Path) -> Path:
    return client_folder.parent if client_folder.name.lower() == "dlcache" else client_folder


def _parse_server_endpoint(value: str) -> tuple[str, str]:
    """Return (IP address, API base URL) from an IP or HTTP(S) endpoint."""
    raw = value.strip()
    if not raw:
        raise ValueError("empty server address")

    # Bare IPv4/IPv6 addresses keep using the default API port.
    try:
        host = str(ipaddress.ip_address(raw))
        port = DEFAULT_API_PORT
        scheme = "http"
    except ValueError:
        # Accept full URLs and host:port entries. Require a literal IP address;
        # this field is deliberately not a general-purpose URL field.
        if "://" in raw:
            parsed = urlsplit(raw)
            scheme = parsed.scheme.lower()
            if scheme not in ("http", "https"):
                raise ValueError("unsupported server URL scheme")
            if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
                raise ValueError("server URL must not contain a path or query")
        else:
            parsed = urlsplit("//" + raw)
            scheme = "http"

        if parsed.username or parsed.password:
            raise ValueError("credentials are not allowed in server URL")
        if not parsed.hostname:
            raise ValueError("missing server IP address")
        host = str(ipaddress.ip_address(parsed.hostname))
        try:
            port = parsed.port or DEFAULT_API_PORT
        except ValueError as exc:
            raise ValueError("invalid server port") from exc

    if not 1 <= port <= 65535:
        raise ValueError("invalid server port")
    api_host = f"[{host}]" if ":" in host else host
    return host, f"{scheme}://{api_host}:{port}"


def _dpapi(data: bytes, *, decrypt: bool = False) -> bytes:
    """Encrypt/decrypt launcher credentials for the current Windows user."""
    if os.name != "nt":
        raise OSError("Saved accounts require Windows DPAPI")

    class DataBlob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD),
                    ("pbData", ctypes.POINTER(ctypes.c_ubyte))]

    source = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    source_blob = DataBlob(len(data), ctypes.cast(
        source, ctypes.POINTER(ctypes.c_ubyte)))
    output_blob = DataBlob()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    function = (crypt32.CryptUnprotectData if decrypt
                else crypt32.CryptProtectData)
    function.argtypes = [ctypes.POINTER(DataBlob), wintypes.LPCWSTR,
                         ctypes.POINTER(DataBlob), ctypes.c_void_p,
                         ctypes.c_void_p, wintypes.DWORD,
                         ctypes.POINTER(DataBlob)]
    function.restype = wintypes.BOOL
    if not function(ctypes.byref(source_blob), None, None, None, None, 1,
                    ctypes.byref(output_blob)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.LocalFree(output_blob.pbData)


def _settings_dir() -> Path:
    local_app_data = os.getenv("LOCALAPPDATA")
    base = Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
    return base / "TESTCLIENT"


def _credentials_path() -> Path:
    return _settings_dir() / "saved-account.bin"


def _server_address_path() -> Path:
    return _settings_dir() / "server-address.txt"


def _server_folder_path() -> Path:
    return _settings_dir() / "server-folder.txt"


def _configured_server_folder() -> Path:
    try:
        value = _server_folder_path().read_text(encoding="utf-8").strip()
    except OSError:
        value = ""
    return Path(value).expanduser() if value else app_dir()


def _save_server_folder(folder: Path) -> None:
    path = _server_folder_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(folder.resolve()), encoding="utf-8")


def _load_server_address() -> str:
    try:
        value = _server_address_path().read_text(encoding="utf-8").strip()
    except OSError:
        return DEFAULT_SERVER_HOST
    return value or DEFAULT_SERVER_HOST


def _save_server_address(value: str) -> None:
    path = _server_address_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value.strip(), encoding="utf-8")


def _client_dir_path() -> Path:
    return _settings_dir() / "client-folder.txt"


GAME_LANGUAGES = tuple(LANGUAGE_NAMES.items())
GAME_LANGUAGE_CODES = {code for code, _name in GAME_LANGUAGES}
LANGUAGE_CODES_BY_NAME = {name: code for code, name in GAME_LANGUAGES}


def _game_language_path() -> Path:
    return _settings_dir() / "game-language.txt"


def _load_game_language() -> str:
    try:
        language = _game_language_path().read_text(encoding="utf-8").strip()
    except OSError:
        return "en"
    return language if language in GAME_LANGUAGE_CODES else "en"


def _save_game_language(language: str) -> None:
    if language not in GAME_LANGUAGE_CODES:
        return
    path = _game_language_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(language, encoding="utf-8")
    except OSError:
        pass


def _old_look_path() -> Path:
    return _settings_dir() / "old-look.txt"


def _load_old_look() -> bool:
    try:
        return _old_look_path().read_text(encoding="utf-8").strip() == "1"
    except OSError:
        return False


def _save_old_look(enabled: bool) -> None:
    path = _old_look_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("1" if enabled else "0", encoding="utf-8")
    except OSError:
        pass


def _launcher_language_path() -> Path:
    return _settings_dir() / "launcher-language.txt"


def _load_launcher_language() -> str:
    try:
        language = _launcher_language_path().read_text(encoding="utf-8").strip()
    except OSError:
        # Migrate an existing game-language preference to the single selector.
        try:
            _game_language_path().stat()
        except OSError:
            return "tr"
        return _load_game_language()
    return language if language in STRINGS else "tr"


def _save_launcher_language(language: str) -> None:
    if language not in STRINGS:
        return
    path = _launcher_language_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(language, encoding="utf-8")
    except OSError:
        pass
    _save_game_language(language)


def _save_client_dir(path: Path) -> None:
    config_path = _client_dir_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(str(path.resolve()), encoding="utf-8")


def _load_client_dir() -> Path | None:
    try:
        value = _client_dir_path().read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not value:
        return None
    folder = Path(value)
    cache_folder = folder / "dlcache"
    if (
        folder.name.lower() != "dlcache"
        and (cache_folder / "dro_client64.exe").is_file()
    ):
        return cache_folder
    return folder


def _load_saved_account() -> tuple[str, str] | None:
    path = _credentials_path()
    if not path.is_file():
        return None
    try:
        document = json.loads(_dpapi(path.read_bytes(), decrypt=True).decode("utf-8"))
        username, password = document.get("username"), document.get("password")
        if isinstance(username, str) and isinstance(password, str):
            return username, password
    except Exception:
        return None
    return None


def _save_account(username: str, password: str) -> None:
    path = _credentials_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    payload = json.dumps({"username": username, "password": password}).encode("utf-8")
    temporary.write_bytes(_dpapi(payload))
    os.replace(temporary, path)


def _forget_account() -> None:
    _credentials_path().unlink(missing_ok=True)

# ---------------------------------------------------------------- palette ---
BG = "#0d0b09"          # near-black coal
BG_PANEL = "#161210"    # dark leather panel
BG_INPUT = "#1f1a16"    # input fields
BORDER = "#3a2c1e"      # dark bronze border
RED = "#8e1616"         # drakensang deep red
RED_HOVER = "#b31e1e"
GOLD = "#d4af37"        # antique gold
TEXT = "#e8dcc3"        # parchment white
TEXT_DIM = "#8a7d68"
OK_GREEN = "#4c9a4c"
FONT_TITLE = ("Georgia", 26, "bold")
FONT_SUB = ("Georgia", 10, "italic")
FONT_LABEL = ("Georgia", 10, "bold")
FONT_ENTRY = ("Consolas", 11)
FONT_BTN = ("Georgia", 12, "bold")
FONT_SMALL = ("Segoe UI", 8)

def find_client_exe(
    selected_client_dir: Path | None = None,
) -> tuple[Path | None, Path | None]:
    """Locate the client beside the launcher, at a saved path, or in temp."""
    local_dirs = [
        _configured_server_folder() / "DSOClient" / "dlcache",
        _configured_server_folder() / "Client" / "dlcache",
        _configured_server_folder() / "cloud" / "DSOClient" / "dlcache",
        _default_client_folder(),
    ]
    for client_dir in (selected_client_dir, _load_client_dir()):
        if client_dir is None:
            continue
        local_dirs.insert(0, client_dir / "dlcache")
        if client_dir.name.lower() == "dlcache":
            local_dirs.insert(0, client_dir)
    for local_dir in local_dirs:
        if (local_dir / "dro_client64.exe").is_file():
            return local_dir / "dro_client64.exe", local_dir
    return None, None


def _client_folder_has_executable(client_folder: Path) -> bool:
    if client_folder.name.lower() == "dlcache":
        return (client_folder / "dro_client64.exe").is_file()
    return any((client_folder / relative).is_file() for relative in (
        Path("dro_client64.exe"), Path("dlcache") / "dro_client64.exe",
    ))


def api_post(api: str, path: str, payload: dict) -> tuple[int, dict]:
    """POST JSON to the account service. Returns (status, body)."""
    url = f"{api.rstrip('/')}{path}"
    data = json.dumps(payload).encode("utf-8")
    req = urlrequest.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
    try:
        with urlrequest.urlopen(req, timeout=8) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urlerror.HTTPError as exc:
        try:
            body = json.loads(exc.read().decode("utf-8"))
        except Exception:
            body = {"error": str(exc)}
        return exc.code, body
    except Exception as exc:  # connection refused etc.
        raise ConnectionError("server unavailable") from exc


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(APP_TITLE)
        self.configure(bg=BG)
        self.resizable(True, True)
        self.lang = _load_launcher_language()
        self._center(1060, 760)
        if ICON_PATH.exists():
            try:
                self.iconbitmap(str(ICON_PATH))
            except Exception:
                pass
        self._build()

    # ---------------------------------------------------------------- ui ---
    def _center(self, w: int, h: int) -> None:
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        w = max(480, min(w, sw - 24))
        h = max(450, min(h, sh - 64))
        self.geometry(f"{w}x{h}+{(sw - w) // 2}+{(sh - h) // 2}")
        self.minsize(min(640, w), min(560, h))

    def _panel(self, parent, **kw):
        return tk.Frame(parent, bg=BG_PANEL, highlightbackground=BORDER,
                        highlightthickness=1, **kw)

    def _build(self) -> None:
        s = STRINGS[self.lang]
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(
            "Client.TCombobox",
            fieldbackground=BG_INPUT,
            background=BG_PANEL,
            foreground=TEXT,
            arrowcolor=GOLD,
            bordercolor=BORDER,
            lightcolor=BORDER,
            darkcolor=BORDER,
            padding=(8, 5),
            font=FONT_ENTRY,
        )
        scrollbar_style = {
            "background": "#59452f",
            "troughcolor": BG,
            "bordercolor": BORDER,
            "lightcolor": "#59452f",
            "darkcolor": "#34271b",
            "arrowcolor": GOLD,
            "relief": "flat",
            "gripcount": 0,
            "width": 12,
        }
        for scrollbar_name in (
            "Vertical.TScrollbar", "Client.Vertical.TScrollbar",
        ):
            style.configure(scrollbar_name, **scrollbar_style)
            style.map(
                scrollbar_name,
                background=[("active", "#806239"), ("pressed", "#a17b3c")],
                arrowcolor=[("active", "#f0cf68")],
            )
        style.map(
            "Client.TCombobox",
            fieldbackground=[("readonly", BG_INPUT), ("focus", BG_INPUT)],
            foreground=[("readonly", TEXT)],
            selectbackground=[("readonly", BG_INPUT)],
            selectforeground=[("readonly", TEXT)],
        )
        self.option_add("*TCombobox*Listbox*Background", BG_INPUT)
        self.option_add("*TCombobox*Listbox*Foreground", TEXT)
        self.option_add("*TCombobox*Listbox*selectBackground", BORDER)
        self.option_add("*TCombobox*Listbox*selectForeground", TEXT)

        # --- title banner ---------------------------------------------------
        banner = tk.Frame(self, bg=BG, height=88)
        self.banner = banner
        banner.pack(fill="x")
        banner.pack_propagate(False)

        self.title_lbl = tk.Label(
            banner, text="DRAKENSANG ONLINE", font=("Georgia", 22, "bold"),
            fg=GOLD, bg=BG,
        )
        self.title_lbl.pack(pady=(12, 0))
        self.subtitle_lbl = tk.Label(banner, text=s["subtitle"],
                                     font=FONT_SUB, fg=TEXT_DIM, bg=BG)
        self.subtitle_lbl.pack()
        self.language_row = tk.Frame(banner, bg=BG, height=28)
        self.language_row.pack(fill="x")
        self.language_row.pack_propagate(False)
        self.lang_label = tk.Label(
            self.language_row, text=s["language"],
            font=FONT_SMALL, fg=TEXT_DIM, bg=BG,
        )
        self.lang_label.place(relx=1.0, x=-198, y=2, anchor="ne")
        self.lang_choice = tk.StringVar(value=LANGUAGE_NAMES[self.lang])
        self.lang_combo = ttk.Combobox(
            self.language_row, textvariable=self.lang_choice,
            values=tuple(LANGUAGE_NAMES.values()),
            state="readonly", width=17, style="Client.TCombobox",
        )
        self.lang_combo.place(relx=1.0, x=-18, y=0, anchor="ne")
        self.lang_combo.bind("<<ComboboxSelected>>", self._on_launcher_language)
        sep = tk.Canvas(banner, height=2, bg=GOLD, highlightthickness=0, bd=0)
        sep.pack(fill="x", pady=(4, 0))

        # --- launcher content ----------------------------------------------
        content = tk.Frame(self, bg=BG)
        content.pack(fill="both", expand=True, padx=18, pady=(14, 10))
        self.content_frame = content
        self.content_grid = content
        content.grid_rowconfigure(0, weight=1)
        content.grid_columnconfigure(0, weight=2, uniform="launcher-columns")
        content.grid_columnconfigure(1, weight=1, minsize=280, uniform="launcher-columns")
        self._responsive_mode = None

        # Commit activity replaces the official launcher's news column.
        feed_panel = self._panel(content)
        self.feed_panel = feed_panel
        feed_header = tk.Frame(feed_panel, bg=BG_PANEL)
        feed_header.pack(fill="x", padx=18, pady=(14, 8))
        self.feed_title_lbl = tk.Label(
            feed_header, text=s["feed_title"], font=FONT_LABEL,
            fg=GOLD, bg=BG_PANEL,
        )
        self.feed_title_lbl.pack(side="left")
        tk.Label(feed_header, text="main", font=FONT_SMALL,
                 fg=TEXT_DIM, bg=BG_PANEL).pack(side="right")
        commit_area = tk.Frame(feed_panel, bg=BG_PANEL)
        commit_area.pack(fill="both", expand=True, padx=18, pady=(0, 8))
        self.commit_text = tk.Text(
            commit_area, bg=BG_PANEL, fg=TEXT, relief="flat",
            highlightthickness=0, wrap="word", font=("Segoe UI", 10),
            padx=8, pady=8, insertwidth=0,
        )
        self.commit_text.pack(side="left", fill="both", expand=True)
        self.commit_scrollbar = ttk.Scrollbar(
            commit_area, orient="vertical", command=self.commit_text.yview,
            style="Client.Vertical.TScrollbar",
        )
        self.commit_scrollbar.pack(side="right", fill="y")
        self.commit_text.config(
            yscrollcommand=self.commit_scrollbar.set, state="disabled",
        )
        self._set_commit_text(s["feed_loading"])
        self.commit_text.bind("<Double-Button-1>", self._open_latest_commit)
        self.feed_hint_lbl = tk.Label(
            feed_panel, text=s["feed_hint"], font=FONT_SMALL,
            fg=TEXT_DIM, bg=BG_PANEL,
        )
        self.feed_hint_lbl.pack(anchor="w", padx=18, pady=(0, 12))

        # --- account panel --------------------------------------------------
        panel = self._panel(content)
        self.account_panel = panel

        page_tabs = tk.Frame(panel, bg=BG_PANEL)
        page_tabs.pack(fill="x", padx=12, pady=(12, 4))
        self.play_tab_btn = tk.Button(
            page_tabs, text=s["play"], font=FONT_LABEL, fg=GOLD,
            bg=BG_PANEL, activebackground=BG_PANEL, activeforeground=TEXT,
            relief="flat", bd=0, cursor="hand2",
            command=lambda: self._show_launcher_page("play"),
        )
        self.play_tab_btn.pack(side="left", expand=True, fill="x")
        self.settings_tab_btn = tk.Button(
            page_tabs, text=s["settings"], font=FONT_LABEL, fg=TEXT_DIM,
            bg=BG_PANEL, activebackground=BG_PANEL, activeforeground=TEXT,
            relief="flat", bd=0, cursor="hand2",
            command=lambda: self._show_launcher_page("settings"),
        )
        self.settings_tab_btn.pack(side="right", expand=True, fill="x")
        self.page_container = tk.Frame(panel, bg=BG_PANEL)
        self.page_container.pack(fill="both", expand=True)
        self.page_canvas = tk.Canvas(
            self.page_container, bg=BG_PANEL, highlightthickness=0, bd=0,
        )
        self.page_scrollbar = ttk.Scrollbar(
            self.page_container, orient="vertical", command=self.page_canvas.yview,
            style="Client.Vertical.TScrollbar",
        )
        self.page_canvas.configure(yscrollcommand=self.page_scrollbar.set)
        self.page_scrollbar.pack(side="right", fill="y")
        self.page_canvas.pack(side="left", fill="both", expand=True)
        self.page_inner = tk.Frame(self.page_canvas, bg=BG_PANEL)
        self.page_window = self.page_canvas.create_window(
            (0, 0), window=self.page_inner, anchor="nw",
        )
        self.page_inner.bind(
            "<Configure>",
            lambda _event: self.page_canvas.configure(
                scrollregion=self.page_canvas.bbox("all"),
            ),
        )
        self.page_canvas.bind("<Configure>", self._resize_page_view)
        self.page_canvas.bind("<Enter>", self._bind_page_mousewheel)
        self.page_canvas.bind("<Leave>", self._unbind_page_mousewheel)
        inner = tk.Frame(self.page_inner, bg=BG_PANEL)
        inner.pack(padx=20, pady=14, fill="both", expand=True)
        self.play_page = inner
        self.settings_page = tk.Frame(self.page_inner, bg=BG_PANEL)
        self.active_page = "play"

        self.username_var = tk.StringVar()
        self.password_var = tk.StringVar()
        self.password2_var = tk.StringVar()
        self.server_ip_var = tk.StringVar(value=_load_server_address())
        self.server_folder_var = tk.StringVar(value=str(_configured_server_folder()))
        self.game_language_var = tk.StringVar(value=self.lang)
        saved_client_dir = _load_client_dir()
        self.client_dir_var = tk.StringVar(
            value=str(saved_client_dir or _default_client_folder())
        )
        self.remember_var = tk.BooleanVar(value=False)
        self.old_look_var = tk.BooleanVar(value=_load_old_look())
        self.inventory_vfx_var = tk.BooleanVar(value=False)

        self.username_row = self._field(inner, s["username"], self.username_var)
        self.password_row = self._field(inner, s["password"], self.password_var,
                                        show="\u25cf")
        self.pw2_row = self._field(inner, s["password2"], self.password2_var, show="\u25cf")
        self.pw2_row.pack_forget()  # hidden until "register" mode

        # status line
        self.status_var = tk.StringVar(value="")
        self.status_lbl = tk.Label(inner, textvariable=self.status_var,
                                   font=FONT_SMALL, fg=TEXT_DIM, bg=BG_PANEL,
                                   wraplength=340, justify="center")
        self.status_lbl.pack(pady=(6, 2))

        self.remember_check = tk.Checkbutton(
            inner, text=s["remember"], variable=self.remember_var,
            font=("Segoe UI", 9), fg=TEXT, bg=BG_PANEL,
            activebackground=BG_PANEL, activeforeground=GOLD,
            selectcolor=BG_INPUT, highlightthickness=0,
            command=self._on_remember_toggle,
        )
        self.remember_check.pack(fill="x", before=self.status_lbl, pady=(8, 0))

        self.old_look_check = tk.Checkbutton(
            inner, text=s["old_look"], variable=self.old_look_var,
            font=("Segoe UI", 9), fg=TEXT, bg=BG_PANEL,
            activebackground=BG_PANEL, activeforeground=GOLD,
            selectcolor=BG_INPUT, highlightthickness=0,
            command=self._on_old_look_toggle,
        )
        self.old_look_check.pack(fill="x", before=self.status_lbl, pady=(2, 0))
        self.inventory_vfx_check = tk.Checkbutton(
            inner, text=s["inventory_vfx"], variable=self.inventory_vfx_var,
            font=("Segoe UI", 9), fg=TEXT, bg=BG_PANEL,
            activebackground=BG_PANEL, activeforeground=GOLD,
            selectcolor=BG_INPUT, highlightthickness=0,
        )
        self.inventory_vfx_check.pack(fill="x", before=self.status_lbl, pady=(2, 0))

        # --- settings page --------------------------------------------------
        settings_inner = tk.Frame(self.settings_page, bg=BG_PANEL)
        settings_inner.pack(fill="both", expand=True, padx=20, pady=14)
        self.settings_inner = settings_inner
        self.settings_title_lbl = tk.Label(
            settings_inner, text=s["settings_title"], font=FONT_LABEL,
            fg=GOLD, bg=BG_PANEL,
        )
        self.settings_title_lbl.pack(anchor="w", pady=(0, 10))
        self.server_folder_label = tk.Label(
            settings_inner, text=s["server_folder"].upper(), font=FONT_LABEL,
            fg=GOLD, bg=BG_PANEL,
        )
        self.server_folder_label.pack(anchor="w", pady=(0, 0))
        server_path_row = tk.Frame(settings_inner, bg=BG_PANEL)
        server_path_row.pack(fill="x", pady=(2, 0))
        self.server_folder_entry = tk.Entry(
            server_path_row, textvariable=self.server_folder_var,
            font=("Segoe UI", 9), bg=BG_INPUT, fg=TEXT,
            insertbackground=GOLD, relief="flat", highlightthickness=1,
            highlightbackground=BORDER, highlightcolor=RED,
        )
        self.browse_server_btn = self._button(
            server_path_row, s["browse"], self._browse_server_folder,
        )
        self.browse_server_btn.pack(side="right", padx=(6, 0), pady=(2, 0))
        self.server_folder_entry.pack(side="left", fill="x", expand=True, ipady=5)
        self.server_ip_row = self._field(
            settings_inner, s["server_ip"], self.server_ip_var,
        )
        self.client_folder_label = tk.Label(
            settings_inner, text=s["client_folder"].upper(), font=FONT_LABEL,
            fg=GOLD, bg=BG_PANEL,
        )
        self.client_folder_label.pack(anchor="w", pady=(10, 0))
        client_path_row = tk.Frame(settings_inner, bg=BG_PANEL)
        client_path_row.pack(fill="x", pady=(2, 0))
        self.client_dir_entry = tk.Entry(
            client_path_row, textvariable=self.client_dir_var,
            font=("Segoe UI", 9), bg=BG_INPUT, fg=TEXT,
            insertbackground=GOLD, relief="flat", highlightthickness=1,
            highlightbackground=BORDER, highlightcolor=RED,
        )
        self.browse_client_btn = self._button(
            client_path_row, s["browse"], self._browse_client_folder,
        )
        self.browse_client_btn.pack(side="right", padx=(6, 0), pady=(2, 0))
        self.client_dir_entry.pack(side="left", fill="x", expand=True, ipady=5)
        settings_actions = tk.Frame(settings_inner, bg=BG_PANEL)
        settings_actions.pack(fill="x", pady=(14, 0))
        self.save_settings_btn = self._button(
            settings_actions, s["save_settings"], self._save_settings,
        )
        self.save_settings_btn.pack(fill="x", ipady=3)
        self.test_connection_btn = self._button(
            settings_actions, s["test_connection"], self._test_connection,
        )
        self.test_connection_btn.pack(fill="x", ipady=3, pady=(7, 0))
        self.unpack_client_btn = self._button(
            settings_actions, s["unpack_client"], self._unpack_client,
        )
        self.unpack_client_btn.pack(fill="x", ipady=3, pady=(7, 0))
        self.settings_status_var = tk.StringVar(value="")
        self.settings_status_lbl = tk.Label(
            settings_inner, textvariable=self.settings_status_var,
            font=FONT_SMALL, fg=TEXT_DIM, bg=BG_PANEL,
            wraplength=300, justify="left", anchor="w",
        )
        self.settings_status_lbl.pack(fill="x", pady=(10, 0))
        self.account_panel.bind("<Configure>", self._on_account_resize, add="+")
        self.unpack_progress_bar = ttk.Progressbar(
            settings_inner, mode="determinate", maximum=100,
        )

        # buttons
        btns = tk.Frame(inner, bg=BG_PANEL)
        btns.pack(pady=(10, 0), fill="x")

        self.action_btn = self._button(btns, s["login"], self._on_action)
        self.action_btn.pack(fill="x", ipady=6)

        self.toggle_btn = tk.Button(
            btns, text=s["to_register"],
            font=("Segoe UI", 9, "underline"), fg=GOLD, bg=BG_PANEL,
            activebackground=BG_PANEL, activeforeground=RED_HOVER,
            bd=0, cursor="hand2", command=self._toggle_mode,
        )
        self.toggle_btn.pack(pady=(10, 0))

        self.version_lbl = tk.Label(inner, text=s["version"], font=FONT_SMALL,
                                    fg=TEXT_DIM, bg=BG_PANEL)
        self.version_lbl.pack(side="bottom", pady=(14, 0))

        self.mode = "login"
        saved = _load_saved_account()
        if saved is not None:
            self.username_var.set(saved[0])
            self.password_var.set(saved[1])
            self.remember_var.set(True)
            self._busy_refresh()
        self.bind("<Return>", lambda e: self._on_action())

        update_bar = self._panel(self)
        update_bar.pack(
            fill="x", side="bottom", before=content,
            padx=18, pady=(0, 10),
        )
        self.update_bar = update_bar
        self.update_status_var = tk.StringVar(value=s["update_checking"])
        self.update_status_lbl = tk.Label(
            update_bar, textvariable=self.update_status_var,
            font=("Segoe UI", 9, "bold"), fg=TEXT, bg=BG_PANEL,
            anchor="w", padx=14,
        )
        self.update_status_lbl.pack(side="left", fill="x", expand=True, pady=9)
        self.update_btn = self._button(update_bar, s["update_check"], self._check_update_clicked)
        self.update_btn.pack(side="right", padx=10, pady=7)
        self._update_bar_compact = False

        self.bind("<Configure>", self._on_window_resize, add="+")
        self.after_idle(lambda: self._apply_responsive_layout(self.winfo_width()))
        self._load_github_activity()

    def _on_window_resize(self, event) -> None:
        if event.widget is self:
            self._apply_responsive_layout(event.width)
            if hasattr(self, "update_status_lbl"):
                compact = bool(self._responsive_mode and self._responsive_mode[1])
                self.update_status_lbl.configure(
                    wraplength=max(
                        180,
                        event.width - 24 if compact
                        else event.width - self.update_btn.winfo_width() - 70,
                    ),
                )

    def _apply_responsive_layout(self, width: int) -> None:
        mode = "stacked" if width < 900 else "columns"
        compact = width < 720
        layout = (mode, compact)
        if layout == self._responsive_mode:
            return
        self._responsive_mode = layout
        self.feed_panel.grid_forget()
        self.account_panel.grid_forget()
        self.banner.configure(height=108 if compact else 100)
        self.content_frame.pack_configure(
            padx=12 if compact else 18, pady=(8, 8) if compact else (14, 10),
        )
        self.update_bar.pack_configure(padx=12 if compact else 18,
                                       pady=(0, 8 if compact else 10))
        if compact != self._update_bar_compact:
            self._update_bar_compact = compact
            self.update_status_lbl.pack_forget()
            self.update_btn.pack_forget()
            if compact:
                self.update_status_lbl.pack(fill="x", padx=10, pady=(6, 0))
                self.update_btn.pack(anchor="e", padx=8, pady=(2, 6))
            else:
                self.update_status_lbl.pack(side="left", fill="x", expand=True, pady=9)
                self.update_btn.pack(side="right", padx=10, pady=7)
        self.settings_inner.pack_configure(padx=12 if compact else 20,
                                           pady=10 if compact else 14)
        self.title_lbl.configure(font=("Georgia", 18 if compact else 22, "bold"))
        if compact:
            self.lang_label.place(relx=0.5, x=-56, y=2, anchor="ne")
            self.lang_combo.place(relx=0.5, x=-50, y=0, anchor="nw")
        else:
            self.lang_label.place(relx=1.0, x=-198, y=2, anchor="ne")
            self.lang_combo.place(relx=1.0, x=-18, y=0, anchor="ne")
        if mode == "columns":
            self.content_grid.grid_rowconfigure(0, weight=1)
            self.content_grid.grid_rowconfigure(1, weight=0)
            self.content_grid.grid_columnconfigure(0, weight=2, minsize=0)
            self.content_grid.grid_columnconfigure(1, weight=1, minsize=280)
            self.feed_panel.grid(
                row=0, column=0, sticky="nsew", padx=(0, 12),
            )
            self.account_panel.grid(row=0, column=1, sticky="nsew")
        else:
            self.content_grid.grid_rowconfigure(0, weight=1)
            self.content_grid.grid_rowconfigure(1, weight=1)
            # Clear the two-column uniform group used in wide mode. Keeping
            # that group while stacking can force phantom width on column 1
            # and squeeze both panels out of a narrow window.
            self.content_grid.grid_columnconfigure(
                0, weight=1, minsize=0, uniform="",
            )
            self.content_grid.grid_columnconfigure(
                1, weight=0, minsize=0, uniform="",
            )
            self.feed_panel.grid(row=0, column=0, sticky="nsew", pady=(0, 10))
            self.account_panel.grid(row=1, column=0, sticky="nsew")
        if self.active_page == "play":
            self.play_page.pack_configure(padx=10 if compact else 20,
                                          pady=8 if compact else 14)
        self._on_account_resize()

    def _on_account_resize(self, _event=None) -> None:
        if not hasattr(self, "status_lbl") or not hasattr(self, "settings_status_lbl"):
            return
        width = max(260, self.account_panel.winfo_width())
        wrap = max(200, width - 40)
        self.status_lbl.configure(wraplength=wrap)
        self.settings_status_lbl.configure(wraplength=wrap)

    def _resize_page_view(self, event) -> None:
        height = max(event.height, self.page_inner.winfo_reqheight())
        self.page_canvas.itemconfigure(
            self.page_window, width=event.width, height=height,
        )

    def _bind_page_mousewheel(self, _event=None) -> None:
        self.bind_all("<MouseWheel>", self._on_page_mousewheel)

    def _unbind_page_mousewheel(self, _event=None) -> None:
        self.unbind_all("<MouseWheel>")

    def _on_page_mousewheel(self, event) -> None:
        self.page_canvas.yview_scroll(-1 * (event.delta // 120), "units")

    def _on_launcher_language(self, _event=None) -> None:
        language = LANGUAGE_CODES_BY_NAME.get(self.lang_choice.get())
        if language is None:
            return
        self.lang = language
        self.game_language_var.set(language)
        _save_launcher_language(self.lang)
        self._apply_language()

    def _show_launcher_page(self, page: str) -> None:
        if page == self.active_page:
            return
        if self.active_page == "play":
            self.play_page.pack_forget()
        else:
            self.settings_page.pack_forget()
        self.active_page = page
        if page == "settings":
            self.settings_page.pack(fill="both", expand=True)
        else:
            compact = bool(self._responsive_mode and self._responsive_mode[1])
            self.play_page.pack(
                fill="both", expand=True,
                padx=10 if compact else 20, pady=8 if compact else 14,
            )
        self.play_tab_btn.config(fg=GOLD if page == "play" else TEXT_DIM)
        self.settings_tab_btn.config(fg=GOLD if page == "settings" else TEXT_DIM)

    def _settings_status(self, text: str, color: str = TEXT_DIM) -> None:
        self.connection_test_state = None
        self.settings_status_var.set(text)
        self.settings_status_lbl.config(fg=color)

    def _selected_client_folder(self) -> Path | None:
        raw = self.client_dir_var.get().strip()
        if not raw:
            return None
        return Path(raw).expanduser().resolve()

    def _selected_server_folder(self) -> Path | None:
        raw = self.server_folder_var.get().strip()
        if not raw:
            return None
        return Path(raw).expanduser().resolve()

    def _browse_server_folder(self) -> None:
        current = self._selected_server_folder()
        initial = current if current and current.is_dir() else app_dir()
        selected = filedialog.askdirectory(
            title=STRINGS[self.lang]["server_folder"],
            initialdir=str(initial), mustexist=True,
        )
        if selected:
            self.server_folder_var.set(str(Path(selected).resolve()))

    def _browse_client_folder(self) -> None:
        current = self._selected_client_folder()
        initial = current if current and current.is_dir() else Path.home()
        selected = filedialog.askdirectory(
            title=STRINGS[self.lang]["client_folder"],
            initialdir=str(initial), mustexist=True,
        )
        if selected:
            folder = Path(selected)
            self.client_dir_var.set(str(folder.resolve()))

    def _save_settings(self) -> None:
        s = STRINGS[self.lang]
        try:
            _parse_server_endpoint(self.server_ip_var.get())
            server_folder = self._selected_server_folder()
            if server_folder is None or not is_server_root(server_folder):
                raise ValueError(s["server_folder_invalid"])
            client_folder = self._selected_client_folder()
            if client_folder is not None:
                if not client_folder.is_dir() or not _client_folder_has_executable(client_folder):
                    raise ValueError(s["client_path_invalid"])
                _save_client_dir(client_folder)
            else:
                _client_dir_path().unlink(missing_ok=True)
            _save_server_folder(server_folder)
            _save_server_address(self.server_ip_var.get())
        except ValueError as exc:
            message = str(exc) if str(exc) in {
                s["client_path_invalid"], s["server_folder_invalid"],
            } else s["err_ip"]
            self._settings_status(message, RED_HOVER)
            return
        except OSError as exc:
            self._settings_status(str(exc), RED_HOVER)
            return
        self._settings_status(s["settings_saved"], OK_GREEN)
        if hasattr(self, "remote_server"):
            local = local_head(server_folder)
            self._show_update_status(self.remote_server, local)

    def _test_connection(self) -> None:
        s = STRINGS[self.lang]
        try:
            _host, api = _parse_server_endpoint(self.server_ip_var.get())
        except ValueError:
            self._settings_status(s["err_ip"], RED_HOVER)
            return
        self.test_connection_btn.config(state="disabled", text=s["testing_connection"])
        self._settings_status(s["testing_connection"])
        self.connection_test_state = ("testing", "")

        def worker():
            try:
                request = urlrequest.Request(
                    f"{api}/api/status", headers={"Accept": "application/json"},
                )
                with urlrequest.urlopen(request, timeout=8) as response:
                    document = json.loads(response.read().decode("utf-8"))
                    if response.status != 200 or not isinstance(document, dict):
                        raise ConnectionError("invalid status response")
                result = (True, "")
            except Exception as exc:
                result = (False, str(exc))
            self.after(0, lambda result=result: self._connection_test_done(*result))

        threading.Thread(target=worker, daemon=True).start()

    def _connection_test_done(self, success: bool, error: str) -> None:
        self.connection_test_state = ("success" if success else "failure", error)
        self._render_connection_test_status()

    def _render_connection_test_status(self) -> None:
        state = getattr(self, "connection_test_state", None)
        if not state:
            return
        s = STRINGS[self.lang]
        kind, error = state
        if kind == "testing":
            self.test_connection_btn.config(
                state="disabled", text=s["testing_connection"],
            )
            self._settings_status(s["testing_connection"], TEXT_DIM)
            self.connection_test_state = ("testing", "")
        elif kind == "success":
            self.test_connection_btn.config(state="normal", text=s["test_connection"])
            self.settings_status_var.set(s["connection_ok"])
            self.settings_status_lbl.config(fg=OK_GREEN)
        else:
            self.test_connection_btn.config(state="normal", text=s["test_connection"])
            self.settings_status_var.set(s["connection_failed"].format(error=error))
            self.settings_status_lbl.config(fg=RED_HOVER)

    def _unpack_client(self) -> None:
        s = STRINGS[self.lang]
        server_folder = self._selected_server_folder()
        if server_folder is None or not is_server_root(server_folder):
            self._settings_status(s["server_folder_invalid"], RED_HOVER)
            return
        source = self._selected_client_folder()
        if source is None:
            self._settings_status(s["client_path_required"], RED_HOVER)
            return
        if not source.is_dir() or not _client_folder_has_executable(source):
            self._settings_status(s["client_path_invalid"], RED_HOVER)
            return
        self.unpack_client_btn.config(state="disabled", text=s["checking_client"])
        self._settings_status(s["checking_client"])

        def worker():
            try:
                from setup_client_assets import verify_complete_client_download

                cache_root = _client_cache_root(source)
                result = verify_complete_client_download(cache_root)
                error = None
            except Exception as exc:
                cache_root = _client_cache_root(source)
                result = None
                error = str(exc)
            self.after(
                0, lambda cache_root=cache_root, result=result, error=error:
                self._client_preflight_done(cache_root, result, error),
            )

        threading.Thread(target=worker, daemon=True).start()

    def _client_preflight_done(
        self, source: Path, result: dict[str, int] | None, error: str | None,
    ) -> None:
        s = STRINGS[self.lang]
        if error or result is None:
            self.unpack_client_btn.config(state="normal", text=s["unpack_client"])
            self._settings_status(
                s["client_not_complete"].format(error=error or ""), RED_HOVER,
            )
            return
        if (not getattr(sys, "frozen", False)
                and not (app_dir() / "setup_client_assets.py").is_file()):
            self.unpack_client_btn.config(state="normal", text=s["unpack_client"])
            self._settings_status(s["unpack_no_script"], RED_HOVER)
            return
        prompt = s["unpack_confirm"].format(bundles=result["bundles"])
        if not messagebox.askyesno(s["unpack_client"], prompt, parent=self):
            self.unpack_client_btn.config(state="normal", text=s["unpack_client"])
            self._settings_status("")
            return
        self._settings_status(s["unpack_running"])
        self.unpack_client_btn.config(state="disabled", text=s["unpack_running"])
        self.unpack_progress_bar.configure(value=0)
        self.unpack_progress_bar.pack(fill="x", pady=(7, 0))
        self._set_unpack_progress(1)
        threading.Thread(
            target=self._run_client_setup, args=(source, self._selected_server_folder()), daemon=True,
        ).start()

    def _run_client_setup(self, source: Path, server_folder: Path | None) -> None:
        if server_folder is None:
            self.after(0, self._client_setup_finished, 2,
                       STRINGS[self.lang]["server_folder_invalid"])
            return
        arguments = [
            "--source", str(source), "--server-dir", str(server_folder),
            "--client-cache", "--force-unpack", "--overwrite-quests-xml",
            "--replace-assets", "--yes",
        ]
        if getattr(sys, "frozen", False):
            from contextlib import redirect_stderr, redirect_stdout
            from setup_client_assets import main as setup_assets

            class ProgressOutput:
                def __init__(self):
                    self.pending = ""
                    self.last_line = ""

                def write(self, chunk):
                    self.pending += chunk
                    lines = self.pending.split("\n")
                    self.pending = lines.pop()
                    for output_line in lines:
                        self._handle_line(output_line)
                    return len(chunk)

                def flush(self):
                    if self.pending:
                        self._handle_line(self.pending)
                        self.pending = ""

                def _handle_line(self, output_line):
                    self.last_line = output_line.strip()
                    if self.last_line.startswith("SCANNING:"):
                        self_outer.after(0, self_outer._set_unpack_scanning)
                        return
                    if self.last_line.startswith("PROGRESS:"):
                        try:
                            percent = max(0, min(100, int(self.last_line.partition(":")[2].strip())))
                        except ValueError:
                            return
                        self_outer.after(0, self_outer._set_unpack_progress, percent)

            self_outer = self
            output = ProgressOutput()
            try:
                with redirect_stdout(output), redirect_stderr(output):
                    return_code = setup_assets(arguments)
            except Exception as exc:
                return_code, output.last_line = 2, str(exc)
            self.after(0, self._client_setup_finished, return_code, output.last_line)
            return

        command = [sys.executable, str(app_dir() / "setup_client_assets.py"), *arguments]
        try:
            process = subprocess.Popen(
                command, cwd=str(app_dir()), stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            last_line = ""
            assert process.stdout is not None
            for line in process.stdout:
                last_line = line.strip()
                if last_line.startswith("SCANNING:"):
                    self.after(0, self._set_unpack_scanning)
                elif last_line.startswith("PROGRESS:"):
                    try:
                        percent = max(0, min(100, int(last_line.partition(":")[2].strip())))
                    except ValueError:
                        continue
                    self.after(0, self._set_unpack_progress, percent)
            return_code = process.wait()
        except Exception as exc:
            self.after(0, self._client_setup_finished, 2, str(exc))
            return
        self.after(0, self._client_setup_finished, return_code, last_line)

    def _client_setup_finished(self, return_code: int, detail: str) -> None:
        s = STRINGS[self.lang]
        self.unpack_client_btn.config(state="normal", text=s["unpack_client"])
        self.unpack_progress_bar.stop()
        self.unpack_progress_bar.pack_forget()
        if return_code == 0:
            self._settings_status(s["unpack_done"], OK_GREEN)
        else:
            message = s["unpack_failed"].format(
                error=detail or f"exit code {return_code}",
            )
            self._settings_status(message, RED_HOVER)

    def _set_unpack_progress(self, percent: int) -> None:
        self.unpack_progress_bar.stop()
        self.unpack_progress_bar.configure(mode="determinate")
        self.unpack_progress_bar.configure(value=percent)
        self._settings_status(
            f"{STRINGS[self.lang]['unpack_running']} {percent}%", TEXT_DIM,
        )

    def _set_unpack_scanning(self) -> None:
        if str(self.unpack_progress_bar["mode"]) != "indeterminate":
            self.unpack_progress_bar.configure(mode="indeterminate")
            self.unpack_progress_bar.start(12)
        self._settings_status(STRINGS[self.lang]["checking_client"], TEXT_DIM)

    def _field(self, parent, label: str, var: tk.StringVar,
               show: str | None = None):
        row = tk.Frame(parent, bg=BG_PANEL)
        row.pack(fill="x", pady=(8, 0))
        tk.Label(row, text=label.upper(), font=FONT_LABEL, fg=GOLD,
                 bg=BG_PANEL).pack(anchor="w")
        entry = tk.Entry(row, textvariable=var, show=show, font=FONT_ENTRY,
                         bg=BG_INPUT, fg=TEXT, insertbackground=GOLD,
                         relief="flat", highlightthickness=1,
                         highlightbackground=BORDER,
                         highlightcolor=RED)
        entry.pack(fill="x", ipady=5, pady=(2, 0))
        return row

    def _load_github_activity(self) -> None:
        server_folder = _configured_server_folder()
        def worker():
            try:
                remote = latest_server_commit()
                local = local_head(server_folder)
                self.after(0, lambda remote=remote: self._show_commit(remote))
                self.after(0, lambda remote=remote, local=local:
                           self._show_update_status(remote, local))
            except Exception as exc:
                error = str(exc)
                self.after(0, lambda error=error: self._show_commit_error(error))
                self.after(0, lambda error=error: self._update_check_failed(error))
        threading.Thread(target=worker, daemon=True).start()

    def _set_commit_text(self, value: str) -> None:
        self.commit_text.config(state="normal")
        self.commit_text.delete("1.0", "end")
        self.commit_text.insert("1.0", value)
        self.commit_text.config(state="disabled")

    def _show_commit(self, commit: dict) -> None:
        self.latest_commit = commit
        self.commit_error = None
        date = commit.get("date", "")[:10]
        heading = f"DSOR_Server  ·  {date}  ·  {commit['sha'][:7]}\n\n"
        self._set_commit_text(heading + commit.get("message", ""))

    def _show_commit_error(self, error: str) -> None:
        self.commit_error = error
        self._set_commit_text(STRINGS[self.lang]["feed_error"].format(error=error))

    def _open_latest_commit(self, _event=None) -> None:
        import webbrowser
        commit = getattr(self, "latest_commit", None)
        if commit:
            webbrowser.open(commit["url"])

    def _show_update_status(self, remote: dict, local: str | None) -> None:
        s = STRINGS[self.lang]
        self.remote_server = remote
        self.local_server_head = local
        if local is None:
            selected_server = self._selected_server_folder()
            if selected_server is not None and is_server_root(selected_server):
                self.update_status_var.set(s["update_available"].format(
                    local="?", remote=remote["sha"][:7],
                ))
                self.update_btn.config(text=s["update_button"], state="normal",
                                       command=self._apply_update_clicked)
            else:
                self.update_status_var.set(
                    s["update_disconnected"].format(sha=remote["sha"][:7])
                )
                self.update_btn.config(text=s["open_repo"], state="normal",
                                       command=self._open_server_repo)
        elif local == remote["sha"]:
            self.update_status_var.set(s["up_to_date"].format(sha=local[:7]))
            self.update_btn.config(text=s["up_to_date_button"], state="disabled")
        else:
            self.update_status_var.set(s["update_available"].format(
                local=local[:7], remote=remote["sha"][:7],
            ))
            self.update_btn.config(text=s["update_button"], state="normal",
                                   command=self._apply_update_clicked)

    def _check_update_clicked(self) -> None:
        s = STRINGS[self.lang]
        server_folder = self._selected_server_folder()
        if server_folder is None:
            self._update_apply_failed(s["server_folder_invalid"])
            return
        self.update_btn.config(state="disabled", text=s["update_checking_button"])
        self.update_status_var.set(s["update_checking"])
        def worker():
            try:
                remote = latest_server_commit()
                local = local_head(server_folder)
                self.after(0, lambda remote=remote, local=local:
                           self._show_update_status(remote, local))
            except Exception as exc:
                error = str(exc)
                self.after(0, lambda error=error: self._update_check_failed(error))
        threading.Thread(target=worker, daemon=True).start()

    def _update_check_failed(self, error: str) -> None:
        s = STRINGS[self.lang]
        self.update_check_error = error
        self.update_status_var.set(s["update_check_failed"].format(error=error))
        self.update_btn.config(text=s["update_retry"], state="normal",
                               command=self._check_update_clicked)

    def _open_server_repo(self) -> None:
        import webbrowser
        webbrowser.open("https://github.com/DSO-Reconstruction/DSOR_Server")

    def _apply_update_clicked(self) -> None:
        s = STRINGS[self.lang]
        remote = getattr(self, "remote_server", None)
        if not remote:
            return
        server_folder = self._selected_server_folder()
        if server_folder is None or not is_server_root(server_folder):
            self._update_apply_failed(s["server_folder_invalid"])
            return
        message = s["update_confirm"].format(
            sha=remote["sha"][:7], message=remote["message"],
        )
        if not messagebox.askyesno(s["update_title"], message, parent=self):
            return
        self.update_btn.config(state="disabled", text=s["update_running_button"])
        self.update_status_var.set(s["update_running"])
        def worker():
            try:
                commit = apply_server_update(
                    server_folder, remote["branch"], remote["sha"],
                )
                self.after(0, lambda commit=commit: self._update_applied(commit))
            except Exception as exc:
                error = str(exc)
                self.after(0, lambda error=error: self._update_apply_failed(error))
        threading.Thread(target=worker, daemon=True).start()

    def _update_applied(self, commit: str) -> None:
        s = STRINGS[self.lang]
        self.update_status_var.set(s["update_done"].format(sha=commit[:7]))
        self.update_btn.config(text=s["up_to_date_button"], state="disabled")
        messagebox.showinfo(
            s["update_done_title"], s["update_done_message"],
            parent=self,
        )

    def _update_apply_failed(self, error: str) -> None:
        s = STRINGS[self.lang]
        self.update_status_var.set(s["update_failed"].format(error=error))
        self.update_btn.config(text=s["update_retry_check"], state="normal",
                               command=self._check_update_clicked)

    def _button(self, parent, text: str, cmd) -> tk.Button:
        return tk.Button(
            parent, text=text, font=FONT_BTN, fg=TEXT, bg=RED,
            activebackground=RED_HOVER, activeforeground=TEXT,
            relief="flat", bd=0, cursor="hand2", command=cmd,
        )

    def _apply_language(self) -> None:
        s = STRINGS[self.lang]
        self.subtitle_lbl.config(text=s["subtitle"])
        self.lang_label.config(text=s["language"])
        self.lang_choice.set(LANGUAGE_NAMES[self.lang])
        self.feed_title_lbl.config(text=s["feed_title"])
        self.feed_hint_lbl.config(text=s["feed_hint"])
        self.play_tab_btn.config(text=s["play"])
        self.settings_tab_btn.config(text=s["settings"])
        self.settings_title_lbl.config(text=s["settings_title"])
        self.client_folder_label.config(text=s["client_folder"].upper())
        self.server_folder_label.config(text=s["server_folder"].upper())
        self.browse_server_btn.config(text=s["browse"])
        self.browse_client_btn.config(text=s["browse"])
        self.save_settings_btn.config(text=s["save_settings"])
        self.test_connection_btn.config(text=s["test_connection"])
        self.unpack_client_btn.config(text=s["unpack_client"])
        self._render_connection_test_status()
        self.toggle_btn.config(text=s["to_register"] if self.mode == "login"
                               else s["to_login"])
        self.version_lbl.config(text=s["version"])
        rows = [self.server_ip_row, self.username_row, self.password_row]
        labels = [s["server_ip"], s["username"], s["password"]]
        if self.mode == "register":
            rows.append(self.pw2_row)
            labels.append(s["password2"])
        for row, label in zip(rows, labels):
            for child in row.winfo_children():
                if isinstance(child, tk.Label):
                    child.config(text=label.upper())
        self.remember_check.config(text=s["remember"])
        self.old_look_check.config(text=s["old_look"])
        self.inventory_vfx_check.config(text=s["inventory_vfx"])
        if getattr(self, "commit_error", None):
            self._show_commit_error(self.commit_error)
        elif hasattr(self, "latest_commit"):
            self._show_commit(self.latest_commit)
        else:
            self._set_commit_text(s["feed_loading"])
        if hasattr(self, "remote_server"):
            self._show_update_status(self.remote_server, self.local_server_head)
        elif hasattr(self, "update_check_error"):
            self.update_status_var.set(
                s["update_check_failed"].format(error=self.update_check_error)
            )
            self.update_btn.config(text=s["update_retry"], state="normal",
                                   command=self._check_update_clicked)
        else:
            self.update_status_var.set(s["update_checking"])
            self.update_btn.config(text=s["update_check"], state="normal",
                                   command=self._check_update_clicked)
        self._busy_refresh()
        self._set_status("")

    # ------------------------------------------------------------ actions ---
    def _toggle_mode(self) -> None:
        s = STRINGS[self.lang]
        if self.mode == "login":
            self.mode = "register"
        else:
            self.mode = "login"
            self.pw2_row.pack_forget()
        rows = [self.username_row, self.password_row]
        if self.mode == "register":
            rows.append(self.pw2_row)
        for row in (self.username_row, self.password_row, self.pw2_row):
            row.pack_forget()
        for row in rows:
            row.pack(fill="x", pady=(8, 0), before=self.remember_check)
        self._apply_language()

    def _on_remember_toggle(self) -> None:
        if not self.remember_var.get():
            try:
                _forget_account()
            except OSError:
                pass
        self._busy_refresh()

    def _on_old_look_toggle(self) -> None:
        _save_old_look(self.old_look_var.get())

    def _set_status(self, msg: str, color: str = TEXT_DIM) -> None:
        self.status_var.set(msg)
        self.status_lbl.config(fg=color)

    def _busy_refresh(self) -> None:
        s = STRINGS[self.lang]
        if str(self.action_btn["state"]) == "disabled":
            self.action_btn.config(text=s["connecting"])
        else:
            saved_account = self.remember_var.get() and self.mode == "login"
            self.action_btn.config(
                text=(s["register"] if self.mode == "register"
                      else s["play_saved"] if saved_account else s["login"])
            )

    def _busy(self, busy: bool) -> None:
        self.action_btn.config(state="disabled" if busy else "normal")
        self._busy_refresh()

    def _on_action(self) -> None:
        s = STRINGS[self.lang]
        username = self.username_var.get().strip()
        password = self.password_var.get()
        server_address = self.server_ip_var.get().strip()
        try:
            server_host, api = _parse_server_endpoint(server_address)
        except ValueError:
            self._set_status(s["err_ip"], RED_HOVER)
            return

        if not username or not password:
            self._set_status(s["err_fields"], RED_HOVER)
            return

        client_exe, _working_dir = find_client_exe()
        if not client_exe:
            title = STRINGS[self.lang]["client_folder"]
            selected = filedialog.askdirectory(
                title=title,
                initialdir=str(_configured_server_folder()),
                mustexist=True,
            )
            if not selected:
                self._set_status(s["err_no_client"], RED_HOVER)
                return
            selected_dir = Path(selected)
            client_exe, working_dir = find_client_exe(selected_dir)
            if not client_exe or working_dir is None:
                self._set_status(s["err_no_client"], RED_HOVER)
                return
            try:
                _save_client_dir(working_dir)
            except OSError:
                self._set_status(s["err_no_client"], RED_HOVER)
                return

        if self.mode == "register":
            if password != self.password2_var.get():
                self._set_status(s["err_pw_match"], RED_HOVER)
                return
            self._busy(True)
            self._set_status(s["status_creating"])
            threading.Thread(target=self._do_register,
                             args=(api, server_host, username, password),
                             daemon=True).start()
        else:
            self._busy(True)
            self._set_status(s["status_connecting"])
            threading.Thread(target=self._do_login,
                             args=(api, server_host, username, password),
                             daemon=True).start()

    def _do_register(self, api: str, server_host: str,
                     username: str, password: str) -> None:
        s = STRINGS[self.lang]
        try:
            status, data = api_post(api, "/api/register",
                                    {"username": username,
                                     "password": password})
        except ConnectionError as exc:
            msg = s["err_conn"]
            self.after(0, self._busy, False)
            self.after(0, self._set_status, msg, RED_HOVER)
            return

        if status == 201:
            self.after(0, self._busy, False)
            self.after(0, self._set_status, s["status_created"], OK_GREEN)
            self.after(400, lambda: self._start_login(
                api, server_host, username, password
            ))
        else:
            msg = s["err_register"].format(code=status)
            self.after(0, self._busy, False)
            self.after(0, self._set_status, msg, RED_HOVER)

    def _do_login(self, api: str, server_host: str,
                  username: str, password: str) -> None:
        self._start_login(api, server_host, username, password)

    def _start_login(self, api: str, server_host: str,
                     username: str, password: str) -> None:
        s = STRINGS[self.lang]
        try:
            status, data = api_post(api, "/api/login",
                                    {"username": username,
                                     "password": password})
        except ConnectionError as exc:
            msg = s["err_conn"]
            self.after(0, self._busy, False)
            self.after(0, self._set_status, msg, RED_HOVER)
            return
        if status != 200:
            msg = s["err_launch"].format(code=status)
            self.after(0, self._busy, False)
            self.after(0, self._set_status, msg, RED_HOVER)
            return

        if self.remember_var.get():
            try:
                _save_account(username, password)
            except Exception as exc:
                self.after(0, self._set_status,
                           f"{s['status_launching']} ({exc})", TEXT_DIM)
        else:
            try:
                _forget_account()
            except OSError:
                pass

        self.after(0, self._set_status, s["status_launching"], OK_GREEN)
        try:
            launch_game(api, username, password, self.lang,
                        server_host_override=server_host,
                        game_language=self.game_language_var.get(),
                        old_look=self.old_look_var.get(),
                        inventory_vfx=self.inventory_vfx_var.get())
        except RuntimeError as exc:
            self.after(0, self._busy, False)
            self.after(0, self._set_status, str(exc), RED_HOVER)
            return
        self.after(300, self.destroy)


def _cleanup_client_session(
    visuals: OldItemVisuals | None, temporary_client: Path | None,
) -> None:
    try:
        if visuals is not None:
            restore_old_item_visuals(visuals)
    finally:
        if temporary_client is not None:
            temporary_client.unlink(missing_ok=True)


def _wait_for_client_exit(
    process: subprocess.Popen, visuals: OldItemVisuals | None,
    temporary_client: Path | None,
) -> None:
    process.wait()
    _cleanup_client_session(visuals, temporary_client)


def launch_game(
    api: str,
    username: str,
    password: str,
    lang: str,
    *,
    server_host_override: str | None = None,
    game_language: str | None = None,
    old_look: bool = False,
    inventory_vfx: bool = False,
) -> None:
    """Login then spawn the game client. Raises RuntimeError on failure."""
    s = STRINGS[lang]
    game_language = game_language or lang
    if game_language not in GAME_LANGUAGE_CODES:
        raise RuntimeError(s["err_language"])
    client_exe, working_dir = find_client_exe()
    if not client_exe:
        raise RuntimeError(s["err_no_client"])

    status, data = api_post(api, "/api/login",
                            {"username": username, "password": password})
    if status != 200:
        raise RuntimeError(s["err_launch"].format(code=status))

    session = data["session"]
    launch = data["launch"]
    session_id = session["session_id"]
    account_id = session["account_id"]
    host = server_host_override or launch["host"]
    address_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
    port = launch["gateway_port"]
    chat_port = launch["chat_port"]
    instance_id = launch["instance_id"]

    cmd = [
        str(client_exe),
        "-ip", f"{address_host}:{port}",
        "-uid", str(account_id),
        "-accid", str(account_id),
        "-sid", str(session_id),
        "-w", "1024",
        "-h", "768",
        "-language", game_language,
        "-serverlanguage", game_language,
        "-instanceid", str(instance_id),
        "-bica", str(host),
        "-bicp", str(chat_port),
        "-useDeltaDNA",
        "-standalone",
    ]
    for log_dir in (working_dir, Path(os.path.expandvars(r"%LOCALAPPDATA%\Temp\DSOClient\dlcache"))):
        for name in ("crashlog", "callstacklog", "crashsysinfolog"):
            try:
                (log_dir / name).unlink(missing_ok=True)
            except OSError:
                pass
    enable_all_cooldown_timers(working_dir)
    if old_look:
        visuals = apply_old_item_visuals(working_dir)
    else:
        # Clean up a patch left behind if the launcher or game was terminated
        # before its normal restore handler ran.
        discard_old_item_visuals(working_dir)
        visuals = None
    temporary_client = None
    try:
        if inventory_vfx:
            temporary_client = create_inventory_vfx_client(client_exe)
            if temporary_client is None:
                raise RuntimeError(s["err_inventory_vfx"])
            cmd[0] = str(temporary_client)
        process = subprocess.Popen(cmd, cwd=str(working_dir))
    except (OSError, RuntimeError) as exc:
        _cleanup_client_session(visuals, temporary_client)
        if isinstance(exc, OSError):
            raise RuntimeError(s["err_launch"].format(code=str(exc))) from exc
        raise
    if visuals is not None:
        remember_client_process(visuals, process.pid)
    if visuals is not None or temporary_client is not None:
        # Keep the interpreter alive after the launcher window closes.
        threading.Thread(
            target=_wait_for_client_exit,
            args=(process, visuals, temporary_client),
            name="cleanup-client-session",
            daemon=False,
        ).start()


if __name__ == "__main__":
    App().mainloop()
