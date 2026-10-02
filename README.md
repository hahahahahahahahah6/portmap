# portmap

Stable names for local dev-server ports. Zero dependencies, stdlib-only Python.

## The problem

Run 3–4 agents (or terminals) at once and every dev server lands on a
different port: 3000 becomes 3001, becomes 5174. Then an agent probes the
wrong port, concludes the app is broken, and "fixes" code that was never
broken. Port numbers are the wrong handle for a service — names are stable,
ports aren't.

`portmap` gives each local service a stable name and keeps the
name → port mapping accurate, so agents (or you) stop guessing ports.

```bash
pip install portmap-cli

portmap name api 3000        # bind the name "api" to port 3000
portmap run api -- npm run dev   # start it with $PORT set to the mapped port
portmap get api               # -> 3000   (agents can call this directly)
portmap list                  # name  port  status table
portmap serve                 # live dashboard at http://127.0.0.1:8471
portmap unname api            # remove a binding
```

## How it works

Bindings live in `~/.portmap.json` (override with `PORTMAP_FILE`).

- `portmap run <name> -- <cmd>` launches the command detached with
  `$PORT` set to the mapped port and records the child pid.
- `portmap watch` (or the watcher thread inside `portmap serve`) polls every
  5 seconds. For tracked pids it resolves the pid's **actual** listening
  ports — via `/proc/net/tcp` on Linux, `netstat -ano` on Windows. If the
  process moved to a new port, the mapping is **updated automatically**.
  Bindings without a tracked pid are checked with a plain TCP connect probe.
- `portmap list` / `get` / `serve` always reflect the latest poll.

Typical agent workflow:

```bash
portmap run api -- npm run dev
PORT=$(portmap get api)   # the agent reads the real port, never guesses
curl http://127.0.0.1:$PORT/health
```

## Honest limitations

- **Polling delay.** Port changes are detected on the next poll (default 5s),
  not instantly. Inside that window `get` can return a stale port.
- **Auto-follow needs `portmap run`.** Automatic port-change tracking only
  works for processes launched via `portmap run` (we track the pid). A plain
  `portmap name api 3000` binding can only be probed up/down — if that
  server restarts on another port, portmap can't know where it went.
- **Naming only.** This solves port naming, not multi-session management.
  If you want session panels and switching, look at Agent Deck, Claude
  Control, and similar — different product, different problem.
- **`$PORT` cooperation required.** `portmap run` sets the `PORT`
  environment variable; your dev server has to respect it (most do:
  Vite, Next.js, Express, etc.). A server with a hardcoded port ignores it.
- **Platform coverage: Windows/Linux first.** pid → port resolution uses
  `/proc` (Linux) and `netstat` (Windows). macOS falls back to TCP probing
  (up/down only). [localdock](https://localdock.app) already covers macOS
  with a menubar app; this tool fills the Windows/Linux gap.
- **No auth on the dashboard.** `portmap serve` binds 127.0.0.1 only and has
  no authentication — it's a local dev tool, don't expose it.

## Commands

| command | what it does |
|---|---|
| `portmap name <name> <port>` | bind a stable name to a port |
| `portmap get <name>` | print the current port (exit 1 if unknown) |
| `portmap list` | table of all bindings with status |
| `portmap unname <name>` | remove a binding |
| `portmap run <name> [--port P] -- <cmd>` | start cmd detached with `$PORT` set; records pid |
| `portmap stop <name>` | stop the tracked process |
| `portmap watch [--interval N] [--once]` | poll and refresh bindings |
| `portmap serve [--port 8471]` | live dashboard + background watcher |

## Development

```bash
pytest          # 15 tests, stdlib only
```

## License

MIT — see [LICENSE](LICENSE).
