"""A page to make an account on, because a launcher line is not a sign-up.

The real service has a website: you register there, and the launcher it hands back
carries ``-accid`` and ``-sid`` already filled in. Nothing else could work -- the
capture shows no password anywhere on the wire, so the credential has to be issued
somewhere off it, and this is that somewhere.

What it serves is deliberately small: a form to register with, a form to sign in with,
and the launcher line that comes back. No sessions, no cookies, no JavaScript. The one
thing it hands out is the ``-sid``, and it hands it out on the page rather than keeping
it, because the client is what needs it.

It keeps its **own** :class:`~dsor.store.Store` on the same file rather than sharing the
server's. A sqlite3 connection belongs to the thread that opened it, and the portal runs
on its own thread beside the selector loop; two connections to one file is what sqlite
is for, and it costs nothing at this scale.

Bound to localhost by default. The password check is scrypt, in the store, but the page
itself is plain HTTP: putting it on a public address would put passwords on the wire in
the clear, which is exactly what the rest of this file exists to avoid.
"""

from __future__ import annotations

import html
import logging
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from dsor.store import Store

log = logging.getLogger(__name__)

#: The launcher line an account is handed, with the two values that are its whole
#: credential. Everything else in it is CDN and window settings; -rootkey is for the
#: content delivery and never reaches a game server. It lives here rather than in
#: server.py because the page and the `account` subcommand both hand it out, and a
#: page cannot import from server.py without dragging the whole server in behind it.
LAUNCHER = (
    '"{client}" -rooturl {root} -cdnurl {root} -rootkey {rootkey} '
    "-ip {host}:{port} -uid 0 -accid {account} -sid {session} "
    "-w 888 -h 544 -language en -serverlanguage en -instanceid 481 "
    "-bica {host} -bicp 2191 -standalone -fullscreen"
)

PAGE = """<!doctype html>
<title>{title}</title>
<style>
 body {{ background:#17130f; color:#e8dcc8; font:15px/1.6 system-ui, sans-serif;
         margin:0; padding:3rem 1rem; }}
 main {{ max-width:34rem; margin:0 auto; }}
 h1 {{ font-size:1.4rem; letter-spacing:.04em; margin:0 0 .3rem; }}
 p.sub {{ color:#9a8d78; margin:0 0 2.2rem; }}
 form {{ background:#221c16; border:1px solid #3a3128; border-radius:6px;
         padding:1.2rem 1.3rem; margin:0 0 1.2rem; }}
 h2 {{ font-size:1rem; margin:0 0 .9rem; color:#d8b878; }}
 label {{ display:block; font-size:.82rem; color:#9a8d78; margin:.7rem 0 .2rem; }}
 input {{ width:100%; box-sizing:border-box; background:#17130f; color:#e8dcc8;
          border:1px solid #3a3128; border-radius:4px; padding:.5rem .6rem;
          font:inherit; }}
 button {{ margin-top:1rem; background:#8a6a2a; color:#fff; border:0;
           border-radius:4px; padding:.55rem 1.1rem; font:inherit; cursor:pointer; }}
 pre {{ background:#0f0c09; border:1px solid #3a3128; border-radius:4px;
        padding:.9rem; overflow-x:auto; white-space:pre-wrap; word-break:break-all;
        color:#b8e8b0; }}
 .bad {{ color:#e08878; }}
 a {{ color:#d8b878; }}
</style>
<main>
<h1>{title}</h1>
<p class="sub">{sub}</p>
{body}
</main>
"""

FORMS = """
<form method="post" action="/register">
 <h2>Create an account</h2>
 <label>Account name</label><input name="name" autocomplete="username" required>
 <label>Password</label>
 <input name="password" type="password" autocomplete="new-password" required>
 <label>Character name</label><input name="character" required>
 <label>Class</label><input name="class" value="warrior">
 <button>Create</button>
</form>
<form method="post" action="/login">
 <h2>Already have one</h2>
 <label>Account name</label><input name="name" autocomplete="username" required>
 <label>Password</label>
 <input name="password" type="password" autocomplete="current-password" required>
 <button>Get a launcher line</button>
</form>
"""


def _launcher(host: str, port: int, account: int, session: str) -> str:
    return LAUNCHER.format(
        client="C:/path/to/dro_client64.exe",
        root="httpnz://your-cdn/cdndata",
        rootkey="0" * 32,
        host=host,
        port=port,
        account=account,
        session=session,
    )


def _handler(
    path: str, host: str, port: int, first_map: str
) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "DrasaPortal"

        def log_message(self, fmt: str, *args) -> None:  # noqa: A003
            log.info("portal: %s", fmt % args)

        def _page(self, title: str, sub: str, body: str, code: int = 200) -> None:
            page = PAGE.format(title=title, sub=sub, body=body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page)))
            self.end_headers()
            self.wfile.write(page)

        def _fields(self) -> dict[str, str]:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > 4096:  # noqa: PLR2004 - a form, not a file
                return {}
            raw = self.rfile.read(length).decode("utf-8", "replace")
            return {
                key: value[0]
                for key, value in urllib.parse.parse_qs(raw).items()
                if value
            }

        def _issued(self, name: str, account: int, session: str, made: str) -> None:
            """The one page that matters: the launcher line, with the credential in it."""
            self._page(
                "Ready",
                f"{made} Paste this into a terminal on the machine the client is on.",
                "<pre>"
                + html.escape(_launcher(host, port, account, session))
                + "</pre><p>Signing in again issues a new <code>-sid</code> and this "
                "one stops working.</p><p><strong>One account plays from one "
                "client.</strong> A second client on this same line is refused, on "
                "purpose &mdash; to run two at once, register a second account and give "
                'each client its own line.</p><p><a href="/">Back</a></p>',
            )

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's name
            if urllib.parse.urlparse(self.path).path != "/":
                self._page("Not here", "No such page.", '<p><a href="/">Back</a></p>', 404)
                return
            self._page(
                "Drasa Online",
                "Make an account, and the launcher line comes back with its "
                "credential already in it.",
                FORMS,
            )

        def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's name
            where = urllib.parse.urlparse(self.path).path
            fields = self._fields()
            name = (fields.get("name") or "").strip()
            password = fields.get("password") or ""
            if not name or not password:
                self._refused("A name and a password, both.")
                return
            store = Store(path)
            try:
                if where == "/register":
                    self._register(store, fields, name, password)
                elif where == "/login":
                    self._login(store, name, password)
                else:
                    self._page(
                        "Not here", "No such page.", '<p><a href="/">Back</a></p>', 404
                    )
            finally:
                store.close()

        def _register(self, store: Store, fields, name: str, password: str) -> None:
            character = (fields.get("character") or "").strip()
            if not character:
                self._refused("A character name too: one character per account, made "
                              "with it.")
                return
            try:
                account = store.add_account(name, password)
            except ValueError as refused:
                self._refused(str(refused))
                return
            store.add_character(
                account.id,
                character,
                (fields.get("class") or "warrior").strip() or "warrior",
                level=1,
                map_name=first_map,
            )
            signed = store.sign_in(name, password)
            if signed is None:  # pragma: no cover - it was just created
                self._refused("The account was created but would not sign in.")
                return
            self._issued(
                name, account.id, signed[1],
                f"Account {account.id} created, with {html.escape(character)} in it.",
            )

        def _login(self, store: Store, name: str, password: str) -> None:
            signed = store.sign_in(name, password)
            if signed is None:
                # One message for both a wrong name and a wrong password, so the page
                # does not say which accounts exist.
                self._refused("No account of that name with that password.")
                return
            account, session = signed
            characters = store.characters_of(account.id)
            self._issued(
                name, account.id, session,
                "Signed in as "
                + (
                    html.escape(characters[0].name or name)
                    if characters
                    else html.escape(name)
                )
                + ".",
            )

        def _refused(self, why: str) -> None:
            self._page(
                "Drasa Online",
                "Make an account, and the launcher line comes back with its "
                "credential already in it.",
                f'<p class="bad">{html.escape(why)}</p>' + FORMS,
                400,
            )

    return Handler


def serve(
    characters: str = "characters.sqlite",
    port: int = 8080,
    bind: str = "127.0.0.1",
    host: str = "127.0.0.1",
    game_port: int = 2190,
    first_map: str = "a0200_kingscity",
) -> ThreadingHTTPServer:
    """Start the portal on its own thread and hand back the server.

    *bind* is where the page listens; *host* and *game_port* are what goes into the
    launcher lines it prints, which is the client's route to the game and not
    necessarily this machine's own address.
    """
    handler = _handler(characters, host, game_port, first_map)
    http = ThreadingHTTPServer((bind, port), handler)
    thread = threading.Thread(target=http.serve_forever, name="portal", daemon=True)
    thread.start()
    log.info("portal: http://%s:%d/ (accounts in %s)", bind, port, characters)
    return http
