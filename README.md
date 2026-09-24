# mht-viewer-server

Companion localhost server for the **[MHT Viewer](https://greasyfork.org/)** userscript
(GreasyFork link TBD — owner will paste the script URL here).

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
a small menu, always with live status and all options — start, stop,
install/remove auto-start, quit. Start launches the server detached in the
background (this window is only the control panel — close it anytime).
If one is already running, Start asks before restarting it (a restart
clears stored pages, so open view tabs 404).
Only one copy ever runs (a second start exits quietly); stopping stops
every copy. **Stop means stop:** the menu sets a stop-sign so auto-start
will NOT restart the server — only a manual Start lifts it. The task and
all helpers run windowless — no console flashes, ever. The task action is
checked for drift on Start (a renewed interpreter reinstalls it).
Pass `--no-dialogs` to skip the menu. Colors appear on any Windows 10+
console; the fancier Unicode box borders additionally need a UTF-8 codepage
(`chcp 65001`) and a TrueType font (e.g. Consolas) — otherwise you get the
same layout in plain ASCII.

| Command | What it does |
|---|---|
| `python mht-viewer-server.py` | Start (port 8081, falls through 8090, 8091, 18081) |
| `python mht-viewer-server.py --install-task` | Windows Scheduled Task: start at logon, heal every minute |
| `python mht-viewer-server.py --task-status` | Show whether the task is installed |
| `python mht-viewer-server.py --remove-task` | Remove the task |
| `python mht-viewer-server.py --stop-server` | Stop the running server (same as answering "Stop it?" with y) |
| `python mht-viewer-server.py --hide --no-browser` | Start windowless-ish: hide own console, no questions, no browser tab |
| `python mht-viewer-server.py --probe` | Print triage state (listening server? installed task?) |
| `python mht-viewer-server.py --port 8090 --save ./saved --no-browser` | Custom port, archive uploads, no browser tab |

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
