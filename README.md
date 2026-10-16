# mht-viewer-server

Companion localhost server for the **[MHT Viewer](https://greasyfork.org/en/scripts/597235-mht-viewer)** userscript.

## When do you need this?

Never, to start with. The userscript's **blob view** works with zero setup:
open any local `.mht`/`.mhtml` file and the converted message appears.

Get this server when you want the **server view** — the converted HTML served
at a real `http://127.0.0.1:PORT/view/<id>` address, where:

- other extensions (e.g. SingleFile) and userscripts run normally
  (on `blob:` pages the browser sandbox-blocks them — that is a browser
  rule, not a bug),
- "Save Page As" / Ctrl+S behaves like a normal web page.

## Install & run

Standard library only — no `pip install` anything (Python 3.7+).

```text
python mht-viewer-server.py
```

Double-click works too on Windows: after the WHAT/HOW/WHY banner you get
a small menu, always with live status and all options — start, the two
stops, install/remove auto-start, quit. Start launches the server detached
in the background (this window is only the control panel — close it
anytime). If one is already running, Start asks before restarting it (a
restart clears stored pages, so open view tabs 404).
Only one copy ever runs (a second start exits quietly); stopping stops
every copy.

**Two kinds of stop, and the menu says which is which:**

| Stop | What happens | Flag |
|---|---|---|
| Stop for now (menu 2) | The server goes away; auto-start brings it back within a minute | `--stop-server` |
| Stop for good (menu 3) | A stop-sign is written: auto-start stays off until you start it yourself | `--stop-server --stay-stopped` |

The status block shows the stop-sign state *before* you choose, so the two
cannot be confused. Every menu action has a flag twin (the CLI is a
superset of the menu), so scripts and agents never need the interactive
console. The task and all helpers run windowless — no console flashes,
ever. The task action is checked for drift on Start (a renewed interpreter
reinstalls it; that reinstall needs elevation and says so when Windows
refuses).
Pass `--no-dialogs` to skip the menu. Colors appear on any Windows 10+
console; the fancier Unicode box borders additionally need a UTF-8 codepage
(`chcp 65001`) and a TrueType font (e.g. Consolas) — otherwise you get the
same layout in plain ASCII.

| Command | What it does |
|---|---|
| `python mht-viewer-server.py` | Start (port 8081, falls through 8090, 8091, 18081) |
| `python mht-viewer-server.py --start-server` | Menu item 1: start in the background (an already healthy server is left alone) |
| `python mht-viewer-server.py --restart-server` | Menu item 1 on a running server: stop every copy, then start fresh |
| `python mht-viewer-server.py --stop-server` | Menu item 2: stop for now (auto-start brings it back) |
| `python mht-viewer-server.py --stop-server --stay-stopped` | Menu item 3: stop for good (auto-start stays off until a start) |
| `python mht-viewer-server.py --install-task` | Windows Scheduled Task: start at logon, heal every minute |
| `python mht-viewer-server.py --task-status` | Show whether the task is installed |
| `python mht-viewer-server.py --remove-task` | Remove the task |
| `python mht-viewer-server.py --probe` | Print triage state (listening server? auto-start? stop-sign?) |
| `python mht-viewer-server.py --port 8090 --save ./saved --no-browser` | Custom port, archive uploads, no browser tab |

Exit codes: `0` success (including "nothing to do"), `1` failed or aborted,
`2` bad usage.

The userscript probes the ports in order, so a moved server keeps working
with zero configuration.

## Non-Windows keep-alive (systemd / launchd)

Windows uses `--install-task` (Scheduled Task: logon + 1-minute heal).
On Linux/macOS run the same flags under your platform's keeper —
`--no-browser --no-dialogs` never prompts, so it is safe unattended.
Replace `/path/to/mht-viewer-server.py` with the real path in both examples.

### Linux (systemd user service)

Save as `~/.config/systemd/user/mhtviewer.service`:

```ini
[Unit]
Description=MHT Viewer localhost server
After=network.target

[Service]
ExecStart=/usr/bin/python3 /path/to/mht-viewer-server.py --no-browser --no-dialogs
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
```

```sh
systemctl --user daemon-reload
systemctl --user enable --now mhtviewer
systemctl --user status mhtviewer        # check
journalctl --user -u mhtviewer -f        # logs
```

### macOS (launchd)

Save as `~/Library/LaunchAgents/com.piknockyou.mhtviewer.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
 "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.piknockyou.mhtviewer</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>/path/to/mht-viewer-server.py</string>
    <string>--no-browser</string>
    <string>--no-dialogs</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
</dict>
</plist>
```

```sh
launchctl load ~/Library/LaunchAgents/com.piknockyou.mhtviewer.plist
launchctl unload -w ~/Library/LaunchAgents/com.piknockyou.mhtviewer.plist  # stop
```

No systemd/launchd? Cron fallback (reboot only, no crash-heal):
`@reboot sleep 10; /usr/bin/python3 /path/to/mht-viewer-server.py --no-browser --no-dialogs`

## Notes

- Listening address is always `127.0.0.1` (your own machine). Nothing leaves it.
- Stored pages live in memory (max 50, 1 h TTL) and survive refresh.
- License: AGPL-3.0 (same as the userscript).
