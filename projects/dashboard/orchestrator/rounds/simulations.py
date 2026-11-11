"""Simulations: a gameplan's attempt played against a pretend clock (src/gameplan/simulation.py),
for staff's Simulations page on the dashboard (docs/05-live-environment.md).

    document(filename, duration)  the round player's document (progression.py's format, environment
                                  "simulation"), made on the first request for a version of the file,
                                  a duration per team and SIMULATION_VERSION, then kept
                                  (gameplan_simulations)

The gameplans are here and on the node only, never on the dashboard VM (docs/07-security.md), so
the attempt is simulated here, in a child process limited in time, CPU and memory (a gameplan is
code: a loop in it must not hold the web worker), and the dashboard gets the document through the
orchestrator API (orchestrator/api.py).

The document holds what a run's timeline holds: the sandbox's VMs, networks, addresses and
firewall rules, and what happened when -- the run's states, power, flags, the worm's deployment,
the waits, and the files written and read, by their number. What a real run keeps off the
database stays off it here too: no path, file content, setup command or package, no log line of
the gameplan and no problem text. Those go to the orchestrator's journal (`journalctl -u
orchestrator`), with the files' paths by number.
"""

from __future__ import annotations

import hashlib
import json
import logging
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

from core.models.rounds import GameplanSimulation
from django.db import IntegrityError, connection, transaction
from django.utils import timezone

from orchestrator.rounds import gameplans, progression

log = logging.getLogger("orchestrator")

# Bump when the simulated attempt or its document changes: the documents kept are made again.
SIMULATION_VERSION = 1
TIMEOUT = 20  # seconds for the child (gunicorn ends a worker at 30)
MAX_OUTPUT = 32 * 1024 * 1024
KEEP = 20  # documents kept per gameplan file (its durations)
DURATION = (60, 7 * 86400)
NO_TRAFFIC = "A simulation has no worm and no traffic."

CHILD = "import sys; sys.path.insert(0, sys.argv[1]); from gameplan.simulation import main; sys.exit(main(sys.argv[2:]))"

# What each event of the simulation keeps in the document (src/gameplan/simulation.py).
EVENT_DATA = {
    "state": ("state",),
    "secret_released": (),
    "flag_removed": (),
    "vm_started": ("how",),
    "vm_stopped": ("how",),
    "vm_suspended": ("how",),
    "vm_resumed": ("how",),
    "worm_deployed": (),
    "worm_started": (),
    "file_written": ("file", "at_reset"),
    "file_read": ("file", "watched", "exists"),
    "wait": ("seconds", "cut"),
    "log": ("line",),
    "execute_returned": (),
    "error": (),
}
POWER = {"vm_started": "running", "vm_stopped": "stopped", "vm_suspended": "paused", "vm_resumed": "running"}


class NotFound(LookupError):
    """No gameplan of that name here."""


class Busy(RuntimeError):
    """Another simulation is being made."""


LOCK = int.from_bytes(hashlib.sha256(b"worms:gameplan-simulation").digest()[:8], "big", signed=True)


@contextmanager
def _one_at_a_time():
    """At most one simulation is made at a time, whichever worker asks (each may keep a CPU busy
    for 10 s): whoever holds the API token cannot take more than that. Busy if one is under way."""
    with transaction.atomic():
        if connection.vendor == "postgresql":
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_try_advisory_xact_lock(%s)", [LOCK])
                if not cursor.fetchone()[0]:
                    raise Busy("another simulation is being made: try again in a few seconds")
        yield


def timing():
    """The simulation's nominal times, with the conductor's own (the fuse, the drain)."""
    from orchestrator.rounds.conductor import config

    gameplans.framework()
    from gameplan.simulation import TIMING

    cfg = config()
    return {**TIMING, "worm_start_delay": int(cfg["worm_start_delay"]), "end_drain": int(cfg["end_drain"])}


def _failed(problem):
    return {"outcome": "failed", "problems": [problem], "log": [], "spec": None, "events": [], "truncated": False,
            "worm_start": None, "duration": None}  # fmt: skip


def _simulate(filename, sha256, duration, times):
    """The child's record (src/gameplan/simulation.py, simulate), or a failed one if it did not
    finish."""
    gameplans.framework()
    import gameplan

    lib = str(Path(gameplan.__file__).resolve().parent.parent)
    command = [sys.executable, "-I", "-c", CHILD, lib, gameplans.directory(), filename, sha256, str(duration or ""), json.dumps(times)]
    # Its stderr (what the gameplan prints, a traceback) in a file: the child limits what it writes there.
    with tempfile.TemporaryFile() as errors:
        try:
            child = subprocess.run(command, stdout=subprocess.PIPE, stderr=errors, timeout=TIMEOUT, cwd=gameplans.directory(),
                                   env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"})  # fmt: skip
        except subprocess.TimeoutExpired:
            return _failed(f"the simulation did not finish within {TIMEOUT} s (does execute() loop without waiting?)")
        errors.seek(max(0, errors.seek(0, 2) - 2000))
        tail = errors.read().decode(errors="replace")
    if child.returncode != 0:
        log.warning("simulation of %s: the child stopped (exit %s): %s", filename, child.returncode, tail)
        return _failed(f"the simulation stopped (exit {child.returncode}; its limits: 10 s of CPU, 1 GiB of memory)")
    if len(child.stdout) > MAX_OUTPUT:
        return _failed("the simulation's record is too large")
    try:
        return json.loads(child.stdout)
    except ValueError:
        return _failed("the simulation's record is not JSON")


def _journal(filename, sha256, duration, record):
    """What the document leaves out, for staff on the orchestrator: problems, log lines, paths."""
    name = f"simulation of {filename} ({sha256[:12]}, {duration or 'its own duration'})"
    log.info("%s: %s", name, record["outcome"])
    for problem in record.get("problems") or []:
        log.warning("%s: %s", name, problem)
    for i, line in enumerate(record.get("log") or []):
        log.info("%s: log line %s: %s", name, i, line)
    spec = record.get("spec") or {}
    written = {(f["vm"], f["path"]) for f in spec.get("files") or []}
    for f in spec.get("files") or []:
        log.info("%s: file %s is %s:%s", name, f["id"], f["vm"], f["path"])
    watched = [e for e in spec.get("reads") or [] if (e["vm"], e["path"]) not in written]
    for i, entry in enumerate(watched):
        log.info("%s: watched path %s is %s:%s", name, i, entry["vm"], entry["path"])


def _firewall(vm):
    rules = []
    for rule in vm["firewall"]:
        peers = ", ".join(rule["peers"])
        rules.append({
            "direction": rule["direction"], "action": rule["action"], "proto": rule["proto"], "dport": rule["dport"] or "",
            "source": peers if rule["direction"] == "in" else "", "dest": peers if rule["direction"] == "out" else "",
            "comment": "the gameplan's rule",
        })  # fmt: skip
    return {
        "enabled": True, "policy_in": "DROP", "policy_out": "DROP", "ipfilter": True,
        "groups": [
            {"name": "sandbox", "iface": "", "comment": "scoring, logging, DNS and the private internet, through sandbox-gw"},
            {"name": "ssh", "iface": "management NIC", "comment": "SSH from the Proxmox host"},
        ],
        "rules": rules,
    }  # fmt: skip


def _scene(spec):
    """What the player draws around the sandbox: scoring, logging, the private internet and the
    gateway (the infrastructure model's shape, dashboard/infra_view.py)."""
    gateway = [str(net["cidr"]).rsplit(".", 1)[0] + ".1" for net in spec.get("networks", [])]
    node = progression._scene_node
    nodes = [
        node("role:scoring", "scoring", "scoring"),
        node("role:logging", "logging", "logging"),
        node("pi:dns", "DNS", "dns"),
        node("pi:caches", "caches", "private_internet"),
        node("sandbox-gw", "sandbox-gw", "sandbox_gw", addresses=gateway),
    ]
    group = {"id": "group:sandbox", "kind": "sandbox", "team": None, "label": "Sandbox", "gateway": "sandbox-gw", "members": []}
    edges = [
        {"id": f"service:group:sandbox->{target}", "source": "group:sandbox", "target": target, "kind": "service",
         "verified": True, "label": role}
        for role, target in progression.SERVICE_NODES.items()
    ]  # fmt: skip
    edges.append({"id": "service:group:sandbox->pi", "source": "group:sandbox", "target": "pi", "kind": "service",
                  "verified": True, "label": "private internet", "targets": ["DNS", "caches"]})  # fmt: skip
    return {"host": {}, "nodes": nodes, "groups": [group], "networks": [], "edges": edges}


def build(filename, sha256, requested, record, times, now) -> dict:
    """The round player's document of a simulation record (see the module)."""
    spec = record.get("spec") or {"vms": [], "networks": [], "files": [], "reads": [], "entrypoint": None}
    raw = record.get("events") or []
    running = next((e["t"] for e in raw if e["kind"] == "state" and e["data"].get("state") == "running"), 0.0)
    zero = record["worm_start"] if record.get("worm_start") is not None else running
    sec = lambda t: round(t - zero, 2)  # noqa: E731

    cidr = {net["name"]: net["cidr"] for net in spec["networks"]}
    nodes = [
        {
            "id": vm["name"], "label": vm["name"], "vmid": None,
            "addresses": [nic["address"].split("/")[0] for nic in vm["nics"]],
            "networks": [{"name": nic["network"], "cidr": cidr.get(nic["network"])} for nic in vm["nics"]],
            "start_vm": vm["name"] == spec["entrypoint"], "captured": False, "captured_at": None,
            "firewall": _firewall(vm),
        }
        for vm in spec["vms"]
    ]  # fmt: skip
    names = {n["id"] for n in nodes}

    events = []
    for e in raw:
        keep = EVENT_DATA.get(e["kind"])
        if keep is None:
            continue
        data = {key: e["data"][key] for key in keep if key in e["data"]}
        if e.get("d"):
            data["duration"] = e["d"]
        vm = e["vm"] if e["vm"] in names else None
        events.append({"t": sec(e["t"]), "kind": e["kind"], "node": vm, "vm": vm, "data": data})
    for i, e in enumerate(events):
        e["id"] = i

    # Each VM's power: running from the check on (prepare built it), as execute() switched it, all
    # stopped at the end, gone at the teardown (timeline._vm_status).
    checking = next((e["t"] for e in events if e["kind"] == "state" and e["data"]["state"] == "checking"), 0.0)
    status = {n["id"]: [[checking, "running"]] for n in nodes}
    for e in events:
        if e["kind"] in POWER and e["vm"] in status:
            status[e["vm"]].append([round(e["t"] + e["data"].get("duration", 0), 2), POWER[e["kind"]]])
        elif e["kind"] == "state" and e["data"]["state"] in ("finished", "failed"):
            for points in status.values():
                points.append([e["t"], "destroyed"])
    duration = record.get("duration")
    worm = record.get("worm_start") is not None
    times_seen = [e["t"] for e in events] + [e["t"] + e["data"].get("duration", 0) for e in events]
    t_min, t_max = min([0.0] + times_seen), max([duration if worm and duration else 0.0] + times_seen)
    failed_at = next((e["t"] for e in events if e["kind"] == "error"), None)
    timeline = {
        "run": {
            "uuid": None, "team": filename, "round": "simulation", "state": "failed" if failed_at is not None else "finished",
            "position": None, "worm_start_at": None, "ends_at": None, "start_vm": spec["entrypoint"],
            "duration": duration if worm else None, "t_min": t_min, "t_max": t_max,
        },
        "nodes": nodes,
        "networks": [{"id": net["name"], "label": net["name"], "cidr": net["cidr"]} for net in spec["networks"]],
        "edges": [{"source": n["id"], "target": net["name"], "network": net["name"]} for n in nodes for net in n["networks"]],
        "vm_status": status,
        "events": events,
        "captures": [],
        "truncated": {"flows": False, "flag_submissions": False, "logs": False},
    }  # fmt: skip
    return {
        "format": progression.FORMAT,
        "version": progression.VERSION,
        "generated_at": now.isoformat(),
        "audience": "staff",
        "environment": "simulation",
        "round": None,
        "team": None,
        "clock": {"t0": None, "round_start": None, "round_end": None, "now": None, "finished": True, "t_min": t_min, "t_max": t_max},
        "scene": _scene(spec),
        "timeline": timeline,
        "logs": {"count": 0, "truncated": False, "included": False, "path": None, "url": None},
        "network": {"available": False, "reason": NO_TRAFFIC, "count": 0, "truncated": False, "included": False, "path": None, "url": None},
        "simulation": {
            "gameplan": filename,
            "sha256": sha256,
            "version": SIMULATION_VERSION,
            "duration": duration,
            "own_duration": requested == 0,
            "outcome": record.get("outcome", "failed"),
            "problems": len(record.get("problems") or []),
            "log_lines": len(record.get("log") or []),
            "failed_at": failed_at,
            "events_truncated": bool(record.get("truncated")),
            "files": len(spec.get("files") or []),
            "watched": len(spec.get("reads") or []) - len({(f["vm"], f["path"]) for f in spec.get("files") or []}),
            "timing": times,
            "simulated_at": now.isoformat(),
        },
    }


def parse_duration(value):
    """A duration per team from a request: 0 (the gameplan's own) or whole seconds in DURATION."""
    if value in (None, ""):
        return 0
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        raise ValueError("duration is whole seconds") from None
    if seconds != 0 and not DURATION[0] <= seconds <= DURATION[1]:
        raise ValueError(f"duration is whole seconds in [{DURATION[0]}, {DURATION[1]}], or 0 for the gameplan's own")
    return seconds


def document(filename, duration=0):
    """(document, cached) of the gameplan file as the orchestrator holds it now, for `duration`
    seconds per team (0: its own round_duration). NotFound if there is no such file; Busy while
    another is being made; gameplans.Unavailable on a machine without gameplans."""
    loader, _ = gameplans.framework()
    from gameplan.errors import GameplanError

    if not loader.valid_name(filename):
        raise NotFound(filename)
    try:
        _source, sha256 = loader.read(gameplans.directory(), filename)
    except GameplanError:
        raise NotFound(filename) from None
    kept = GameplanSimulation.objects.filter(filename=filename, sha256=sha256, duration=duration, version=SIMULATION_VERSION)
    if (found := kept.first()) is not None:
        return found.document, True
    with _one_at_a_time():
        if (found := kept.first()) is not None:  # made meanwhile
            return found.document, True
        times = timing()
        record = _simulate(filename, sha256, duration, times)
        _journal(filename, sha256, duration, record)
        doc = build(filename, sha256, duration, record, times, timezone.now())
        try:
            with transaction.atomic():
                GameplanSimulation.objects.create(filename=filename, sha256=sha256, duration=duration,
                                                  version=SIMULATION_VERSION, document=doc)  # fmt: skip
        except IntegrityError:
            pass  # the same one, made meanwhile (SQLite: no lock)
    # Other versions of the file (or of the simulation) are never asked for again; a few durations are kept.
    GameplanSimulation.objects.filter(filename=filename).exclude(sha256=sha256, version=SIMULATION_VERSION).delete()
    old = list(GameplanSimulation.objects.filter(filename=filename).order_by("-created_at", "-id").values_list("id", flat=True)[KEEP:])
    if old:
        GameplanSimulation.objects.filter(id__in=old).delete()
    return doc, False
