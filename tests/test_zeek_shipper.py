"""zeek-shipper (ansible/roles/applications/network_monitor/files/
zeek_shipper.py): parsing of Zeek's JSON logs, following them across
rotation and restarts, and shipping into the test database as the
INSERT-only role dbuser_network_monitor.

The records below have the shape of Zeek 8 JSON logs (policy/tuning/json-logs
with community-id and MAC logging loaded).
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import os
import time

import pytest

from conftest import REPO

spec = importlib.util.spec_from_file_location(
    "zeek_shipper", REPO / "ansible" / "roles" / "applications" / "network_monitor" / "files" / "zeek_shipper.py"
)
zs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(zs)

NOW = time.time()

CONN = {
    "ts": NOW - 60,
    "uid": "CHhAvVGS1DHFjwGM9",
    "id.orig_h": "10.64.0.11",
    "id.orig_p": 40000,
    "id.resp_h": "10.64.0.12",
    "id.resp_p": 80,
    "proto": "tcp",
    "service": "http",
    "duration": 0.25,
    "orig_bytes": 78,
    "resp_bytes": 1520,
    "conn_state": "SF",
    "local_orig": True,
    "local_resp": True,
    "missed_bytes": 0,
    "history": "ShADadFf",
    "orig_pkts": 6,
    "orig_ip_bytes": 398,
    "resp_pkts": 4,
    "resp_ip_bytes": 1732,
    "orig_l2_addr": "bc:24:11:00:00:01",
    "resp_l2_addr": "bc:24:11:00:00:02",
    "community_id": "1:LQU9qZlK+B5F3KDmev6m5PMibrg=",
}

DNS = {
    "ts": NOW - 30,
    "uid": "CdnsUid1",
    "id.orig_h": "10.64.0.11",
    "id.orig_p": 53000,
    "id.resp_h": "10.2.0.11",
    "id.resp_p": 53,
    "proto": "udp",
    "query": "apt-cache.worms.internal",
    "qtype_name": "A",
    "rcode_name": "NOERROR",
    "answers": ["10.2.0.20"],
}


# --- parsing ------------------------------------------------------------------


def test_parse_conn():
    row = dict(zip(zs.FLOW_COLUMNS, zs.parse_conn(CONN)))
    assert row["uid"] == CONN["uid"]
    assert row["src_ip"] == "10.64.0.11" and row["dst_ip"] == "10.64.0.12"
    assert row["src_port"] == 40000 and row["dst_port"] == 80
    assert row["transport"] == "tcp" and row["service"] == "http"
    assert row["src_ip_bytes"] == 398 and row["dst_ip_bytes"] == 1732
    assert row["ts"] == dt.datetime.fromtimestamp(CONN["ts"], tz=dt.timezone.utc)
    assert row["community_id"].startswith("1:")
    assert row["src_mac"] == "bc:24:11:00:00:01"


def test_parse_conn_minimal_udp_without_reply():
    record = {"ts": NOW, "uid": "Cudp", "id.orig_h": "10.64.0.11", "id.orig_p": 5, "id.resp_h": "10.64.0.99", "id.resp_p": 9, "proto": "udp", "conn_state": "S0"}
    row = dict(zip(zs.FLOW_COLUMNS, zs.parse_conn(record)))
    assert row["service"] is None and row["duration"] is None and row["dst_bytes"] is None


def test_parse_conn_icmp_and_ipv6():
    record = {**CONN, "proto": "icmp", "id.orig_p": 8, "id.resp_p": 0, "id.orig_h": "fe80::1", "id.resp_h": "fe80::2"}
    row = dict(zip(zs.FLOW_COLUMNS, zs.parse_conn(record)))
    assert row["transport"] == "icmp" and row["src_ip"] == "fe80::1"


@pytest.mark.parametrize(
    "change",
    [
        {"id.orig_h": "10.64.0.300"},
        {"id.orig_h": "'; DROP TABLE network_flows; --"},
        {"id.resp_p": 70000},
        {"id.resp_p": -1},
        {"orig_bytes": -5},
        {"orig_bytes": 2**64},
        {"orig_bytes": "100"},
        {"orig_bytes": True},
        {"duration": float("nan")},
        {"ts": 0},
        {"ts": NOW + 30 * 24 * 3600},
        {"ts": "yesterday"},
        {"uid": None},
    ],
)
def test_parse_conn_rejects(change):
    with pytest.raises(zs.Invalid):
        zs.parse_conn({**CONN, **change})


def test_parse_conn_cleans_text():
    row = dict(zip(zs.FLOW_COLUMNS, zs.parse_conn({**CONN, "history": "S\x00h" * 500, "proto": "sctp", "service": ["http", "ssl"]})))
    assert "\x00" not in row["history"] and len(row["history"]) <= 256
    assert row["transport"] == "unknown_transport"
    assert row["service"] == "http,ssl"


def test_parse_event():
    name, ts, uid, data = zs.parse_event("dns", DNS, 4096)
    assert (name, uid) == ("dns", "CdnsUid1")
    assert data["query"] == "apt-cache.worms.internal" and "ts" not in data and "uid" not in data


def test_parse_event_is_size_limited():
    record = {**DNS, "query": "a" * 100_000, "answers": ["x" * 1000] * 1000, "nul": "a\x00b"}
    data = zs.parse_event("dns", record, 4096)[3]
    assert len(json.dumps(data)) <= 4096
    assert "\x00" not in json.dumps(data)


def test_shrink_always_fits():
    record = {f"field{i}": "x" * 100 for i in range(500)}
    assert len(json.dumps(zs.shrink(record, 512), separators=(",", ":"))) <= 512
    # Too small for anything useful: only the field names are kept.
    assert zs.shrink(record, 10)["_truncated"] is True


# --- following files ----------------------------------------------------------


def write(path, *records, newline=True):
    with open(path, "a") as f:
        for i, record in enumerate(records):
            last = i == len(records) - 1
            f.write(json.dumps(record) + ("\n" if newline or not last else ""))


@pytest.fixture
def dirs(tmp_path):
    logs, archive = tmp_path / "logs", tmp_path / "archive"
    logs.mkdir()
    archive.mkdir()
    return logs, archive


def test_follower_reads_complete_lines_only(dirs):
    logs, archive = dirs
    f = zs.Follower("conn", logs, archive)
    assert f.read(10) == []  # no file yet
    write(logs / "conn.log", {"a": 1}, {"b": 2}, newline=False)
    assert [json.loads(x) for x in f.read(10)] == [{"a": 1}]
    with open(logs / "conn.log", "a") as fh:
        fh.write("\n")
    assert [json.loads(x) for x in f.read(10)] == [{"b": 2}]


def test_follower_survives_rotation(dirs):
    logs, archive = dirs
    f = zs.Follower("conn", logs, archive)
    write(logs / "conn.log", {"n": 1})
    assert len(f.read(10)) == 1
    write(logs / "conn.log", {"n": 2})  # written before the rotation, not read yet
    os.rename(logs / "conn.log", archive / "conn.2026-10-22-12-00-00.log")
    write(logs / "conn.log", {"n": 3})
    assert [json.loads(x)["n"] for x in f.read(10)] == [2, 3]


def test_follower_resumes_after_restart_across_rotation(dirs, tmp_path):
    logs, archive = dirs
    state = tmp_path / "state.json"
    f = zs.Follower("conn", logs, archive)
    write(logs / "conn.log", {"n": 1}, {"n": 2})
    assert len(f.read(1)) == 1
    f.mark_committed()
    zs.save_state(state, [f])
    # Down while Zeek writes and rotates.
    write(logs / "conn.log", {"n": 3})
    os.rename(logs / "conn.log", archive / "conn.2026-10-22-13-00-00.log")
    write(logs / "conn.log", {"n": 4})
    restarted = zs.Follower("conn", logs, archive, zs.load_state(state)["conn"])
    assert [json.loads(x)["n"] for x in restarted.read(10)] == [2, 3, 4]


def test_follower_rewind(dirs):
    logs, archive = dirs
    f = zs.Follower("conn", logs, archive)
    write(logs / "conn.log", {"n": 1}, {"n": 2})
    assert len(f.read(10)) == 2
    f.rewind()  # nothing committed yet: from the start
    assert [json.loads(x)["n"] for x in f.read(1)] == [1]
    f.mark_committed()
    f.read(10)
    f.rewind()
    assert [json.loads(x)["n"] for x in f.read(10)] == [2]


# --- shipping -----------------------------------------------------------------


@pytest.fixture
def shipper_env(database, dirs, tmp_path):
    logs, archive = dirs
    return {
        **database.env("dbuser_network_monitor"),
        "ZEEK_LOG_DIR": str(logs),
        "ZEEK_ARCHIVE_DIR": str(archive),
        "STATE_FILE": str(tmp_path / "state.json"),
        "EVENT_LOGS": "dns,weird",
        "REPORT_SECONDS": "3600",
    }


def flows(database, where="TRUE", params=None):
    conn = database.connect()
    with conn.cursor() as cur:
        cur.execute(f"SELECT uid, host(src_ip), dst_port, transport, service, dst_ip_bytes FROM network_flows WHERE {where}", params)
        rows = cur.fetchall()
    conn.close()
    return rows


def test_ships_flows_and_events(database, shipper_env, dirs):
    logs, _ = dirs
    uid = f"Cship{time.time_ns()}"
    write(logs / "conn.log", {**CONN, "uid": uid}, {"broken": True})
    write(logs / "dns.log", {**DNS, "uid": uid})
    write(logs / "conn.log", "not even an object")
    shipper = zs.Shipper(shipper_env)
    assert shipper.step() is True
    assert flows(database, "uid = %s", [uid]) == [(uid, "10.64.0.11", 80, "tcp", "http", 1732)]
    conn = database.connect()
    with conn.cursor() as cur:
        cur.execute("SELECT log, data->>'query' FROM network_events WHERE uid = %s", [uid])
        assert cur.fetchall() == [("dns", "apt-cache.worms.internal")]
    conn.close()
    assert shipper.counts["invalid"] == 2
    assert shipper.step() is False  # nothing new


def test_restart_does_not_duplicate(database, shipper_env, dirs):
    logs, _ = dirs
    uid = f"Crestart{time.time_ns()}"
    write(logs / "conn.log", {**CONN, "uid": uid})
    zs.Shipper(shipper_env).step()
    # Restarted with the saved state: nothing new to ship.
    assert zs.Shipper(shipper_env).step() is False
    # Restarted WITHOUT state (lost): everything again, flows still stored once.
    os.unlink(shipper_env["STATE_FILE"])
    zs.Shipper(shipper_env).step()
    assert len(flows(database, "uid = %s", [uid])) == 1


def test_database_outage_loses_nothing(database, shipper_env, dirs):
    logs, _ = dirs
    uid = f"Coutage{time.time_ns()}"
    write(logs / "conn.log", {**CONN, "uid": uid})
    down = zs.Shipper({**shipper_env, "DB_PASSWORD": "wrong"})
    with pytest.raises(Exception):
        down.step()
    assert flows(database, "uid = %s", [uid]) == []
    # Back: the same lines are read again.
    down.db.env = shipper_env
    assert down.step() is True
    assert len(flows(database, "uid = %s", [uid])) == 1


def test_budget_throttles_without_dropping(database, shipper_env, dirs):
    logs, _ = dirs
    base = f"Cbudget{time.time_ns()}"
    write(logs / "conn.log", *({**CONN, "uid": f"{base}-{i}"} for i in range(10)))
    shipper = zs.Shipper({**shipper_env, "MAX_FLOWS_PER_SECOND": "0.001", "FLOW_BURST": "4"})
    shipper.step()
    assert len(flows(database, "uid LIKE %s", [base + "%"])) == 4
    shipper.flow_budget.burst = shipper.flow_budget.tokens = 100  # time passes
    shipper.step()
    assert len(flows(database, "uid LIKE %s", [base + "%"])) == 10


def test_heartbeat_event(database, shipper_env):
    shipper = zs.Shipper({**shipper_env, "REPORT_SECONDS": "0"})
    assert shipper.step() is True
    conn = database.connect()
    with conn.cursor() as cur:
        cur.execute("SELECT data FROM network_events WHERE log = 'shipper' ORDER BY id DESC LIMIT 1")
        data = cur.fetchone()[0]
    conn.close()
    assert "backlog_bytes" in data and "conn" in data["backlog_bytes"]
