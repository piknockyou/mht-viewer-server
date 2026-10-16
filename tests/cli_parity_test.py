#!/usr/bin/env python3
"""CLI/menu parity check: no arguments in, exit code out.

Exercises the command-line surface of the server beside this file (start,
idempotent start, restart, both stop meanings, the from-task rule, guard-rail
usage errors, the probe lines, the banner, a menu render and an upload round
trip) and fails when the menu and the flags stop agreeing.

Safe to run while the real server is up: it works on a scratch port derived
from the server's own constants, refuses to run when a foreign instance sits
on a port its stop sweep would reach, and restores the global stop-sign.
Skips, with a printed reason, where it cannot run without side effects.

Run:  python tests/cli_parity_test.py
Exit: 0 = every check passed or skipped, 1 = a check failed.
"""

from __future__ import annotations

import http.client
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

# basedpyright noise, same audit as the server script: these are deliberate
# side-effect calls whose results nobody needs (a best-effort stop, writing
# the stop-sign) and one wrapped message string. ty and ruff need none of it.
# pyright: reportUnusedCallResult=false
# pyright: reportImplicitStringConcatenation=false

SERVER = Path(__file__).resolve().parents[1] / "mht-viewer-server.py"
SOURCE = SERVER.read_text(encoding="utf-8")


def server_constant(name: str, pattern: str) -> str:
    """Read one constant out of the server source; fail loudly if it moved."""
    found = re.search(pattern, SOURCE, re.MULTILINE)
    if found is None:
        raise SystemExit(
            f"cannot read {name} from {SERVER.name} - did the server change?"
        )
    return found.group(1)


# Derived from the server itself, never restated: a moved port or a new
# fallback must not silently invalidate this test.
DEFAULT_PORT = int(server_constant("DEFAULT_PORT", r"^DEFAULT_PORT = (\d+)"))
PORT_FALLBACKS = [
    int(p)
    for p in server_constant("PORT_FALLBACKS", r"^PORT_FALLBACKS = \[([^\]]*)\]")
    .replace(" ", "")
    .split(",")
    if p
]
SERVER_VERSION = server_constant("SERVER_VERSION", r'^SERVER_VERSION = "([^"]+)"')
SCRATCH_PORT = max(DEFAULT_PORT, *PORT_FALLBACKS) + 4
# A stop sweep from the scratch port reaches the scratch port plus every
# fallback - so those are exactly the ports a foreign server must not occupy.
SWEEP_PORTS = [p for p in PORT_FALLBACKS if p != SCRATCH_PORT]

TMP = Path(tempfile.gettempdir())
MARKER = TMP / "mhtviewer-stopped"
PIDFILE = TMP / f"mhtviewer-{SCRATCH_PORT}.pid"
SAVE_DIR = Path(tempfile.mkdtemp(prefix="mht-cli-parity-save-"))
PAYLOAD = (
    b'<!doctype html><html><head><meta charset="utf-8"></head><body>'
    b"<h1>parity</h1><p>umlaut: \xc3\xa4\xc3\xb6\xc3\xbc \xe2\x82\xac</p>"
    b"</body></html>"
)

ENV = {**os.environ, "MHTV_ALLOW_MULTI": "1"}
passed: list[str] = []
failed: list[str] = []
skipped: list[str] = []


def server(*args: str, timeout: int = 90, stdin: str | None = None):
    """Run the server beside this file and capture its console output."""
    return subprocess.run(
        [sys.executable, str(SERVER), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        input=stdin,
        timeout=timeout,
        env=ENV,
        cwd=str(SERVER.parent),
        check=False,
    )


def health(port: int = SCRATCH_PORT) -> str:
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        conn.request("GET", "/health")
        body = conn.getresponse().read().decode("utf-8", "replace")
        conn.close()
        return body
    except OSError:
        return ""


def our_server_on(port: int) -> bool:
    return health(port) == "ok\n"


def check(name: str, ok: bool, detail: str = "") -> None:
    if ok:
        passed.append(name)
        print(f"  PASS  {name}")
    else:
        failed.append(name)
        print(f"  FAIL  {name}" + (f"  [{detail}]" if detail else ""))


def skip(name: str, why: str) -> None:
    skipped.append(name)
    print(f"  SKIP  {name}  ({why})")


def post_upload(payload: bytes = PAYLOAD) -> str:
    conn = http.client.HTTPConnection("127.0.0.1", SCRATCH_PORT, timeout=10)
    conn.request(
        "POST",
        "/upload",
        body=payload,
        headers={"Content-Type": "text/html"},
    )
    url = conn.getresponse().read().decode().strip()
    conn.close()
    return url


def get(url: str) -> bytes:
    port_and_path = url.split("127.0.0.1:", 1)[1]
    port, path = port_and_path.split("/", 1)
    conn = http.client.HTTPConnection("127.0.0.1", int(port), timeout=10)
    conn.request("GET", "/" + path)
    body = conn.getresponse().read()
    conn.close()
    return body


def wait_for(predicate: Callable[[], bool], seconds: float = 10.0) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.25)
    return predicate()


def stop_scratch() -> None:
    """Best-effort cleanup; never lets a stray scratch instance survive."""
    try:
        server("--stop-server", "--port", str(SCRATCH_PORT), timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        pass


def main() -> int:
    print(f"parity check: {SERVER.name} v{SERVER_VERSION}, scratch port {SCRATCH_PORT}")
    marker_was_set = MARKER.exists()

    # Safety gate: a stop sweep from the scratch port would reach these.
    for port in SWEEP_PORTS:
        if our_server_on(port):
            print(
                f"\nREFUSING TO RUN: our server answers on {port}, which is in the\n"
                f"stop-sweep range of the scratch port {SCRATCH_PORT}. Stop it first\n"
                "(menu 2, or --stop-server) - this test never kills a foreign copy."
            )
            return 0
    if health(SCRATCH_PORT):
        print(f"\nREFUSING TO RUN: something already answers on port {SCRATCH_PORT}.")
        return 0

    try:
        print("\n== guard rails are usage errors (exit 2) ==")
        check(
            "--stay-stopped without --stop-server",
            server("--stay-stopped").returncode == 2,
        )
        check(
            "--start-server with --stop-server",
            server("--start-server", "--stop-server").returncode == 2,
        )
        check(
            "--start-server with --restart-server",
            server("--start-server", "--restart-server").returncode == 2,
        )

        print("\n== probe reports the state the menu shows ==")
        r = server("--probe", "--port", str(SCRATCH_PORT))
        check(
            "probe: server line",
            "server    : not listening" in r.stdout,
            r.stdout[:120],
        )
        check(
            "probe: auto-start line",
            bool(re.search(r"auto-start: (installed|not installed)", r.stdout)),
        )
        check("probe: stop-sign line", "stop-sign :" in r.stdout)

        print("\n== stop for good with nothing running is harmless ==")
        r = server("--stop-server", "--port", str(SCRATCH_PORT), "--stay-stopped")
        check("exit 0", r.returncode == 0, r.stdout + r.stderr)
        check("reports nothing listening", "No server listening." in r.stdout)
        check("says stopped for good", "Stopped for good:" in r.stdout)
        check("stop-sign is set", MARKER.exists())

        r = server("--probe", "--port", str(SCRATCH_PORT))
        check("probe now shows the stop-sign", "stop-sign : set" in r.stdout)

        if marker_was_set:
            skip(
                "start / restart / stop-for-now",
                "stop-sign was already set before the run",
            )
        else:
            print("\n== start clears the stop-sign and serves ==")
            r = server(
                "--start-server",
                "--port",
                str(SCRATCH_PORT),
                "--no-browser",
                "--no-dialogs",
            )
            check("exit 0", r.returncode == 0, r.stdout + r.stderr)
            check("stop-sign cleared", not MARKER.exists())
            check("answers /health", wait_for(lambda: health() == "ok\n"))
            first_pid = PIDFILE.read_text().strip() if PIDFILE.exists() else ""

            print("\n== a repeated start is an idempotent no-op ==")
            r = server(
                "--start-server",
                "--port",
                str(SCRATCH_PORT),
                "--no-browser",
                "--no-dialogs",
            )
            check("exit 0", r.returncode == 0, r.stdout + r.stderr)
            check("says already running", "already running" in r.stdout)
            check(
                "same process kept",
                PIDFILE.exists() and PIDFILE.read_text().strip() == first_pid,
            )

            print("\n== restart replaces the process ==")
            r = server(
                "--restart-server",
                "--port",
                str(SCRATCH_PORT),
                "--no-browser",
                "--no-dialogs",
            )
            check("exit 0", r.returncode == 0, r.stdout + r.stderr)
            check("announced the stop", "Stopping the running copy on port" in r.stdout)
            check(
                "new PID",
                PIDFILE.exists() and PIDFILE.read_text().strip() != first_pid,
                f"first={first_pid}",
            )
            check("healthy after restart", wait_for(lambda: health() == "ok\n"))

            print("\n== plain stop means stop for now ==")
            r = server("--stop-server", "--port", str(SCRATCH_PORT))
            check("exit 0", r.returncode == 0, r.stdout + r.stderr)
            check("says stopped for now", "Stopped for now:" in r.stdout)
            check("port quiet", wait_for(lambda: not health()))
            check("no stop-sign left", not MARKER.exists())

        print("\n== from-task stays dead while the stop-sign is set ==")
        if not MARKER.exists():
            server("--stop-server", "--port", str(SCRATCH_PORT), "--stay-stopped")
        started = time.time()
        r = server(
            "--from-task", "--no-browser", "--port", str(SCRATCH_PORT), timeout=30
        )
        elapsed = time.time() - started
        check("exit 0 (quiet stay-dead)", r.returncode == 0)
        check(f"returns at once ({elapsed:.1f}s)", elapsed < 10)
        check("nothing listening", not health())
        check("stop-sign still set", MARKER.exists())

        print("\n== menu render ==")
        r = server(stdin="6\n")
        for needle in (
            "[1] Start server",
            "[2] Stop for now",
            "[3] Stop for good",
            "[6] Quit",
        ):
            check(f"menu shows {needle!r}", needle in r.stdout)
        check("menu shows the stop-sign state", "Stop-sign :" in r.stdout)
        check("menu exits on 6", r.returncode == 0, r.stdout + r.stderr)

        print("\n== banner and identity ==")
        r = server(
            "--start-server",
            "--port",
            str(SCRATCH_PORT),
            "--save",
            str(SAVE_DIR),
            "--no-browser",
            "--no-dialogs",
        )
        if r.returncode != 0 or not wait_for(lambda: health() == "ok\n"):
            check("server up for the banner/upload checks", False, r.stdout + r.stderr)
        else:
            check(
                "status box names the listening port",
                f"Listening   http://127.0.0.1:{SCRATCH_PORT}/" in r.stdout,
            )
            check(
                "status box names the stop routes", "Stop: menu items 2/3" in r.stdout
            )
            conn = http.client.HTTPConnection("127.0.0.1", SCRATCH_PORT, timeout=5)
            conn.request("HEAD", "/health")
            header = conn.getresponse().getheader("Server") or ""
            conn.close()
            check(
                f"HTTP Server header carries v{SERVER_VERSION}",
                f"MHTViewerServer/{SERVER_VERSION}" in header,
                header,
            )

            print("\n== upload round trip ==")
            url = post_upload()
            check(
                "upload returned a view url",
                url.startswith(f"http://127.0.0.1:{SCRATCH_PORT}/view/"),
                url,
            )
            if url.startswith("http"):
                check("view bytes identical to what was sent", get(url) == PAYLOAD)

            print("\n== --save writes the upload ==")
            wait_for(lambda: bool(list(SAVE_DIR.glob("*.html"))), 8)
            saved = list(SAVE_DIR.glob("*.html"))
            check("one file written", len(saved) == 1, str(saved))
            if saved:
                check(
                    "file name is the timestamp plus the id",
                    bool(
                        re.fullmatch(
                            r"\d{4}-\d{2}-\d{2} \d{2}-\d{2}-\d{2}_[0-9a-f]{12}\.html",
                            saved[0].name,
                        )
                    ),
                    saved[0].name,
                )
                check("saved bytes identical", saved[0].read_bytes() == PAYLOAD)
    finally:
        stop_scratch()
        if marker_was_set and not MARKER.exists():
            MARKER.write_text("stopped\n", encoding="utf-8")
        elif not marker_was_set and MARKER.exists():
            MARKER.unlink()
        if PIDFILE.exists():
            PIDFILE.unlink()
        shutil.rmtree(SAVE_DIR, ignore_errors=True)

    print("\n== after the run ==")
    check("scratch port quiet", not health())
    check("no scratch pidfile left", not PIDFILE.exists())
    check(
        "nothing of ours on the fallback ports",
        not any(our_server_on(p) for p in PORT_FALLBACKS),
    )
    print(f"stop-sign restored: {'set' if MARKER.exists() else 'absent'}")

    print(
        f"\nRESULT: {len(passed)} passed, {len(failed)} failed, {len(skipped)} skipped"
    )
    if failed:
        print("failed checks:")
        for name in failed:
            print(f"  - {name}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
