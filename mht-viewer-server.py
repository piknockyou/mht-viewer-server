#!/usr/bin/env python3
"""
MHT Viewer Localhost Server v1.5
Companion to the "MHT Viewer" userscript v7.0+.

Protocol:
    POST /upload   Content-Type: text/html   → 200 "http://127.0.0.1:PORT/view/<id>\n"
    GET  /view/<id>                         → 200 text/html
    GET  /health                            → 200 "ok\n"
    GET  /                                  → 200 status page
    OPTIONS *                               → 204 CORS preflight

Usage:
    python mht-viewer-server.py
    python mht-viewer-server.py --port 8090 --save ./saved --no-browser

    # Double-click (Windows): the console narrates each step and asks y/n.
    # Pass --no-dialogs to skip the questions.

    # Keep it alive across reboots/kills (Windows): registers a Scheduled
    # Task that starts the server at logon and re-checks every minute.
    python mht-viewer-server.py --install-task
    python mht-viewer-server.py --task-status
    python mht-viewer-server.py --remove-task
"""

import argparse
import http.server
import socketserver
import subprocess
import threading
import uuid
import time
import sys
import os
import webbrowser
from urllib.parse import urlparse
from collections import OrderedDict

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8081
# Ports the userscript probes in order — keep both lists in sync.
PORT_FALLBACKS = [8090, 8091, 18081]
TASK_NAME = "MHTViewerServer"
MAX_STORED = 50
MAX_AGE_SEC = 60 * 60
MAX_BODY = 256 * 1024 * 1024

store = OrderedDict()
store_lock = threading.Lock()
SAVE_DIR = None


def _pidfile(port):
    import tempfile

    return os.path.join(tempfile.gettempdir(), f"mhtviewer-{port}.pid")


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
                data, ts, meta = entry
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
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), msg))


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


def _find_ours(host, ports, verbose=False):
    """Port where OUR server answers /health, or None.

    An open port alone proves nothing (could be anyone's occupier) —
    identity is the /health body this server returns.

    Probed in parallel: on some Windows setups a closed loopback port hangs
    the connect until timeout instead of refusing, so a sequential probe
    costs timeout × ports (measured 8 s for 4 ports at timeout=2).
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
        for hit in ex.map(_check, ports):
            if hit is not None:
                return hit
    return None


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


_MB_YESNO = 4
_MB_ICONQUESTION = 32
_MB_ICONINFO = 64
_IDYES = 6


def _msgbox(text, title, style):
    import ctypes as _c

    return _c.windll.user32.MessageBoxW(0, text, title, style)


def _task_installed():
    if os.name != "nt":
        return False
    return task_command("status", quiet=True) == 0


def _ask_gui(prompt, default):
    """MessageBox fallback for one y/n question (stdin unusable: pipe/.pyw)."""
    if os.name != "nt":
        return default
    try:
        return (
            _msgbox(prompt, "MHT Viewer Server", _MB_YESNO | _MB_ICONQUESTION) == _IDYES
        )
    except OSError:
        return default


def _ask_yn(prompt, default=False):
    """Console yes/no question. Falls back to a dialog when stdin is dead."""
    suffix = " [Y/n]: " if default else " [y/N]: "
    try:
        ans = input(prompt + suffix).strip().lower()
    except (OSError, EOFError, RuntimeError):
        return _ask_gui(prompt, default)
    if not ans:
        return default
    return ans in ("y", "yes")


def _own_console():
    """True if this process is the only one on its console (own window).

    Double-click gives the script a console window of its own; a terminal
    launch shares the terminal's console with the shell. Only the first
    case may be hidden without stealing someone's terminal.
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


def _free_console():
    """Detach from our console window; the process keeps running headless."""
    import ctypes as _c

    _c.windll.kernel32.FreeConsole()


def _offer_background():
    """Offer to hide our own console window. States the deal up front."""
    if not _own_console():
        return
    hide = _ask_yn(
        "Hide this window? The server keeps running silently in the background. "
        'To stop it later, run this file again and answer "Stop it?" with y',
        default=False,
    )
    if not hide:
        return
    print("Running in background - this window closes now.")
    sys.stdout.flush()
    try:
        _free_console()
    except OSError:
        pass
    _fix_stdio()  # freed-console handles are dead; park streams on NUL


def guided_start(args):
    """Console double-click flow. Returns True to keep serving, False to exit."""
    print(f"MHT Viewer server - probing {args.host} ...")
    ports = [args.port] + [p for p in PORT_FALLBACKS if p != args.port]
    found = _find_ours(args.host, ports, verbose=True)
    if found:
        print(f"Server already running (port {found}).")
        if _ask_yn("Stop it?"):
            if _stop_server(args.host, found):
                print("Server stopped.")
            else:
                print(
                    "Could not stop it automatically (no pidfile — older or manual start).\n"
                    "Stop pythonw.exe via Task Manager."
                )
            return False
        if _task_installed():
            if _ask_yn("Auto-start is installed. Remove auto-start?"):
                ok = task_command("remove") == 0
                print("Auto-start removed." if ok else "Removal failed - see above.")
            return False
        if _ask_yn("Install auto-start (survives restarts)?"):
            ok = task_command("install") == 0
            print("Auto-start installed." if ok else "Install failed - see above.")
        return False
    print("No server listening.")
    if not _ask_yn("Start the server now?", default=True):
        return False
    if not _task_installed():
        if _ask_yn("Install auto-start so the server survives restarts?"):
            ok = task_command("install") == 0
            print("Auto-start installed." if ok else "Install failed - see above.")
    return True


def _task_ps1(action):
    """PowerShell text for scheduled-task install/remove (Windows only)."""
    script = os.path.abspath(__file__)
    pythonw = sys.executable.replace("python.exe", "pythonw.exe")
    if not os.path.exists(pythonw):
        pythonw = sys.executable
    if action == "install":
        # AtLogOn + 1-minute repetition heals kills/reboots within a minute.
        # MultipleInstances IgnoreNew keeps a running server untouched.
        return (
            f"$a = New-ScheduledTaskAction -Execute '{pythonw}' "
            f"-Argument '\"{script}\" --no-browser'\n"
            "$l = New-ScheduledTaskTrigger -AtLogOn\n"
            "$r = New-ScheduledTaskTrigger -Once -At (Get-Date) "
            "-RepetitionInterval (New-TimeSpan -Minutes 1) "
            "-RepetitionDuration ([TimeSpan]::FromDays(365))\n"
            "$s = New-ScheduledTaskSettingsSet "
            "-AllowStartIfOnBatteries -DontStopIfGoingOnBatteries "
            "-StartWhenAvailable -MultipleInstances IgnoreNew "
            "-ExecutionTimeLimit ([TimeSpan]::Zero)\n"
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
            cmd = ["gsudo"] + cmd
        r = subprocess.run(cmd, capture_output=True, text=True, check=False)
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
            _nul = open(os.devnull, "w")  # noqa: SIM115 — process-lifetime handle
            setattr(sys, _name, _nul)


def main():
    _fix_stdio()
    ap = argparse.ArgumentParser(description="MHT Viewer companion server")
    ap.add_argument("--host", default=DEFAULT_HOST)
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--no-browser", action="store_true")
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
        sys.exit(task_command("install"))
    if args.remove_task:
        sys.exit(task_command("remove"))
    if args.task_status:
        sys.exit(task_command("status"))

    # Console prompts: double-click flow only. The scheduled task runs with
    # --no-browser, so it can never pop a dialog from the background.
    if (
        os.name == "nt"
        and not args.no_browser
        and not args.no_dialogs
        and not guided_start(args)
    ):
        sys.exit(0)

    global SAVE_DIR
    SAVE_DIR = args.save

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
    print("=" * 58)
    print(" MHT Viewer Localhost Server")
    print("=" * 58)
    print(f" Listening :  {base_url}")
    print(f" Upload    :  {base_url}upload")
    print(f" Health    :  {base_url}health")
    print(f" TTL       :  {MAX_AGE_SEC // 60} min   Max stored: {MAX_STORED}")
    if SAVE_DIR:
        print(f" Save to   :  {SAVE_DIR}")
    print(" Ctrl+C to stop.")
    print("=" * 58)
    sys.stdout.flush()

    if not args.no_dialogs:
        _offer_background()

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
