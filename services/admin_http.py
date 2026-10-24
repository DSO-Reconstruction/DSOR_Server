"""Small, same-origin HTTP boundary for the admin CMS (standard library only)."""

import hmac
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sqlite3
from urllib.parse import parse_qs, urlsplit

from dso_server.persistence.account_registry import AccountRegistryError
from dso_server.services.admin import AdminError


ASSETS = Path(__file__).with_name("admin_static")


class AdminHTTPServer(ThreadingHTTPServer):
    # Drain in-flight handlers before the application closes shared stores.
    daemon_threads = False
    block_on_close = True

    def handle_error(self, request, client_address):
        # Request handlers return sanitized errors; disconnected browsers are routine.
        pass


class AdminRequestHandler(BaseHTTPRequestHandler):
    server_version = "DSO-Admin"
    sys_version = ""

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def log_message(self, format, *args):
        pass

    @property
    def service(self):
        return self.server.service

    def _send(self, status, data, content_type="application/json; charset=utf-8", cookie=None):
        body = data if isinstance(data, bytes) else json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; "
                         "img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        self.send_header("Connection", "close")
        if cookie is not None:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.close_connection = True
        self.wfile.write(body)

    def _cookie(self, token, max_age=14400):
        return (f"{self.service.cookie_name}={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={max_age}"
                + ("; Secure" if self.service.secure_cookie else ""))

    def _token(self):
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
            value = cookie.get(self.service.cookie_name)
            return value.value if value else ""
        except Exception:
            return ""

    def _json(self):
        if self.headers.get_content_type() != "application/json":
            raise AdminError(415, "JSON request required")
        if self.headers.get("Transfer-Encoding"):
            raise AdminError(400, "chunked requests are not supported")
        lengths = self.headers.get_all("Content-Length", [])
        if len(lengths) != 1:
            raise AdminError(400, "one content length is required")
        length = int(lengths[0])
        if not 1 <= length <= 16384:
            raise AdminError(413, "request body must be from 1 to 16,384 bytes")
        payload = json.loads(self.rfile.read(length))
        if not isinstance(payload, dict):
            raise ValueError("JSON object required")
        return payload

    def _origin(self):
        origin = self.headers.get("Origin")
        scheme = "https" if self.service.secure_cookie else "http"
        if origin and origin != f"{scheme}://{self.headers.get('Host', '')}":
            raise AdminError(403, "cross-origin requests are not allowed")
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            raise AdminError(403, "cross-site requests are not allowed")

    def _dispatch(self, write):
        try:
            parsed = urlsplit(self.path)
            path = parsed.path
            self._origin()
            assets = {"/": ("index.html", "text/html"), "/admin.css": ("admin.css", "text/css")}
            for name in ("admin.js", "admin_ui.js", "admin_pages.js", "admin_character.js", "admin_booking.js", "admin_live.js", "admin_events.js", "admin_shops.js", "admin_season_pass.js"):
                assets["/" + name] = (name, "text/javascript")
            if not write and path in assets:
                name, kind = assets[path]
                self._send(200, (ASSETS / name).read_bytes(), kind + "; charset=utf-8")
                return
            if write and path == "/api/login":
                payload = self._json()
                token, document = self.service.login(payload.get("username"), payload.get("password"),
                                                     self.client_address[0])
                self._send(200, document, cookie=self._cookie(token))
                return
            token = self._token()
            account, session = self.service.authenticate(token)
            if write:
                csrf = self.headers.get("X-CSRF-Token", "")
                if not hmac.compare_digest(csrf, session["csrf"]):
                    raise AdminError(403, "invalid CSRF token; reload the page")
                document = self.service.post(path, self._json(), account, token)
            else:
                query = {key: values[-1] for key, values in parse_qs(parsed.query).items()}
                document = self.service.get(path, query, account, session)
            self._send(200, document, cookie=self._cookie("", 0) if path == "/api/logout" else None)
        except AdminError as exc:
            self._send(exc.status, {"error": str(exc)})
        except (ValueError, TypeError, AccountRegistryError) as exc:
            self._send(400, {"error": str(exc)})
        except sqlite3.IntegrityError:
            self._send(409, {"error": "the account name or request already exists"})
        except (ConnectionError, TimeoutError, OSError):
            self.close_connection = True
        except Exception as exc:
            print(f"[AdminCMS] request failed: {type(exc).__name__}")
            self._send(500, {"error": "admin request failed; check the server log"})

    def do_GET(self):
        self._dispatch(False)

    def do_POST(self):
        self._dispatch(True)
