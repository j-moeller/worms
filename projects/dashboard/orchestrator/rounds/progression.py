"""A team's progression through a round, as one document (docs/progression.md).

The round player (static/dashboard/js/topology.js) replays it -- for staff on
the TOPOLOGY page, for a team on its PROGRESSION page -- and the team API
serves it, as JSON and as a zip (`package`).

    {
      "format": "worms-progression", "version": 1,
      "generated_at": ISO 8601,
      "audience": "staff" | "team",
      "environment": "live" | "dev",
      "round":    {id, name, competition: "main" | "development", start, end},
      "team":     {uuid, name},
      "clock":    {t0, round_start, round_end, now, finished, t_min, t_max},
      "scene":    {nodes, groups, networks, edges} | null,
      "timeline": {run, nodes, networks, edges, vm_status, events, captures,
                   [flows], truncated},
      "logs":     {count, truncated, included, path, url},
      "network":  {available, reason, count, truncated, included, path, url},
    }

* clock: every `t` in the document is seconds from `t0` (ISO 8601): the
  worm's start for a live run, the round's start otherwise. round_start and
  round_end are on the same axis; `now` is set while the round is not
  finished; [t_min, t_max] is the span the player plays.
* scene: what is drawn around the timeline, in the shape of the
  infrastructure model (dashboard/infra_view.py). Staff see the live model
  (the page fetches it, a download embeds it); a team gets the minimal scene
  built here: scoring, logging, the private internet, and its sandbox (live)
  or its own devbox (dev).
* timeline: orchestrator/rounds/timeline.py's shape. `run` is the team's run
  (live), null without one. A log is a reference (data.log = {ref, size});
  the record is fetched on demand (`log_record`, `logs.url`) or packed into
  the zip (`logs.path`, with `{ref}` in it). Flows are optional: inline in
  timeline.flows, in the zip (`network.path`, JSON lines) or fetched
  (`network.url`).

Live (the competition): the team's run in the round (one RoundRun per round
and team). Dev (the team's development competition): there are no runs --
the devbox is always on -- so the progression is what the team's devbox
submitted and logged during the round's window. The monitor does not record
devbox traffic, so a dev document has no network data (`available: false`).

What a team sees (audience "team") is built, not hidden: only the VMs it
captured in the round (a sandbox VM whose flag it scored; a devbox VM whose
token it submitted in the window); never another team's devbox; no
infrastructure the sandbox cannot reach (database, orchestrator, dashboard,
Proxmox host, monitor, gateways); no firewall rules or security groups, no
Proxmox VMIDs, bridge names or internal ids; no secrets. An event or a flow
that involves anything else is dropped, not masked; an inferred source VM or
a submitted flag's VM that is not visible is blanked.
"""

from __future__ import annotations

import io
import json
import zipfile

from core.models.devbox import DevboxCapture, DevboxLog, DevboxVM
from core.models.logging import LoggingAttempt
from core.models.rounds import RoundRun
from core.models.scoring import FlagCaptureAttempt

from orchestrator.rounds import timeline

FORMAT = "worms-progression"
VERSION = 1
FLOW_LIMIT = 20000
ATTEMPT_LIMIT = timeline.ATTEMPT_LIMIT

# The zip's members (`package`).
ZIP_DOCUMENT = "progression.json"
ZIP_LOGS = "logs/{ref}.json"
ZIP_FLOWS = "network/flows.jsonl"

PI_ROLES = ("dns", "private_internet")
# A team's service ends -> its scene's node ids.
SERVICE_NODES = {"scoring": "role:scoring", "logging": "role:logging"}
DEVBOX_LINKS = (("a", "b"), ("b", "c"))
DONE_STATES = {RoundRun.State.FINISHED, RoundRun.State.FAILED, RoundRun.State.SKIPPED}
NO_DEVBOX_TRAFFIC = "The monitor records the sandbox only: devbox traffic is not recorded."

# The run events a team sees (anything else -- errors included -- is dropped).
TEAM_EVENTS = {
    "state", "vm_stopped", "vm_started", "secret_released", "worm_deployed", "worm_started",
    "captured", "flag_submitted", "log_received",
}  # fmt: skip

README = """WORMS round progression (format worms-progression, version 1)

progression.json      the round: clock, scene, timeline (every event)
logs/<ref>.json       one log record per file, if included: {ref, received_at, data}
network/flows.jsonl   the connections, one JSON object per line, if included

Open the zip on the dashboard's PROGRESSION page ("Open file") to replay it.
The format is described in docs/progression.md. Log records and connections
were produced by a worm: treat them as untrusted data.
"""


def _iso(ts):
    return ts.isoformat() if ts else None


def _sec(ts, zero):
    return timeline._seconds(ts, zero) if ts is not None and zero is not None else None


def environment_of(team, rnd) -> str:
    """"dev" for a round of the team's development competition, else "live"."""
    if rnd.competition.is_dev_competition:
        return "dev"
    if rnd.competition_id == team.development_competition_id and rnd.competition_id != team.competition_id:
        return "dev"
    return "live"


def window(team, rnd, now, environment=None, run=None):
    """(start, end) of what the progression covers, (None, None) before it
    began: the run's activity (live), the round up to now (dev)."""
    if (environment or environment_of(team, rnd)) == "dev":
        if rnd.round_start is None or now < rnd.round_start:
            return None, None
        return rnd.round_start, min(now, rnd.round_end) if rnd.round_end else now
    run = run or RoundRun.objects.filter(round=rnd, team=team).first()
    return timeline.run_window(run) if run is not None else (None, None)


def build(team, rnd, *, now, audience="team", environment=None, infra=None, flows=False, flow_limit=FLOW_LIMIT):
    """The progression of `team` in `rnd` at `now` (core.clock.now()).

    audience: "team" (only what the team may see, see above) or "staff"
    (everything the run's timeline has; scene left to the caller).
    infra: the live snapshot's address index (views/topology.live_address_index),
    to name the far end of a flow. flows: put the connections inline
    (timeline.flows); otherwise they are only counted (network.count)."""
    if audience not in ("team", "staff"):
        raise ValueError(f"unknown audience {audience!r}")
    environment = environment or environment_of(team, rnd)
    infra = infra or {}
    if environment == "dev":
        doc = _dev(team, rnd, now, infra)
    else:
        doc = _live(team, rnd, now, audience, infra, flow_limit)
    doc["audience"] = audience
    if not flows:
        doc["timeline"].pop("flows", None)
        doc["network"]["included"] = False
    return doc


def _document(team, rnd, now, environment, clock, scene, tl, network):
    return {
        "format": FORMAT,
        "version": VERSION,
        "generated_at": _iso(now),
        "audience": None,
        "environment": environment,
        "round": {
            "id": rnd.id,
            "name": rnd.name,
            "competition": "development" if environment == "dev" else "main",
            "start": _iso(rnd.round_start),
            "end": _iso(rnd.round_end),
        },
        "team": {"uuid": str(team.uuid), "name": team.name},
        "clock": clock,
        "scene": scene,
        "timeline": tl,
        "logs": {
            "count": sum(1 for e in tl["events"] if e["kind"] == "log_received"),
            "truncated": tl["truncated"]["logs"],
            "included": False,
            "path": None,
            "url": None,
        },
        "network": network,
    }


def _clock(rnd, zero, now, t_min, t_max):
    finished = rnd.round_end is not None and now >= rnd.round_end
    return {
        "t0": _iso(zero),
        "round_start": _sec(rnd.round_start, zero),
        "round_end": _sec(rnd.round_end, zero),
        "now": None if finished else _sec(now, zero),
        "finished": finished,
        "t_min": t_min,
        "t_max": max(t_max, t_min),
    }


def _span(tl, *extra):
    times = [e["t"] for e in tl["events"]]
    for f in tl.get("flows", []):
        times += [f["t"], f["t"] + f["d"]]
    times += [t for t in extra if t is not None]
    return min([0.0] + times), max([0.0] + times)


def _empty_timeline():
    return {
        "run": None, "nodes": [], "networks": [], "edges": [], "vm_status": {}, "events": [], "flows": [],
        "captures": [], "truncated": {"flows": False, "flag_submissions": False, "logs": False},
    }  # fmt: skip


# --- live: the team's run ------------------------------------------------------


def _live(team, rnd, now, audience, infra, flow_limit):
    run = (
        RoundRun.objects.filter(round=rnd, team=team)
        .select_related("round", "team")
        .prefetch_related("virtual_machines", "events__vm")
        .first()
    )
    if run is None:
        zero, tl = rnd.round_start or now, _empty_timeline()
    else:
        zero = run.worm_start_at or run.created_at
        tl = timeline.build(run, flow_limit=flow_limit, infra=infra)
        tl.pop("run")
        tl["run"] = _run_info(run, zero)
        if audience == "team":
            tl = for_team(tl)
        else:
            tl["run"].update(uuid=str(run.uuid), team_uuid=str(team.uuid), team_slug=f"t{team.id}")
    t_min, t_max = _span(tl, tl["run"]["duration"] if tl["run"] else None)
    # A run in progress plays up to now.
    if run is not None and run.state not in DONE_STATES and tl["events"]:
        t_max = max(t_max, _sec(now, zero))
    clock = _clock(rnd, zero, now, t_min, t_max)
    if tl["run"]:
        tl["run"].update(t_min=clock["t_min"], t_max=clock["t_max"])
    network = {
        "available": True,
        "reason": None,
        "count": len(tl["flows"]),
        "truncated": tl["truncated"]["flows"],
        "included": True,
        "path": None,
        "url": None,
    }
    scene = team_scene("live", team, infra) if audience == "team" else None
    return _document(team, rnd, now, "live", clock, scene, tl, network)


def _run_info(run, zero):
    start_vm = run.sandbox_params.get("start_vm")
    return {
        "uuid": None,
        "team": run.team.name,
        "round": run.round.name,
        "state": run.state,
        "position": run.position,
        "worm_start_at": _iso(run.worm_start_at),
        "ends_at": _iso(run.ends_at),
        "start_vm": start_vm,
        "duration": _sec(run.ends_at, zero),
        "t_min": 0.0,
        "t_max": 0.0,
    }


def for_team(tl):
    """The part of a run's timeline (timeline.build) its team may see: the VMs
    it captured, scoring, logging and the private internet."""
    visible = {n["id"] for n in tl["nodes"] if n["captured"]}
    label = {n["id"]: n["label"] for n in tl["nodes"] if n["id"] in visible}
    see = lambda vm: vm if vm in visible else None  # noqa: E731

    # Networks get an opaque id and are labelled by their CIDR: the bridge
    # name is a Proxmox internal. Only networks a visible VM is on.
    net_ids = {}
    for n in tl["nodes"]:
        if n["id"] in visible:
            for net in n["networks"]:
                net_ids.setdefault(net["name"], f"net-{len(net_ids) + 1}")
    cidr = {net["id"]: net.get("cidr") for net in tl["networks"]}
    for n in tl["nodes"]:
        for net in n["networks"]:
            cidr.setdefault(net["name"], net.get("cidr"))

    nodes = [
        {
            "id": n["id"],
            "label": n["label"],
            "group": "sandbox",
            "addresses": list(n["addresses"]),
            "networks": [{"name": net_ids[x["name"]], "cidr": x.get("cidr")} for x in n["networks"]],
            "start_vm": n["start_vm"],
            "captured": True,
            "captured_at": n["captured_at"],
        }
        for n in tl["nodes"]
        if n["id"] in visible
    ]
    networks = [{"id": nid, "label": f"net {nid[4:]}", "cidr": cidr.get(name)} for name, nid in net_ids.items()]
    edges = [
        {"source": e["source"], "target": net_ids[e["target"]], "network": net_ids[e["target"]]}
        for e in tl["edges"]
        if e["source"] in visible and e["target"] in net_ids
    ]

    events = []
    for e in tl["events"]:
        kind, d = e["kind"], e["data"] or {}
        if kind not in TEAM_EVENTS:
            continue
        if kind == "state":
            node, vm, data = None, None, {"state": d.get("state")}
        elif kind in ("vm_stopped", "vm_started", "secret_released", "captured"):
            if e["vm"] not in visible:
                continue
            node, vm, data = e["vm"], e["vm"], {}
        elif kind in ("worm_deployed", "worm_started"):
            node = vm = see(e["node"])
            data = {}
        else:
            source = see(d.get("source_vm"))
            node, vm = e["node"], source
            data = {"source_vm": source, "source_label": label.get(source), "source_inferred": source is not None}
            if kind == "flag_submitted":
                target = see(d.get("flag_of"))
                data.update(
                    result=d.get("result"), flag_of=target, flag_of_label=label.get(target),
                    hash_prefix=d.get("hash_prefix"), processed=d.get("processed"),
                )  # fmt: skip
            else:
                data.update(stored=d.get("stored"), processed=d.get("processed"), log=d.get("log"))
        events.append({"t": e["t"], "kind": kind, "node": node, "vm": vm, "data": data})
    for i, e in enumerate(events):
        e["id"] = i

    def end(node, role, name):
        if role == "sandbox":
            return (node, role, label[node]) if node in visible else None
        if role in SERVICE_NODES:
            return SERVICE_NODES[role], role, role
        if role in PI_ROLES:
            return f"pi:{name}", role, name
        return None  # infrastructure, a gateway, an unknown address

    flows = []
    for f in tl.get("flows", []):
        src = end(f["src"], f["src_role"], f["src_name"])
        dst = end(f["dst"], f["dst_role"], f["dst_name"])
        if src is None or dst is None or "sandbox" not in (src[1], dst[1]):
            continue
        flows.append(
            {
                **f,
                "src": src[0], "src_role": src[1], "src_name": src[2],
                "dst": dst[0], "dst_role": dst[1], "dst_name": dst[2],
            }  # fmt: skip
        )

    run = dict(tl["run"])
    run["start_vm"] = next((n["label"] for n in nodes if n["start_vm"]), None)
    return {
        "run": run,
        "nodes": nodes,
        "networks": networks,
        "edges": edges,
        "vm_status": {vm: points for vm, points in tl["vm_status"].items() if vm in visible},
        "events": events,
        "flows": flows,
        "captures": [{"t": c["t"], "vm": c["vm"]} for c in tl["captures"] if c["vm"] in visible],
        "truncated": dict(tl["truncated"]),
    }


def _pi_nodes(infra):
    """The private internet's VMs (DNS first), named as in the snapshot."""
    found = {}
    for hit in infra.values():
        if hit["role"] in PI_ROLES:
            found.setdefault(hit["name"], hit["role"])
    ordered = sorted(found.items(), key=lambda item: (item[1] != "dns", item[0]))
    return [_scene_node(f"pi:{name}", name, role) for name, role in ordered]


def _scene_node(node_id, name, role, **extra):
    return {"id": node_id, "name": name, "role": role, "status": "running", "active": True, **extra}


def team_scene(environment, team, infra, devbox_nodes=()):
    """What a team's player draws: scoring, logging, the private internet and
    one untrusted group (the sandbox, or the team's own devbox with the VMs it
    captured). No addresses, firewall or Proxmox details."""
    pi = _pi_nodes(infra)
    nodes = [_scene_node("role:scoring", "scoring", "scoring"), _scene_node("role:logging", "logging", "logging"), *pi]
    if environment == "dev":
        group = {
            "id": "group:devbox:own", "kind": "devbox", "team": None, "team_name": team.name,
            "label": f"Devbox {team.name}", "gateway": None, "members": [n["id"] for n in devbox_nodes],
        }  # fmt: skip
        nodes += devbox_nodes
    else:
        group = {"id": "group:sandbox", "kind": "sandbox", "team": None, "label": "Sandbox", "gateway": None, "members": []}
    edges = [
        {"id": f"service:{group['id']}->{node}", "source": group["id"], "target": node, "kind": "service",
         "verified": True, "label": role}
        for role, node in SERVICE_NODES.items()
    ]  # fmt: skip
    if pi:
        edges.append(
            {"id": f"service:{group['id']}->pi", "source": group["id"], "target": "pi", "kind": "service",
             "verified": True, "label": "private internet", "targets": [n["name"] for n in pi]}
        )  # fmt: skip
    if environment == "dev":
        by_role = {n["devbox_role"]: n["id"] for n in devbox_nodes}
        for a, b in DEVBOX_LINKS:
            if a in by_role and b in by_role:
                edges.append({"id": f"lan:{by_role[a]}->{by_role[b]}", "source": by_role[a], "target": by_role[b],
                              "kind": "lan", "verified": True, "label": None})  # fmt: skip
    return {"host": {}, "nodes": nodes, "groups": [group], "networks": [], "edges": edges}


# --- dev: the team's devbox during the round's window -----------------------------


def _dev(team, rnd, now, infra):
    zero = rnd.round_start or now
    start, end = window(team, rnd, now, "dev")
    vms = list(DevboxVM.objects.filter(devbox__team=team).order_by("role", "vm_identifier"))
    by_hash = {vm.ctf_flag_hash: vm for vm in vms if vm.ctf_flag_hash}
    by_id = {vm.id: vm for vm in vms}
    truncated = {"flows": False, "flag_submissions": False, "logs": False}
    submissions, logs, captured_at = [], [], {}

    if start is not None:
        attempts = list(
            FlagCaptureAttempt.objects.filter(
                team_uuid=str(team.uuid), devbox=True, created_at__gte=start, created_at__lte=end
            ).order_by("created_at", "id")[: ATTEMPT_LIMIT + 1]
        )
        truncated["flag_submissions"] = len(attempts) > ATTEMPT_LIMIT
        attempts = attempts[:ATTEMPT_LIMIT]
        capture_vm = dict(
            DevboxCapture.objects.filter(team=team, flag_capture_attempt__in=attempts).values_list(
                "flag_capture_attempt_id", "devbox_vm_id"
            )
        )
        for attempt in attempts:
            t = _sec(attempt.created_at, zero)
            if attempt.id in capture_vm:
                result, vm = "captured", by_id.get(capture_vm[attempt.id])
            elif not attempt.processed:
                result, vm = "pending", by_hash.get(attempt.vm_flag_hash)
            elif attempt.vm_flag_hash in by_hash:
                # The token is one of the team's devbox VMs, already counted
                # at this devbox version: still found in this round.
                result, vm = "duplicate", by_hash[attempt.vm_flag_hash]
            else:
                result, vm = "invalid", None
            if vm is not None and result in ("captured", "duplicate"):
                captured_at.setdefault(str(vm.uuid), t)
            submissions.append((t, attempt, result, vm))

        rows = list(
            timeline.log_attempts(
                LoggingAttempt.objects.filter(
                    team_uuid=str(team.uuid), devbox=True, created_at__gte=start, created_at__lte=end
                )
            ).order_by("created_at", "id")[: ATTEMPT_LIMIT + 1]
        )
        truncated["logs"] = len(rows) > ATTEMPT_LIMIT
        rows = rows[:ATTEMPT_LIMIT]
        stored = set(DevboxLog.objects.filter(logging_attempt__in=rows).values_list("logging_attempt_id", flat=True))
        logs = [(_sec(a.created_at, zero), a, a.id in stored) for a in rows]

    visible = [vm for vm in vms if str(vm.uuid) in captured_at]
    label = {str(vm.uuid): vm.vm_identifier for vm in visible}
    unknown_source = {"source_vm": None, "source_label": None, "source_inferred": False}
    events = []
    for vm_id, t in captured_at.items():
        events.append({"t": t, "kind": "captured", "node": vm_id, "vm": vm_id, "data": {}})
    for t, attempt, result, vm in submissions:
        target = str(vm.uuid) if vm is not None and str(vm.uuid) in label else None
        events.append(
            {
                "t": t, "kind": "flag_submitted", "node": "role:scoring", "vm": None,
                "data": {
                    "result": result, "flag_of": target, "flag_of_label": label.get(target),
                    "hash_prefix": (attempt.vm_flag_hash or "")[:16], "processed": attempt.processed,
                    **unknown_source,
                },
            }  # fmt: skip
        )
    for t, attempt, is_stored in logs:
        events.append(
            {
                "t": t, "kind": "log_received", "node": "role:logging", "vm": None,
                "data": {"stored": is_stored, "processed": attempt.processed, "log": timeline.log_ref(attempt), **unknown_source},
            }  # fmt: skip
        )
    events.sort(key=lambda e: e["t"])
    for i, e in enumerate(events):
        e["id"] = i

    t_min, t_max = 0.0, _sec(end, zero) if end is not None else 0.0
    nodes = [
        {
            "id": str(vm.uuid), "label": vm.vm_identifier, "group": "devbox", "devbox_role": vm.role or None,
            "addresses": [], "networks": [], "start_vm": False, "captured": True,
            "captured_at": captured_at[str(vm.uuid)],
        }  # fmt: skip
        for vm in visible
    ]
    tl = {
        "run": None,
        "nodes": nodes,
        "networks": [],
        "edges": [],
        # The devbox is always on.
        "vm_status": {n["id"]: [[t_min, "running"]] for n in nodes},
        "events": events,
        "captures": [{"t": captured_at[n["id"]], "vm": n["id"]} for n in nodes],
        "truncated": truncated,
    }
    scene_nodes = [
        _scene_node(n["id"], n["label"], "devbox", devbox_role=n["devbox_role"], team=None) for n in nodes
    ]
    t_min, t_max = min([t_min] + [e["t"] for e in events]), max([t_max] + [e["t"] for e in events])
    network = {
        "available": False,
        "reason": NO_DEVBOX_TRAFFIC,
        "count": 0,
        "truncated": False,
        "included": False,
        "path": None,
        "url": None,
    }
    clock = _clock(rnd, zero, now, t_min, t_max)
    return _document(team, rnd, now, "dev", clock, team_scene("dev", team, infra, scene_nodes), tl, network)


# --- log records and the zip -----------------------------------------------------------


def _log_file(attempt):
    return {"ref": str(attempt.uuid), "received_at": _iso(attempt.created_at), "data": attempt.log_data}


def log_record(team, rnd, ref, *, now, environment=None):
    """One log record of the progression ({ref, received_at, data}; ref is
    the attempt's uuid), or None when it is not the team's or not part of the
    round's progression."""
    environment = environment or environment_of(team, rnd)
    start, end = window(team, rnd, now, environment)
    if start is None:
        return None
    attempt = LoggingAttempt.objects.filter(
        uuid=ref, team_uuid=str(team.uuid), devbox=environment == "dev", created_at__gte=start, created_at__lte=end
    ).first()
    return _log_file(attempt) if attempt is not None else None


def log_files(doc) -> dict:
    """{ref: record} for every log the document references (they were
    selected by `build`, for its team and window)."""
    refs = [e["data"]["log"]["ref"] for e in doc["timeline"]["events"] if e["kind"] == "log_received"]
    files = {}
    for i in range(0, len(refs), 500):
        for attempt in LoggingAttempt.objects.filter(uuid__in=refs[i : i + 500]):
            files[str(attempt.uuid)] = _log_file(attempt)
    return files


def package(doc, logs=None) -> bytes:
    """The progression as a zip: progression.json; logs/<ref>.json when `logs`
    ({ref: record}, `log_files`) is given; network/flows.jsonl when the
    document has its flows inline. `doc` itself is not modified."""
    doc = {**doc, "timeline": dict(doc["timeline"]), "logs": dict(doc["logs"]), "network": dict(doc["network"])}
    flows = doc["timeline"].pop("flows", None)
    doc["network"].update(included=flows is not None, path=ZIP_FLOWS if flows is not None else None, url=None)
    doc["logs"].update(included=logs is not None, path=ZIP_LOGS if logs is not None else None, url=None)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(ZIP_DOCUMENT, json.dumps(doc, ensure_ascii=False))
        archive.writestr("README.txt", README)
        for ref, record in (logs or {}).items():
            archive.writestr(ZIP_LOGS.format(ref=ref), json.dumps(record, ensure_ascii=False))
        if flows is not None:
            archive.writestr(ZIP_FLOWS, "".join(json.dumps(f, ensure_ascii=False) + "\n" for f in flows))
    return buffer.getvalue()


def filename(doc, extension="zip") -> str:
    """A download name: worms-<round>-<team>-<environment>.<extension>."""
    parts = ["worms", doc["round"]["name"], doc["team"]["name"], doc["environment"]]
    safe = "-".join("".join(c if c.isalnum() or c in "._-" else "_" for c in str(p)) for p in parts)
    return f"{safe}.{extension}"
