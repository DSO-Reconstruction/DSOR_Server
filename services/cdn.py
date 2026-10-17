"""Local CDN HTTP server serving client assets from DSOClient."""

from __future__ import annotations

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import mimetypes
import os
from pathlib import Path
import threading
from urllib.parse import unquote, urlparse

from dso_server.paths import PACKAGE_ROOT

DEFAULT_CDN_PORT = 8082


class CdnRequestHandler(BaseHTTPRequestHandler):
    """Serve cached Drakensang Online CDN files."""

    server_version = "DSO-Local-CDN/1.0"

    def do_HEAD(self) -> None:  # noqa: N802
        self._serve_file(send_body=False)

    def do_GET(self) -> None:  # noqa: N802
        self._serve_file(send_body=True)

    def _serve_file(self, *, send_body: bool = True) -> None:
        parsed = urlparse(self.path)
        path = unquote(parsed.path).lstrip("/")

        # Strip optional /cdndata prefix
        if path.startswith("cdndata/"):
            path = path[len("cdndata/"):]
        elif path == "cdndata":
            path = ""

        file_path = self.server.cdn.resolve(path)
        if self.server.cdn.verbose:
            print(f"[CDN] '{self.path}' (key='{path}') -> {file_path}")
        if file_path is None or not file_path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND, f"File not found: {path}")
            return

        try:
            stat = file_path.stat()
            size = stat.st_size
            mtime = stat.st_mtime
        except OSError as exc:
            self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))
            return

        content_type = "application/octet-stream"
        if file_path.suffix == ".txt":
            content_type = "text/plain; charset=utf-8"
        elif file_path.suffix == ".xml":
            content_type = "application/xml"

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(size))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Last-Modified", self.date_time_string(mtime))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        if send_body:
            try:
                with open(file_path, "rb") as f:
                    while chunk := f.read(64 * 1024):
                        self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def log_message(self, format: str, *args) -> None:
        if self.server.cdn.verbose:
            super().log_message(format, *args)


class CdnService:
    """Local HTTP file server for offline/self-hosted DSO client assets."""

    def __init__(
        self,
        client_dir: str | Path | None = None,
        host: str = "127.0.0.1",
        port: int = DEFAULT_CDN_PORT,
        verbose: bool = False,
    ):
        self.host = host
        self.port = int(port)
        self.verbose = verbose
        if client_dir is None:
            client_dir = os.getenv("DSO_CDN_DATA") or (PACKAGE_ROOT / "DSOClient")
        self.client_dir = Path(client_dir)
        self._index: dict[str, Path] = {}
        self._root_key: str = "f910a2e8a4a19589c8f96e07d4c0051d"
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._rebuild_index()

    @property
    def url(self) -> str:
        host = self.host if self.host not in ("", "0.0.0.0", "::") else "127.0.0.1"
        return f"http://{host}:{self.port}/cdndata"

    @property
    def httpnz_url(self) -> str:
        host = self.host if self.host not in ("", "0.0.0.0", "::") else "127.0.0.1"
        return f"httpnz://{host}:{self.port}/cdndata"

    def _rebuild_index(self) -> None:
        if not self.client_dir.exists():
            return

        # Check __root.txt
        for cand in [self.client_dir / "dlcache" / "__root.txt", self.client_dir / "__root.txt"]:
            if cand.exists():
                val = cand.read_text(encoding="utf-8").strip()
                if val:
                    self._root_key = val
                    self._index["__root.txt"] = cand
                break

        # Scan client_dir root
        for entry in os.scandir(self.client_dir):
            if not entry.is_file():
                continue
            name = entry.name
            path_obj = Path(entry.path)
            self._index[name] = path_obj

            # Strip .<hash>.nz or ._<hash>
            parts = name.split(".")
            if len(parts) >= 3 and parts[-1] == "nz":
                prefix = ".".join(parts[:-2])
                if prefix not in self._index:
                    self._index[prefix] = path_obj
            elif len(parts) >= 2 and parts[-1].startswith("_"):
                prefix = ".".join(parts[:-1])
                if prefix not in self._index:
                    self._index[prefix] = path_obj

        # Subdirectories: bundles, dlcache
        for subdir_name in ("bundles", "dlcache"):
            sub = self.client_dir / subdir_name
            if sub.exists():
                for entry in os.scandir(sub):
                    if entry.is_file():
                        p = Path(entry.path)
                        self._index[f"{subdir_name}/{entry.name}"] = p
                        self._index[f"{subdir_name}_{entry.name}"] = p

    def resolve(self, path: str) -> Path | None:
        """Resolve a CDN request path into an actual file on disk."""
        if not path or path == "__root.txt":
            return self._index.get("__root.txt")

        # Fallback for root key / toc
        if path == self._root_key or path == "__toc":
            cand = self._index.get(f"__toc.{self._root_key}.nz") or self._index.get("__toc")
            if cand:
                return cand

        # Fallback for eventbankmapping
        if path == "eventbankmapping.txt":
            cand = self.client_dir / "dlcache" / "eventbankmapping.txt"
            if cand.is_file():
                return cand

        # 1. Direct match
        if path in self._index:
            return self._index[path]

        # 2. Forward slash to underscore conversion
        underscore_key = path.replace("/", "_")
        if underscore_key in self._index:
            return self._index[underscore_key]

        # 3. Direct path relative to client_dir
        direct = self.client_dir / path
        if direct.is_file():
            return direct

        # 4. Direct path relative to dlcache
        dlcache_direct = self.client_dir / "dlcache" / path
        if dlcache_direct.is_file():
            return dlcache_direct

        return None

    def start(self) -> None:
        if self._httpd is not None:
            return
        httpd = ThreadingHTTPServer((self.host, self.port), CdnRequestHandler)
        httpd.cdn = self
        self._httpd = httpd
        self.port = httpd.server_address[1]
        self._thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.2}, name="cdn-service", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        httpd, self._httpd = self._httpd, None
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None
