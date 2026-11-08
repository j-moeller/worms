"""Least privilege of the database roles, checked against a real PostgreSQL
with the real migrations and the real rendered grants (tests/pg.py).

The roles that face the sandbox (scoring, logging, network monitor) are the
ones that matter most: their input is attacker-controlled, so they must be
able to append to exactly one table each and read nothing they wrote.
"""

from __future__ import annotations

import json

import pytest

psycopg2 = pytest.importorskip("psycopg2")

from pg import PASSWORD  # noqa: E402

FLOW = {
    "uid": "CtestFlow1",
    "ts": "2026-10-22T12:00:00Z",
    "src_ip": "10.64.0.11",
    "src_port": 40000,
    "dst_ip": "10.64.0.12",
    "dst_port": 80,
    "transport": "tcp",
    "service": "http",
    "src_bytes": 100,
    "dst_bytes": 200,
    "src_ip_bytes": 400,
    "dst_ip_bytes": 520,
}


def run(database, role, sql, params=None):
    """Execute as `role`; returns the rows or raises the database error."""
    conn = database.connect(role, PASSWORD)
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall() if cur.description else None
    finally:
        conn.close()


def denied(database, role, sql, params=None):
    with pytest.raises(psycopg2.errors.InsufficientPrivilege):
        run(database, role, sql, params)


def insert_flow(database, role, **overrides):
    row = {**FLOW, **overrides}
    cols = ", ".join(row)
    marks = ", ".join(["%s"] * len(row))
    return run(
        database, role,
        f"INSERT INTO network_flows ({cols}) VALUES ({marks}) ON CONFLICT DO NOTHING",
        list(row.values()),
    )  # fmt: skip


def count(database, table, where="TRUE", params=None):
    return run(database, "postgres", f"SELECT count(*) FROM {table} WHERE {where}", params)[0][0]


# --- network monitor ----------------------------------------------------------


def test_monitor_appends_flows_once(database):
    insert_flow(database, "dbuser_network_monitor", uid="Conce")
    # Shipped twice (at-least-once delivery): stored once, no error.
    insert_flow(database, "dbuser_network_monitor", uid="Conce")
    assert count(database, "network_flows", "uid = %s", ["Conce"]) == 1


def test_monitor_appends_events(database):
    run(
        database, "dbuser_network_monitor",
        "INSERT INTO network_events (log, ts, uid, data) VALUES ('dns', now(), 'Cx', '{\"query\": \"a.example\"}')",
    )  # fmt: skip
    assert count(database, "network_events", "uid = 'Cx'") == 1


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM network_flows",
        "SELECT uid FROM network_flows",
        "SELECT * FROM network_events",
        "UPDATE network_flows SET service = 'x'",
        "DELETE FROM network_flows",
        "DELETE FROM network_events",
        "TRUNCATE network_flows",
        "SELECT * FROM access_tokens",
        "SELECT * FROM flag_capture_attempts",
        "SELECT * FROM users",
        "INSERT INTO logging_attempts (team_uuid, log_data) VALUES ('x', '{}')",
        "INSERT INTO flag_capture_attempts (team_uuid, vm_flag_hash) VALUES ('x', 'y')",
        "CREATE TABLE evil (id int)",
    ],
)
def test_monitor_cannot(database, sql):
    denied(database, "dbuser_network_monitor", sql)


def test_monitor_cannot_set_bookkeeping_columns(database):
    denied(database, "dbuser_network_monitor",
           "INSERT INTO network_flows (uid, ts, src_ip, dst_ip, transport, inserted_at) "
           "VALUES ('Cbk', now(), '10.64.0.1', '10.64.0.2', 'tcp', now() - interval '1 year')")  # fmt: skip


def test_monitor_cannot_upsert(database):
    """ON CONFLICT DO UPDATE would let it overwrite flows: needs UPDATE."""
    insert_flow(database, "dbuser_network_monitor", uid="Cupsert")
    denied(database, "dbuser_network_monitor",
           "INSERT INTO network_flows (uid, ts, src_ip, dst_ip, transport) "
           "VALUES ('Cupsert', now(), '10.64.0.1', '10.64.0.2', 'tcp') "
           "ON CONFLICT (uid) DO UPDATE SET transport = 'udp'")  # fmt: skip


# --- password resets ----------------------------------------------------------

RESET_ROW = (
    "INSERT INTO password_resets (created_at, actor_uuid, actor_username, actor_role, target_uuid, "
    "target_username, target_role) VALUES (now(), gen_random_uuid(), 'tutor', 'staff', "
    "gen_random_uuid(), 'anna', 'student')"
)


def test_dashboard_appends_password_resets_and_reads_them(database):
    before = count(database, "password_resets")
    run(database, "django_dashboard", RESET_ROW)
    assert count(database, "password_resets") == before + 1
    assert run(database, "django_dashboard", "SELECT count(*) FROM password_resets")[0][0] == before + 1


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE password_resets SET actor_username = 'someone-else'",
        "UPDATE password_resets SET created_at = now() - interval '1 year'",
        "DELETE FROM password_resets",
        "TRUNCATE password_resets",
        "DROP TABLE password_resets",
        "ALTER TABLE password_resets DROP COLUMN actor_username",
        "ALTER TABLE password_resets RENAME TO old_resets",
    ],
)
def test_dashboard_cannot_change_or_remove_password_resets(database, sql):
    run(database, "django_dashboard", RESET_ROW)
    before = run(database, "postgres", "SELECT * FROM password_resets ORDER BY id")

    denied(database, "django_dashboard", sql)

    assert run(database, "postgres", "SELECT * FROM password_resets ORDER BY id") == before


@pytest.mark.parametrize("role", ["django_orchestrator", "round_agent", "dbuser_scoring", "dbuser_logging", "dbuser_network_monitor"])
def test_no_other_service_can_write_password_resets(database, role):
    denied(database, role, RESET_ROW)
    denied(database, role, "DELETE FROM password_resets")


@pytest.mark.parametrize("role", ["round_agent", "dbuser_scoring", "dbuser_logging", "dbuser_network_monitor"])
def test_the_services_that_need_no_accounts_cannot_read_password_resets(database, role):
    denied(database, role, "SELECT * FROM password_resets")


# --- dev uploads --------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO host_jobs (uuid, kind, params, state, result, error) VALUES (gen_random_uuid(), 'devbox_deploy', '{}', 'queued', '{}', '')",
        "UPDATE devbox_runs SET state = 'finished'",
        "DELETE FROM devbox_runs",
    ],
)
def test_the_dashboard_cannot_start_a_devbox_run_itself(database, sql):
    """It stores the upload. Only the orchestrator asks the agent for anything."""
    denied(database, "django_dashboard", sql)


@pytest.mark.parametrize("role", ["round_agent", "dbuser_scoring", "dbuser_logging", "dbuser_network_monitor"])
def test_the_other_services_cannot_read_devbox_runs(database, role):
    denied(database, role, "SELECT * FROM devbox_runs")


# --- scoring / logging --------------------------------------------------------


def test_scoring_appends_attempts(database):
    run(database, "dbuser_scoring",
        "INSERT INTO flag_capture_attempts (team_uuid, vm_flag_hash) VALUES ('t', 'f')")  # fmt: skip
    assert count(database, "flag_capture_attempts", "team_uuid = 't'") == 1


def test_logging_appends_attempts(database):
    run(database, "dbuser_logging",
        "INSERT INTO logging_attempts (uuid, team_uuid, log_data) VALUES (gen_random_uuid(), 't', '{\"a\": 1}')")  # fmt: skip
    assert count(database, "logging_attempts", "team_uuid = 't'") == 1


@pytest.mark.parametrize("role", ["dbuser_scoring", "dbuser_logging"])
def test_sandbox_apis_cannot_touch_access_tokens(database, role):
    # No access token is used at the scoring/logging servers anymore, so they
    # have no access to the table at all.
    denied(database, role, "SELECT access_token_hash, team_id, revoked FROM access_tokens")
    denied(database, role, "SELECT * FROM access_tokens")


@pytest.mark.parametrize(
    "role, sql",
    [
        ("dbuser_scoring", "SELECT * FROM flag_capture_attempts"),
        ("dbuser_scoring", "UPDATE flag_capture_attempts SET processed = true"),
        ("dbuser_scoring", "DELETE FROM flag_capture_attempts"),
        ("dbuser_scoring", "INSERT INTO flag_capture_attempts (team_uuid, vm_flag_hash, processed) VALUES ('a', 'b', true)"),
        ("dbuser_scoring", "INSERT INTO logging_attempts (team_uuid, log_data) VALUES ('x', '{}')"),
        ("dbuser_scoring", "SELECT access_token_hash FROM access_tokens"),
        ("dbuser_scoring", "INSERT INTO flag_captures (team_id, round_id, vm_id, flag_capture_attempt_id, created_at) VALUES (1, 1, 1, 1, now())"),
        ("dbuser_scoring", "SELECT * FROM users"),
        ("dbuser_scoring", "SELECT * FROM virtual_machines"),
        ("dbuser_scoring", "INSERT INTO network_flows (uid, ts, src_ip, dst_ip, transport) VALUES ('C', now(), '1.1.1.1', '1.1.1.1', 'tcp')"),
        ("dbuser_logging", "SELECT * FROM logging_attempts"),
        ("dbuser_logging", "UPDATE logging_attempts SET processed = true"),
        ("dbuser_logging", "DELETE FROM logging_attempts"),
        ("dbuser_logging", "INSERT INTO flag_capture_attempts (team_uuid, vm_flag_hash) VALUES ('a', 'b')"),
        ("dbuser_logging", "SELECT access_token_hash FROM access_tokens"),
        ("dbuser_logging", "SELECT * FROM users"),
        ("dbuser_logging", "SELECT * FROM network_flows"),
    ],
)  # fmt: skip
def test_sandbox_apis_cannot(database, role, sql):
    denied(database, role, sql)


# --- dashboard / orchestrator -------------------------------------------------


@pytest.mark.parametrize("role", ["django_dashboard", "django_orchestrator"])
def test_django_reads_monitoring(database, role):
    run(database, role, "SELECT count(*) FROM network_flows")
    run(database, role, "SELECT count(*) FROM network_events")
    denied(database, role, "DELETE FROM network_flows")
    denied(database, role, "INSERT INTO network_events (log, ts, data) VALUES ('x', now(), '{}')")


# --- every application role ---------------------------------------------------


@pytest.mark.parametrize(
    "role",
    ["dbuser_network_monitor", "dbuser_scoring", "dbuser_logging", "django_dashboard", "django_orchestrator"],
)
def test_no_role_creates_objects(database, role):
    denied(database, role, "CREATE TABLE public.evil (id int)")
    denied(database, role, "CREATE TEMPORARY TABLE evil (id int)")


def test_connection_limits(database):
    rows = run(database, "postgres",
               "SELECT rolname, rolconnlimit FROM pg_roles WHERE rolname LIKE 'dbuser_%' ORDER BY rolname")  # fmt: skip
    limits = dict(rows)
    assert all(limit > 0 for limit in limits.values()), limits


def test_privileges_reapply_cleanly(database, render):
    """setup_db_permissions runs on every provision: idempotent."""
    sql = render("ansible/roles/database/setup_db_permissions", "privileges.sql.j2")
    conn = database.connect()
    conn.autocommit = False
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.commit()
    conn.close()
    insert_flow(database, "dbuser_network_monitor", uid="Creapply")


# --- round orchestration (docs/05-live-environment.md) -------------------------------------


def test_orchestrator_requests_host_jobs_but_cannot_fake_results(database):
    run(database, "django_orchestrator",
        "INSERT INTO host_jobs (uuid, kind, params, state, result, error) "
        "VALUES (gen_random_uuid(), 'check', '{}', 'queued', '{}', '')")  # fmt: skip
    denied(database, "django_orchestrator", "UPDATE host_jobs SET state = 'done'")
    denied(database, "django_orchestrator", "DELETE FROM host_jobs")


def test_round_agent_reads_jobs_and_writes_results_only(database):
    run(database, "postgres",
        "INSERT INTO host_jobs (uuid, kind, params, state, result, error) "
        "VALUES (gen_random_uuid(), 'teardown', '{}', 'queued', '{}', '')")  # fmt: skip
    # Claim a job the way the agent does.
    rows = run(database, "round_agent",
               "UPDATE host_jobs SET state = 'running', started_at = now() WHERE id = ("
               "SELECT id FROM host_jobs WHERE state = 'queued' ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 1) "
               "RETURNING id, kind")  # fmt: skip
    assert rows and rows[0][1] in ("teardown", "check")
    run(database, "round_agent",
        "UPDATE host_jobs SET state = 'done', result = '{\"ok\": true}', finished_at = now() WHERE id = %s", [rows[0][0]])  # fmt: skip


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE host_jobs SET kind = 'prepare'",
        "UPDATE host_jobs SET params = '{}'",
        "INSERT INTO host_jobs (uuid, kind, params, state, result, error) VALUES (gen_random_uuid(), 'check', '{}', 'queued', '{}', '')",
        "DELETE FROM host_jobs",
        "SELECT * FROM round_runs",
        "SELECT * FROM virtual_machines",
        "SELECT * FROM access_tokens",
        "SELECT * FROM users",
        "UPDATE virtual_machines SET ctf_flag_hash = 'x'",
        "INSERT INTO flag_captures (team_id, round_id, vm_id, flag_capture_attempt_id, created_at) VALUES (1, 1, 1, 1, now())",
    ],
)  # fmt: skip
def test_round_agent_cannot(database, sql):
    denied(database, "round_agent", sql)


def test_orchestrator_processes_attempts(database):
    run(database, "django_orchestrator", "UPDATE flag_capture_attempts SET processed = processed, processed_at = processed_at WHERE false")
    denied(database, "django_orchestrator", "UPDATE flag_capture_attempts SET vm_flag_hash = 'x'")
    denied(database, "django_orchestrator", "DELETE FROM flag_capture_attempts")


# --- the event tables (core/models/events.py, docs/06-background-processes.md) ---------------------------

DASHBOARD_EVENTS = {
    "login_events": ("outcome, username, client_ip, not_recorded_before", ("ok", "someone", "192.0.2.7", 0)),
    "password_events": ("kind, actor_username, target_username", ("reset", "admin", "sam")),
    "impersonation_events": ("kind, actor_username, target_username", ("started", "admin", "sam")),
    "account_events": ("kind, actor_username, detail", ("created", "admin", "{}")),
    "team_events": ("kind, actor_username, team_name", ("joined", "sam", "red")),
    "credential_events": ("kind, actor_username, ssh_fingerprint", ("ssh_key_added", "sam", "SHA256:x")),
    "competition_events": ("kind, actor_username, detail", ("round_created", "admin", "{}")),
}
NODE_EVENTS = {
    "ssh_gateway_logins": ("logged_in_at, gateway_user, key_type, ssh_fingerprint, gateway_peer_port",
                           ("2026-11-06T10:00:00Z", "team", "ED25519", "SHA256:x", 4000)),
    "platform_events": ("kind, actor, detail", ("internet_allowed", "Internet.yml", "{}")),
}
SERVICES = ["django_dashboard", "django_orchestrator", "dbuser_scoring", "dbuser_logging", "dbuser_network_monitor",
            "round_agent", "dbuser_events_dashboard", "dbuser_events_node"]  # fmt: skip


def insert_event(database, role, table, events):
    columns, values = events[table]
    marks = ", ".join(["%s"] * len(values))
    return run(database, role, f"INSERT INTO {table} ({columns}) VALUES ({marks})", values)


@pytest.mark.parametrize("role, tables", [("dbuser_events_dashboard", DASHBOARD_EVENTS), ("dbuser_events_node", NODE_EVENTS)])
def test_each_events_account_appends_to_its_own_tables_only(database, role, tables):
    others = {**DASHBOARD_EVENTS, **NODE_EVENTS}
    for table in others:
        if table in tables:
            before = count(database, table)
            insert_event(database, role, table, tables)
            assert count(database, table) == before + 1
            # The database names the writer and stamps the time; nobody may supply either.
            [(writer, recent)] = run(database, "postgres",
                                     f"SELECT written_by, created_at > now() - interval '1 minute' FROM {table} ORDER BY id DESC LIMIT 1")  # fmt: skip
            assert (writer, recent) == (role, True)
            denied(database, role, f"INSERT INTO {table} (written_by) VALUES ('admin')")
            denied(database, role, f"INSERT INTO {table} (created_at) VALUES (now() - interval '1 day')")
        else:
            denied(database, role, f"INSERT INTO {table} ({others[table][0]}) VALUES ({', '.join(['%s'] * len(others[table][1]))})",
                   others[table][1])  # fmt: skip


@pytest.mark.parametrize("role", SERVICES)
@pytest.mark.parametrize("table", [*DASHBOARD_EVENTS, *NODE_EVENTS])
def test_no_service_reads_changes_or_removes_events(database, role, table):
    for sql in (f"SELECT * FROM {table}", f"UPDATE {table} SET written_by = 'x'", f"DELETE FROM {table}", f"TRUNCATE {table}"):
        denied(database, role, sql)


@pytest.mark.parametrize("role", ["django_dashboard", "django_orchestrator", "round_agent", "dbuser_scoring"])
def test_the_main_roles_cannot_write_events(database, role):
    for table, events in [*[(t, DASHBOARD_EVENTS) for t in DASHBOARD_EVENTS], *[(t, NODE_EVENTS) for t in NODE_EVENTS]]:
        columns, values = events[table]
        denied(database, role, f"INSERT INTO {table} ({columns}) VALUES ({', '.join(['%s'] * len(values))})", values)


def test_events_stay_small(database):
    with pytest.raises(psycopg2.errors.CheckViolation):
        run(database, "dbuser_events_dashboard", "INSERT INTO account_events (kind, detail) VALUES ('created', %s)",
            (json.dumps({"big": "x" * 5000}),))  # fmt: skip
    with pytest.raises(psycopg2.errors.CheckViolation):
        run(database, "dbuser_events_dashboard", "INSERT INTO login_events (outcome) VALUES ('anything')")
