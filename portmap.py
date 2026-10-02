#!/usr/bin/env python3
"""portmap — stable names for local dev-server ports.

When several agents (or several terminals) each start a dev server, ports
drift: 3000 becomes 3001, becomes 5174. Agents then "fix" code that was never
broken because they probed the wrong port. portmap gives each local service a
stable name and keeps the name -> port mapping accurate.

Mechanism (stdlib only, no dependencies):
  * `portmap name api 3000` records a binding in ~/.portmap.json
  * `portmap run api -- npm run dev` launches a command with $PORT set to the
    mapped port and records the child pid
  * `portmap watch` polls (default every 5s): for tracked pids it resolves the
    pid's *actual* listening ports (via /proc on Linux, netstat on Windows).
    If the process moved to a new port, the mapping is updated automatically.
    Bindings without a tracked pid are checked with a TCP connect probe.
  * `portmap serve` shows a tiny live dashboard (stdlib http.server)

Only the port-naming problem is solved here. Multi-session orchestration
(panels, session switching) is deliberately out of scope.
"""

import argparse
import html
import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

VERSION = "0.1.0"
DEFAULT_MAP_FILE = os.path.join(os.path.expanduser("~"), ".portmap.json")
DEFAULT_WATCH_INTERVAL = 5.0
DEFAULT_DASHBOARD_PORT = 8471


# --------------------------------------------------------------------------
# map file
# --------------------------------------------------------------------------

def map_path():
    return os.environ.get("PORTMAP_FILE", DEFAULT_MAP_FILE)


def load_map():
    try:
        with open(map_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_map(data):
    path = map_path()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def valid_port(port):
    return isinstance(port, int) and 1 <= port <= 65535


def die(msg, code=1):
    print(f"portmap: {msg}", file=sys.stderr)
    raise SystemExit(code)


# --------------------------------------------------------------------------
# liveness probing
# --------------------------------------------------------------------------

def port_listening(port, host="127.0.0.1", timeout=0.4):
    """True if something accepts TCP on host:port."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        return s.connect_ex((host, int(port))) == 0
    except (OSError, ValueError, OverflowError):
        return False
    finally:
        s.close()


def pid_alive(pid):
    """True if pid exists (cross-platform)."""
    if not pid:
        return False
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if os.name == "nt":
        try:
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True, text=True, timeout=10,
            ).stdout
            return str(pid) in out
        except (OSError, subprocess.SubprocessError):
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, we just can't signal it
    except OSError:
        return False
    return True


# --------------------------------------------------------------------------
# pid -> listening ports
# --------------------------------------------------------------------------

def parse_proc_net_tcp(text):
    """Parse /proc/net/tcp[/6] text -> {socket_inode: port} for LISTEN sockets."""
    result = {}
    lines = text.strip().splitlines()
    for line in lines[1:]:  # skip header
        parts = line.split()
        if len(parts) < 10:
            continue
        local, state, inode = parts[1], parts[3], parts[9]
        if state != "0A":  # LISTEN
            continue
        try:
            result[inode] = int(local.rsplit(":", 1)[-1], 16)
        except ValueError:
            continue
    return result


def linux_pid_listening_ports(pid):
    """Listening ports of a pid via /proc (Linux, no root needed for own procs)."""
    inode_port = {}
    for proto in ("tcp", "tcp6"):
        try:
            with open(f"/proc/net/{proto}", "r") as f:
                text = f.read()
        except (FileNotFoundError, PermissionError):
            continue
        inode_port.update(parse_proc_net_tcp(text))
    if not inode_port:
        return set()
    ports = set()
    try:
        fds = os.listdir(f"/proc/{pid}/fd")
    except (FileNotFoundError, PermissionError, NotADirectoryError):
        return ports
    for fd in fds:
        try:
            link = os.readlink(os.path.join(f"/proc/{pid}/fd", fd))
        except OSError:
            continue
        m = re.fullmatch(r"socket:\[(\d+)\]", link)
        if m and m.group(1) in inode_port:
            ports.add(inode_port[m.group(1)])
    return ports


def parse_windows_netstat(text):
    """Parse `netstat -ano` text -> [(port, pid)] for LISTENING TCP rows."""
    rows = []
    for line in text.splitlines():
        m = re.match(
            r"\s*TCP\s+(\S+)\s+\S+\s+LISTENING\s+(\d+)\s*$", line, re.IGNORECASE
        )
        if not m:
            continue
        addr, pid = m.group(1), int(m.group(2))
        try:
            port = int(addr.rsplit(":", 1)[-1])
        except ValueError:
            continue
        rows.append((port, pid))
    return rows


def windows_pid_listening_ports(pid):
    try:
        out = subprocess.run(
            ["netstat", "-ano", "-p", "TCP"],
            capture_output=True, text=True, timeout=15,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return set()
    return {port for port, p in parse_windows_netstat(out) if p == int(pid)}


def pid_listening_ports(pid):
    """Best-effort listening ports for a pid; empty set if unresolvable."""
    if not pid:
        return set()
    try:
        if os.name == "nt":
            return windows_pid_listening_ports(pid)
        if sys.platform.startswith("linux"):
            return linux_pid_listening_ports(pid)
    except Exception:
        pass
    return set()


# --------------------------------------------------------------------------
# watcher: refresh one binding
# --------------------------------------------------------------------------

def refresh_binding(name, entry, ports_fn=None, listen_fn=None):
    """Re-check one binding. Returns (new_entry, changed: bool).

    ports_fn(pid) -> set of listening ports (injectable for tests).
    listen_fn(port) -> bool (injectable for tests).
    """
    ports_fn = ports_fn or pid_listening_ports
    listen_fn = listen_fn or port_listening
    entry = dict(entry)
    port = entry.get("port")
    pid = entry.get("pid")
    changed = False

    def mark(status, **kw):
        nonlocal changed
        entry["status"] = status
        entry["updated_at"] = now_iso()
        entry.update(kw)
        return entry, True

    if pid and pid_alive(pid):
        ports = set(ports_fn(pid))
        if port in ports:
            if entry.get("status") != "up":
                return mark("up")
            return entry, False
        if ports:
            # The tracked process moved to a new port: follow it.
            new_port = sorted(ports)[0]
            return mark("up", port=new_port,
                        note=f"auto-updated {port} -> {new_port}")
        return mark("down", note="process alive but not listening")
    if pid and not pid_alive(pid):
        return mark("down", note="process exited")
    # No tracked pid: plain TCP probe of the recorded port.
    if listen_fn(port):
        if entry.get("status") != "up":
            return mark("up")
        return entry, False
    return mark("down", note="nothing listening")


def watch_once():
    """Refresh every binding once. Returns number of changed bindings."""
    data = load_map()
    changed = 0
    for name, entry in data.items():
        new_entry, did_change = refresh_binding(name, entry)
        if did_change:
            data[name] = new_entry
            changed += 1
    if changed:
        save_map(data)
    return changed


def watch_loop(interval, stop_event=None):
    while True:
        try:
            watch_once()
        except Exception as exc:  # never kill the daemon on a bad poll
            print(f"portmap watch: poll error: {exc}", file=sys.stderr)
        if stop_event is not None and stop_event.wait(interval):
            break
        elif stop_event is None:
            time.sleep(interval)


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

def cmd_name(args):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", args.name):
        die(f"invalid name {args.name!r}: use letters, digits, '-' or '_'")
    if not valid_port(args.port):
        die(f"invalid port {args.port}: must be 1-65535")
    data = load_map()
    if not port_listening(args.port):
        print(f"portmap: warning: nothing is listening on port {args.port} yet",
              file=sys.stderr)
    data[args.name] = {
        "port": args.port, "pid": None, "cmd": None,
        "status": "up" if port_listening(args.port) else "down",
        "updated_at": now_iso(),
    }
    save_map(data)
    print(f"{args.name} -> {args.port}")


def cmd_get(args):
    data = load_map()
    entry = data.get(args.name)
    if entry is None:
        die(f"unknown name {args.name!r} (see `portmap list`)")
    print(entry["port"])


def cmd_list(args):
    data = load_map()
    if not data:
        print("no bindings (use `portmap name <name> <port>`)")
        return
    width = max(len(n) for n in data)
    for name in sorted(data):
        e = data[name]
        status = e.get("status", "?")
        extra = ""
        if e.get("pid"):
            extra = f" pid={e['pid']}"
        note = f" ({e['note']})" if e.get("note") else ""
        print(f"{name:<{width}}  {e['port']:<5}  {status}{extra}{note}")


def cmd_unname(args):
    data = load_map()
    if args.name not in data:
        die(f"unknown name {args.name!r}")
    del data[args.name]
    save_map(data)
    print(f"removed {args.name}")


def find_free_port(preferred):
    port = preferred
    for _ in range(200):
        if not port_listening(port):
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                s.bind(("127.0.0.1", port))
                s.close()
                return port
            except OSError:
                pass
            finally:
                try:
                    s.close()
                except OSError:
                    pass
        port += 1
        if port > 65535:
            port = 1024
    die("could not find a free port")


def cmd_run(args):
    data = load_map()
    entry = data.get(args.name, {})
    preferred = entry.get("port") or args.port or 3000
    if not valid_port(preferred):
        die(f"invalid port {preferred}")
    if port_listening(preferred):
        # Don't steal someone else's port; move up.
        actual = find_free_port(preferred + 1)
        print(f"portmap: warning: port {preferred} is taken, "
              f"using {actual} for {args.name!r}", file=sys.stderr)
    else:
        actual = preferred
    env = dict(os.environ, PORT=str(actual))
    cmd = args.run_cmd
    popen_kw = {}
    if os.name == "nt":
        popen_kw["creationflags"] = (subprocess.DETACHED_PROCESS
                                     | subprocess.CREATE_NEW_PROCESS_GROUP)
    else:
        popen_kw["start_new_session"] = True
    try:
        proc = subprocess.Popen(cmd, env=env, **popen_kw)
    except FileNotFoundError:
        die(f"command not found: {cmd[0]}")
    except OSError as exc:
        die(f"could not start command: {exc}")
    data[args.name] = {
        "port": actual, "pid": proc.pid, "cmd": " ".join(cmd),
        "status": "up", "updated_at": now_iso(),
    }
    save_map(data)
    print(f"{args.name} -> {actual} (pid {proc.pid})")


def cmd_stop(args):
    data = load_map()
    entry = data.get(args.name)
    if entry is None:
        die(f"unknown name {args.name!r}")
    pid = entry.get("pid")
    if not pid or not pid_alive(pid):
        entry["status"] = "down"
        entry["pid"] = None
        entry["updated_at"] = now_iso()
        save_map(data)
        print(f"{args.name}: no running process, marked down")
        return
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, timeout=15)
        else:
            os.kill(pid, signal.SIGTERM)
            time.sleep(1.0)
            if pid_alive(pid):
                os.kill(pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError) as exc:
        die(f"could not stop pid {pid}: {exc}")
    if os.name != "nt":
        # Reap the child if it is ours; otherwise it lingers as a zombie and
        # pid_alive() keeps reporting True.
        end = time.time() + 5
        while time.time() < end:
            try:
                done, _ = os.waitpid(pid, os.WNOHANG)
                if done:
                    break
            except ChildProcessError:
                break  # not our child (or already reaped)
            except OSError:
                break
            time.sleep(0.1)
    entry["status"] = "down"
    entry["pid"] = None
    entry["updated_at"] = now_iso()
    save_map(data)
    print(f"stopped {args.name} (pid {pid})")


def cmd_watch(args):
    if args.once:
        n = watch_once()
        print(f"refreshed, {n} changed")
        return
    print(f"portmap watch: polling every {args.interval}s "
          f"(Ctrl+C to stop, map: {map_path()})")
    try:
        watch_loop(args.interval)
    except KeyboardInterrupt:
        print("\nstopped")


# --------------------------------------------------------------------------
# dashboard
# --------------------------------------------------------------------------

DASHBOARD_HTML = """<!doctype html><html><head><meta charset="utf-8">
<meta http-equiv="refresh" content="5">
<title>portmap</title>
<style>
body{{font-family:monospace;background:#0a0e14;color:#c9d1d9;margin:2em}}
h1{{color:#7ee787}}table{{border-collapse:collapse}}
td,th{{border:1px solid #30363d;padding:.4em .8em;text-align:left}}
.up{{color:#7ee787}}.down{{color:#f85149}}.stale{{color:#d29922}}
small{{color:#8b949e}}
</style></head><body>
<h1>portmap</h1>
<p><small>stable names for local dev-server ports &middot; {count} bindings
&middot; refreshed {now} (auto-refresh 5s)</small></p>
<table><tr><th>name</th><th>port</th><th>status</th><th>pid</th><th>command</th></tr>
{rows}
</table>
<p><small>served by <code>portmap serve</code> &middot; stdlib only</small></p>
</body></html>"""


class DashboardHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        data = load_map()
        rows = []
        for name in sorted(data):
            e = data[name]
            status = e.get("status", "?")
            cls = "up" if status == "up" else "down"
            rows.append(
                "<tr><td>{}</td><td>{}</td><td class='{}'>{}</td>"
                "<td>{}</td><td>{}</td></tr>".format(
                    html.escape(name), e.get("port", "?"), cls,
                    html.escape(status),
                    html.escape(str(e.get("pid") or "-")),
                    html.escape(e.get("cmd") or "-")))
        body = DASHBOARD_HTML.format(
            count=len(data), now=html.escape(now_iso()),
            rows="\n".join(rows) or
            "<tr><td colspan=5><small>no bindings yet</small></td></tr>")
        raw = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def make_dashboard_server(port):
    return HTTPServer(("127.0.0.1", port), DashboardHandler)


def cmd_serve(args):
    server = make_dashboard_server(args.port)
    actual = server.server_address[1]
    stop = threading.Event()
    t = threading.Thread(target=watch_loop, args=(args.interval, stop),
                         daemon=True)
    t.start()
    print(f"portmap dashboard: http://127.0.0.1:{actual} "
          f"(watching every {args.interval}s, Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.server_close()


# --------------------------------------------------------------------------
# cli
# --------------------------------------------------------------------------

def build_parser():
    p = argparse.ArgumentParser(
        prog="portmap",
        description="Stable names for local dev-server ports.")
    p.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    sub = p.add_subparsers(dest="cmd", metavar="<command>")

    s = sub.add_parser("name", help="bind a stable name to a local port")
    s.add_argument("name"); s.add_argument("port", type=int)
    s.set_defaults(func=cmd_name)

    s = sub.add_parser("get", help="print the current port for a name")
    s.add_argument("name")
    s.set_defaults(func=cmd_get)

    s = sub.add_parser("list", help="list all bindings")
    s.set_defaults(func=cmd_list)

    s = sub.add_parser("unname", help="remove a binding")
    s.add_argument("name")
    s.set_defaults(func=cmd_unname)

    s = sub.add_parser("run",
                       help="start a command with $PORT set to the mapped port")
    s.add_argument("name")
    s.add_argument("--port", type=int, default=None,
                   help="preferred port if the name has no binding yet")
    s.add_argument("run_cmd", nargs="*",
                   help="command to run (after --; pre-parsed by main)")
    s.set_defaults(func=cmd_run)

    s = sub.add_parser("stop", help="stop the tracked process for a name")
    s.add_argument("name")
    s.set_defaults(func=cmd_stop)

    s = sub.add_parser("watch", help="poll and refresh bindings")
    s.add_argument("--interval", type=float, default=DEFAULT_WATCH_INTERVAL)
    s.add_argument("--once", action="store_true",
                   help="single refresh pass, then exit")
    s.set_defaults(func=cmd_watch)

    s = sub.add_parser("serve", help="live dashboard + background watcher")
    s.add_argument("--port", type=int, default=DEFAULT_DASHBOARD_PORT)
    s.add_argument("--interval", type=float, default=DEFAULT_WATCH_INTERVAL)
    s.set_defaults(func=cmd_serve)

    return p


def split_run_args(raw):
    """Pre-parse `portmap run ...` so argparse.REMAINDER can't swallow --port.

    Returns (head_argv, port_or_None, command_list). head_argv is
    ["run", name] suitable for argparse; the command tail is extracted either
    at `--` or, when `--` is absent, as everything after the name.
    """
    i = raw.index("run")
    rest = raw[i + 1:]
    if "--" in rest:
        j = rest.index("--")
        command, head = rest[j + 1:], rest[:j]
    else:
        head, command = rest, None
    port = None
    name = None
    leftover = []
    k = 0
    while k < len(head):
        tok = head[k]
        if tok == "--port" and k + 1 < len(head):
            try:
                port = int(head[k + 1])
            except ValueError:
                die(f"invalid port {head[k + 1]!r}")
            k += 2
        elif tok.startswith("--port="):
            try:
                port = int(tok.split("=", 1)[1])
            except ValueError:
                die(f"invalid port {tok!r}")
            k += 1
        elif name is None and not tok.startswith("-"):
            name, leftover = tok, leftover
            k += 1
        else:
            leftover.append(tok)
            k += 1
    if name is None:
        die("run needs a name: portmap run <name> -- <command...>")
    if command is None:
        # No `--`: everything after the name (dashes allowed) is the command.
        command, leftover = leftover, []
    if leftover:
        die(f"unrecognized arguments for run: {' '.join(leftover)}")
    if not command:
        die("run needs a command: portmap run <name> -- <command...>")
    return raw[:i] + ["run", name], port, command


def main(argv=None):
    raw = list(sys.argv[1:] if argv is None else argv)
    run_port, run_command = None, None
    if "run" in raw:
        raw, run_port, run_command = split_run_args(raw)
    parser = build_parser()
    args = parser.parse_args(raw)
    if not getattr(args, "cmd", None):
        parser.print_help()
        return 0
    if args.cmd == "run":
        if run_port is not None:
            args.port = run_port
        args.run_cmd = run_command
    args.func(args)
    return 0


if __name__ == "__main__":
    main()
