"""The public status page: status.<public name> (docs/06-background-processes.md, "Status page").

Modelled on the common status pages (Atlassian Statuspage, as status.openai.com was built on): the
platform's components in groups, each in one of five states; one headline for the whole page,
rolled up from them by Statuspage's rules; a bar per day for the last 90 days with the uptime; the
incidents and maintenance staff post, with their updates; and the same as JSON (summary.json, in
Statuspage's shape).

    operational · degraded_performance · partial_outage · major_outage · under_maintenance

How it stays up when what it reports on is down: the checks run on the Proxmox node (a systemd
timer, every minute, `worms-status-page`), from outside every VM, and the page is static: copied to
the dashboard VM, whose nginx serves it for the status name without Django or the database. If the
dashboard VM or the node is down, so is the page: they are the only way in from the internet.

    python3 -m worms_cli.statuspage run [--out DIR]   check, record, render, publish (the timer)

The checks: the website and the team API over HTTPS as a browser reaches them; the SSH gateway's
banner; each VM's services (systemctl over the management SSH, as `worms login`, the WORMS key
never forwarded); the round agent on the node; the devbox VMs' power in Proxmox. A check that fails
is tried again once before it counts. Nothing a check returns is shown: only the state.

History: per component and day, how many checks found each state (SQLite under STATE_DIR). A day's
bar shows its worst state that lasted two checks or more; uptime counts a major outage as down and
a partial outage as half down.
"""

from __future__ import annotations

import html
import json
import os
import socket
import sqlite3
import ssl
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

STATE_DIR = Path(os.environ.get("STATUS_STATE_DIR", "/var/lib/worms/status"))
NOTICES = "notices.json"
HISTORY = "history.sqlite"
WEB_ROOT = "/var/www/status"  # on the dashboard VM (role nginx_tls_django)
DAYS = 90
KEEP_DAYS = 100
HELD_DEVBOXES = Path("/var/lib/worms/held-devboxes.json")  # round_agent/handlers.py, hold_devboxes
INTERNET_FLAG = Path("/run/worms/internet-allowed")

STATES = ("operational", "degraded_performance", "partial_outage", "major_outage", "under_maintenance")
LABEL = {
    "operational": "Operational", "degraded_performance": "Degraded performance", "partial_outage": "Partial outage",
    "major_outage": "Major outage", "under_maintenance": "Under maintenance",
}  # fmt: skip
DOWN = {"major_outage": 1.0, "partial_outage": 0.5}
INCIDENT_STATUSES = ("investigating", "identified", "monitoring", "resolved")
IMPACTS = ("minor", "major", "critical")
RANK = {"none": 0, "maintenance": 1, "minor": 2, "major": 3, "critical": 4}

# The components, as teams and staff know them. A check: (kind, target, what); targets are
# management_ips / private_internet_ips keys from the node's inventory.
COMPONENTS = [
    ("Website", "Dashboard", [("https", "/", (200, 399)), ("units", "dashboard", ["dashboard", "nginx"])]),
    ("Website", "Team API", [("https", "/api/v1/rounds/", (401, 401))]),
    ("Rounds", "Round evaluation", [("units", "orchestrator", ["orchestrator-conduct-rounds", "orchestrator-process-attempts"]),
                                    ("local_unit", None, ["worms-round-agent"])]),
    ("Rounds", "Flag submissions (scoring)", [("units", "scoring", ["sandbox-api", "nginx"])]),
    ("Rounds", "Worm logs (logging)", [("units", "logging", ["sandbox-api", "nginx"])]),
    ("Devboxes", "SSH gateway", [("ssh_banner", None, None)]),
    ("Devboxes", "Devbox VMs", [("devbox_power", None, None)]),
    ("Private internet", "Debian packages (apt)", [("units", "pi:apt_cache", ["apt-cacher-ng"])]),
    ("Private internet", "Python packages (pip)", [("units", "pi:devpi", ["devpi"])]),
    ("Private internet", "Container images", [("units", "pi:docker_registry", ["docker-registry"])]),
    ("Private internet", "Wikipedia", [("units", "pi:wikipedia", ["kiwix-serve"])]),
    ("Private internet", "CVE search", [("units", "pi:cve_search", ["cve-search", "mongod", "redis-server"])]),
    ("Private internet", "Man pages", [("units", "pi:manpages", ["apache2"])]),
    ("Private internet", "Time (NTP)", [("units", "pi:ntp", ["chrony"])]),
    ("Private internet", "DNS", [("units", "dns", ["named"])]),
    ("Platform", "Database", [("command", "database", "pg_isready -q")]),
    ("Platform", "Log server", [("units", "syslog", ["rsyslog"])]),
    ("Platform", "Network monitor", [("units", "monitor", ["zeek", "zeek-shipper"])]),
]  # fmt: skip
NAMES = [name for _, name, _ in COMPONENTS]
SLOW = 3.0  # seconds: a page this slow is degraded


def now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(t: datetime) -> str:
    return t.isoformat().replace("+00:00", "Z")


def parse(value: str) -> datetime:
    t = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


# -- the checks ------------------------------------------------------------------------------------


class Checks:
    def __init__(self, inventory: dict, public_name: str):
        v = inventory.get("all", {}).get("vars", {})
        self.mgmt = dict(v.get("management_ips") or {})
        self.mgmt.update({f"pi:{k}": d.get("maintenance") for k, d in (v.get("private_internet_ips") or {}).items()})
        self.dashboard = (v.get("infrastructure_ips") or {}).get("dashboard")
        self.gateway = (v.get("ssh_gateway_ips") or {}).get("devbox_ssh")
        self.public_name = public_name

    def ssh(self, host, command, timeout=15):
        argv = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "-o", "ForwardAgent=no", "-o", "StrictHostKeyChecking=no",
                "-o", "UserKnownHostsFile=/dev/null", "-o", "LogLevel=ERROR", "-o", "IdentityFile=none", f"debian@{host}", command]  # fmt: skip
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)

    def https(self, path, ok):
        """(state, seconds) of a GET of the public site at its own address, by its name (SNI, Host)."""
        context = ssl.create_default_context()
        context.check_hostname, context.verify_mode = False, ssl.CERT_NONE  # its own CA; the name is what is checked
        start = time.monotonic()
        try:
            with socket.create_connection((self.dashboard, 443), timeout=8) as raw, context.wrap_socket(raw, server_hostname=self.public_name) as s:
                s.sendall(f"GET {path} HTTP/1.1\r\nHost: {self.public_name}\r\nUser-Agent: worms-status-page\r\nConnection: close\r\n\r\n".encode())
                line = s.recv(64).split(b"\r\n", 1)[0].split()
            code = int(line[1]) if len(line) > 1 and line[1].isdigit() else 0
        except (OSError, ValueError):
            return "major_outage", None
        took = time.monotonic() - start
        if not ok[0] <= code <= ok[1]:
            return "major_outage", took
        return ("degraded_performance" if took > SLOW else "operational"), took

    def units(self, target, units):
        host = self.mgmt.get(target)
        if not host:
            return "major_outage"
        try:
            proc = self.ssh(host, "systemctl is-active " + " ".join(units))
        except subprocess.TimeoutExpired:
            return "major_outage"
        active = proc.stdout.split().count("active")
        return "operational" if active == len(units) else "major_outage" if active == 0 else "partial_outage"

    def command(self, target, command):
        host = self.mgmt.get(target)
        try:
            return "operational" if host and self.ssh(host, command).returncode == 0 else "major_outage"
        except subprocess.TimeoutExpired:
            return "major_outage"

    @staticmethod
    def local_unit(units):
        proc = subprocess.run(["systemctl", "is-active", *units], capture_output=True, text=True)
        active = proc.stdout.split().count("active")
        return "operational" if active == len(units) else "major_outage" if active == 0 else "partial_outage"

    def ssh_banner(self):
        try:
            with socket.create_connection((self.gateway, 22), timeout=8) as s:
                return "operational" if s.recv(32).startswith(b"SSH-2.0-") else "major_outage"
        except OSError:
            return "major_outage"

    @staticmethod
    def devbox_power():
        """The devbox VMs and gateways in Proxmox: all running, some, none; shut down on purpose
        (a round holds them, the internet window is open): under maintenance. None at all: None."""
        out = subprocess.run(["pvesh", "get", "/cluster/resources", "--type", "vm", "--output-format", "json"],
                             capture_output=True, text=True, timeout=30).stdout  # fmt: skip
        vms = [vm for vm in json.loads(out or "[]") if vm.get("pool") == "devbox"]
        if not vms:
            return None
        if HELD_DEVBOXES.exists() or INTERNET_FLAG.exists():
            return "under_maintenance"
        running = sum(vm.get("status") == "running" for vm in vms)
        return "operational" if running == len(vms) else "major_outage" if running == 0 else "partial_outage"

    def one(self, check):
        kind, target, what = check
        if kind == "https":
            return self.https(target, what)[0]
        if kind == "units":
            return self.units(target, what)
        if kind == "local_unit":
            return self.local_unit(what)
        if kind == "command":
            return self.command(target, what)
        if kind == "ssh_banner":
            return self.ssh_banner()
        if kind == "devbox_power":
            return self.devbox_power()
        raise ValueError(kind)

    def component(self, checks):
        """The component's state: its worst check's, a failed check tried once more."""
        worst = "operational"
        for check in checks:
            state = self.one(check)
            if state in ("major_outage", "partial_outage"):
                time.sleep(2)
                state = self.one(check)
            if state is None:
                return None
            worst = max(worst, state, key=lambda s: ("operational", "under_maintenance", "degraded_performance",
                                                     "partial_outage", "major_outage").index(s))  # fmt: skip
        return worst

    def run(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            states = list(pool.map(lambda c: self.component(c[2]), COMPONENTS))
        return {name: state for (_, name, _), state in zip(COMPONENTS, states) if state is not None}


# -- incidents and maintenance (`worms incident`) ------------------------------------------------


def load_notices(state_dir=STATE_DIR) -> dict:
    try:
        return json.loads((Path(state_dir) / NOTICES).read_text())
    except (OSError, ValueError):
        return {"next_id": 1, "notices": []}


def active(notice, t):
    if notice["kind"] == "maintenance":
        return parse(notice["starts"]) <= t < parse(notice["ends"]) and not notice.get("resolved_at")
    return not notice.get("resolved_at")


MAINTENANCE_STATUSES = ("scheduled", "in_progress", "completed")


def component_names(text: str) -> list[str]:
    """`Dashboard, Team API` -> the components' names, as COMPONENTS spells them; ValueError for one it has not."""
    names = []
    for part in (p.strip() for p in (text or "").split(",")):
        if not part:
            continue
        match = next((n for n in NAMES if n.lower() == part.lower()), None)
        if match is None:
            raise ValueError(f"no component {part!r}: one of {', '.join(NAMES)}")
        names.append(match)
    return names


def new_notice(notices, kind, title, components, message, t, impact="minor", starts=None, ends=None) -> dict:
    """An incident (investigating, from now) or a maintenance (scheduled, `starts` to `ends`)."""
    if kind == "incident" and impact not in IMPACTS:
        raise ValueError(f"impact is one of {', '.join(IMPACTS)}")
    if kind == "maintenance" and not (starts and ends and starts < ends):
        raise ValueError("a maintenance needs --from and --until, the end after the start")
    status = "investigating" if kind == "incident" else "scheduled"
    notice = {
        "id": notices["next_id"], "kind": kind, "title": title.strip(), "impact": impact if kind == "incident" else "maintenance",
        "components": components, "starts": iso(starts or t), "ends": iso(ends) if ends else None, "resolved_at": None,
        "updates": [{"t": iso(t), "status": status, "text": message.strip() or ("We are looking into it." if kind == "incident" else title.strip())}],
    }  # fmt: skip
    notices["next_id"] += 1
    notices["notices"].append(notice)
    return notice


def update_notice(notices, notice_id, status, message, t) -> dict:
    """A notice's next update; resolved (an incident) or completed (a maintenance) ends it."""
    notice = next((n for n in notices["notices"] if n["id"] == notice_id), None)
    if notice is None:
        raise ValueError(f"no incident or maintenance #{notice_id}: `worms incident` lists them")
    allowed = INCIDENT_STATUSES if notice["kind"] == "incident" else MAINTENANCE_STATUSES
    if status not in allowed:
        raise ValueError(f"{'an incident' if notice['kind'] == 'incident' else 'a maintenance'}'s status is one of {', '.join(allowed)}")
    if notice.get("resolved_at"):
        raise ValueError(f"#{notice_id} is over already")
    notice["updates"].append({"t": iso(t), "status": status, "text": message.strip() or status.replace("_", " ").capitalize() + "."})
    if status in ("resolved", "completed"):
        notice["resolved_at"] = iso(t)
        if notice["kind"] == "maintenance" and parse(notice["ends"]) > t:
            notice["ends"] = iso(t)
    return notice


def maintained(notices, t) -> set:
    """The components under an active maintenance."""
    return {c for n in notices["notices"] if n["kind"] == "maintenance" and active(n, t) for c in n["components"]}


# -- history -------------------------------------------------------------------------------------


def record(states: dict, t: datetime, state_dir=STATE_DIR) -> sqlite3.Connection:
    db = sqlite3.connect(Path(state_dir) / HISTORY)
    db.execute("CREATE TABLE IF NOT EXISTS daily (day TEXT, component TEXT, state TEXT, checks INTEGER, PRIMARY KEY (day, component, state))")
    db.execute("CREATE TABLE IF NOT EXISTS latest (component TEXT PRIMARY KEY, state TEXT, since TEXT)")
    day = t.date().isoformat()
    for component, state in states.items():
        db.execute("INSERT INTO daily VALUES (?, ?, ?, 1) ON CONFLICT (day, component, state) DO UPDATE SET checks = checks + 1",
                   (day, component, state))  # fmt: skip
        db.execute("INSERT INTO latest VALUES (?, ?, ?) ON CONFLICT (component) DO UPDATE SET since = "
                   "CASE WHEN latest.state = excluded.state THEN latest.since ELSE excluded.since END, state = excluded.state",
                   (component, state, iso(t)))  # fmt: skip
    db.execute("DELETE FROM daily WHERE day < ?", ((t - timedelta(days=KEEP_DAYS)).date().isoformat(),))
    db.commit()
    return db


def history(db, t: datetime):
    """{component: [(day, {state: checks}) for the last DAYS days, oldest first]}, {component: (state, since)}."""
    first = (t - timedelta(days=DAYS - 1)).date()
    counts: dict = {}
    for day, component, state, checks in db.execute("SELECT day, component, state, checks FROM daily WHERE day >= ?", (first.isoformat(),)):
        counts.setdefault(component, {}).setdefault(day, {})[state] = checks
    days = [(first + timedelta(days=i)).isoformat() for i in range(DAYS)]
    bars = {name: [(d, counts.get(name, {}).get(d, {})) for d in days] for name in NAMES}
    latest = {component: (state, since) for component, state, since in db.execute("SELECT component, state, since FROM latest")}
    return bars, latest


def day_state(counts: dict) -> str | None:
    """A day's bar: its worst state that lasted two checks or more (or every check, the day's first
    minutes); None without a check."""
    if not counts:
        return None
    least = min(2, sum(counts.values()))
    for state in ("major_outage", "partial_outage", "degraded_performance", "under_maintenance"):
        if counts.get(state, 0) >= least:
            return state
    return "operational"


def uptime(days) -> float | None:
    total = sum(sum(c.values()) for _, c in days)
    if not total:
        return None
    down = sum(DOWN.get(state, 0) * n for _, c in days for state, n in c.items())
    return 100.0 * (1 - down / total)


# -- the headline (Statuspage's rules) -------------------------------------------------------------


def headline(states: list[str]) -> tuple[str, str]:
    """(indicator, description) of the page from its components' states, by Statuspage's rules
    (support.atlassian.com/statuspage/docs/top-level-status-and-incident-impact-calculations)."""
    s = set(states)
    if not states or s == {"operational"}:
        return "none", "All Systems Operational"
    if len(states) == 1:
        return {"under_maintenance": ("maintenance", "Service Under Maintenance"),
                "degraded_performance": ("minor", "Partially Degraded Service"),
                "partial_outage": ("major", "Partial System Outage"),
                "major_outage": ("critical", "Major System Outage")}[states[0]]  # fmt: skip
    if s == {"major_outage"}:
        return "critical", "Major System Outage"
    if s == {"partial_outage"}:
        return "major", "Partial System Outage"
    if "major_outage" in s:
        return "major", "Partial System Outage"
    if "partial_outage" in s:
        return "minor", "Minor Service Outage"
    if s == {"degraded_performance"}:
        return "minor", "Degraded System Service"
    if "degraded_performance" in s:
        return "minor", "Partially Degraded Service"
    return "maintenance", "System Under Maintenance"


# -- the page ------------------------------------------------------------------------------------


def summary(states, notices, bars, t, public_name) -> dict:
    """summary.json: Statuspage's /api/v2/summary.json, in part."""
    shown = {name: ("under_maintenance" if name in maintained(notices, t) else state) for name, state in states.items()}
    indicator, description = headline(list(shown.values()))
    incidents = [n for n in notices["notices"] if n["kind"] == "incident" and active(n, t)]
    for n in incidents:
        if RANK[n["impact"]] > RANK[indicator]:
            indicator = n["impact"]
    maintenances = [n for n in notices["notices"] if n["kind"] == "maintenance" and not n.get("resolved_at") and parse(n["ends"]) > t]
    return {
        "page": {"name": "WORMS", "url": f"https://status.{public_name}", "updated_at": iso(t)},
        "status": {"indicator": indicator, "description": description},
        "components": [
            {"name": name, "group": group, "status": shown[name], "uptime_90d": uptime(bars[name])}
            for group, name, _ in COMPONENTS if name in shown
        ],  # fmt: skip
        "incidents": incidents,
        "scheduled_maintenances": maintenances,
    }


CSS = """
:root{--bg:#fff;--fg:#1f2328;--muted:#656d76;--line:#d8dee4;--card:#f6f8fa;--ok:#1a7f37;--deg:#d4a72c;--part:#e16f24;--maj:#cf222e;--mnt:#0969da;--none:#e4e7eb}
@media (prefers-color-scheme:dark){:root{--bg:#0d1117;--fg:#e6edf3;--muted:#8d96a0;--line:#30363d;--card:#161b22;--ok:#3fb950;--deg:#d29922;--part:#f0883e;--maj:#f85149;--mnt:#4493f8;--none:#2d333b}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:860px;margin:0 auto;padding:32px 16px 64px}header{display:flex;justify-content:space-between;align-items:baseline;gap:16px;flex-wrap:wrap}
h1{font-size:22px;margin:0}h2{font-size:17px;margin:36px 0 12px}h3{font-size:15px;margin:0}.muted{color:var(--muted)}small{font-size:13px}
.banner{margin:24px 0;padding:16px 20px;border-radius:8px;color:#fff;font-size:18px;font-weight:600}
.i-none{background:var(--ok)}.i-minor{background:var(--deg)}.i-major{background:var(--part)}.i-critical{background:var(--maj)}.i-maintenance{background:var(--mnt)}
.notice{border:1px solid var(--line);border-left:4px solid var(--deg);border-radius:8px;padding:12px 16px;margin:12px 0;background:var(--card)}
.notice.major{border-left-color:var(--part)}.notice.critical{border-left-color:var(--maj)}.notice.maintenance{border-left-color:var(--mnt)}
.notice p{margin:6px 0}.update b{text-transform:capitalize}
.group{border:1px solid var(--line);border-radius:8px;margin:16px 0}.group>h3{padding:10px 16px;border-bottom:1px solid var(--line);background:var(--card);border-radius:8px 8px 0 0}
.comp{padding:12px 16px;border-bottom:1px solid var(--line)}.comp:last-child{border-bottom:0}
.row{display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap}.state{font-weight:600}
.s-operational{color:var(--ok)}.s-degraded_performance{color:var(--deg)}.s-partial_outage{color:var(--part)}.s-major_outage{color:var(--maj)}.s-under_maintenance{color:var(--mnt)}
.bars{display:flex;gap:2px;margin:8px 0 4px;height:28px}.bars span{flex:1;border-radius:2px;background:var(--none)}
.b-operational{background:var(--ok)!important}.b-degraded_performance{background:var(--deg)!important}.b-partial_outage{background:var(--part)!important}
.b-major_outage{background:var(--maj)!important}.b-under_maintenance{background:var(--mnt)!important}
.scale{display:flex;justify-content:space-between;font-size:12px;color:var(--muted)}
.day{border-bottom:1px solid var(--line);padding:10px 0}footer{margin-top:40px;font-size:13px;color:var(--muted)}
"""


def _when(value) -> str:
    return parse(value).strftime("%b %d, %H:%M UTC") if value else ""


def render(doc: dict, bars: dict, latest: dict, notices: dict, t: datetime) -> str:
    e = html.escape
    out = [f"<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
           f"<meta http-equiv=\"refresh\" content=\"60\"><title>WORMS status</title><style>{CSS}</style></head><body><main>",
           f"<header><h1>WORMS status</h1><a class=\"muted\" href=\"https://{e(doc['page']['url'].removeprefix('https://status.'))}\">"
           f"{e(doc['page']['url'].removeprefix('https://status.'))}</a></header>",
           f"<div class=\"banner i-{e(doc['status']['indicator'])}\">{e(doc['status']['description'])}</div>"]  # fmt: skip

    def notice_html(n):
        cls = "maintenance" if n["kind"] == "maintenance" else n.get("impact", "minor")
        when = f"{_when(n['starts'])} – {_when(n['ends'])}" if n["kind"] == "maintenance" else ""
        affected = f"<p class=\"muted\"><small>Affects: {e(', '.join(n['components']))}</small></p>" if n["components"] else ""
        updates = "".join(f"<p class=\"update\"><b>{e(u['status'])}</b> — {e(u['text'])} <small class=\"muted\">{_when(u['t'])}</small></p>"
                          for u in reversed(n["updates"]))  # fmt: skip
        return f"<div class=\"notice {cls}\"><h3>{e(n['title'])}</h3>{f'<p class=muted><small>{when}</small></p>' if when else ''}{updates}{affected}</div>"

    for n in doc["incidents"]:
        out.append(notice_html(n))
    for n in doc["scheduled_maintenances"]:
        out.append(notice_html(n))
    shown = {c["name"]: c for c in doc["components"]}
    for group in dict.fromkeys(g for g, _, _ in COMPONENTS):
        names = [name for g, name, _ in COMPONENTS if g == group and name in shown]
        if not names:
            continue
        out.append(f"<section class=\"group\"><h3>{e(group)}</h3>")
        for name in names:
            c = shown[name]
            up = c["uptime_90d"]
            since = latest.get(name, (None, None))[1]
            cells = []
            for day, counts in bars[name]:
                state = day_state(counts)
                total = sum(counts.values())
                tip = (f"{day}: no data" if state is None else f"{day}: {LABEL[state]}, {uptime([(day, counts)]):.2f}% up "
                       f"({total} checks)")  # fmt: skip
                cells.append(f"<span class=\"b-{state or 'none'}\" title=\"{e(tip)}\"></span>")
            out.append(
                f"<div class=\"comp\"><div class=\"row\"><span>{e(name)}</span><span class=\"state s-{c['status']}\" "
                f"title=\"since {e(_when(since))}\">{LABEL[c['status']]}</span></div><div class=\"bars\">{''.join(cells)}</div>"
                f"<div class=\"scale\"><span>{DAYS} days ago</span><span>{'' if up is None else f'{up:.2f} % uptime'}</span><span>Today</span></div></div>"
            )  # fmt: skip
        out.append("</section>")
    out.append("<h2>Past incidents</h2>")
    past = [n for n in notices["notices"] if n["kind"] == "incident"]
    for i in range(14):
        day = (t - timedelta(days=i)).date()
        that = [n for n in past if parse(n["starts"]).date() == day]
        body = "".join(notice_html(n) for n in that) or "<p class=\"muted\">No incidents reported.</p>"
        out.append(f"<div class=\"day\"><h3>{day.strftime('%b %d, %Y')}</h3>{body}</div>")
    out.append(f"<footer>Checked every minute from the Proxmox node, from outside every VM; a failed check is tried again "
               f"once before it counts. Last check: {e(_when(iso(t)))}. <a href=\"summary.json\">summary.json</a></footer>"
               "</main></body></html>")  # fmt: skip
    return "".join(out)


# -- running it ----------------------------------------------------------------------------------


def publish(checks: Checks, files: dict) -> None:
    """Copy the page to the dashboard VM: each file written aside, then renamed (never half a page)."""
    host = checks.mgmt.get("dashboard")
    for name, content in files.items():
        tmp, dest = f"{WEB_ROOT}/.{name}.new", f"{WEB_ROOT}/{name}"
        proc = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "-o", "ForwardAgent=no", "-o", "StrictHostKeyChecking=no",
             "-o", "UserKnownHostsFile=/dev/null", "-o", "LogLevel=ERROR", "-o", "IdentityFile=none", f"debian@{host}",
             f"sudo sh -c 'cat > {tmp} && chmod 644 {tmp} && mv -f {tmp} {dest}'"],
            input=content, capture_output=True, text=True, timeout=30,
        )  # fmt: skip
        if proc.returncode != 0:
            raise RuntimeError(f"could not publish {name} on the dashboard VM: {proc.stderr.strip()[:200]}")


def run(out_dir: str | None = None) -> int:
    import yaml

    public_name = os.environ["STATUS_PUBLIC_NAME"]
    repo = Path(os.environ.get("REPO_ROOT", "."))
    inventory = yaml.safe_load((repo / "ansible" / "inventory.generated.yml").read_text())
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    checks = Checks(inventory, public_name)
    t = now()
    notices = load_notices()
    states = checks.run()
    # A component under maintenance is recorded so (a blue bar), whatever its checks found.
    under = maintained(notices, t)
    db = record({n: ("under_maintenance" if n in under else s) for n, s in states.items()}, t)
    bars, latest = history(db, t)
    doc = summary(states, notices, bars, t, public_name)
    files = {"index.html": render(doc, bars, latest, notices, t), "summary.json": json.dumps(doc, indent=1)}
    if out_dir:
        for name, content in files.items():
            (Path(out_dir) / name).write_text(content)
    else:
        publish(checks, files)
    print(f"{doc['status']['description']}: " + ", ".join(f"{c['name']} {c['status']}" for c in doc["components"] if c["status"] != "operational"))
    return 0


if __name__ == "__main__":
    args = sys.argv[1:]
    if args[:1] != ["run"]:
        sys.exit("usage: python3 -m worms_cli.statuspage run [--out DIR]")
    sys.exit(run(args[args.index("--out") + 1] if "--out" in args else None))
