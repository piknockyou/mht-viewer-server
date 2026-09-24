#!/usr/bin/env python3
"""
MHT Viewer Localhost Server v1.17
Companion to the "MHT Viewer" userscript v7.1+.

Protocol:
    POST /upload   Content-Type: text/html   → 200 "http://127.0.0.1:PORT/view/<id>\n"
    GET  /view/<id>                         → 200 text/html
    GET  /health                            → 200 "ok\n"
    GET  /                                  → 200 status page
    OPTIONS *                               → 204 CORS preflight

Only one copy runs at a time (a second start exits quietly); the menu
and --stop-server stop every running copy.

Usage:
    python mht-viewer-server.py
    python mht-viewer-server.py --port 8090 --save ./saved --no-browser

    # Double-click (Windows): banner + menu with live status and options.
    # Pass --no-dialogs to skip the menu.

    # Keep it alive across reboots/kills (Windows): registers a Scheduled
    # Task that starts the server at logon and re-checks every minute.
    python mht-viewer-server.py --install-task
    python mht-viewer-server.py --task-status
    python mht-viewer-server.py --remove-task
"""

import argparse
import http.server
import os
import socketserver
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from collections import OrderedDict
from urllib.parse import urlparse

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8081
# Ports the userscript probes in order — keep both lists in sync.
PORT_FALLBACKS = [8090, 8091, 18081]
TASK_NAME = "MHTViewerServer"
MAX_STORED = 50
MAX_AGE_SEC = 60 * 60
MAX_BODY = 256 * 1024 * 1024
SERVER_VERSION = "1.17"

store = OrderedDict()
store_lock = threading.Lock()
SAVE_DIR = None


# ---- Minimal terminal UI (single-column banner box) ----
# Single-column subset of the KB TUI reference: one renderer, runtime
# unicode detection with an identical-geometry ASCII fallback, bg-aware
# color (plain on light or unknown backgrounds), prose wrapped inside the
# box. Console output stays ASCII unless Unicode is detected — a legacy
# codepage renders box-drawing as mojibake otherwise (hit live in v1.4a).

_TUI_MIN_W = 60
_TUI_MAX_W = 68
_TUI_PAD = 2

_UNICODE_BOX = {
    "TL": "┌",
    "TR": "┐",
    "BL": "└",
    "BR": "┘",
    "H": "─",
    "V": "│",
    "LT": "├",
    "RT": "┤",
}
_ASCII_BOX = {
    "TL": "+",
    "TR": "+",
    "BL": "+",
    "BR": "+",
    "H": "-",
    "V": "|",
    "LT": "+",
    "RT": "+",
}

# (border, title, label, value, good, bad, reset). Dark values are the
# KB §10.4 WCAG-checked palette; light values are checked against white.
_DARK_PAL = (
    "\033[38;5;243m",  # border subtle
    "\033[1m\033[38;5;116m",  # title cyan bold
    "\033[38;5;186m",  # labels yellow
    "\033[38;5;114m",  # values green
    "\033[38;5;114m",  # good (active) green
    "\033[38;5;203m",  # bad (inactive) red
    "\033[0m",
)
_LIGHT_PAL = (
    "\033[38;5;240m",  # border grey
    "\033[1m\033[38;5;18m",  # title dark blue bold
    "\033[38;5;94m",  # labels brown
    "\033[38;5;22m",  # values dark green
    "\033[38;5;22m",  # good (active) dark green
    "\033[38;5;124m",  # bad (inactive) dark red
    "\033[0m",
)
_PLAIN_PAL = ("", "", "", "", "", "", "")

# SetConsoleMode flag that makes ANSI escapes work on modern conhost.
_ENABLE_VT = 0x0004
_tui_vt_cache = None

# Approximate luminance of the 16 classic console colors, index = bg nibble.
_CONSOLE_LUMA = (
    0,
    11,
    92,
    103,
    27,
    38,
    119,
    185,
    69,
    29,
    182,
    211,
    54,
    91,
    227,
    255,
)


def _tui_unicode():
    if sys.platform != "win32":
        return True
    try:
        import ctypes as _c

        return _c.windll.kernel32.GetConsoleOutputCP() == 65001
    except OSError:
        return False


def _tui_vt():
    """True if the console processes ANSI escapes. Enables VT mode on
    Windows conhost (cached); codepage 65001 alone does NOT imply this."""
    global _tui_vt_cache
    if _tui_vt_cache is not None:
        return _tui_vt_cache
    ok = True
    if sys.platform == "win32":
        ok = False
        try:
            import ctypes as _c
            import ctypes.wintypes as _w

            k = _c.windll.kernel32
            h = k.GetStdHandle(-11)
            mode = _w.DWORD()
            if h and k.GetConsoleMode(h, _c.byref(mode)):
                if mode.value & _ENABLE_VT or k.SetConsoleMode(
                    h, mode.value | _ENABLE_VT
                ):
                    ok = True
        except OSError:
            ok = False
    elif os.environ.get("TERM") == "dumb":
        ok = False
    _tui_vt_cache = ok
    return ok


def _tui_console_bg():
    """Console background color nibble, or None (no console / failure)."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes as _c
        import ctypes.wintypes as _w

        class _COORD(_c.Structure):
            _fields_ = [("X", _w.SHORT), ("Y", _w.SHORT)]

        class _SMALL_RECT(_c.Structure):
            _fields_ = [
                ("Left", _w.SHORT),
                ("Top", _w.SHORT),
                ("Right", _w.SHORT),
                ("Bottom", _w.SHORT),
            ]

        class _CSBI(_c.Structure):
            _fields_ = [
                ("dwSize", _COORD),
                ("dwCursorPosition", _COORD),
                ("wAttributes", _w.WORD),
                ("srWindow", _SMALL_RECT),
                ("dwMaximumWindowSize", _COORD),
            ]

        k = _c.windll.kernel32
        h = k.GetStdHandle(-11)
        info = _CSBI()
        if not h or not k.GetConsoleScreenBufferInfo(h, _c.byref(info)):
            return None
        return (info.wAttributes >> 4) & 0xF
    except OSError:
        return None


def _tui_palette():
    # Gated on VT processing ONLY — box-drawing stays on the codepage gate,
    # but colors work with the ASCII box on legacy codepages.
    if not _tui_vt():
        return _PLAIN_PAL
    if sys.platform != "win32":
        return _DARK_PAL
    bg = _tui_console_bg()
    if bg is None:
        return _PLAIN_PAL
    return _DARK_PAL if _CONSOLE_LUMA[bg] < 100 else _LIGHT_PAL


def _tui_wrap(text, width):
    words, cur, out = text.split(), "", []
    for w in words:
        cand = w if not cur else cur + " " + w
        if len(cand) <= width:
            cur = cand
        else:
            if cur:
                out.append(cur)
            cur = w
    if cur:
        out.append(cur)
    return out or [""]


def _tui_align(text, width, center=False):
    if len(text) > width:
        text = text[: width - 3] + "..." if width > 3 else text[:width]
    if center:
        left = (width - len(text)) // 2
        return text.rjust(left + len(text)).ljust(width)
    return text.ljust(width)


def _render_box(items, width, box, pal):
    """One renderer for every banner line. items: ("title"|"label"|"row"|
    ("status", label, value[, color-override])|"blank"|"sep", text).
    Returns one string."""
    bord, title_c, label_c, value_c, _good_c, _bad_c, reset = pal
    inner = width - 2 * _TUI_PAD
    top = bord + box["TL"] + box["H"] * width + box["BR"] + reset
    sep = bord + box["LT"] + box["H"] * width + box["RT"] + reset
    bot = bord + box["BL"] + box["H"] * width + box["BR"] + reset

    def row(text, color=""):
        t = _tui_align(text, inner)
        return (
            bord
            + box["V"]
            + reset
            + " " * _TUI_PAD
            + color
            + t
            + reset
            + " " * _TUI_PAD
            + bord
            + box["V"]
            + reset
        )

    lines = [top]
    for kind, text in items:
        if kind == "sep":
            lines.append(sep)
        elif kind == "blank":
            lines.append(row(""))
        elif kind == "title":
            lines.append(row(_tui_align(text, inner, center=True), title_c))
        elif kind == "label":
            lines.append(row(text, label_c))
        elif kind == "status":
            label, value = text[0], text[1]
            color = text[2] if len(text) > 2 else value_c
            lines.append(row(label.ljust(10) + "  " + value, color))
        else:
            for chunk in _tui_wrap(text, inner):
                lines.append(row(chunk))
    lines.append(bot)
    return "\n".join(lines)


def _tui_setup():
    box = _UNICODE_BOX if _tui_unicode() else _ASCII_BOX
    return box, _tui_palette()


def _box_width(rows):
    return max(_TUI_MIN_W, min(_TUI_MAX_W, max(len(x) for x in rows) + 2 * _TUI_PAD))


def print_banner_head():
    """WHAT/HOW/WHY box. Prints FIRST at startup (no port known yet) so the
    user knows what this thing is before any question is asked.

    NOTE: never spawn a child process here — this runs on the task path,
    where the parent is windowless and ANY child console flashes every
    minute (found live 2026-09-24). VT enabling lives in _tui_vt()
    (SetConsoleMode, no child process)."""
    box, pal = _tui_setup()
    title = f"MHT Viewer Localhost Server v{SERVER_VERSION}"
    items = [
        ("title", title),
        ("sep", ""),
        ("label", "WHAT"),
        (
            "row",
            "Companion to the MHT Viewer userscript. Shows saved .mht mails as clean pages.",
        ),
        ("blank", ""),
        ("label", "HOW"),
        (
            "row",
            "Open any .mht in your browser. The script converts it, sends it here, and opens the view tab for you.",
        ),
        ("blank", ""),
        ("label", "WHY A SERVER"),
        (
            "row",
            "The instant Blob view is sandboxed: no extensions run on it. This serves a real http://127.0.0.1 page where SingleFile and other tools work.",
        ),
    ]
    print(_render_box(items, _box_width([title]), box, pal), flush=True)


def print_banner_status(host, port, save_dir):
    """Status box. Prints AFTER the bind, so the port is the real one."""
    box, pal = _tui_setup()
    good_c = pal[4]
    base = f"http://{host}:{port}/"
    fixed = [
        "Listening :  " + base,
        "Upload    :  " + base + "upload",
        "Health    :  " + base + "health",
        f"TTL       :  {MAX_AGE_SEC // 60} min   Max stored: {MAX_STORED}",
    ]
    if save_dir:
        fixed.append("Save to   :  " + save_dir)
    items = [
        ("status", ("Listening", base, good_c)),
        ("status", ("Upload", base + "upload")),
        ("status", ("Health", base + "health")),
        ("status", ("TTL", f"{MAX_AGE_SEC // 60} min   Max stored: {MAX_STORED}")),
    ]
    if save_dir:
        items.append(("status", ("Save to", save_dir)))
    items.append(("row", "Stop: re-run this file, menu item 2."))
    print(_render_box(items, _box_width(fixed), box, pal), flush=True)


def _pidfile(port):
    import tempfile

    return os.path.join(tempfile.gettempdir(), f"mhtviewer-{port}.pid")


def _stop_marker():
    import tempfile

    return os.path.join(tempfile.gettempdir(), "mhtviewer-stopped")


def _stop_requested():
    """True when the user stopped via menu (stay dead until manual start)."""
    return os.path.exists(_stop_marker())


def _request_stop():
    """Leave the stop-sign so task runs stay dead. Never raises."""
    try:
        with open(_stop_marker(), "w", encoding="utf-8") as f:
            f.write("stopped\n")
    except OSError:
        pass


def _clear_stop():
    """Delete the stop-sign (manual start / install / remove). Never raises."""
    try:
        if os.path.exists(_stop_marker()):
            os.remove(_stop_marker())
    except OSError:
        pass


# No console window for our own children, ever (CursorScribe lesson: a
# task that flashes a console every minute is a bug, not a feature).
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_DETACHED = getattr(subprocess, "DETACHED_PROCESS", 0)

_singleton_handle = None


def _singleton():
    """True if this process owns the one-server lock (else exit quietly).

    Only one server ever runs: Windows named mutex, POSIX lockfile, held
    for the process lifetime (a crash releases it automatically). The loser
    exits 0 — same spirit as the task's IgnoreNew. MHTV_ALLOW_MULTI=1
    escapes for tests only.
    """
    global _singleton_handle
    if os.environ.get("MHTV_ALLOW_MULTI"):
        return True
    if os.name == "nt":
        try:
            import ctypes as _c

            h = _c.windll.kernel32.CreateMutexW(None, True, "Local\\MHTViewerServer")
            if not h:
                return False
            if _c.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
                _c.windll.kernel32.CloseHandle(h)
                return False
            _singleton_handle = h
            return True
        except OSError:
            return False
    try:
        import fcntl as _f
        import tempfile as _t

        fh = open(os.path.join(_t.gettempdir(), "mhtviewer-single.lock"), "w")
        _f.flock(fh.fileno(), _f.LOCK_EX | _f.LOCK_NB)
        _singleton_handle = fh
        return True
    except OSError:
        return False


def _pythonw():
    """Windowless interpreter next to this one, else this one."""
    exe = sys.executable
    if os.name == "nt" and exe.lower().endswith("python.exe"):
        from pathlib import Path as _P

        cand = _P(exe).with_name("pythonw.exe")
        if cand.exists():
            return str(cand)
    return exe


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "MHTViewerServer/1.0"
    protocol_version = "HTTP/1.1"

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _send(self, code, body, content_type="text/plain; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _text(self, code, text, ct="text/plain; charset=utf-8"):
        self._send(code, text, ct)

    def _prune(self):
        now = time.time()
        for k in [k for k, (_, ts, _) in store.items() if now - ts > MAX_AGE_SEC]:
            store.pop(k, None)
        while len(store) > MAX_STORED:
            store.popitem(last=False)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = urlparse(self.path).path

        if path in ("/", "/index.html"):
            self._render_index()
            return
        if path == "/health":
            self._text(200, "ok\n")
            return

        if path.startswith("/view/"):
            key = path[len("/view/") :].strip("/")
            if not key or not all(c in "0123456789abcdef" for c in key):
                self._text(400, "Bad id\n")
                return
            with store_lock:
                entry = store.get(key)
                if entry is None:
                    self._text(404, "Not found or expired.\n")
                    return
                data, _ts, _meta = entry
                store.move_to_end(key)
            self._send(200, data, "text/html; charset=utf-8")
            return

        self._text(404, "Unknown path: " + path)

    def do_POST(self):
        path = urlparse(self.path).path
        if path != "/upload":
            self._text(404, "POST only at /upload\n")
            return

        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            self._text(400, "Bad Content-Length\n")
            return
        if length > MAX_BODY:
            self._text(413, "Payload too large\n")
            return

        body = b""
        remaining = length
        while remaining > 0:
            chunk = self.rfile.read(min(65536, remaining))
            if not chunk:
                break
            body += chunk
            remaining -= len(chunk)

        key = uuid.uuid4().hex[:12]
        meta = {
            "size": len(body),
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "ua": self.headers.get("User-Agent", "?")[:120],
        }
        with store_lock:
            store[key] = (body, time.time(), meta)
            self._prune()

        if SAVE_DIR:
            try:
                os.makedirs(SAVE_DIR, exist_ok=True)
                safe_time = meta["time"].replace(":", "-")
                fp = os.path.join(SAVE_DIR, f"{safe_time}_{key}.html")
                with open(fp, "wb") as f:
                    f.write(body)
            except OSError as e:
                print(f"[!] could not save to disk: {e}", file=sys.stderr)

        host = self.headers.get("Host", f"{DEFAULT_HOST}:{DEFAULT_PORT}")
        url = f"http://{host}/view/{key}"
        print(f"[+] stored {len(body):>9} bytes  id={key}  -> {url}", flush=True)
        self._text(200, url + "\n")

    def _render_index(self):
        with store_lock:
            self._prune()
            items = list(store.items())
        rows = []
        for key, (_, ts, meta) in reversed(items):
            age = int(time.time() - ts)
            size_kb = meta["size"] / 1024
            rows.append(
                f"<tr>"
                f"<td><a href='/view/{key}'>{key}</a></td>"
                f"<td>{meta['time']}</td>"
                f"<td>{age}s ago</td>"
                f"<td>{size_kb:.1f} KB</td>"
                f"</tr>"
            )
        rows_html = (
            "\n".join(rows) if rows else "<tr><td colspan=4><i>none yet</i></td></tr>"
        )
        page = f"""<!doctype html>
<html><head><meta charset=utf-8><title>MHT Viewer Server</title>
<style>
body{{font-family:system-ui,Segoe UI,sans-serif;max-width:820px;margin:40px auto;padding:0 20px;color:#222}}
h2{{margin-bottom:4px}}
table{{width:100%;border-collapse:collapse;margin-top:16px}}
th,td{{border:1px solid #ddd;padding:8px 12px;text-align:left;font-size:14px}}
th{{background:#f5f5f5}}
a{{color:#0366d6;text-decoration:none}}a:hover{{text-decoration:underline}}
code{{background:#f5f5f5;padding:2px 6px;border-radius:4px;font-size:13px}}
.muted{{color:#888;font-size:13px}}
</style></head>
<body>
<h2>MHT Viewer Localhost Server</h2>
<p class=muted>Listening on <code>http://{DEFAULT_HOST}:{DEFAULT_PORT}</code> &mdash;
open any <code>.mht</code> file in your browser and the userscript will POST the converted
HTML here; a new tab will open automatically.</p>
<h3>Recent uploads</h3>
<table>
<tr><th>ID</th><th>Stored</th><th>Age</th><th>Size</th></tr>
{rows_html}
</table>
<p class=muted>TTL {MAX_AGE_SEC // 60} min &middot; max {MAX_STORED} in memory</p>
</body></html>"""
        self._text(200, page, "text/html; charset=utf-8")

    def log_message(self, fmt, *args):
        msg = fmt % args
        if " 404 " in msg or " 405 " in msg:
            return
        sys.stderr.write(f"[{self.log_date_time_string()}] {msg}\n")


class ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    # No SO_REUSEADDR: a second instance must FAIL (→ port fallback) instead
    # of shadow-binding the same port (Windows lets SO_REUSEADDR sockets
    # share a port and split traffic). SO_REUSEADDR and SO_EXCLUSIVEADDRUSE
    # are mutually exclusive on Windows, so the bind below is a full
    # override, not a setsockopt add-on.
    allow_reuse_address = False

    def server_bind(self):
        import socket as _socket

        try:
            # Windows-only: a second instance must FAIL (→ port fallback)
            # instead of shadow-binding the same port. Raises OSError on
            # Linux/macOS, where a plain bind already rejects doubles, so
            # the fallback works there without it.
            self.socket.setsockopt(_socket.SOL_SOCKET, _socket.SO_EXCLUSIVEADDRUSE, 1)
        except OSError:
            pass
        self.socket.bind(self.server_address)
        host, port = self.server_address[:2]
        self.server_name = _socket.getfqdn(host)
        self.server_port = port


def bind_first_free(host, port):
    """Bind the first free port: requested first, then PORT_FALLBACKS.

    Never random — the userscript probes exactly this ordered list, so
    both sides stay in sync with zero configuration.
    """
    candidates = [port] + [p for p in PORT_FALLBACKS if p != port]
    last_err = None
    for p in candidates:
        try:
            return ThreadedHTTPServer((host, p), Handler), p
        except OSError as e:
            last_err = e
    print(f"\nCould not bind to any port {candidates} -- {last_err}", file=sys.stderr)
    print("  Is another copy already running on all of them?", file=sys.stderr)
    sys.exit(1)


def _find_all_ours(host, ports, verbose=False):
    """Every port where OUR server answers /health, in port order.

    An open port alone proves nothing (could be anyone's occupier) —
    identity is the /health body this server returns.

    Probed in parallel: on some Windows setups a closed loopback port hangs
    the connect until timeout instead of refusing, so a sequential probe
    costs timeout x ports (measured 8 s for 4 ports at timeout=2).
    """
    import http.client as _h
    from concurrent.futures import ThreadPoolExecutor

    def _check(p):
        try:
            c = _h.HTTPConnection(host, p, timeout=1)
            c.request("GET", "/health")
            r = c.getresponse()
            if r.status == 200 and r.read() == b"ok\n":
                if verbose:
                    print(f"  {host}:{p} ... ours (already running)")
                return p
            if verbose:
                print(f"  {host}:{p} ... occupied by something else")
        except OSError:
            if verbose:
                print(f"  {host}:{p} ... no answer")
        return None

    with ThreadPoolExecutor(max_workers=len(ports)) as ex:
        return [hit for hit in ex.map(_check, ports) if hit is not None]


def _find_ours(host, ports, verbose=False):
    """First port where OUR server answers /health, or None."""
    hits = _find_all_ours(host, ports, verbose=verbose)
    return hits[0] if hits else None


def _stop_server(host, port):
    """Stop our server on port via its pidfile. True if the port went quiet.

    Stale pidfiles are harmless: the port is re-probed after the kill, and
    the pidfile is removed only when our server is actually gone. No pidfile
    (older/manual starts) → False, caller shows the manual note.
    """
    try:
        with open(_pidfile(port), encoding="utf-8") as f:
            pid = int(f.read().strip())
    except (OSError, ValueError):
        return False
    if os.name == "nt":
        import subprocess as _sp

        _sp.run(
            ["taskkill", "/PID", str(pid), "/F"],
            capture_output=True,
            check=False,
            creationflags=_NO_WINDOW,
        )
    else:
        try:
            os.kill(pid, 15)
        except OSError:
            return False
    time.sleep(2)
    if _find_ours(host, [port]) is None:
        try:
            os.unlink(_pidfile(port))
        except OSError:
            pass
        return True
    return False


def _task_installed():
    if os.name != "nt":
        return False
    return task_command("status", quiet=True) == 0


def _own_console():
    """True if this process is the only one on its console (own window).

    Double-click gives the script a console window of its own; a terminal
    launch shares the terminal's console with the shell. Only the first
    case owns the window (clear-screen gating below).
    """
    if os.name != "nt":
        return False
    try:
        import ctypes as _c
        import ctypes.wintypes as _w

        _arr = (_w.DWORD * 64)()
        return _c.windll.kernel32.GetConsoleProcessList(_arr, 64) == 1
    except OSError:
        return False


def stop_server_cmd(host, ports):
    """Stop EVERY running copy of our server. Returns exit code.

    The menu and --stop-server share this, so flag and menu walks print the
    SAME lines for the same outcome (parity by construction). One server is
    the norm (singleton); the sweep still clears strays from older builds.
    """
    hits = _find_all_ours(host, ports)
    if not hits:
        print("No server listening.")
        return 0
    failed = False
    for port in hits:
        if _stop_server(host, port):
            print(f"Port {port}: server stopped.")
        else:
            print(
                f"Port {port}: could not stop automatically "
                "(no pidfile - older or manual start)."
            )
            failed = True
    if failed:
        print("Stop pythonw.exe via Task Manager for the leftovers.")
    return 1 if failed else 0


def _clear_screen():
    """Viewport + scrollback clear (KB §3.2: replace, never append). Only for
    our own console window — never wipe a shared terminal's scrollback."""
    try:
        print("\x1b[2J\x1b[3J\x1b[H", end="", flush=True)
    except OSError:
        pass


def spawn_server(args):
    """Start the server as a detached background child. Returns its Popen.

    The menu process never serves: this child (windowless pythonw where
    available, stdio silenced, no window ever) is the server. It survives
    menu exit.
    """
    import subprocess as _sp

    cmd = [
        _pythonw(),
        os.path.abspath(__file__),
        "--no-browser",
        "--no-dialogs",
        "--port",
        str(args.port),
    ]
    if args.save:
        cmd += ["--save", args.save]
    return _sp.Popen(
        cmd,
        stdin=_sp.DEVNULL,
        stdout=_sp.DEVNULL,
        stderr=_sp.DEVNULL,
        close_fds=True,
        creationflags=_DETACHED | _NO_WINDOW,
    )


def _launch(args, ports, installed):
    """Fresh-start sequence shared by first start and confirmed restart.

    Clears the stop-sign, heals a drifted task action, spawns the detached
    server and waits for it to answer. Menu-only helper (never the task path).
    """
    _clear_stop()  # manual start lifts any stay-dead marker
    if installed and needs_heal():
        print("Task action drifted (interpreter renewed?) - reinstalling ...")
        heal_ok = task_command("install") == 0
        print("Auto-start healed." if heal_ok else "Heal failed - starting anyway.")
    print("Starting server in the background ...")
    try:
        spawn_server(args)
    except OSError as e:
        print(f"Could not start it: {e}")
        return
    import time as _t

    up = None
    for _ in range(12):
        _t.sleep(0.5)
        up = _find_ours(args.host, ports)
        if up is not None:
            break
    if up is None:
        print("Server did not come up - ports busy?")
        return
    print_banner_status(args.host, up, args.save)
    print("Running in the background. Close this window anytime -")
    print("the server keeps serving. Re-run this file to stop it.")


def launcher_menu(args):
    """Numbered menu loop. Banner + live status every iteration; all options
    always listed (unavailable ones explain why). Never serves — choice 1
    spawns a detached server; quitting leaves everything as it is."""
    ports = [args.port] + [p for p in PORT_FALLBACKS if p != args.port]
    own = _own_console()
    _, _, _, _, good_c, bad_c, reset_c = _tui_palette()
    first = True
    while True:
        if not first and own:
            _clear_screen()
        first = False
        print_banner_head()
        print(f"Probing {args.host} ...")
        found = _find_ours(args.host, ports, verbose=True)
        installed = _task_installed()
        if found:
            print(f"Server    : {good_c}listening{reset_c} http://{args.host}:{found}/")
        else:
            print(f"Server    : {bad_c}not listening{reset_c}")
        if installed:
            print(
                f"Auto-start: {good_c}installed{reset_c} (restarts the server if stopped)"
            )
        else:
            print(f"Auto-start: {bad_c}not installed{reset_c}")
        print("[1] Start server (restart if running)")
        print("[2] Stop server")
        print("[3] Install auto-start")
        print("[4] Remove auto-start")
        print("[5] Quit (leave everything as it is)")
        try:
            choice = input("Choice [1-5]: ").strip()
        except (OSError, EOFError, RuntimeError):
            return False
        if choice == "1":
            if found:
                # Restart, not a second copy: the singleton would exit a
                # fresh spawn quietly. Confirm first — the sweep wipes the
                # live server's in-memory pages (open views 404).
                try:
                    ans = (
                        input(
                            f"Server is running on port {found} - restart it? [y/N]: "
                        )
                        .strip()
                        .lower()
                    )
                except (OSError, EOFError, RuntimeError):
                    continue
                if ans not in ("y", "yes"):
                    print("Kept running - nothing to start.")
                    continue
                print("Stopping the running copy ...")
                stop_server_cmd(
                    args.host, ports
                )  # sweep only: never sets the stop-sign
                if _find_ours(args.host, ports) is not None:
                    # Still up (e.g. no pidfile) — it holds the singleton
                    # lock, so a spawn would die quietly and we'd report the
                    # OLD server as fresh. Abort instead.
                    print("Could not stop it - leaving it alone.")
                    continue
            _launch(args, ports, installed)
            continue
        if choice == "2":
            stop_server_cmd(args.host, ports)
            _request_stop()  # stays dead: task runs exit quietly until Start
            if _task_installed():
                print("Stop-sign set: auto-start will NOT restart it. Start clears it.")
            continue
        if choice == "3":
            if installed:
                print("Auto-start is already installed.")
                continue
            ok = task_command("install") == 0
            print("Auto-start installed." if ok else "Install failed - see above.")
            continue
        if choice == "4":
            if not installed:
                print("Auto-start is not installed.")
                continue
            ok = task_command("remove") == 0
            print("Auto-start removed." if ok else "Removal failed - see above.")
            continue
        if choice == "5":
            return False
        print("Pick 1-5.")


def _task_ps1(action):
    """PowerShell text for scheduled-task install/remove (Windows only)."""
    script = os.path.abspath(__file__)
    pythonw = _pythonw()
    if action == "install":
        # AtLogOn + 1-minute repetition heals kills/reboots within a minute.
        # MultipleInstances IgnoreNew keeps a running server untouched.
        # RestartCount heals crashes silently on top (CursorScribe lesson).
        return (
            f"$a = New-ScheduledTaskAction -Execute '{pythonw}' "
            f"-Argument '\"{script}\" --no-browser --from-task'\n"
            "$l = New-ScheduledTaskTrigger -AtLogOn\n"
            "$r = New-ScheduledTaskTrigger -Once -At (Get-Date) "
            "-RepetitionInterval (New-TimeSpan -Minutes 1) "
            "-RepetitionDuration ([TimeSpan]::FromDays(365))\n"
            "$s = New-ScheduledTaskSettingsSet "
            "-AllowStartIfOnBatteries -DontStopIfGoingOnBatteries "
            "-StartWhenAvailable -MultipleInstances IgnoreNew "
            "-ExecutionTimeLimit ([TimeSpan]::Zero) "
            "-RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)\n"
            f'Register-ScheduledTask -TaskName "{TASK_NAME}" '
            "-Action $a -Trigger $l, $r -Settings $s -Force | Out-Null\n"
            f'Start-ScheduledTask -TaskName "{TASK_NAME}"\n'
            f'Write-Host "Autostart installed: {TASK_NAME} (heals every 1 min)"'
        )
    return (
        "$ConfirmPreference='None'\n"
        f'Unregister-ScheduledTask -TaskName "{TASK_NAME}" '
        "-ErrorAction SilentlyContinue\n"
        f'Write-Host "Autostart removed: {TASK_NAME}"'
    )


def _desired_action():
    exe = _pythonw()
    return exe, f'"{os.path.abspath(__file__)}" --no-browser --from-task'


def _current_action():
    """(exe, args) the registered task would run, or None if missing."""
    if os.name != "nt":
        return None
    try:
        r = subprocess.run(
            ["schtasks", "/query", "/tn", TASK_NAME, "/xml"],
            capture_output=True,
            text=True,
            check=False,
            creationflags=_NO_WINDOW,
        )
        if r.returncode != 0:
            return None
        import html as _h
        import re as _re

        exe = _re.search(r"<Command>(.*?)</Command>", r.stdout, _re.S)
        arg = _re.search(r"<Arguments>(.*?)</Arguments>", r.stdout, _re.S)
        if not exe:
            return None
        return exe.group(1).strip(), _h.unescape(arg.group(1).strip()) if arg else ""
    except OSError:
        return None


def needs_heal():
    """Task missing or its action drifted (uv renewed python, file moved)."""
    cur = _current_action()
    if not cur:
        return True
    want_exe, want_args = _desired_action()
    return (
        os.path.normcase(cur[0]) != os.path.normcase(want_exe)
        or (cur[1] or "") != want_args
    )


def task_command(action, quiet=False):
    """install/remove/status the keep-alive task. Returns exit code."""
    if os.name != "nt":
        print("Scheduled-task autostart is Windows-only.", file=sys.stderr)
        return 1
    if action == "status":
        r = subprocess.run(
            ["schtasks", "/query", "/tn", TASK_NAME, "/fo", "LIST"],
            capture_output=True,
            text=True,
            check=False,
            creationflags=_NO_WINDOW,
        )
        if not quiet:
            print(r.stdout if r.returncode == 0 else f"Task {TASK_NAME}: not installed")
        return 0 if r.returncode == 0 else 1
    import tempfile

    ps1 = os.path.join(tempfile.gettempdir(), f"mhtviewer-{action}-{os.getpid()}.ps1")
    try:
        with open(ps1, "w", encoding="utf-8") as f:
            f.write(_task_ps1(action))
        # gsudo (no prompt when cached) -> direct (works when elevated).
        cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", ps1]
        import shutil

        if shutil.which("gsudo"):
            cmd = ["gsudo", *cmd]
        r = subprocess.run(
            cmd, capture_output=True, text=True, check=False, creationflags=_NO_WINDOW
        )
    finally:
        try:
            os.unlink(ps1)
        except OSError:
            pass
    if not quiet:
        print((r.stdout or "") + (r.stderr or ""))
    return r.returncode


def _fix_stdio():
    """Park stdout/stderr on NUL when unusable.

    pythonw launched from Task Scheduler has no console: the streams may be
    None or carry invalid handles, and the first print() then dies — the
    task reports exit code 1. Manual runs always have working stdio, which
    is why this only bites from the task.
    """
    for _name in ("stdout", "stderr"):
        _stream = getattr(sys, _name, None)
        _broken = _stream is None
        if not _broken:
            try:
                _stream.flush()
            except (OSError, ValueError, AttributeError):
                _broken = True
        if _broken:
            _nul = open(os.devnull, "w")
            setattr(sys, _name, _nul)


def main():
    _fix_stdio()
    ap = argparse.ArgumentParser(description="MHT Viewer companion server")
    ap.add_argument("--host", default=DEFAULT_HOST)
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument(
        "--from-task",
        action="store_true",
        help="task launch: exit quietly while the menu stop-sign is set",
    )
    ap.add_argument("--save", metavar="DIR", help="also write each upload to DIR")
    ap.add_argument(
        "--install-task",
        action="store_true",
        help="register keep-alive scheduled task (Windows) and exit",
    )
    ap.add_argument(
        "--remove-task",
        action="store_true",
        help="unregister the keep-alive task (Windows) and exit",
    )
    ap.add_argument(
        "--task-status",
        action="store_true",
        help="show whether the keep-alive task is installed",
    )
    ap.add_argument(
        "--probe",
        action="store_true",
        help="print triage state (listening server? installed task?) and exit",
    )
    ap.add_argument(
        "--stop-server",
        action="store_true",
        help="stop the running server (same outcome as answering Stop-it? with y) and exit",
    )
    ap.add_argument(
        "--no-dialogs",
        action="store_true",
        help="never prompt (console questions or fallback dialog; also implied by --no-browser)",
    )
    args = ap.parse_args()

    if args.probe:
        found = _find_ours(
            args.host,
            [args.port] + [p for p in PORT_FALLBACKS if p != args.port],
        )
        if found:
            print(f"server: listening http://{args.host}:{found}/")
        else:
            print("server: not listening")
        if os.name == "nt":
            t = task_command("status")
            sys.exit(0 if (found or t == 0) else 1)
        sys.exit(0 if found else 1)

    if args.install_task:
        _clear_stop()  # keep-alive contradicts stay-dead
        sys.exit(task_command("install"))
    if args.remove_task:
        _clear_stop()  # clean slate
        sys.exit(task_command("remove"))
    if args.task_status:
        sys.exit(task_command("status"))
    if args.stop_server:
        sys.exit(
            stop_server_cmd(
                args.host,
                [args.port] + [p for p in PORT_FALLBACKS if p != args.port],
            )
        )

    # Banner head first: the user learns what this is BEFORE any question.
    # The menu reprints it every round; the direct-serve path prints it once.
    menu = os.name == "nt" and not args.no_browser and not args.no_dialogs
    if not menu:
        print_banner_head()

    # Launcher menu: double-click flow only, and it never serves — choice 1
    # spawns a detached server. The scheduled task runs with --no-browser,
    # so it can never prompt from the background.
    if menu:
        launcher_menu(args)
        sys.exit(0)

    global SAVE_DIR
    SAVE_DIR = args.save

    if args.from_task and _stop_requested():
        sys.exit(0)  # user stopped via menu: stay dead, quietly

    if not _singleton():
        print("Another copy is already running - this one exits.")
        sys.exit(0)

    srv, port = bind_first_free(args.host, args.port)
    if port != args.port:
        print(
            f"Port {args.port} busy -- using {port} (userscript probes it automatically)"
        )
    try:
        with open(_pidfile(port), "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
    except OSError:
        pass

    base_url = f"http://{args.host}:{port}/"
    print_banner_status(args.host, port, SAVE_DIR)
    sys.stdout.flush()

    if not args.no_browser:
        threading.Timer(0.4, lambda: webbrowser.open(base_url)).start()

    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...", file=sys.stderr)
        srv.shutdown()
        srv.server_close()
    finally:
        try:
            os.unlink(_pidfile(port))
        except OSError:
            pass


if __name__ == "__main__":
    main()
