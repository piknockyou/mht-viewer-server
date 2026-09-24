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

Double-click works too on Windows: a dialog asks whether to start now and
whether to install auto-start. Run it again anytime to stop or remove
auto-start. Pass `--no-dialogs` to skip the questions.

| Command | What it does |
|---|---|
| `python mht-viewer-server.py` | Start (port 8081, falls through 8090, 8091, 18081) |
| `python mht-viewer-server.py --install-task` | Windows Scheduled Task: start at logon, heal every minute |
| `python mht-viewer-server.py --task-status` | Show whether the task is installed |
| `python mht-viewer-server.py --remove-task` | Remove the task |
| `python mht-viewer-server.py --probe` | Print triage state (listening server? installed task?) |
| `python mht-viewer-server.py --port 8090 --save ./saved --no-browser` | Custom port, archive uploads, no browser tab |

The userscript probes the ports in order, so a moved server keeps working
with zero configuration.

## Notes

- Listening address is always `127.0.0.1` (your own machine). Nothing leaves it.
- Stored pages live in memory (max 50, 1 h TTL) and survive refresh.
- License: AGPL-3.0 (same as the userscript).
