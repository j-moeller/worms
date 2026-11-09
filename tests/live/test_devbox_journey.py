"""The devbox journey on the real system, from the base state, with nothing faked:

    an administrator creates users, teams and devbox VMs;
    users sign in, add SSH keys and reach their devbox through the SSH gateway;
    on the devbox they create a file, use apt and pip, and call scoring and logging;
    the dashboard shows the scoring and logging, and the database what an administrator expects;
    a user resets the devbox and signs in again: everything they changed is gone.

It needs a deployed platform on the Proxmox node, and it takes about an hour and a half, so it runs
only with --live (docs/03-manage-py.md):

    .venv-test/bin/pytest tests/live --live

It writes a protocol and screenshots to --live-out, and never a password. The steps build on
each other: a step whose prerequisite failed is skipped and says so. It leaves the teams and
devboxes in place to look at; docs/03-manage-py.md says how to get back to the base state.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from livelib import REPO, Member, NotBaseState, Platform, Report, check_wipe_allowed, parse_key_values, split_ssh_command, wait_for

sys.path.insert(0, str(REPO / "scripts"))

TEAMS = ["alpha", "bravo", "charlie"]
MEMBERS = {
    "alpha": ["Anna", "Arne", "Aylin"],
    "bravo": ["Ben", "Bea", "Bo"],
    "charlie": ["Carl", "Cem", "Cleo"],
}
# Held by the private internet (apt_cache_seed_packages, devpi_seed_packages), and not on a
# new devbox. Each with the program or module it provides.
APT_PACKAGE = "jq"
PIP_PACKAGE = "tabulate"

BUILD_TIMEOUT = 2 * 3600
BUILD_ATTEMPTS = 8  # F5 hit 4 of 7 attempts on 2026-11-04
RECONFIGURE_TIMEOUT = 45 * 60
RESET_TIMEOUT = 60 * 60
RECORD_TIMEOUT = 5 * 60  # scoring and logging reach the page through the attempt processor

# The team that resets. The others show that a reset touches only the team asking for it.
RESET_TEAM = "alpha"


class Journey:
    """What the steps share. `done` holds the steps that passed, for the prerequisites."""

    def __init__(self, platform: Platform, report: Report, wipe: str | None):
        self.platform = platform
        self.report = report
        self.wipe = wipe
        self.done: set[str] = set()
        self.failures: list[str] = []
        self.admin_password = ""
        self.members = [Member(first, team.capitalize(), team) for team in TEAMS for first in MEMBERS[team]]
        self.states: dict[str, dict[str, dict]] = {}
        self.marker = f"live-{datetime.now():%H%M%S}"

    def need(self, *steps: str):
        missing = [step for step in steps if step not in self.done]
        if missing:
            pytest.skip(f"needs {', '.join(missing)}, which did not pass")

    def check(self, action: str, expected: str, actual, ok: bool) -> bool:
        """One row of the protocol. A failed check fails the step, after it has finished."""
        self.report.row(action, expected, actual, bool(ok))
        if not ok:
            self.failures.append(f"{action}: expected {expected}; got {actual}")
        return bool(ok)

    def passed(self, step: str):
        if not self.failures:
            self.done.add(step)

    def team(self, name: str) -> list[Member]:
        return [member for member in self.members if member.team == name]

    def chains(self, team: str, database: dict) -> dict[str, list[str]]:
        """How to reach each VM of a team from the node: A directly, B through A, C through B."""
        vms = {vm["role"]: vm for vm in database["teams"][team]["vms"]}
        entry, to_b, to_c = vms["a"]["ssh_fabric_ip"], vms["b"]["addresses"][0], vms["c"]["addresses"][0]
        return {"a": [entry], "b": [entry, to_b], "c": [entry, to_b, to_c]}

    def state(self, team: str, database: dict) -> dict[str, dict]:
        """What is on each of a team's VMs, read as its first member."""
        member = self.team(team)[0]
        result = {}
        for role, chain in self.chains(team, database).items():
            proc = self.platform.on_vm(member, chain, "vm_state.sh", [APT_PACKAGE, PIP_PACKAGE], timeout=180)
            result[role] = parse_key_values(proc.stdout)
        return result


@pytest.fixture(scope="module")
def journey(request):
    option = request.config.getoption
    out = Path(option("--live-out") or f"/var/tmp/worms-live-{datetime.now():%Y%m%d-%H%M%S}")
    out.mkdir(parents=True, exist_ok=True)
    out.chmod(0o700)
    platform = Platform(out)
    report = Report(out)
    yield Journey(platform, report, option("--live-wipe-database"))
    report.finish()
    platform.close()
    print(f"\nprotocol and screenshots: {out}")


@pytest.fixture(autouse=True)
def soft_checks(journey):
    """A failed check does not stop its step: the rest of the step still reports. It fails at the end."""
    journey.failures.clear()
    yield
    if journey.failures:
        pytest.fail("\n".join(journey.failures), pytrace=False)


# --- Stage 0: the base state -------------------------------------------------------------


def test_00_base_state(journey):
    p, check = journey.platform, journey.check
    journey.report.stage("Stage 0: the base state", "whatever the node is in now")
    if p.devbox_vms():
        pytest.fail(
            f"{len(p.devbox_vms())} devbox VMs exist. Destroy them first, this suite does not: "
            "/usr/local/sbin/worms-node-run opentofu/stacks/devbox 'tofu destroy -auto-approve -parallelism=1'",
            pytrace=False,
        )
    orphans = p.orphan_volumes()
    check("Volumes left by earlier VMs", "None: a new VM given that ID cannot be created", orphans or "none", not orphans)
    if orphans:
        pytest.fail(f"Remove these first, with `pvesm free <storage>:<volume>`: {orphans}", pytrace=False)
    try:
        if check_wipe_allowed(journey.wipe, p.node):
            proc = p.worms("reset-database", "--yes", timeout=300)
            check("`worms reset database --yes`", "Exit 0", f"exit {proc.returncode}", proc.returncode == 0)
    except NotBaseState as exc:
        pytest.fail(str(exc), pytrace=False)
    db = p.database()
    not_clean = db["teams"] or db["rounds"] or db["users"] != ["admin"]
    if not_clean:
        pytest.fail(
            f"The database is not empty ({len(db['teams'])} teams, {db['rounds']} rounds, {len(db['users'])} users). "
            f"Wiping it is your decision: add --live-wipe-database={p.node}",
            pytrace=False,
        )
    check("`qm list`", "No devbox VMs", f"{len(p.vms())} VMs, 0 devbox", True)
    check("Database", "Only `admin`, no teams, no rounds", db["users"], True)
    status = p.worms("status").stdout
    check("`worms status`", "Internet denied", "denied" if "denied" in status else status[-200:], "denied" in status)
    jobs = [j for j in db["jobs"] if j["state"] in ("queued", "running")]
    check("Host jobs", "None queued or running", len(jobs), not jobs)
    journey.passed("base state")


# --- Stage 1: the administrator ----------------------------------------------------------


def test_01_admin_signs_in(journey):
    journey.need("base state")
    p, check = journey.platform, journey.check
    journey.report.stage("Stage 1: the administrator creates users and teams", "the base state")
    proc = p.worms("admin", "reset-password", "--yes")
    otp = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
    check("`worms admin-reset-password --yes`", "A one-time password, printed once", f"exit {proc.returncode}, {len(otp)} characters", proc.returncode == 0 and len(otp) >= 16)
    journey.admin_password = Member("x", "x", "x").password
    r = p.browser("admin_first_login", otp=otp, password=journey.admin_password)
    check("Sign in with a wrong password", "Refused", f"{r['wrong']}, {r['wrongPath']}", r["wrong"] == 401 and r["wrongPath"] == "/login/")
    check("Sign in with the one-time password", "Forced to `/change-password/`", r["firstPath"], r["firstPath"] == "/change-password/")
    check("Open a staff page before changing it", "Still forced to change it", r["beforeChangePath"], r["beforeChangePath"] == "/change-password/")
    check("Set a new password", "Accepted, leaves the forced page", r["afterPath"], r["afterPath"] != "/change-password/")
    check("Open the staff pages", "All 200", r["pages"], set(r["pages"].values()) == {200})
    check("JavaScript errors", "None", r["jsErrors"], not r["jsErrors"])
    journey.passed("admin signed in")


def test_02_admin_creates_teams(journey):
    journey.need("admin signed in")
    p, check = journey.platform, journey.check
    r = p.browser("admin_create_teams", password=journey.admin_password, teams=TEAMS, max_members=3)
    check("Staff → Competition", "No development competition to create", r["devPanel"], r["devPanel"] == 0)
    for team, messages in r["teams"].items():
        check(f"Staff → Teams → `{team}`", "Created", messages, any(f"Team {team} created" in m for m in messages))
    db = p.database()
    check("Database: teams", "The 3 teams", sorted(db["teams"]), sorted(db["teams"]) == TEAMS)
    journey.passed("teams")


def test_03_admin_creates_users(journey):
    journey.need("teams")
    p, check = journey.platform, journey.check
    users = [{"first": m.first, "last": m.last, "email": m.email, "team": m.team} for m in journey.members]
    r = p.browser("admin_create_users", password=journey.admin_password, users=users)
    check("Staff → Users: 1 by the form, 8 by CSV", "9 accounts with one-time passwords, no problems", f"{len(r['passwords'])} accounts, problems: {r['problems']}", len(r["passwords"]) == 9 and not r["problems"])
    for member in journey.members:
        member.otp = r["passwords"].get(member.email, "")
    teams = p.worms("teams").stdout
    check("`worms status teams`", "Each team at 3/3", " ".join(line.split()[1] for line in teams.splitlines() if line.split()[:1] and line.split()[0] in TEAMS), all(f"{t}" in teams for t in TEAMS) and teams.count("3/3") == 3)
    users_out = p.worms("users").stdout
    check("`worms status users`", "9 users with a one-time password", users_out.count("one-time pw"), users_out.count("one-time pw") == 9)
    journey.passed("users")


# --- Stage 2: the administrator builds the devboxes ---------------------------------------


def test_04_admin_builds_devboxes(journey):
    journey.need("users")
    p, check = journey.platform, journey.check
    journey.report.stage("Stage 2: the administrator builds the devboxes", "3 teams, 9 users, no devbox VMs")
    for attempt in range(1, BUILD_ATTEMPTS + 1):
        before = len([j for j in p.database()["jobs"] if j["kind"] == "devbox_provision"])
        proc = p.worms("devbox", "provision")
        check(f"`worms build devbox`, attempt {attempt}", "A host job", proc.stdout.strip().splitlines()[-1][:100] if proc.stdout.strip() else proc.stderr[-200:], proc.returncode == 0 and "devbox_provision" in proc.stdout)

        def finished():
            jobs = [j for j in p.database()["jobs"] if j["kind"] == "devbox_provision"]
            return jobs[-1] if len(jobs) > before and jobs[-1]["state"] in ("done", "failed") else None

        job = wait_for("the devbox build", finished, BUILD_TIMEOUT)
        if job["state"] == "done" or "got timeout" not in (job["error"] or ""):
            break
        # A known fault on this disk (F5 in the 2026-11-03 run protocol, docs/archive/ in the git history), for which the
        # documented answer is to run it again. Reported, but it does not fail the journey.
        journey.report.row(f"Attempt {attempt} hit the `zfs create` timeout", "Known fault, F5: retry", (job["error"] or "")[-160:], True)
    check("The build job", "Done", f"{job['state']} on attempt {attempt}, from {job['started_at']} to {job['finished_at']} {job['error'] or ''}", job["state"] == "done")
    vms = p.devbox_vms()
    check("`qm list`", "13 devbox VMs running: SSH gateway, 3 gateways, 9 VMs", f"{len(vms)}, {sum(v['status'] == 'running' for v in vms)} running", len(vms) == 13 and all(v["status"] == "running" for v in vms))
    check("Firewall on every devbox VM", "All on", p.vms_without_firewall() or "all on", not p.vms_without_firewall())
    check("Firewall watchdog", "No VM stopped", p.watchdog_stops(), p.watchdog_stops() == 0)
    db = p.database()
    for team in TEAMS:
        check(f"Database: `{team}`'s devbox", "3 VMs, each with a token hash", len(db["teams"][team]["vms"]), len(db["teams"][team]["vms"]) == 3 and all(vm["token_hash"] for vm in db["teams"][team]["vms"]))
    journey.passed("devboxes built")


# --- Stage 3: users sign in and add their keys --------------------------------------------


def test_05_users_sign_in_and_add_keys(journey):
    journey.need("devboxes built")
    p, check = journey.platform, journey.check
    journey.report.stage("Stage 3: users sign in and add their SSH keys", "13 devbox VMs running, one-time passwords handed out")
    for i, member in enumerate(journey.members):
        p.make_key(member)
        r = p.browser(
            "user_first_login_and_key", user=member.email, otp=member.otp, password=member.password,
            key_name=f"{member.short}-laptop", key=member.public_key, shots=i == 0,
        )  # fmt: skip
        check(f"{member.short}: one-time password", "Forced to `/change-password/`, then signed in", f"{r['firstPath']} → {r['afterPath']}", r["firstPath"] == "/change-password/" and r["afterPath"] != "/change-password/")
        check(f"{member.short}: add an SSH key", "Saved, and being pushed", f"{r['keyStatus']}, {r['keys']}, {r['messages'][-1:]}", r["keyStatus"] == 200 and r["onProfile"] and any("being updated" in m for m in r["messages"]))
        check(f"{member.short}: the page", "Shows the SSH command, no JavaScript errors", r["ssh_command"], bool(r["ssh_command"]) and not r["jsErrors"])
        split_ssh_command(r["ssh_command"])
        if i == 0:
            again = p.browser("try_login", user=member.email, password=member.otp)
            check(f"{member.short}: the one-time password again", "Refused", f"{again['status']}, {again['path']}", again["status"] == 401 and again["path"] == "/login/")
        member.otp = ""

    def settled():
        jobs = [j for j in p.database()["jobs"] if j["kind"] == "devbox_reconfigure"]
        return jobs if jobs and all(j["state"] in ("done", "failed") for j in jobs) else None

    jobs = wait_for("the SSH keys to be pushed", settled, RECONFIGURE_TIMEOUT)
    # A job failing here matters only if the keys do not arrive, which the next step shows, but it should be seen.
    failed = [j for j in jobs if j["state"] == "failed"]
    check("Key push jobs", "All done", f"{len(jobs)} jobs, {len(failed)} failed {[j['error'] for j in failed][:2]}", not failed)
    db = p.database()
    for team in TEAMS:
        check(f"Database: `{team}`'s keys", "3 keys", db["teams"][team]["ssh_keys"], db["teams"][team]["ssh_keys"] == 3)
    journey.passed("keys")


# --- Stage 4: reaching the devbox through the gateway -------------------------------------


def test_06_each_user_reaches_their_own_devbox(journey):
    journey.need("keys")
    p, check = journey.platform, journey.check
    journey.report.stage(
        "Stage 4: SSH through the gateway",
        f"keys pushed. From the node, with the gateway's address {p.gateway_ip} in place of the public name, because the port forward only matches outside traffic",
    )
    db = p.database()
    for member in journey.members:
        entry = next(vm for vm in db["teams"][member.team]["vms"] if vm["role"] == "a")
        proc = p.ssh(member, "hostname; id -un", f"root@{entry['ssh_fabric_ip']}")
        lines = proc.stdout.split()
        check(f"{member.short} → own A", f"`{entry['name']}` as root", " ".join(lines) or proc.stderr[-150:], lines == [entry["name"], "root"])
    journey.passed("ssh")


def test_07_the_gateway_allows_nothing_else(journey):
    journey.need("ssh")
    p, check = journey.platform, journey.check
    db = p.database()
    anna = journey.team("alpha")[0]
    ben = journey.team("bravo")[0]
    vms = {t: {vm["role"]: vm for vm in db["teams"][t]["vms"]} for t in TEAMS}
    refused = [
        ("anna → bravo's A", p.ssh(anna, "hostname", f"root@{vms['bravo']['a']['ssh_fabric_ip']}", timeout=90)),
        ("ben → alpha's A", p.ssh(ben, "hostname", f"root@{vms['alpha']['a']['ssh_fabric_ip']}", timeout=90)),
        ("anna: a command on the gateway", p.ssh(anna, "id", jump=False, timeout=60)),
    ]
    for action, proc in refused:
        check(action, "Refused", f"exit {proc.returncode}", proc.returncode != 0 and "root" not in proc.stdout and "uid=" not in proc.stdout)
    stranger = Member("Stranger", "Nobody", "nobody")
    p.make_key(stranger)
    proc = p.ssh(stranger, "hostname", f"root@{vms['alpha']['a']['ssh_fabric_ip']}", timeout=90)
    check("A key nobody registered", "Refused", f"exit {proc.returncode}", proc.returncode != 0)


# --- Stage 5: working on the devbox -------------------------------------------------------


def test_08_work_on_the_devbox(journey):
    """A file, apt, pip, scoring and logging, on all three VMs of every team."""
    journey.need("ssh")
    p, check = journey.platform, journey.check
    journey.report.stage("Stage 5: on the devbox", "each team's VMs as built: no marker file, no `jq`, no `tabulate`")
    db = p.database()
    for team in TEAMS:
        member = journey.team(team)[0]
        marker = f"{journey.marker}-{team}"
        for role, chain in journey.chains(team, db).items():
            proc = p.on_vm(member, chain, "on_vm.sh", [marker, APT_PACKAGE, PIP_PACKAGE])
            r = parse_key_values(proc.stdout)
            who = f"{team} {role.upper()}"
            check(f"{who}: reached", f"`devbox-…-{role}` as root", f"{r.get('host')} as {r.get('user')}", r.get("user") == "root" and str(r.get("host", "")).endswith(f"-{role}"))
            check(f"{who}: create a file", marker, r.get("file"), r.get("file") == marker)
            check(f"{who}: apt uses the private internet", "The apt cache", r.get("apt_source"), p.apt_cache_ip in str(r.get("apt_source")) or "apt-cache" in str(r.get("apt_source")))
            check(f"{who}: `apt-get update`", "Exit 0", r.get("apt_update_rc"), r.get("apt_update_rc") == "0")
            check(f"{who}: `apt-get install {APT_PACKAGE}`", "Absent, then installed", f"{r.get('apt_before')} → exit {r.get('apt_install_rc')} → {r.get('apt_after')}", r.get("apt_before") == "absent" and r.get("apt_install_rc") == "0" and r.get("apt_after") != "absent")
            check(f"{who}: pip uses the private internet", "devpi", r.get("pip_source"), "pypi.worms.internal" in str(r.get("pip_source")))
            check(f"{who}: `pip3 install {PIP_PACKAGE}`", "Absent, then installed, with no special flags", f"{r.get('pip_before')} → exit {r.get('pip_install_rc')} → {r.get('pip_after')}", r.get("pip_before") == "absent" and r.get("pip_install_rc") == "0" and r.get("pip_after") == "present")
            check(f"{who}: `curl` to scoring", "201 over verified TLS", r.get("scoring"), r.get("scoring") == "http=201 tls_verify=0")
            check(f"{who}: `curl` to logging", "201 over verified TLS", r.get("logging"), r.get("logging") == "http=201 tls_verify=0")
            closed = [r.get(k) for k in ("internet_debian", "internet_pypi", "internet_ip")]
            check(f"{who}: the real internet", "Out of reach", closed, closed == ["closed"] * 3)
    journey.passed("work")


# --- Stage 6: the traces -------------------------------------------------------------------


def test_09_dashboard_shows_scoring_and_logging(journey):
    journey.need("work")
    p, check = journey.platform, journey.check
    journey.report.stage("Stage 6: the traces", "each team's three VMs submitted their token and a log entry")

    def recorded():
        db = p.database()
        ok = all(sum(db["teams"][t]["captures"].values()) == 3 and sum(db["teams"][t]["logs"].values()) == 3 for t in TEAMS)
        return db if ok else None

    try:
        wait_for("scoring and logging to be processed", recorded, RECORD_TIMEOUT, interval=15)
    except TimeoutError as exc:
        check("Processed within 5 minutes", "All 3 teams at 3 captures and 3 logs", exc, False)
    for team in TEAMS:
        member = journey.team(team)[0]
        marker = f"{journey.marker}-{team}"
        others = [f"{journey.marker}-{t}" for t in TEAMS if t != team]
        r = p.browser("dev_view", user=member.email, password=member.password, shot=f"09-dashboard-{team}")
        text = " ".join(r["logs"])
        check(f"`{team}`'s page: tokens found", "3/3", r["score"], r["score"].replace(" ", "") == "3/3")
        check(f"`{team}`'s page: log entries", "3 entries with this team's marker", f"{len(r['logs'])}, marker shown: {marker in text}", len(r["logs"]) == 3 and marker in text)
        check(f"`{team}`'s page: other teams' entries", "None", [o for o in others if o in text], not any(o in text for o in others))
    journey.passed("dashboard")


def test_10_database_holds_what_an_administrator_expects(journey):
    journey.need("work")
    p, check = journey.platform, journey.check
    db = p.database()
    for team in TEAMS:
        t = db["teams"][team]
        check(f"Database: `{team}`", "3 keys, 3 captures and 3 logs at version 1", f"keys {t['ssh_keys']}, captures {t['captures']}, logs {t['logs']}", t["ssh_keys"] == 3 and t["captures"] == {"1": 3} and t["logs"] == {"1": 3})
    jobs = {j["kind"] for j in db["jobs"]}
    check("Database: host jobs", "The build and the key pushes", sorted(jobs), {"devbox_provision", "devbox_reconfigure"} <= jobs)
    # Devbox bridges are not mirrored, by design (docs/06-background-processes.md).
    check("Database: `network_flows` on devbox addresses", "None, by design", db["flows_touching_devboxes"], db["flows_touching_devboxes"] == 0)
    check("Node: mirror filters on devbox ports", "None, by design", p.mirror_filters_on_devbox_ports(), p.mirror_filters_on_devbox_ports() == 0)
    heartbeat = db["zeek_heartbeat"]
    fresh = bool(heartbeat) and datetime.fromisoformat(db["now"]) - datetime.fromisoformat(heartbeat) < timedelta(minutes=15)
    check("Database: Zeek's heartbeat", "Younger than 15 minutes", heartbeat, fresh)


# --- Stage 7: the reset --------------------------------------------------------------------


def test_11_user_resets_the_devbox(journey):
    journey.need("work")
    p, check = journey.platform, journey.check
    journey.report.stage("Stage 7: a user resets the devbox", f"`{RESET_TEAM}` has a marker file, `{APT_PACKAGE}`, `{PIP_PACKAGE}`, 3/3 tokens at version 1")
    db = p.database()
    journey.states["before"] = {team: journey.state(team, db) for team in TEAMS}
    for team, vms in journey.states["before"].items():
        for role, state in vms.items():
            check(f"Before: {team} {role.upper()}", "File, apt package and pip package present", f"{state.get('file')}, {state.get('apt')}, {state.get('pip')}", state.get("file", "absent") != "absent" and state.get("apt") == "present" and state.get("pip") == "present")
    journey.ids_before = {vm["name"]: vm["id"] for vm in p.devbox_vms()}
    member = journey.team(RESET_TEAM)[1]
    r = p.browser("reset_devbox", user=member.email, password=member.password)
    check(f"{member.short} presses Reset devbox and confirms", "Accepted, score back to 0, version 2", f"{r['status']}, {r['after']['version']}, {r['after']['score']}, {r['messages'][-1:]}", r["status"] == 200 and r["after"]["score"].replace(" ", "").startswith("0/") and "2" in r["after"]["version"])

    def finished():
        jobs = [j for j in p.database()["jobs"] if j["kind"] == "devbox_rebuild"]
        return jobs[-1] if jobs and jobs[-1]["state"] in ("done", "failed") else None

    job = wait_for("the reset", finished, RESET_TIMEOUT)
    check("The reset job", "Done", f"{job['state']}, from {job['started_at']} to {job['finished_at']} {job['error'] or ''}", job["state"] == "done")
    journey.passed("reset")


def test_12_everything_the_user_changed_is_gone(journey):
    journey.need("reset")
    p, check = journey.platform, journey.check
    journey.report.stage("Stage 8: after the reset", "the reset job finished")
    db = p.database()
    journey.states["after"] = {team: journey.state(team, db) for team in TEAMS}
    before, after = journey.states["before"][RESET_TEAM], journey.states["after"][RESET_TEAM]
    member = journey.team(RESET_TEAM)[0]
    check(f"{member.short} signs in again with the same key", "Works, on all 3 VMs", sorted(s.get("host", "unreachable") for s in after.values()), len(after) == 3 and all(s.get("host") for s in after.values()))
    for role, state in after.items():
        who = f"{RESET_TEAM} {role.upper()}"
        check(f"{who}: the file", "Gone", state.get("file"), state.get("file") == "absent")
        check(f"{who}: `{APT_PACKAGE}`", "Gone", state.get("apt"), state.get("apt") == "absent")
        check(f"{who}: `{PIP_PACKAGE}`", "Gone", state.get("pip"), state.get("pip") == "absent")
        # A reset rolls the VM back to the snapshot taken after its first boot, so the host key,
        # made at that boot, stays; only a VM without a snapshot is replaced and gets a new one.
        check(f"{who}: back to its clean state", "New boot and token", f"boot {before[role].get('boot')} → {state.get('boot')}", state.get("boot") != before[role].get("boot") and state.get("token") != before[role].get("token"))
    check("Firewall on every devbox VM", "All on, including the reset VMs", p.vms_without_firewall() or "all on", not p.vms_without_firewall())
    check("Firewall watchdog", "No VM stopped", p.watchdog_stops(), p.watchdog_stops() == 0)
    r = p.browser("dev_view", user=member.email, password=member.password, shot="14-after-reset")
    check("The page", "Score 0/3, no log entries, the SSH command", f"{r['version']}, {r['score']}, {len(r['logs'])} entries", r["score"].replace(" ", "") == "0/3" and not r["logs"] and bool(r["ssh_command"]))
    t = db["teams"][RESET_TEAM]
    check("Database: the earlier records", "Kept at version 1, none at version 2", f"version {t['version']}, captures {t['captures']}, logs {t['logs']}", t["version"] == 2 and t["captures"] == {"1": 3} and t["logs"] == {"1": 3})
    journey.passed("gone")


def test_13_the_other_teams_are_untouched(journey):
    journey.need("reset")
    p, check = journey.platform, journey.check
    for team in TEAMS:
        if team == RESET_TEAM:
            continue
        before, after = journey.states["before"][team], journey.states["after"][team]
        for role in before:
            same = all(before[role].get(k) == after[role].get(k) for k in ("file", "apt", "pip", "token", "boot", "host_key"))
            check(f"{team} {role.upper()}", "Exactly as before", "same" if same else f"{before[role]} → {after[role]}", same)
        r = p.browser("dev_view", user=journey.team(team)[0].email, password=journey.team(team)[0].password)
        check(f"`{team}`'s page", "Still 3/3 at version 1", f"{r['version']}, {r['score']}", r["score"].replace(" ", "") == "3/3" and "1" in r["version"])


def test_14_the_new_tokens_score_again(journey):
    journey.need("gone")
    p, check = journey.platform, journey.check
    db = p.database()
    member = journey.team(RESET_TEAM)[0]
    marker = f"{journey.marker}-after-reset"
    for role, chain in journey.chains(RESET_TEAM, db).items():
        r = parse_key_values(p.on_vm(member, chain, "on_vm.sh", [marker, APT_PACKAGE, PIP_PACKAGE]).stdout)
        check(f"{RESET_TEAM} {role.upper()}: apt, pip, scoring and logging again", "All work on the new VM", f"apt {r.get('apt_install_rc')}, pip {r.get('pip_install_rc')}, {r.get('scoring')}, {r.get('logging')}", r.get("apt_install_rc") == "0" and r.get("pip_install_rc") == "0" and r.get("scoring") == "http=201 tls_verify=0" and r.get("logging") == "http=201 tls_verify=0")

    def recorded():
        t = p.database()["teams"][RESET_TEAM]
        return t if t["captures"].get("2") == 3 and t["logs"].get("2") == 3 else None

    try:
        wait_for("the new submissions to be processed", recorded, RECORD_TIMEOUT, interval=15)
    except TimeoutError as exc:
        check("Processed within 5 minutes", "3 captures and 3 logs at version 2", exc, False)
    r = p.browser("dev_view", user=member.email, password=member.password, shot="15-scored-after-reset")
    check("The page", "3/3 at version 2, 3 entries", f"{r['version']}, {r['score']}, {len(r['logs'])} entries", r["score"].replace(" ", "") == "3/3" and "2" in r["version"] and len(r["logs"]) == 3)
    now = datetime.now(timezone.utc)
    check("Finished", "The whole journey ran", f"{now:%H:%M:%S} UTC", True)
