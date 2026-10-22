"""Rehearsal of ansible/Simulate.yml without a platform.

localhost plays the Proxmox host and the database VM (connection local, no
become; the database is the throwaway PostgreSQL with the real schema), the
other groups are empty. So the run creates the test team, polls the
verification query, removes the test data again and reports: everything that
needs a deployed VM fails (and must be reported as such, not crash the run).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from conftest import REPO, require

ANSIBLE = REPO / "ansible"


@pytest.fixture
def rehearsal(database, generated_inventory, tmp_path, ansible_env):
    require("ansible-playbook")
    inventory = tmp_path / "rehearsal.yml"
    inventory.write_text(json.dumps({
        "all": {"children": {"database": {"hosts": {"database.rehearsal": {
            "ansible_connection": "local",
            "ansible_become": False,
            "ansible_python_interpreter": sys.executable,
        }}}}}
    }))  # fmt: skip
    env = dict(
        ansible_env,
        ANSIBLE_INVENTORY=f"{ANSIBLE / 'inventory.yml'},{generated_inventory},{inventory}",
        PGHOST="127.0.0.1",
        PGPORT=str(database.port),
        ANSIBLE_LOCALHOST_WARNING="false",
    )
    env.pop("ANSIBLE_CONFIG_INVENTORY", None)
    proc = subprocess.run(
        ["ansible-playbook", "Simulate.yml", "-e", "sim_wait_seconds=0", "-e", "ansible_python_interpreter=" + sys.executable],
        cwd=ANSIBLE,
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
    )
    return proc


@pytest.fixture
def ansible_env(tmp_path):
    cfg = tmp_path / "ansible.cfg"
    roles = ":".join(str(ANSIBLE / "roles" / d) for d in ("host", "pki", "web", "database", "applications", "dns", "private_internet", "audit"))
    cfg.write_text(f"[defaults]\nroles_path = {roles}\nhost_key_checking = False\n")
    return dict(os.environ, ANSIBLE_CONFIG=str(cfg), ANSIBLE_NOCOLOR="1")


def report(proc):
    lines = [line.strip().strip('",') for line in proc.stdout.splitlines()]
    return [line for line in lines if line.startswith(("PASS  ", "FAIL  "))]


def test_rehearsal_reports_and_cleans_up(rehearsal, database):
    lines = report(rehearsal)
    assert lines, rehearsal.stdout[-3000:] + rehearsal.stderr[-2000:]
    # The run reaches the report and fails (no platform here)...
    assert rehearsal.returncode != 0
    assert "checks failed" in rehearsal.stdout
    # ... but the database part worked against the real schema:
    assert any(line.startswith("PASS") and "test team simulation-" in line for line in lines), lines
    assert any(line.startswith("PASS") and "test data removed" in line for line in lines), lines
    # ... and nothing is left behind.
    conn = database.connect()
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM teams WHERE name LIKE 'simulation-%%'")
        assert cur.fetchone()[0] == 0
        cur.execute("SELECT count(*) FROM access_tokens WHERE name LIKE 'simulation-%%'")
        assert cur.fetchone()[0] == 0
    conn.close()
    # Without a sandbox, that is reported (not a crash).
    assert any(line.startswith("FAIL") and "sandbox deployed" in line for line in lines)


def test_verify_query_sees_a_successful_run(database):
    """files/simulation/verify.sql against the rows a successful run leaves
    (inserted as the services would: the monitor's INSERT-only role)."""
    import hashlib

    from pg import PASSWORD

    sql = (ANSIBLE / "files" / "simulation" / "verify.sql").read_text()
    token_hash = hashlib.sha256(b"verify-token").hexdigest()
    flag_hash = hashlib.sha256(b"FLAG{verify}").hexdigest()
    client, peer, scoring = "10.64.0.11", "10.64.0.12", "10.2.0.14"
    params = {
        "token_hash": token_hash, "flag_hash": flag_hash, "run_id": "verify1",
        "client": client, "api_client": client, "peer": peer, "scoring": scoring,
        "http_port": 23456, "udp_port": 33456, "udp_sport": 43456, "closed_port": 53456,
        "since": "2000-01-01",
    }  # fmt: skip
    flows = [
        ("Cv1", client, 50000, peer, 23456, "tcp", "http", "SF", 6, 548, 78),
        ("Cv2", client, 43456, peer, 33456, "udp", None, "S0", 5, 640, 500),
        ("Cv3", client, 50001, peer, 53456, "tcp", None, "REJ", 1, 60, 0),
        ("Cv4", client, 50002, scoring, 443, "tcp", "ssl", "SF", 12, 1500, 900),
    ]
    monitor = database.connect("dbuser_network_monitor", PASSWORD)
    with monitor.cursor() as cur:
        for uid, src, sport, dst, dport, transport, service, state, pkts, ip_bytes, nbytes in flows:
            cur.execute(
                "INSERT INTO network_flows (uid, ts, src_ip, src_port, dst_ip, dst_port, transport, service,"
                " conn_state, src_pkts, src_ip_bytes, src_bytes) VALUES (%s, now(), %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (uid, src, sport, dst, dport, transport, service, state, pkts, ip_bytes, nbytes),
            )
        for log, data in [
            ("dns", {"id.orig_h": client, "query": "scoring.worms.internal"}),
            ("http", {"id.resp_h": peer, "id.resp_p": 23456, "method": "GET"}),
            ("shipper", {"flows": 4}),
            ("stats", {"pkts_proc": 10}),
        ]:
            cur.execute("INSERT INTO network_events (log, ts, data) VALUES (%s, now(), %s)", (log, json.dumps(data)))
    monitor.close()
    admin = database.connect()
    with admin.cursor() as cur:
        cur.execute("INSERT INTO flag_capture_attempts (team_token_hash, vm_flag_hash) VALUES (%s, %s)", (token_hash, flag_hash))
        cur.execute("INSERT INTO logging_attempts (uuid, team_token_hash, log_data) VALUES (gen_random_uuid(), %s, %s)",
                    (token_hash, json.dumps({"simulation": "verify1"})))  # fmt: skip
        cur.execute(sql, params)
        names = [d.name for d in cur.description]
        r = dict(zip(names, cur.fetchone()))
    admin.close()
    assert r["flags"] == 1 and r["logs"] == 1
    assert r["http_flows"] == 1 and r["http_flow"]["service"] == "http"
    assert r["udp_flows"] == 1 and (r["udp_flow"]["src_pkts"], r["udp_flow"]["src_bytes"], r["udp_flow"]["src_ip_bytes"]) == (5, 500, 640)
    assert r["closed_state"] == "REJ" and r["api_service"] == "ssl"
    assert r["dns_events"] == 1 and r["http_events"] == 1
    assert r["shipper_age"] < 60 and r["stats_age"] < 60
