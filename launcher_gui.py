"""TEST game launcher.

Login with username/password (or register a new account), then start
dro_client64.exe with an authenticated session. TR/EN UI and game languages.
"""

from __future__ import annotations

import json
import ipaddress
import os
import subprocess
import sys
import threading
import ctypes
import tkinter as tk
from ctypes import wintypes
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from urllib.parse import urlsplit
from urllib import error as urlerror
from urllib import request as urlrequest

from client_settings import enable_all_cooldown_timers

def app_dir() -> Path:
    """Directory of the launcher (exe folder when frozen, not PyInstaller temp)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


APP_TITLE = "TEST CLIENT"
DEFAULT_SERVER_HOST = "127.0.0.1"
DEFAULT_API_PORT = 8080
ICON_PATH = app_dir() / "icon.ico"


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


def _credentials_path() -> Path:
    local_app_data = os.getenv("LOCALAPPDATA")
    base = Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
    return base / "TESTCLIENT" / "saved-account.bin"


def _client_dir_path() -> Path:
    local_app_data = os.getenv("LOCALAPPDATA")
    base = Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
    return base / "TESTCLIENT" / "client-folder.txt"


def _save_client_dir(path: Path) -> None:
    config_path = _client_dir_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(str(path.resolve()), encoding="utf-8")


def _load_client_dir() -> Path | None:
    try:
        value = _client_dir_path().read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return Path(value) if value else None


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

# ---------------------------------------------------------------- strings ---
STRINGS = {
    "tr": {
        "subtitle": "TEST CLIENT",
        "server_ip": "Sunucu IP Adresi",
        "err_ip": "Geçerli bir sunucu IP adresi gir.",
        "username": "Kullanıcı Adı",
        "password": "Şifre",
        "password2": "Şifre (tekrar)",
        "remember": "Hesabı kaydet",
        "play_saved": "OYNA",
        "login": "GİRİŞ YAP",
        "register": "HESAP OLUŞTUR",
        "connecting": "BAĞLANIYOR...",
        "to_register": "Hesabın yok mu? Yeni hesap oluştur",
        "to_login": "Zaten hesabın var mı? Giriş yap",
        "version": "V0.1  |  TEST CLIENT",
        "err_fields": "Kullanıcı adı ve şifre gerekli.",
        "err_pw_match": "Şifreler eşleşmiyor.",
        "status_creating": "Hesap oluşturuluyor...",
        "status_created": "Hesap oluşturuldu! Şimdi giriş yapılıyor...",
        "status_connecting": "Sunucuya bağlanılıyor...",
        "status_launching": "Oyun başlatılıyor...",
        "err_conn": "Sunucuya bağlanılamadı. Sunucu adresini ve bağlantını kontrol edip tekrar dene.",
        "err_no_client": "dro_client64.exe bulunamadı!\nClient\\dlcache veya DSOClient\\dlcache klasörünü kontrol et.",
        "err_launch": "Giriş başarısız (HTTP {code})",
        "err_register": "Kayıt başarısız (HTTP {code})",
        "lang_btn": "EN",
    },
    "en": {
        "subtitle": "TEST CLIENT",
        "server_ip": "Server IP Address",
        "err_ip": "Enter a valid server IP address.",
        "username": "Username",
        "password": "Password",
        "password2": "Password (repeat)",
        "remember": "Remember account",
        "play_saved": "PLAY",
        "login": "SIGN IN",
        "register": "CREATE ACCOUNT",
        "connecting": "CONNECTING...",
        "to_register": "No account yet? Create one",
        "to_login": "Already have an account? Sign in",
        "version": "V0.1  |  TEST CLIENT",
        "err_fields": "Username and password are required.",
        "err_pw_match": "Passwords do not match.",
        "status_creating": "Creating account...",
        "status_created": "Account created! Signing in...",
        "status_connecting": "Connecting to server...",
        "status_launching": "Launching game...",
        "err_conn": "Could not connect to the server. Check the server address and your connection, then try again.",
        "err_no_client": "dro_client64.exe not found!\nCheck the Client\\dlcache or DSOClient\\dlcache folder.",
        "err_launch": "Sign-in failed (HTTP {code})",
        "err_register": "Registration failed (HTTP {code})",
        "lang_btn": "TR",
    },
}


def find_client_exe(
    selected_client_dir: Path | None = None,
) -> tuple[Path | None, Path | None]:
    """Locate the client beside the launcher, at a saved path, or in temp."""
    local_dirs = [
        app_dir() / "DSOClient" / "dlcache",
        app_dir() / "Client" / "dlcache",
        app_dir() / "cloud" / "DSOClient" / "dlcache",
        Path(os.path.expandvars(r"%LOCALAPPDATA%\Temp\DSOClient\dlcache")),
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
        self.resizable(False, False)
        self.lang = "tr"
        self._center(460, 610)
        if ICON_PATH.exists():
            try:
                self.iconbitmap(str(ICON_PATH))
            except Exception:
                pass
        self._build()

    # ---------------------------------------------------------------- ui ---
    def _center(self, w: int, h: int) -> None:
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        self.geometry(f"{w}x{h}+{(sw - w) // 2}+{(sh - h) // 2}")

    def _panel(self, parent, **kw):
        return tk.Frame(parent, bg=BG_PANEL, highlightbackground=BORDER,
                        highlightthickness=1, **kw)

    def _build(self) -> None:
        s = STRINGS[self.lang]

        # --- title banner ---------------------------------------------------
        banner = tk.Frame(self, bg=BG, height=130)
        banner.pack(fill="x")
        banner.pack_propagate(False)

        tk.Label(banner, text="TEST CLIENT", font=FONT_TITLE,
                 fg=GOLD, bg=BG).pack(pady=(24, 0))
        self.subtitle_lbl = tk.Label(banner, text=s["subtitle"],
                                     font=FONT_SUB, fg=TEXT_DIM, bg=BG)
        self.subtitle_lbl.pack()
        sep = tk.Canvas(banner, height=3, bg=RED, highlightthickness=0, bd=0)
        sep.pack(fill="x", pady=(8, 0))

        # --- language switch -------------------------------------------------
        self.lang_btn = tk.Button(
            self, text=s["lang_btn"], font=("Segoe UI", 9, "bold"),
            fg=GOLD, bg=BG_PANEL, activebackground=BG_PANEL,
            activeforeground=RED_HOVER, relief="flat", bd=1,
            highlightbackground=BORDER, cursor="hand2",
            command=self._toggle_lang, width=4,
        )
        self.lang_btn.place(x=460 - 44, y=8)

        # --- form panel -----------------------------------------------------
        panel = self._panel(self)
        panel.pack(fill="both", expand=True, padx=30, pady=(14, 10))

        inner = tk.Frame(panel, bg=BG_PANEL)
        inner.pack(padx=28, pady=20, fill="both", expand=True)

        self.username_var = tk.StringVar()
        self.password_var = tk.StringVar()
        self.password2_var = tk.StringVar()
        self.server_ip_var = tk.StringVar(value=DEFAULT_SERVER_HOST)
        self.remember_var = tk.BooleanVar(value=False)

        self.server_ip_row = self._field(inner, s["server_ip"], self.server_ip_var)
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

    def _button(self, parent, text: str, cmd) -> tk.Button:
        return tk.Button(
            parent, text=text, font=FONT_BTN, fg=TEXT, bg=RED,
            activebackground=RED_HOVER, activeforeground=TEXT,
            relief="flat", bd=0, cursor="hand2", command=cmd,
        )

    # ----------------------------------------------------------- language ---
    def _toggle_lang(self) -> None:
        self.lang = "en" if self.lang == "tr" else "tr"
        self._apply_language()

    def _apply_language(self) -> None:
        s = STRINGS[self.lang]
        self.subtitle_lbl.config(text=s["subtitle"])
        self.lang_btn.config(text=s["lang_btn"])
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
            title = "Client klasörünü seç" if self.lang == "tr" else "Select the Client folder"
            selected = filedialog.askdirectory(
                title=title,
                initialdir=str(app_dir()),
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
                _save_client_dir(
                    working_dir.parent if working_dir.name.lower() == "dlcache"
                    else working_dir
                )
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
                        server_host_override=server_host)
        except RuntimeError as exc:
            self.after(0, self._busy, False)
            self.after(0, self._set_status, str(exc), RED_HOVER)
            return
        self.after(300, self.destroy)


def launch_game(
    api: str,
    username: str,
    password: str,
    lang: str,
    *,
    server_host_override: str | None = None,
) -> None:
    """Login then spawn the game client. Raises RuntimeError on failure."""
    s = STRINGS[lang]
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
        "-language", "en",
        "-serverlanguage", lang,
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
    subprocess.Popen(cmd, cwd=str(working_dir))


if __name__ == "__main__":
    App().mainloop()
