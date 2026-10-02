"""Tests for portmap. Run with: pytest"""
import http.client
import json
import os
import socket
import subprocess
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import portmap


@pytest.fixture()
def mapfile(tmp_path, monkeypatch):
    p = tmp_path / "portmap.json"
    monkeypatch.setenv("PORTMAP_FILE", str(p))
    return p


def read_map(mapfile):
    with open(mapfile, encoding="utf-8") as f:
        return json.load(f)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


LISTENER_ENV = (
    "import os, socket, time; "
    "s = socket.socket(); s.bind(('127.0.0.1', int(os.environ['PORT']))); "
    "s.listen(1); time.sleep(120)"
)


def wait_listening(port, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        if portmap.port_listening(port):
            return True
        time.sleep(0.1)
    return False


# --- name / get / list / unname -------------------------------------------

def test_name_get_unname(mapfile, capsys):
    port = free_port()
    portmap.main(["name", "api", str(port)])
    assert capsys.readouterr().out.strip() == f"api -> {port}"

    portmap.main(["get", "api"])
    assert capsys.readouterr().out.strip() == str(port)

    portmap.main(["list"])
    out = capsys.readouterr().out
    assert "api" in out and str(port) in out

    portmap.main(["unname", "api"])
    assert capsys.readouterr().out.strip() == "removed api"
    with pytest.raises(SystemExit):
        portmap.main(["get", "api"])


def test_name_invalid(mapfile):
    with pytest.raises(SystemExit):
        portmap.main(["name", "bad name!", "3000"])
    with pytest.raises(SystemExit):
        portmap.main(["name", "api", "0"])
    with pytest.raises(SystemExit):
        portmap.main(["name", "api", "70000"])
    with pytest.raises(SystemExit):
        portmap.main(["unname", "nope"])


def test_name_warns_when_port_closed(mapfile, capsys):
    port = free_port()  # free => nothing listening
    portmap.main(["name", "idle", str(port)])
    err = capsys.readouterr().err
    assert "warning" in err
    assert read_map(mapfile)["idle"]["status"] == "down"


def test_list_empty(mapfile, capsys):
    portmap.main(["list"])
    assert "no bindings" in capsys.readouterr().out


# --- run / stop ------------------------------------------------------------

def test_run_records_pid_and_port(mapfile, capsys):
    port = free_port()
    portmap.main(["name", "web", str(port)])
    capsys.readouterr()
    portmap.main(["run", "web", "--", sys.executable, "-c", LISTENER_ENV])
    out = capsys.readouterr().out
    assert f"web -> {port}" in out
    entry = read_map(mapfile)["web"]
    assert entry["port"] == port
    assert portmap.pid_alive(entry["pid"])
    assert wait_listening(port), "child did not bind $PORT"
    # cleanup
    portmap.main(["stop", "web"])
    assert not portmap.pid_alive(entry["pid"])


def test_run_picks_free_port_when_taken(mapfile, capsys):
    port = free_port()
    blocker = subprocess.Popen(
        [sys.executable, "-c", LISTENER_ENV],
        start_new_session=True,
        env={**os.environ, "PORT": str(port)})
    try:
        assert wait_listening(port)
        portmap.main(["run", "svc", "--port", str(port), "--",
                      sys.executable, "-c", LISTENER_ENV])
        err = capsys.readouterr().err
        assert "warning" in err and "is taken" in err
        entry = read_map(mapfile)["svc"]
        assert entry["port"] != port
        assert portmap.pid_alive(entry["pid"])
        assert wait_listening(entry["port"]), "child did not bind $PORT"
        portmap.main(["stop", "svc"])
    finally:
        blocker.terminate()


def test_stop_no_process(mapfile, capsys):
    portmap.main(["name", "ghost", "3999"])
    capsys.readouterr()
    portmap.main(["stop", "ghost"])
    assert "marked down" in capsys.readouterr().out


# --- watch / refresh_binding -----------------------------------------------

def test_refresh_pid_moved_port(monkeypatch):
    monkeypatch.setattr(portmap, "pid_alive", lambda pid: True)
    entry = {"port": 3000, "pid": 4242, "status": "up"}
    new, changed = portmap.refresh_binding(
        "api", entry, ports_fn=lambda pid: {4000})
    assert changed
    assert new["port"] == 4000
    assert new["status"] == "up"
    assert "auto-updated" in new["note"]


def test_refresh_pid_alive_not_listening(monkeypatch):
    monkeypatch.setattr(portmap, "pid_alive", lambda pid: True)
    entry = {"port": 3000, "pid": 4242, "status": "up"}
    new, changed = portmap.refresh_binding(
        "api", entry, ports_fn=lambda pid: set())
    assert changed and new["status"] == "down"


def test_refresh_pid_dead(monkeypatch):
    monkeypatch.setattr(portmap, "pid_alive", lambda pid: False)
    entry = {"port": 3000, "pid": 4242, "status": "up"}
    new, changed = portmap.refresh_binding("api", entry)
    assert changed and new["status"] == "down"
    assert "exited" in new["note"]


def test_refresh_no_pid_probe():
    entry = {"port": 3999, "status": "up"}
    new, changed = portmap.refresh_binding(
        "x", entry, listen_fn=lambda p: False)
    assert changed and new["status"] == "down"
    new2, changed2 = portmap.refresh_binding(
        "x", dict(new, status="down"), listen_fn=lambda p: True)
    assert changed2 and new2["status"] == "up"


def test_watch_once_updates_file(mapfile):
    portmap.main(["name", "dead", "3998"])
    data = read_map(mapfile)
    data["dead"]["pid"] = 99999999  # almost certainly not alive
    data["dead"]["status"] = "up"
    portmap.save_map(data)
    n = portmap.watch_once()
    assert n == 1
    assert read_map(mapfile)["dead"]["status"] == "down"


# --- parsers ----------------------------------------------------------------

def test_parse_windows_netstat():
    text = (
        "Active Connections\r\n"
        "\r\n"
        "  Proto  Local Address          Foreign Address        State           PID\r\n"
        "  TCP    127.0.0.1:3000         0.0.0.0:0              LISTENING       1234\r\n"
        "  TCP    192.168.1.5:139        0.0.0.0:0              LISTENING       4\r\n"
        "  TCP    127.0.0.1:3000         127.0.0.1:51234        ESTABLISHED     1234\r\n"
    )
    assert portmap.parse_windows_netstat(text) == [(3000, 1234), (139, 4)]


def test_parse_proc_net_tcp():
    text = (
        "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
        "   0: 0100007F:0BB8 00000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 12345 1 0000000000000000 100 0 0 10 0\n"
        "   1: 0100007F:0FA0 00000000:0000 01 00000000:00000000 00:00000000 00000000     0        0 12346 1 0000000000000000 100 0 0 10 0\n"
    )
    assert portmap.parse_proc_net_tcp(text) == {"12345": 3000}  # 0BB8 == 3000


# --- dashboard ---------------------------------------------------------------

def test_dashboard_serves_table(mapfile):
    portmap.main(["name", "api", "3000"])
    server = portmap.make_dashboard_server(0)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        port = server.server_address[1]
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("GET", "/")
        resp = conn.getresponse()
        body = resp.read().decode("utf-8")
        assert resp.status == 200
        assert "portmap" in body
        assert "api" in body and "3000" in body
    finally:
        server.shutdown()
        server.server_close()
