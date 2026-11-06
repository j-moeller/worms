"""The event tables (core/events.py, scripts/round_agent/events.py, docs/monitoring.md):
the dashboard records the events an investigation needs, with who, whom and from
where, and nothing a writer should not hold; the node records the SSH gateway's
team logins, matched to the client's address. The database's side (each account
appends to its own tables only, reads nothing) is in test_database.py.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import subprocess
import sys

import pytest

from conftest import REPO
from test_dashboard_login import HOST, login, proxied, site  # noqa: F401 (fixture)

sys.path.insert(0, str(REPO / "scripts"))

ORIGIN = {"HTTP_ORIGIN": f"https://{HOST}"}
KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIHx8Y2l5S0bWzv0W8iQ5b2sJw2z8b9C1nM1oQ7mXJt3a laptop"


def rows(model, **where):
    from core.models import events

    return list(getattr(events, model).objects.filter(**where).order_by("id"))


def csrf(client, path="/profile/"):
    page = client.get(path, **proxied(HOST))
    return re.search(rb'name="csrfmiddlewaretoken" value="([^"]+)"', page.content).group(1).decode()


# --- core/events.py --------------------------------------------------------------------------


def test_a_row_is_small_and_names_its_writer(db_clean):
    from core import events

    assert events.platform("database_reset", actor="console:root@node", detail={"x": "y" * 10000})
    [row] = rows("PlatformEvent")
    assert row.created_at is not None and row.actor == "console:root@node"
    assert row.detail["truncated"] is True and len(json.dumps(row.detail)) < 2200


def test_the_client_address_is_nginxs_and_checked():
    from core import events

    class Request:
        def __init__(self, meta):
            self.META = meta

    assert events.client_ip(Request({"HTTP_X_REAL_IP": "198.51.100.4", "REMOTE_ADDR": ""})) == "198.51.100.4"
    assert events.client_ip(Request({"HTTP_X_REAL_IP": "not an address; DROP", "REMOTE_ADDR": "10.0.0.1"})) == "10.0.0.1"
    assert events.client_ip(Request({})) is None


def test_the_fingerprint_is_sshds(tmp_path):
    from core import events

    blob = base64.b64decode(KEY.split()[1])
    assert events.ssh_fingerprint(KEY) == "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")
    keygen = subprocess.run(["ssh-keygen", "-lf", "-"], input=KEY, capture_output=True, text=True)
    if keygen.returncode == 0:
        assert keygen.stdout.split()[1] == events.ssh_fingerprint(KEY)
    assert events.ssh_fingerprint("not a key") is None


def test_a_flood_of_failed_sign_ins_is_capped():
    from core.events import _Bucket

    bucket = _Bucket(rate=3)
    assert [bucket.take() for _ in range(5)] == [0, 0, 0, None, None]
    bucket.tokens = 1
    assert bucket.take() == 2  # the two dropped ones are counted into the next event


# --- the dashboard's events ------------------------------------------------------------------


def test_sign_in_and_out_are_recorded_with_the_client(site):  # noqa: F811
    assert login(site, HOST, f"https://{HOST}").status_code == 302
    [ok] = rows("LoginEvent", outcome="ok")
    assert ok.username == "admin" and ok.user_uuid is not None
    site.post("/logout/", {"csrfmiddlewaretoken": csrf(site)}, HTTP_X_REAL_IP="198.51.100.9", **ORIGIN, **proxied(HOST))
    [out] = rows("LoginEvent", outcome="logout")
    assert out.username == "admin" and out.client_ip == "198.51.100.9"


def test_a_failed_sign_in_never_keeps_what_was_typed(site):  # noqa: F811
    for name in ("admin", "my-secret-password"):
        page = site.get("/login/", **proxied(HOST))
        token = re.search(rb'name="csrfmiddlewaretoken" value="([^"]+)"', page.content).group(1).decode()
        site.post("/login/", {"username": name, "password": "wrong", "csrfmiddlewaretoken": token}, **ORIGIN, **proxied(HOST))
    known, unknown = rows("LoginEvent", outcome="failed")
    assert known.username == "admin" and unknown.username is None and unknown.user_uuid is None
    assert "my-secret-password" not in str([(e.username, e.client_ip) for e in rows("LoginEvent")])


def test_password_change_reset_and_view_as(site):  # noqa: F811
    from core.models.user import User

    student = User.objects.create(username="sam@example.org", must_change_password=True)
    student.set_password("one-time")
    student.save()
    assert login(site, HOST, f"https://{HOST}").status_code == 302

    site.post(f"/staff/users/{student.uuid}/password/", {"csrfmiddlewaretoken": csrf(site, "/staff/users/")}, **ORIGIN, **proxied(HOST))
    [reset] = rows("PasswordEvent", kind="reset")
    assert (reset.actor_username, reset.target_username, reset.target_role) == ("admin", "sam@example.org", "student")

    site.post(f"/view-as/{student.uuid}/", {"csrfmiddlewaretoken": csrf(site, "/staff/users/")}, **ORIGIN, **proxied(HOST))
    site.post("/view-as/stop/", {"csrfmiddlewaretoken": csrf(site, "/")}, **ORIGIN, **proxied(HOST))
    seen = rows("ImpersonationEvent")
    assert [(e.kind, e.actor_username, e.target_username) for e in seen] == [
        ("started", "admin", "sam@example.org"), ("ended", "admin", "sam@example.org")]

    site.logout()
    student.refresh_from_db()
    page = site.get("/login/", **proxied(HOST))
    token = re.search(rb'name="csrfmiddlewaretoken" value="([^"]+)"', page.content).group(1).decode()
    # The reset set a new one-time password we do not know: set our own and sign in with it.
    student.set_password("one-time-2")
    student.save()
    site.post("/login/", {"username": "sam@example.org", "password": "one-time-2", "csrfmiddlewaretoken": token}, **ORIGIN, **proxied(HOST))
    site.post("/change-password/", {"csrfmiddlewaretoken": csrf(site, "/change-password/"),
                                    "new_password1": "a-long-new-pass-29!", "new_password2": "a-long-new-pass-29!"},
              **ORIGIN, **proxied(HOST))  # fmt: skip
    [changed] = rows("PasswordEvent", kind="changed")
    assert changed.actor_username == changed.target_username == "sam@example.org"
    assert changed.one_time_password_replaced is True


def test_keys_and_tokens_are_recorded_without_the_secret(site):  # noqa: F811
    from core.models.competition import ApplicationSetting
    from core.models.team import Team, TeamMembership
    from core.models.user import User

    team = Team.objects.create(name="red", competition=ApplicationSetting.get_current_competition())
    TeamMembership.objects.create(user=User.objects.get(username="admin"), team=team)
    assert login(site, HOST, f"https://{HOST}").status_code == 302
    site.post("/devbox/ssh-keys/add/", {"csrfmiddlewaretoken": csrf(site), "name": "laptop", "public_key": KEY}, **ORIGIN, **proxied(HOST))
    [added] = rows("CredentialEvent", kind="ssh_key_added")
    assert (added.actor_username, added.team_name, added.name) == ("admin", "red", "laptop")
    assert added.ssh_fingerprint.startswith("SHA256:") and KEY.split()[1] not in str(added.__dict__)


# --- the node: SSH gateway logins (scripts/round_agent/events.py) -----------------------------

FP = "SHA256:0LmkJGBxvhAjZu7ePV1quGGe15XFKdd57xdy0g+yVQA"


def journal(message, at, cursor):
    return json.dumps({"MESSAGE": message, "__REALTIME_TIMESTAMP": str(int(at * 1e6)), "__CURSOR": cursor})


def test_gateway_logins_are_parsed_strictly():
    from round_agent import events as node

    lines = [
        journal(f"Accepted publickey for team from 10.7.0.1 port 48215 ssh2: ED25519 {FP}", 1000.0, "c1"),
        journal(f"Accepted publickey for debian from 10.5.0.1 port 41416 ssh2: ED25519 {FP}", 1001.0, "c2"),
        journal("Connection closed by authenticating user team 10.7.0.1 port 47624 [preauth]", 1002.0, "c3"),
        journal(f"Accepted publickey for team from 10.7.0.1 port 1 ssh2: ED25519 {FP} '; DROP TABLE", 1003.0, "c4"),
        "not json",
    ]
    logins, cursor = node.parse_gateway(lines, "team")
    assert cursor == "c4"
    assert logins == [{"at": 1000.0, "user": "team", "port": 48215, "peer": "10.7.0.1", "key_type": "ED25519", "fingerprint": FP}]


def test_a_login_is_matched_to_its_client_by_port_and_time():
    from round_agent import events as node

    lines = [
        json.dumps({"MESSAGE": "worms-ssh-gw: IN=vmbr0 OUT= SRC=203.0.113.5 DST=141.23.117.61 LEN=60 PROTO=TCP SPT=48215 DPT=2222 SYN",
                    "__REALTIME_TIMESTAMP": str(int(999.5 * 1e6))}),
        json.dumps({"MESSAGE": "worms-ssh-gw: IN=vmbr0 OUT= SRC=198.51.100.1 DST=141.23.117.61 LEN=60 PROTO=TCP SPT=40000 DPT=2222 SYN",
                    "__REALTIME_TIMESTAMP": str(int(999.6 * 1e6))}),
        json.dumps({"MESSAGE": "something else SRC=192.0.2.1 SPT=48215 DPT=1", "__REALTIME_TIMESTAMP": "999700000"}),
    ]  # fmt: skip
    connections = node.parse_node_log(lines)
    assert node.client_of({"at": 1000.0, "port": 48215}, connections) == "203.0.113.5"
    assert node.client_of({"at": 1000.0, "port": 1}, connections) is None
    assert node.client_of({"at": 2000.0, "port": 48215}, connections) is None  # too long ago


def test_the_collector_records_new_logins_and_remembers_where_it_stopped(tmp_path):
    from round_agent import events as node

    class Conn:
        def __init__(self):
            self.rows, self.commits = [], 0

        def cursor(self):
            conn = self

            class Cursor:
                def __enter__(self):
                    return self

                def __exit__(self, *exc):
                    return False

                def execute(self, sql, params):
                    assert sql.startswith("INSERT INTO ssh_gateway_logins") and "RETURNING" not in sql
                    conn.rows.append(params)

            return Cursor()

        def commit(self):
            self.commits += 1

    calls = []

    def run(argv, timeout=60):
        calls.append(argv)
        if argv[0] == "ssh":
            return journal(f"Accepted publickey for team from 10.7.0.1 port 48215 ssh2: ED25519 {FP}", 1000.0, "cur;s=1")
        return json.dumps({"MESSAGE": "worms-ssh-gw: SRC=203.0.113.5 DST=1.2.3.4 PROTO=TCP SPT=48215 DPT=2222",
                           "__REALTIME_TIMESTAMP": "999900000"})  # fmt: skip

    cursor_file = tmp_path / "cursor"
    conn = Conn()
    env = {"SSH_GATEWAY_HOST": "10.5.0.40", "SSH_KEY": "/etc/worms/ssh/worms"}
    assert node.collect_gateway(env, conn, run=run, cursor_file=cursor_file) == 1
    logged_in_at, user, key_type, fingerprint, client, port = conn.rows[0]
    assert (user, key_type, fingerprint, client, port) == ("team", "ED25519", FP, "203.0.113.5", 48215)
    assert logged_in_at.timestamp() == 1000.0
    assert cursor_file.read_text().strip() == "cur;s=1" and conn.commits == 1
    # The next run starts after that cursor, quoted for the gateway's shell.
    node.collect_gateway(env, conn, run=run, cursor_file=cursor_file)
    assert "--after-cursor='cur;s=1'" in calls[-2][-1] or "'--after-cursor=cur;s=1'" in calls[-2][-1]
    assert node.collect_gateway({}, conn, run=run, cursor_file=cursor_file) == 0  # no gateway: nothing


def test_event_kinds_from_the_node_are_a_fixed_list():
    from round_agent import events as node

    with pytest.raises(SystemExit):
        node.main(["event", "anything.else"])
    assert node.EVENT_KINDS == {"internet_allowed", "internet_denied", "watchdog_stopped"}


def test_the_node_writes_as_its_events_account_only():
    from round_agent import events as node

    seen = {}

    def connect(env):
        seen.update(env)
        return object()

    import round_agent.agent as agent

    original = agent.connect
    agent.connect = connect
    try:
        node.connect({"DB_USER": "round_agent", "DB_PASSWORD": "agent", "EVENTS_DB_USER": "dbuser_events_node",
                      "EVENTS_DB_PASSWORD": "events"})  # fmt: skip
        assert (seen["DB_USER"], seen["DB_PASSWORD"]) == ("dbuser_events_node", "events")
        with pytest.raises(RuntimeError):
            node.connect({"DB_USER": "round_agent", "DB_PASSWORD": "agent"})  # never falls back to the agent's role
    finally:
        agent.connect = original
