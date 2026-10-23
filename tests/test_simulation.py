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
        # The round data setup.sql added is gone too.
        cur.execute("SELECT count(*) FROM round_runs WHERE seed = 0 AND state = 'finished'")
        assert cur.fetchone()[0] == 0
        cur.execute("SELECT count(*) FROM virtual_machines WHERE vm_identifier LIKE 'simulation-%%'")
        assert cur.fetchone()[0] == 0
        cur.execute("SELECT count(*) FROM rounds WHERE name LIKE 'simulation-%%'")
        assert cur.fetchone()[0] == 0
    conn.close()
    # Without a sandbox, that is reported (not a crash).
    assert any(line.startswith("FAIL") and "sandbox deployed" in line for line in lines)


def test_simulation_sql_round_trip(database):
    """The simulation's SQL together: setup.sql builds the test team, round,
    run and VM; a submitted flag + log are scored into a capture, a `captured`
    timeline event and a logging row (as capture processing would); verify.sql
    reports all of it (flows inserted as the monitor's INSERT-only role would);
    cleanup.sql removes everything."""
    import hashlib

    from pg import PASSWORD

    SIM = ANSIBLE / "files" / "simulation"
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
        # The round/team/run/VM, exactly as the live simulation sets them up.
        cur.execute((SIM / "setup.sql").read_text(), params)
        # The sandbox APIs store the submissions.
        cur.execute("INSERT INTO flag_capture_attempts (team_token_hash, vm_flag_hash) VALUES (%s, %s) RETURNING id", (token_hash, flag_hash))
        attempt_id = cur.fetchone()[0]
        cur.execute("INSERT INTO logging_attempts (uuid, team_token_hash, log_data) VALUES (gen_random_uuid(), %s, %s) RETURNING id",
                    (token_hash, json.dumps({"simulation": "verify1"})))  # fmt: skip
        log_attempt_id = cur.fetchone()[0]
        # Capture processing turns them into a capture, a timeline event and a
        # logging row (done here by hand; the real pass is in test_rounds.py).
        cur.execute(
            "SELECT vm.id, vm.run_id, vm.round_id, rr.team_id FROM virtual_machines vm "
            "JOIN round_runs rr ON rr.id = vm.run_id WHERE vm.ctf_flag_hash = %s", (flag_hash,)
        )
        vm_id, run_id, round_id, team_id = cur.fetchone()
        cur.execute(
            "INSERT INTO flag_captures (team_id, round_id, vm_id, flag_capture_attempt_id, created_at) VALUES (%s, %s, %s, %s, now())",
            (team_id, round_id, vm_id, attempt_id),
        )
        cur.execute(
            "INSERT INTO run_events (run_id, ts, kind, vm_id, data) VALUES (%s, now(), 'captured', %s, '{}')",
            (run_id, vm_id),
        )
        cur.execute(
            "INSERT INTO logging (team_id, round_id, logging_attempt_id, created_at) VALUES (%s, %s, %s, now())",
            (team_id, round_id, log_attempt_id),
        )
        cur.execute((SIM / "verify.sql").read_text(), params)
        names = [d.name for d in cur.description]
        r = dict(zip(names, cur.fetchone()))
    admin.close()

    assert r["flags"] == 1 and r["logs"] == 1
    # The new, round-aware scoring columns.
    assert r["captures"] == 1 and r["captured_events"] == 1 and r["logs_scored"] == 1
    assert r["http_flows"] == 1 and r["http_flow"]["service"] == "http"
    assert r["udp_flows"] == 1 and (r["udp_flow"]["src_pkts"], r["udp_flow"]["src_bytes"], r["udp_flow"]["src_ip_bytes"]) == (5, 500, 640)
    assert r["closed_state"] == "REJ" and r["api_service"] == "ssl"
    assert r["dns_events"] == 1 and r["http_events"] == 1
    assert r["shipper_age"] < 60 and r["stats_age"] < 60

    # cleanup.sql removes the team, round, run, VM, capture, event and logging.
    conn = database.connect()
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute((SIM / "cleanup.sql").read_text(), {"token_hash": token_hash})
        assert cur.fetchone()[1] == 1  # one competition removed
        for table in ("round_runs", "virtual_machines", "flag_captures", "run_events", "logging", "teams"):
            cur.execute(f"SELECT count(*) FROM {table}")
            assert cur.fetchone()[0] == 0, table
    conn.close()
