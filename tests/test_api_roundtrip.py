"""Round trip: write to the database, read it back through the team API, and
get exactly what was written for the team -- the same data, and nothing more.

A seeded generator writes a competition the way the platform would: rounds,
runs with their VMs and timeline events, flag submissions and captures, logs,
the monitor's connections, devboxes with their submissions and logs. It also
writes deliberate noise: another team's run in the same round (same
addresses, another time), records before and after a run's window, devbox
records in the middle of a run, VMs the team never captured, traffic to
infrastructure and to unknown addresses, another competition. It keeps a
ledger of everything it wrote.

What each team's token must get from each endpoint is derived from that
ledger alone -- never from the code under test -- and every response is
compared to it exactly: the same items in the same order, the same values,
the same keys. On top, each response is searched for what must never be in
it (other teams, VMs not captured, infrastructure, internals).

Endpoints: /api/v1/rounds/, .../scoring/ and .../logging/ (fully paged and
in one page), .../progression/, .../progression/flows/,
.../progression/logs/<ref>/ (each record; every other team's or
out-of-scope record is a 404) and .../progression.zip (every member). The
PROGRESSION page's own endpoint serves the same document.
"""

from __future__ import annotations

import io
import itertools
import json
import random
import zipfile
from datetime import timedelta

import pytest

from test_dashboard_login import HOST, proxied, site  # noqa: F401 (fixture)

pytest.importorskip("django")

DRAIN = timedelta(seconds=300)  # a run's window ends 5 minutes after it (the monitor's drain)

# What the sandbox reaches, as the live snapshot (tests/infra_fixture.py)
# names it for a team: address -> (node id, role, name).
SERVICES = {
    "10.2.0.14": ("role:scoring", "scoring", "scoring"),
    "10.2.0.15": ("role:logging", "logging", "logging"),
    "10.2.0.11": ("pi:dns", "dns", "dns"),
    "10.2.0.20": ("pi:apt-cache", "private_internet", "apt-cache"),
    "10.2.0.21": ("pi:devpi", "private_internet", "devpi"),
}
SCORING, LOGGING = "10.2.0.14", "10.2.0.15"
# Infrastructure (Proxmox host, the sandbox gateway's two legs, the
# database) and addresses nobody knows: traffic there is never a team's to see.
INFRA = ["10.4.0.1", "10.64.0.1", "10.2.0.2", "10.11.0.16"]
UNKNOWN = ["8.8.8.8", "10.99.0.5", "192.0.2.77"]
# Never in a team's response: internals, errors, the generator's architecture,
# management addresses (as JSON strings: "10.7" is also the start of 10.75).
NEVER = ["vmbr", "worms-pve", "sandbox-gw", "database", "orchestrator", "proxmox", "firewall", "ERR-", "ARCH-", "id_ed25519",
         '"10.4.', '"10.6.', '"10.7.', '"10.11.']  # fmt: skip


def payload(rng, marker):
    """A log record as a worm might send it: odd text, numbers, nesting."""
    return {
        "marker": marker,
        "n": rng.randint(-10**9, 10**9),
        "x": rng.choice([3.25, 0.5, -2.75, 1e-3]),
        "text": rng.choice(["ünïcödé ✓ 🐛", "<img src=x onerror=alert(1)>", "line\nbreak\ttab", 'quote"s \\ back', ""]),
        "list": [rng.randint(0, 9) for _ in range(rng.randint(0, 4))],
        "nested": {"ok": rng.choice([True, False, None]), "deeper": {"k": [{"v": rng.randint(0, 3)}]}},
    }


class Ledger:
    """Writes the data and remembers it."""

    def __init__(self, rng, now):
        self.rng, self.now = rng, now
        self.attempts, self.logs, self.flows, self.runs = [], [], [], []
        self.devbox_vms = {}  # team id -> [vm]
        self.uids = itertools.count()

    def hash(self):
        return f"{self.rng.getrandbits(256):064x}"

    # --- the monitor's connections ---------------------------------------
    def flow(self, ts, src, dst, port, transport="tcp", service=None, d=None, state=None):
        from core.models.monitoring import NetworkFlow

        rng = self.rng
        rec = {
            "uid": f"C{next(self.uids):06d}", "ts": ts, "d": rng.choice([0.0, 0.25, 0.5, 1.5, 3.0]) if d is None else d,
            "src": src, "dst": dst, "src_port": rng.randint(1024, 65535), "dst_port": port, "transport": transport,
            "service": service, "state": state or rng.choice(["SF", "S0", "REJ", "RSTO"]), "sb": rng.randint(0, 10**6),
            "db": rng.randint(0, 10**6), "sp": rng.randint(1, 500), "dp": rng.randint(0, 500),
        }  # fmt: skip
        NetworkFlow.objects.create(
            uid=rec["uid"], ts=ts, duration=rec["d"], src_ip=src, src_port=rec["src_port"], dst_ip=dst, dst_port=port,
            transport=transport, service=service, conn_state=rec["state"], src_ip_bytes=rec["sb"], dst_ip_bytes=rec["db"],
            src_pkts=rec["sp"], dst_pkts=rec["dp"],
        )  # fmt: skip
        self.flows.append(rec)

    # --- submissions and logs ----------------------------------------------
    def attempt(self, team, flag, ts, devbox=False, processed=True, source=None):
        """A flag submission; `source`: the run VM whose connection to scoring
        carried it (a flow is written for it), or None (no connection)."""
        from core.models.scoring import FlagCaptureAttempt

        obj = FlagCaptureAttempt.objects.create(team_uuid=str(team.uuid), vm_flag_hash=flag, created_at=ts, devbox=devbox, processed=processed)
        if source is not None:
            self.flow(ts - timedelta(seconds=0.5), source["addresses"][0], SCORING, 443, service="ssl", d=1.0, state="SF")
        rec = {"obj": obj, "team": team.id, "hash": flag, "ts": ts, "devbox": devbox, "processed": processed,
               "source": source["uuid"] if source else None, "capture": None, "dev_capture": None}  # fmt: skip
        self.attempts.append(rec)
        return rec

    def log(self, team, ts, devbox=False, stored_round=None, devbox_log=False, source=None):
        from core.models.devbox import DevboxLog
        from core.models.logging import Logging, LoggingAttempt

        data = payload(self.rng, f"{team.name}-log-{len(self.logs)}")
        obj = LoggingAttempt.objects.create(team_uuid=str(team.uuid), log_data=data, created_at=ts, devbox=devbox, processed=True)
        row = Logging.objects.create(team=team, round=stored_round, logging_attempt=obj) if stored_round else None
        if devbox_log:
            DevboxLog.objects.create(team=team, devbox_version=1, logging_attempt=obj)
        if source is not None:
            self.flow(ts - timedelta(seconds=0.5), source["addresses"][0], LOGGING, 443, service="ssl", d=1.0, state="SF")
        rec = {"obj": obj, "uuid": str(obj.uuid), "team": team.id, "ts": ts, "devbox": devbox, "data": data,
               "round": stored_round.id if stored_round else None, "logging_id": row.id if row else None,
               "devbox_log": devbox_log, "source": source["uuid"] if source else None}  # fmt: skip
        self.logs.append(rec)
        return rec

    # --- a run --------------------------------------------------------------------
    def run(self, team, rnd, zero, position, state="finished", until=600):
        """A run of `team` in `rnd`, its worm started at `zero`; records up to
        `until` seconds (a running run: so far)."""
        from core.models.rounds import RoundRun, RunEvent
        from core.models.scoring import FlagCapture
        from core.models.vm import VirtualMachine

        rng = self.rng
        at = lambda s: zero + timedelta(seconds=s)  # noqa: E731
        run = RoundRun.objects.create(
            round=rnd, team=team, position=position, seed=rng.randrange(10**6), state=state, worm_start_at=zero,
            ends_at=at(600), sandbox_params={"start_vm": f"{team.name}-vm0"}, topology={"layers": f"ARCH-{team.name}"},
        )  # fmt: skip
        vms = []
        for i in range(rng.randint(3, 6)):
            nets = sorted(rng.sample(range(3), rng.randint(1, 2)))
            obj = VirtualMachine.objects.create(
                round=rnd, run=run, vm_identifier=f"{team.name}-vm{i}", vm_maintenance_ip=f"10.4.{position}.{10 + i}",
                proxmox_vm_id=4200 + 10 * position + i, addresses=[f"10.64.{k}.{10 + i}" for k in nets],
                networks=[{"name": f"vmbr100{k}", "cidr": f"10.64.{k}.0/24"} for k in nets], ctf_flag_hash=self.hash(),
            )  # fmt: skip
            vms.append({"obj": obj, "uuid": str(obj.uuid), "label": obj.vm_identifier, "addresses": list(obj.addresses),
                        "cidrs": [f"10.64.{k}.0/24" for k in nets], "hash": obj.ctf_flag_hash, "start": i == 0})  # fmt: skip
        rec = {"team": team.id, "round": rnd.id, "run": run, "zero": zero, "state": state, "position": position,
               "vms": vms, "captured": {}, "events": []}  # fmt: skip
        self.runs.append(rec)

        def event(s, kind, vm=None, **data):
            RunEvent.objects.create(run=run, ts=at(s), kind=kind, vm=vm["obj"] if vm else None, data=data)
            rec["events"].append((s, kind, vm["uuid"] if vm else None, data))

        states = [(-60, "preparing"), (-50, "checking"), (-40, "deploying"), (-20, "armed"), (0, "running")]
        if state == "finished":
            states += [(600, "ending"), (620, "tearing_down"), (640, "finished")]
        for s, name in states:
            event(s, "state", state=name)
        event(-30, "error", message=f"ERR-{team.name}: no key /root/.ssh/id_ed25519 on 10.4.0.{position}")
        event(-10, "worm_deployed", worm_start_at=zero.isoformat())
        event(0, "worm_started")
        for vm in vms:
            event(1, "secret_released", vm)
        for vm in rng.sample(vms, rng.randint(0, 2)):
            s = rng.randrange(100, until - 60) + 0.5
            event(s, "vm_stopped", vm)
            event(s + 30, "vm_started", vm)

        slots = list(range(15, until, 10))  # submissions and logs, 10 s apart: unambiguous senders
        rng.shuffle(slots)
        source = lambda: rng.choice(vms + [None])  # noqa: E731
        for vm in rng.sample(vms, rng.randint(0, len(vms) - 1)):  # never all: some VMs stay hidden
            s = slots.pop()
            submission = self.attempt(team, vm["hash"], at(s), source=source())
            FlagCapture.objects.create(team=team, round=rnd, vm=vm["obj"], flag_capture_attempt=submission["obj"])
            submission["capture"] = vm["uuid"]
            event(s, "captured", vm, team=team.id, attempt=submission["obj"].id)
            rec["captured"][vm["uuid"]] = s
        captured = [vm for vm in vms if vm["uuid"] in rec["captured"]]
        hidden = [vm for vm in vms if vm["uuid"] not in rec["captured"]]
        for _ in range(rng.randint(3, 8)):
            kind = rng.choice(["duplicate", "not_counted", "invalid", "pending"])
            if kind == "duplicate" and captured:
                self.attempt(team, rng.choice(captured)["hash"], at(slots.pop()), source=source())
            elif kind == "not_counted":
                self.attempt(team, rng.choice(hidden)["hash"], at(slots.pop()), source=source())
            elif kind == "pending":
                flag = rng.choice([vm["hash"] for vm in vms] + [self.hash()])
                self.attempt(team, flag, at(slots.pop()), processed=False, source=source())
            else:
                self.attempt(team, self.hash(), at(slots.pop()), source=source())
        for _ in range(rng.randint(2, 6)):
            self.log(team, at(slots.pop()), stored_round=rnd if rng.random() < 0.8 else None, source=source())

        # Noise around the run: outside its window, devbox records inside it.
        self.attempt(team, rng.choice(vms)["hash"], at(-3600))
        self.attempt(team, rng.choice(vms)["hash"], at(600 + 3600))
        self.log(team, at(600 + 3600))
        self.attempt(team, self.hash(), at(rng.randrange(20, until)) + timedelta(seconds=0.25), devbox=True)
        self.log(team, at(rng.randrange(20, until)) + timedelta(seconds=0.75), devbox=True, devbox_log=True)

        # Its traffic: between its VMs (hidden ones too), to the private
        # internet, to infrastructure and unknown addresses; outside the
        # window; and of addresses that are not the run's.
        def any_address(vm):
            return rng.choice(vm["addresses"])

        for _ in range(rng.randint(5, 15)):
            a, b = rng.sample(vms, 2)
            self.flow(at(rng.randrange(-55, until) + 0.25), any_address(a), any_address(b), rng.choice([22, 80, 445, 3306]))
        for _ in range(rng.randint(2, 6)):
            pi = rng.choice(["10.2.0.11", "10.2.0.20", "10.2.0.21"])
            self.flow(at(rng.randrange(0, until) + 0.75), any_address(rng.choice(vms)), pi, 53 if pi == "10.2.0.11" else 80,
                      transport="udp" if pi == "10.2.0.11" else "tcp", service="dns" if pi == "10.2.0.11" else "http")  # fmt: skip
        for address in INFRA + UNKNOWN:
            self.flow(at(rng.randrange(0, until) + 0.25), any_address(rng.choice(vms)), address, rng.choice([22, 443, 5432, 8006]))
        self.flow(at(600 + 3600), any_address(vms[0]), any_address(vms[1]), 22)
        self.flow(at(-3600), any_address(vms[0]), SCORING, 443)
        self.flow(at(30.25), "10.99.0.1", "10.99.0.2", 22)
        return rec

    # --- a devbox -------------------------------------------------------------------
    def devbox(self, team, index, others, start):
        """The team's devbox (A, B, C) and what it submitted and logged, in and
        before the Dev round that starts at `start`; `others`: other teams'
        tokens it also submits."""
        from core.models.devbox import Devbox, DevboxCapture, DevboxVM

        rng = self.rng
        box = Devbox.objects.create(team=team, index=index, services_source_ip=f"10.2.0.{32 + index}", gateway_vm_id=5000 + index)
        vms = []
        for n, role in enumerate("abc"):
            obj = DevboxVM.objects.create(
                devbox=box, vm_identifier=f"devbox-{team.name}-{role}", role=role, vm_maintenance_ip=f"10.6.{index}.{11 + n}",
                ssh_fabric_ip=f"10.7.{index}.{11 + n}", proxmox_vm_id=5100 + 10 * index + n, ctf_flag_hash=self.hash(),
            )  # fmt: skip
            vms.append({"obj": obj, "uuid": str(obj.uuid), "label": obj.vm_identifier, "role": role, "hash": obj.ctf_flag_hash})
        self.devbox_vms[team.id] = vms
        hours = rng.sample(range(1, 60), 12)  # distinct times in the round
        when = lambda: self.now - timedelta(hours=hours.pop(), minutes=rng.randint(0, 59))  # noqa: E731
        before = lambda: start - timedelta(hours=rng.randint(1, 48))  # noqa: E731
        for vm in vms:
            scenario = rng.choice(["never", "before", "before_and_again", "found", "found_twice", "pending"])
            if scenario in ("before", "before_and_again"):
                first = self.attempt(team, vm["hash"], before(), devbox=True)
                DevboxCapture.objects.create(team=team, devbox_vm=vm["obj"], devbox_version=1, flag_capture_attempt=first["obj"])
                first["dev_capture"] = vm["uuid"]
                if scenario == "before_and_again":
                    self.attempt(team, vm["hash"], when(), devbox=True)
            elif scenario in ("found", "found_twice"):
                first = self.attempt(team, vm["hash"], when(), devbox=True)
                DevboxCapture.objects.create(team=team, devbox_vm=vm["obj"], devbox_version=1, flag_capture_attempt=first["obj"])
                first["dev_capture"] = vm["uuid"]
                if scenario == "found_twice":
                    self.attempt(team, vm["hash"], when(), devbox=True)
            elif scenario == "pending":
                self.attempt(team, vm["hash"], when(), devbox=True, processed=False)
        for token in others:
            self.attempt(team, token, when(), devbox=True)  # another team's token: never this team's VM
        self.attempt(team, self.hash(), when(), devbox=True)
        for _ in range(rng.randint(1, 3)):
            self.log(team, when(), devbox=True, devbox_log=rng.random() < 0.7)
        self.log(team, before(), devbox=True, devbox_log=True)
        return vms


@pytest.fixture(params=[1, 2, 3], ids=lambda seed: f"seed{seed}")
def world(request, site):  # noqa: F811
    """Three teams: alpha and bravo in the competition (and its Dev
    competition), charlie in another one; tokens for each."""
    import hashlib

    import infra_fixture
    from core.models.competition import ApplicationSetting, Competition, Round
    from core.models.team import AccessTokens, Team, TeamMembership
    from core.models.topology import TopologySnapshot
    from core.models.user import User
    from dashboard.views import topology as topology_views
    from django.utils import timezone

    rng = random.Random(request.param)
    now = timezone.now().replace(microsecond=0) - timedelta(seconds=5)
    hours = lambda h: timedelta(hours=h)  # noqa: E731
    comp = ApplicationSetting.get_current_competition()
    dev = Competition.objects.create(name="dev", is_dev_competition=True)
    other = Competition.objects.create(name="elsewhere")
    staff = User.objects.get(username="admin")
    teams, tokens = {}, {}
    for name, competition, dev_competition in [("alpha", comp, dev), ("bravo", comp, dev), ("charlie", other, None)]:
        team = Team.objects.create(name=name, competition=competition, development_competition=dev_competition)
        user = User.objects.create(username=f"{name}-member")
        TeamMembership.objects.create(user=user, team=team)
        tokens[name] = f"tok-{name}-" + "x" * 30
        AccessTokens.objects.create(name=name, access_token_hash=hashlib.sha256(tokens[name].encode()).hexdigest(), team=team, created_by=staff)
        teams[name] = team

    rounds = {
        "R1": Round.objects.create(competition=comp, name="R1", seed=1, round_start=now - hours(8), round_end=now - hours(2)),
        "R2": Round.objects.create(competition=comp, name="R2", seed=2, round_start=now - hours(1), round_end=now + hours(3)),
        "RD": Round.objects.create(competition=dev, name="Dev week", seed=3, round_start=now - hours(72), round_end=now + hours(96)),
        "RX": Round.objects.create(competition=other, name="X1", seed=4, round_start=now - hours(8), round_end=now - hours(2)),
    }
    ledger = Ledger(rng, now)
    alpha, bravo = teams["alpha"], teams["bravo"]
    # Same round, same addresses, two hours apart: told apart by time alone.
    ledger.run(alpha, rounds["R1"], now - hours(7.8), position=0)
    ledger.run(bravo, rounds["R1"], now - hours(5.8), position=1)
    # A run in progress: started four minutes ago.
    ledger.run(alpha, rounds["R2"], now - timedelta(minutes=4), position=0, state="running", until=235)
    alpha_box = ledger.devbox(alpha, 1, [], rounds["RD"].round_start)
    ledger.devbox(bravo, 2, [vm["hash"] for vm in alpha_box], rounds["RD"].round_start)

    TopologySnapshot.objects.create(source="proxmox", data=infra_fixture.snapshot(), collected_at=timezone.now())
    topology_views._cache.clear()
    return {"ledger": ledger, "teams": teams, "tokens": tokens, "rounds": rounds, "client": site, "staff": staff}


# --- the oracle: what a team must get, from the ledger alone -------------------------


def iso(ts):
    return ts.isoformat() if ts else None


def seconds(ts, zero):
    return round((ts - zero).total_seconds(), 2)


def run_of(world, team, rnd):
    return next((r for r in world["ledger"].runs if r["team"] == team.id and r["round"] == rnd.id), None)


def window(run):
    first = min(s for s, *_ in run["events"])
    return run["zero"] + timedelta(seconds=min(first, 0)), run["zero"] + timedelta(seconds=600) + DRAIN


def in_window(ts, bounds):
    return bounds[0] <= ts <= bounds[1]


def live_submissions(world, team, rnd):
    """(attempt, result, the run VM whose flag it is) for the team's
    submissions in its run's window, oldest first."""
    run = run_of(world, team, rnd)
    if run is None:
        return []
    bounds = window(run)
    by_hash = {vm["hash"]: vm for vm in run["vms"]}
    out = []
    for a in sorted(world["ledger"].attempts, key=lambda a: (a["ts"], a["obj"].id)):
        if a["team"] != team.id or a["devbox"] or not in_window(a["ts"], bounds):
            continue
        vm = by_hash.get(a["hash"])
        if a["capture"]:
            result, vm = "captured", next(v for v in run["vms"] if v["uuid"] == a["capture"])
        elif not a["processed"]:
            result = "pending"
        elif vm is not None:
            result = "duplicate" if vm["uuid"] in run["captured"] else "not_counted"
        else:
            result = "invalid"
        out.append((a, result, vm))
    return out


def live_logs(world, team, rnd):
    run = run_of(world, team, rnd)
    if run is None:
        return []
    bounds = window(run)
    return sorted((g for g in world["ledger"].logs if g["team"] == team.id and not g["devbox"] and in_window(g["ts"], bounds)),
                  key=lambda g: (g["ts"], g["obj"].id))  # fmt: skip


def expected_rounds(world, team):
    comps = {team.competition_id, team.development_competition_id} - {None}
    rounds = sorted((r for r in world["rounds"].values() if r.competition_id in comps), key=lambda r: (r.round_start, r.id))
    out = []
    for rnd in rounds:
        run = run_of(world, team, rnd)
        info = None
        if run is not None:
            info = {"state": run["state"], "position": run["position"], "worm_start_at": iso(run["zero"]),
                    "ends_at": iso(run["zero"] + timedelta(seconds=600))}  # fmt: skip
        out.append({
            "id": rnd.id, "name": rnd.name,
            "competition": "development" if rnd.competition_id == team.development_competition_id else "main",
            "start": iso(rnd.round_start), "end": iso(rnd.round_end), "run": info,
            "scoring": f"/api/v1/rounds/{rnd.id}/scoring/", "logging": f"/api/v1/rounds/{rnd.id}/logging/",
            "progression": f"/api/v1/rounds/{rnd.id}/progression/", "download": f"/api/v1/rounds/{rnd.id}/progression.zip",
        })  # fmt: skip
    return {"team": {"uuid": str(team.uuid), "name": team.name}, "rounds": out}


def expected_scoring(world, team, rnd):
    run = run_of(world, team, rnd)
    captured = run["captured"] if run else {}
    items = [
        {"id": a["obj"].id, "submitted_at": iso(a["ts"]), "flag_hash": a["hash"], "result": result,
         "vm": vm["label"] if vm is not None and vm["uuid"] in captured else None}
        for a, result, vm in live_submissions(world, team, rnd)
    ]  # fmt: skip
    return items, {"captured": len(captured), "vms": len(run["vms"]) if run else 0}


def expected_logging(world, team, rnd):
    rows = sorted((g for g in world["ledger"].logs if g["team"] == team.id and g["round"] == rnd.id), key=lambda g: g["logging_id"])
    return [{"id": g["logging_id"], "uuid": g["uuid"], "received_at": iso(g["ts"]), "log": g["data"]} for g in rows]


def db_size(ref):
    """The record's size as the database renders it (the progression's `size`)."""
    from django.db import connection

    with connection.cursor() as cur:
        cur.execute("SELECT length(log_data::text) FROM logging_attempts WHERE uuid = %s", [ref])
        return cur.fetchone()[0]


def expected_live(world, team, rnd):
    """The team's view of its run in `rnd`: events, nodes, flows, ..."""
    run = run_of(world, team, rnd)
    if run is None:
        return {"events": [], "nodes": [], "cidrs": set(), "edges": set(), "vm_status": {}, "captures": [], "flows": [], "run": None,
                "logs": [], "span": (0.0, 0.0)}  # fmt: skip
    zero, V = run["zero"], run["captured"]
    vm_by_uuid = {vm["uuid"]: vm for vm in run["vms"]}
    label = lambda u: vm_by_uuid[u]["label"] if u in V else None  # noqa: E731
    see = lambda u: u if u in V else None  # noqa: E731
    start = see(run["vms"][0]["uuid"])
    events = []
    for s, kind, vm, data in run["events"]:
        if kind == "state":
            events.append({"t": float(s), "kind": kind, "node": None, "vm": None, "data": {"state": data["state"]}})
        elif kind in ("worm_deployed", "worm_started"):
            events.append({"t": float(s), "kind": kind, "node": start, "vm": start, "data": {}})
        elif kind in ("vm_stopped", "vm_started", "secret_released", "captured") and vm in V:
            events.append({"t": float(s), "kind": kind, "node": vm, "vm": vm, "data": {}})
    for a, result, vm in live_submissions(world, team, rnd):
        source, target = see(a["source"]), see(vm["uuid"]) if vm else None
        events.append({"t": seconds(a["ts"], zero), "kind": "flag_submitted", "node": "role:scoring", "vm": source, "data": {
            "source_vm": source, "source_label": label(source), "source_inferred": source is not None, "result": result,
            "flag_of": target, "flag_of_label": label(target), "hash_prefix": a["hash"][:16], "processed": a["processed"]}})  # fmt: skip
    logs = live_logs(world, team, rnd)
    for g in logs:
        source = see(g["source"])
        events.append({"t": seconds(g["ts"], zero), "kind": "log_received", "node": "role:logging", "vm": source, "data": {
            "source_vm": source, "source_label": label(source), "source_inferred": source is not None,
            "stored": g["round"] == rnd.id, "processed": True, "log": {"ref": g["uuid"], "size": db_size(g["uuid"])}}})  # fmt: skip

    def end(address):
        owner = next((vm for vm in run["vms"] if address in vm["addresses"]), None)
        if owner is not None:
            return (owner["uuid"], "sandbox", owner["label"]) if owner["uuid"] in V else None
        return SERVICES.get(address)

    bounds = window(run)
    run_addresses = {a for vm in run["vms"] for a in vm["addresses"]}
    flows = []
    for f in world["ledger"].flows:
        if not in_window(f["ts"], bounds) or not ({f["src"], f["dst"]} & run_addresses):
            continue
        src, dst = end(f["src"]), end(f["dst"])
        if src is None or dst is None or "sandbox" not in (src[1], dst[1]):
            continue
        kind = "lan" if src[1] == dst[1] == "sandbox" else (dst[1] if src[1] == "sandbox" else src[1])
        flows.append({
            "t": seconds(f["ts"], zero), "d": round(f["d"], 3), "src": src[0], "dst": dst[0], "src_role": src[1], "dst_role": dst[1],
            "src_name": src[2], "dst_name": dst[2], "kind": kind, "src_port": f["src_port"], "dst_port": f["dst_port"],
            "transport": f["transport"], "service": f["service"], "conn_state": f["state"], "bytes": f["sb"] + f["db"],
            "pkts": f["sp"] + f["dp"], "uid": f["uid"],
        })  # fmt: skip

    status = {}
    for vm in V:
        points = [[-50.0, "running"]]
        for s, kind, who, data in run["events"]:
            if kind in ("vm_stopped", "vm_started") and who == vm:
                points.append([float(s), "stopped" if kind == "vm_stopped" else "running"])
            elif kind == "state" and data["state"] in ("ending", "finished"):
                points.append([float(s), "stopped" if data["state"] == "ending" else "destroyed"])
        status[vm] = sorted(points, key=lambda p: p[0])

    times = [e["t"] for e in events] + [f["t"] for f in flows] + [f["t"] + f["d"] for f in flows]
    return {
        "events": events,
        "nodes": [{"id": u, "label": vm_by_uuid[u]["label"], "group": "sandbox", "addresses": vm_by_uuid[u]["addresses"],
                   "start_vm": vm_by_uuid[u]["start"], "captured": True, "captured_at": float(V[u])} for u in V],  # fmt: skip
        "cidrs": {c for u in V for c in vm_by_uuid[u]["cidrs"]},
        "edges": {(u, c) for u in V for c in vm_by_uuid[u]["cidrs"]},
        "vm_status": status,
        "captures": sorted([[float(t), u] for u, t in V.items()]),
        "flows": flows,
        "run": {"uuid": None, "team": team.name, "round": rnd.name, "state": run["state"], "position": run["position"],
                "worm_start_at": iso(zero), "ends_at": iso(zero + timedelta(seconds=600)),
                "start_vm": vm_by_uuid[start]["label"] if start else None, "duration": 600.0},  # fmt: skip
        "logs": logs,
        "span": (min([0.0] + times), max([0.0, 600.0] + times)),
        "hidden": [vm for vm in run["vms"] if vm["uuid"] not in V],
    }


def expected_dev(world, team, rnd):
    """The team's devbox in the Dev round's window."""
    ledger = world["ledger"]
    zero = rnd.round_start
    bounds = (rnd.round_start, ledger.now + timedelta(minutes=10))  # everything written is before the request
    vms = ledger.devbox_vms.get(team.id, [])
    by_hash = {vm["hash"]: vm for vm in vms}
    found, submissions = {}, []
    for a in sorted(ledger.attempts, key=lambda a: (a["ts"], a["obj"].id)):
        if a["team"] != team.id or not a["devbox"] or not in_window(a["ts"], bounds):
            continue
        vm = by_hash.get(a["hash"])
        if a["dev_capture"]:
            result = "captured"
        elif not a["processed"]:
            result = "pending"
        elif vm is not None:
            result = "duplicate"
        else:
            result = "invalid"
        if result in ("captured", "duplicate"):
            found.setdefault(vm["uuid"], seconds(a["ts"], zero))
        submissions.append((a, result, vm))
    label = {vm["uuid"]: vm["label"] for vm in vms if vm["uuid"] in found}
    unknown = {"source_vm": None, "source_label": None, "source_inferred": False}
    events = [{"t": t, "kind": "captured", "node": u, "vm": u, "data": {}} for u, t in found.items()]
    for a, result, vm in submissions:
        target = vm["uuid"] if vm is not None and vm["uuid"] in found else None
        events.append({"t": seconds(a["ts"], zero), "kind": "flag_submitted", "node": "role:scoring", "vm": None, "data": {
            "result": result, "flag_of": target, "flag_of_label": label.get(target), "hash_prefix": a["hash"][:16],
            "processed": a["processed"], **unknown}})  # fmt: skip
    logs = sorted((g for g in ledger.logs if g["team"] == team.id and g["devbox"] and in_window(g["ts"], bounds)),
                  key=lambda g: (g["ts"], g["obj"].id))  # fmt: skip
    for g in logs:
        events.append({"t": seconds(g["ts"], zero), "kind": "log_received", "node": "role:logging", "vm": None, "data": {
            "stored": g["devbox_log"], "processed": True, "log": {"ref": g["uuid"], "size": db_size(g["uuid"])}, **unknown}})
    nodes = [{"id": vm["uuid"], "label": vm["label"], "group": "devbox", "devbox_role": vm["role"], "addresses": [], "networks": [],
              "start_vm": False, "captured": True, "captured_at": found[vm["uuid"]]} for vm in vms if vm["uuid"] in found]  # fmt: skip
    return {"events": events, "nodes": nodes, "logs": logs, "hidden": [vm for vm in vms if vm["uuid"] not in found],
            "others": [vm for tid, box in ledger.devbox_vms.items() if tid != team.id for vm in box]}  # fmt: skip


# --- reading through the API -------------------------------------------------------------


def get(world, path, team):
    client = world["client"]
    response = client.get(path, HTTP_AUTHORIZATION=f"Bearer {world['tokens'][team]}", **proxied(HOST))
    body = json.loads(response.content) if response["Content-Type"].startswith("application/json") else None
    return response, body


def walk(world, path, team, limit):
    items, url, pages = [], f"{path}?limit={limit}", 0
    while url:
        response, body = get(world, url, team)
        assert response.status_code == 200, body
        assert set(body) >= {"team", "round", "run", "items", "page"}
        items += body["items"]
        pages += 1
        url = body["page"]["next"]
    return items, pages, body


def events_key(events):
    return sorted(json.dumps(e, sort_keys=True) for e in events)


def strip_ids(events):
    return [{k: v for k, v in e.items() if k != "id"} for e in events]


def assert_nothing_of_others(world, text, team, hidden=()):
    """No other team, no VM the team did not capture, no internals."""
    for name, other in world["teams"].items():
        if name != team:
            assert name not in text and str(other.uuid) not in text, name
    for vm in hidden:
        assert vm["uuid"] not in text and vm["label"] not in text, vm["label"]
        for address in vm.get("addresses", []):
            assert f'"{address}"' not in text, address
    for word in NEVER:
        assert word not in text, word


LIVE_ROUNDS = ("R1", "R2")
TEAMS = ("alpha", "bravo")


# --- the tests ------------------------------------------------------------------------------


def test_rounds_listing_is_exactly_the_teams_rounds(world):
    for name, team in world["teams"].items():
        response, body = get(world, "/api/v1/rounds/", name)
        assert response.status_code == 200
        assert body == expected_rounds(world, team), name


def test_scoring_is_exactly_what_was_submitted(world):
    for name in TEAMS:
        team = world["teams"][name]
        for key in LIVE_ROUNDS + ("RD",):
            rnd = world["rounds"][key]
            items, summary = expected_scoring(world, team, rnd)
            path = f"/api/v1/rounds/{rnd.id}/scoring/"
            one, _, body = walk(world, path, name, 500)
            paged, pages, _ = walk(world, path, name, 3)
            assert one == items, (name, key)
            assert paged == items and pages == max(1, -(-len(items) // 3)), (name, key)
            assert all(set(i) == {"id", "submitted_at", "flag_hash", "result", "vm"} for i in items)
            assert body["summary"] == summary
            hidden = expected_live(world, team, rnd).get("hidden", [])
            assert_nothing_of_others(world, json.dumps(one, ensure_ascii=False), name, [{"uuid": v["uuid"], "label": v["label"]} for v in hidden])


def test_logging_is_exactly_what_was_logged(world):
    for name in TEAMS:
        team = world["teams"][name]
        for key in LIVE_ROUNDS + ("RD",):
            rnd = world["rounds"][key]
            items = expected_logging(world, team, rnd)
            path = f"/api/v1/rounds/{rnd.id}/logging/"
            one, _, _ = walk(world, path, name, 200)
            paged, pages, _ = walk(world, path, name, 2)
            assert one == items and paged == items, (name, key)  # byte for byte, unicode and all
            assert pages == max(1, -(-len(items) // 2))
            assert_nothing_of_others(world, json.dumps(one, ensure_ascii=False), name)


def test_progression_is_exactly_what_happened_to_the_team(world):
    for name in TEAMS:
        team = world["teams"][name]
        for key in LIVE_ROUNDS:
            rnd = world["rounds"][key]
            want = expected_live(world, team, rnd)
            response, doc = get(world, f"/api/v1/rounds/{rnd.id}/progression/", name)
            assert response.status_code == 200
            tl = doc["timeline"]
            assert set(doc) == {"format", "version", "generated_at", "audience", "environment", "round", "team", "clock", "scene",
                                "timeline", "logs", "network"}  # fmt: skip
            assert doc["team"] == {"uuid": str(team.uuid), "name": name} and doc["audience"] == "team" and doc["environment"] == "live"
            assert set(tl) == {"run", "nodes", "networks", "edges", "vm_status", "events", "captures", "truncated"}

            # Every event, and nothing else.
            assert events_key(strip_ids(tl["events"])) == events_key(want["events"]), (name, key)
            assert [e["id"] for e in tl["events"]] == list(range(len(tl["events"])))
            assert [e["t"] for e in tl["events"]] == sorted(e["t"] for e in tl["events"])

            # The VMs it captured, their networks (by CIDR), power state, captures.
            cidr = {n["id"]: n["cidr"] for n in tl["networks"]}
            nodes = [{**{k: v for k, v in n.items() if k != "networks"}} for n in tl["nodes"]]
            assert sorted(nodes, key=lambda n: n["id"]) == sorted(want["nodes"], key=lambda n: n["id"])
            assert all(set(n) == {"id", "label", "group", "addresses", "networks", "start_vm", "captured", "captured_at"} for n in tl["nodes"])
            assert {(n["id"], cidr[x["name"]]) for n in tl["nodes"] for x in n["networks"]} == want["edges"]
            assert set(cidr.values()) == want["cidrs"] and len(cidr) == len(want["cidrs"])
            assert {(e["source"], cidr[e["target"]]) for e in tl["edges"]} == want["edges"]
            assert tl["vm_status"] == want["vm_status"]
            assert sorted([c["t"], c["vm"]] for c in tl["captures"]) == want["captures"]
            if want["run"] is None:
                assert tl["run"] is None
            else:
                assert {k: v for k, v in tl["run"].items() if k not in ("t_min", "t_max")} == want["run"]

            # Its connections, exactly.
            _, flows = get(world, doc["network"]["url"], name)
            assert sorted(flows["flows"], key=lambda f: f["uid"]) == sorted(want["flows"], key=lambda f: f["uid"]), (name, key)
            assert flows["count"] == doc["network"]["count"] == len(want["flows"])

            # The counts and the clock.
            assert doc["logs"]["count"] == len(want["logs"])
            clock = doc["clock"]
            if key == "R1":
                assert clock["finished"] is True and clock["now"] is None
                assert (clock["t_min"], clock["t_max"]) == want["span"]
                zero = run_of(world, team, rnd)["zero"]
                assert clock["t0"] == iso(zero)
                assert clock["round_start"] == seconds(rnd.round_start, zero) and clock["round_end"] == seconds(rnd.round_end, zero)
            else:
                assert clock["finished"] is False and clock["now"] is not None
            text = json.dumps(doc, ensure_ascii=False) + json.dumps(flows, ensure_ascii=False)
            assert_nothing_of_others(world, text, name, want.get("hidden", []))


def test_log_records_are_exactly_the_teams_and_nothing_else(world):
    ledger = world["ledger"]
    for name in TEAMS:
        team = world["teams"][name]
        for key in LIVE_ROUNDS + ("RD",):
            rnd = world["rounds"][key]
            want = expected_dev(world, team, rnd) if key == "RD" else expected_live(world, team, rnd)
            mine = {g["uuid"] for g in want["logs"]}
            for g in ledger.logs:
                response, body = get(world, f"/api/v1/rounds/{rnd.id}/progression/logs/{g['uuid']}/", name)
                if g["uuid"] in mine:
                    assert response.status_code == 200
                    assert body == {"ref": g["uuid"], "received_at": iso(g["ts"]), "data": g["data"]}
                else:  # another team's, another round's, outside the window, devbox vs run
                    assert response.status_code == 404 and body["error"]["code"] == "not_found", (name, key, g["team"], g["devbox"])


def test_dev_progression_is_exactly_the_teams_devbox(world):
    rnd = world["rounds"]["RD"]
    for name in TEAMS:
        team = world["teams"][name]
        want = expected_dev(world, team, rnd)
        response, doc = get(world, f"/api/v1/rounds/{rnd.id}/progression/", name)
        assert response.status_code == 200 and doc["environment"] == "dev"
        tl = doc["timeline"]
        assert events_key(strip_ids(tl["events"])) == events_key(want["events"]), name
        assert tl["nodes"] == want["nodes"] and tl["run"] is None
        assert doc["network"]["available"] is False and doc["network"]["url"] is None and doc["network"]["count"] == 0
        assert doc["logs"]["count"] == len(want["logs"])
        group = doc["scene"]["groups"]
        assert len(group) == 1 and group[0]["members"] == [n["id"] for n in want["nodes"]]
        scene_vms = [n for n in doc["scene"]["nodes"] if n["role"] == "devbox"]
        assert [n["id"] for n in scene_vms] == [n["id"] for n in want["nodes"]]
        _, flows = get(world, f"/api/v1/rounds/{rnd.id}/progression/flows/", name)
        assert flows["flows"] == [] and flows["available"] is False
        hidden = want["hidden"] + want["others"]
        assert_nothing_of_others(world, json.dumps(doc, ensure_ascii=False), name, hidden)


def test_zip_holds_exactly_the_progression(world):
    for name in TEAMS:
        team = world["teams"][name]
        for key in ("R1", "RD"):
            rnd = world["rounds"][key]
            want = expected_dev(world, team, rnd) if key == "RD" else expected_live(world, team, rnd)
            _, doc = get(world, f"/api/v1/rounds/{rnd.id}/progression/", name)
            response, _ = get(world, f"/api/v1/rounds/{rnd.id}/progression.zip", name)
            assert response.status_code == 200 and response["Content-Type"] == "application/zip"
            archive = zipfile.ZipFile(io.BytesIO(response.content))
            members = {f"logs/{g['uuid']}.json" for g in want["logs"]} | {"progression.json", "README.txt"}
            if key == "R1":
                members.add("network/flows.jsonl")
            assert set(archive.namelist()) == members and len(archive.namelist()) == len(members)
            for g in want["logs"]:
                assert json.loads(archive.read(f"logs/{g['uuid']}.json")) == {"ref": g["uuid"], "received_at": iso(g["ts"]), "data": g["data"]}
            if key == "R1":
                lines = archive.read("network/flows.jsonl").decode().splitlines()
                assert sorted((json.loads(x) for x in lines), key=lambda f: f["uid"]) == sorted(want["flows"], key=lambda f: f["uid"])
            inner = json.loads(archive.read("progression.json"))
            # (A running round's clock moves between two requests.)
            same = lambda d: {k: v for k, v in d.items() if k not in ("generated_at", "logs", "network", "clock")}  # noqa: E731
            assert same(inner) == same(doc)
            assert inner["logs"] == {**doc["logs"], "included": True, "path": "logs/{ref}.json", "url": None}
            if key == "R1":
                assert inner["clock"] == doc["clock"]
                assert inner["network"] == {**doc["network"], "included": True, "path": "network/flows.jsonl", "url": None}
            else:
                assert inner["network"] == {**doc["network"], "included": False, "path": None, "url": None}
            text = "\n".join(archive.read(m).decode() for m in archive.namelist())
            assert_nothing_of_others(world, text, name, want.get("hidden", []))

            lean = zipfile.ZipFile(io.BytesIO(get(world, f"/api/v1/rounds/{rnd.id}/progression.zip?logs=0&network=0", name)[0].content))
            assert sorted(lean.namelist()) == ["README.txt", "progression.json"]


def test_other_competitions_rounds_are_not_there(world):
    rx, r1 = world["rounds"]["RX"], world["rounds"]["R1"]
    paths = ["scoring/", "logging/", "progression/", "progression/flows/", "progression.zip"]
    for name in TEAMS:
        for path in paths:
            assert get(world, f"/api/v1/rounds/{rx.id}/{path}", name)[0].status_code == 404
    for path in paths:
        assert get(world, f"/api/v1/rounds/{r1.id}/{path}", "charlie")[0].status_code == 404


def test_the_page_serves_the_same_document(world):
    """The PROGRESSION page's endpoint (signed in) and the API: one document."""
    from core.models.user import User

    client = world["client"]
    for name in TEAMS:
        client.force_login(User.objects.get(username=f"{name}-member"))
        rnd = world["rounds"]["R1"]
        page = json.loads(client.get(f"/progression/rounds/{rnd.id}/", **proxied(HOST)).content)
        _, api = get(world, f"/api/v1/rounds/{rnd.id}/progression/", name)
        same = lambda d: {k: v for k, v in d.items() if k not in ("generated_at", "logs", "network")}  # noqa: E731
        assert same(page) == same(api)
        assert {**page["logs"], "url": None} == {**api["logs"], "url": None}
        assert {**page["network"], "url": None} == {**api["network"], "url": None}
