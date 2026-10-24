"""Assemble a run's timeline: the data the dashboard's round player replays.

One run -> one JSON document:

    {
      "run": {uuid, team, round, state, worm_start_at, ends_at, start_vm,
              duration, t_min, t_max},
      "nodes": [{id, label, vmid, addresses, networks, start_vm,
                 captured, captured_at}],                 # the run's sandbox VMs
      "networks": [{id, label, cidr}],                    # the sandbox bridges
      "edges": [{source, target, network}],               # VM <-> network
      "vm_status": {vm id: [[t, "running"|"stopped"|"destroyed"], ...]},
      "events": [{id, t, kind, node, vm, data}],          # t = seconds from worm start
      "flows": [{t, d, src, dst, src_role, dst_role, dst_name, kind,
                 src_port, dst_port, transport, service, conn_state,
                 bytes, pkts, uid}],                      # connections, as dots
      "captures": [{t, vm, team}],
      "architecture": {...},                               # the generator graph
      "truncated": {"flows": bool, "flag_submissions": bool, "logs": bool},
    }

Times are seconds relative to the worm's start (negative during deployment),
so the player has a single clock.

* Flows come from network_flows (the monitor), matched to this run by the
  run's VM addresses and its time window. The monitor records them before the
  gateway's SNAT, so the run's end is the VM's own address; the far end is
  another run VM (`lan`), or -- through `infra`, the address index of the live
  Proxmox snapshot (dashboard/infra_view.address_index) -- scoring, logging,
  DNS, a private-internet VM, ...
* Besides the run's own events (state changes, VMs stopped/started, secrets
  released, worm deployed/started, captures) this derives:
    - `flag_submitted` on scoring: every flag the team's sandbox submitted
      during the run (captured / duplicate / not_counted / invalid / pending);
    - `log_received` on logging: every log the team's sandbox sent.
  Scoring and logging only see the gateway's address, so the submitting VM is
  inferred from the monitor (the latest connection from a run VM to that
  server that was open at the time) and marked `source_inferred`.

Every value derived from the sandbox (flows, log data, submitted hashes) is
attacker-controlled: the player renders it as text only. Pure read model: this
does not touch Proxmox and writes nothing.
"""

from __future__ import annotations

import bisect
import json
from datetime import timedelta

from core.models.logging import Logging, LoggingAttempt
from core.models.monitoring import NetworkFlow
from core.models.rounds import RoundRun, RunEvent
from core.models.scoring import FlagCapture, FlagCaptureAttempt
from django.db.models import Max, Min, Q

FLOW_DRAIN = timedelta(seconds=300)  # flows are written when they end
ATTRIBUTION_SLACK = 2.0  # seconds of clock skew between the monitor and the API
ATTRIBUTION_SCAN = 500  # connections looked back at per submission
LOG_PREVIEW = 4000  # characters of a log record kept for the detail view
ATTEMPT_LIMIT = 5000

# Roles of the far end that name the flow's kind.
SERVICE_ROLES = {"scoring", "logging", "dns", "private_internet", "proxmox", "sandbox_gw"}


def _seconds(ts, zero):
    return round((ts - zero).total_seconds(), 2)


def _preview(value):
    text = json.dumps(value, ensure_ascii=False, default=str)
    if len(text) <= LOG_PREVIEW:
        return {"data": value, "truncated": False}
    return {"data": text[:LOG_PREVIEW], "truncated": True, "size": len(text)}


def _vm_status(vms, events, t_min):
    """Each VM's power state over time: built (running) once prepare is done,
    then the game plan's stops/starts, all stopped at ENDING, destroyed at
    teardown."""
    status = {str(vm.uuid): [] for vm in vms}
    built = next((e["t"] for e in events if e["kind"] == "state" and e["data"].get("state") == "checking"), t_min)
    for vm_id in status:
        status[vm_id].append([built, "running"])
    for e in events:
        if e["kind"] in ("vm_stopped", "vm_started") and e["vm"] in status:
            status[e["vm"]].append([e["t"], "stopped" if e["kind"] == "vm_stopped" else "running"])
        elif e["kind"] == "state" and e["data"].get("state") in ("ending", "finished", "failed"):
            state = "stopped" if e["data"]["state"] == "ending" else "destroyed"
            for vm_id in status:
                status[vm_id].append([e["t"], state])
    for points in status.values():
        points.sort(key=lambda p: p[0])
    return status


def run_window(run: RoundRun, event_times=None):
    """(start, end) of a run's activity: from its first event (prepare) or the
    worm's start, to its end plus the monitor's drain. (None, None) before the
    run began. Flows, flag submissions and logs of the run fall in here."""
    if event_times is None:
        agg = run.events.aggregate(first=Min("ts"), last=Max("ts"))
        event_times = [t for t in (agg["first"], agg["last"]) if t is not None]
    starts = list(event_times) + ([run.worm_start_at] if run.worm_start_at else [])
    if not starts:
        return None, None
    end = run.ends_at or max(event_times, default=min(starts))
    return min(starts), end + FLOW_DRAIN


def classify_submission(attempt, capture, vm, captured_vm_ids):
    """(result, vm) for a flag submission of the run's team.

    capture: its FlagCapture (or None); vm: the run VM whose flag hash it is
    (or None); captured_vm_ids: uuids (str) of the run's captured VMs.
    result: captured | pending (not processed yet) | duplicate (that VM was
    already captured) | not_counted (a run VM's flag outside the live window)
    | invalid (no VM of the run has that flag)."""
    if capture is not None:
        return "captured", capture.vm
    if not attempt.processed:
        return "pending", vm
    if vm is not None:
        return ("duplicate" if str(vm.uuid) in captured_vm_ids else "not_counted"), vm
    return "invalid", None


def build(run: RoundRun, flow_limit=20000, infra: dict | None = None) -> dict:
    infra = infra or {}
    vms = list(run.virtual_machines.all())
    zero = run.worm_start_at or run.created_at
    start_vm = run.sandbox_params.get("start_vm")
    team_uuid = str(run.team.uuid)

    # Address -> run VM id, to turn a flow's sandbox end into a node.
    by_address = {}
    networks = {}
    for vm in vms:
        for address in vm.addresses or []:
            by_address[address] = str(vm.uuid)
        for net in vm.networks or []:
            networks.setdefault(net.get("name") or net.get("cidr"), net)
    labels = {str(vm.uuid): vm.vm_identifier for vm in vms}
    by_hash = {vm.ctf_flag_hash: vm for vm in vms if vm.ctf_flag_hash}

    run_events = list(run.events.select_related("vm").order_by("ts", "id"))
    captured_at = {}
    for event in run_events:
        if event.kind == RunEvent.Kind.CAPTURED and event.vm_id:
            captured_at.setdefault(str(event.vm.uuid), _seconds(event.ts, zero))

    nodes = [
        {
            "id": str(vm.uuid),
            "label": vm.vm_identifier,
            "vmid": vm.proxmox_vm_id,
            "addresses": list(vm.addresses or []),
            "networks": [{"name": n.get("name") or n.get("cidr"), "cidr": n.get("cidr")} for n in (vm.networks or [])],
            "start_vm": vm.vm_identifier == start_vm,
            "captured": str(vm.uuid) in captured_at,
            "captured_at": captured_at.get(str(vm.uuid)),
        }
        for vm in vms
    ]
    network_list = [
        {"id": name, "label": net.get("name", name), "cidr": net.get("cidr")}
        for name, net in networks.items()
    ]
    edges = [
        {"source": str(vm.uuid), "target": (n.get("name") or n.get("cidr")), "network": (n.get("name") or n.get("cidr"))}
        for vm in vms
        for n in (vm.networks or [])
    ]

    start_node = next((n["id"] for n in nodes if n["start_vm"]), None)
    events = []
    for event in run_events:
        vm_id = str(event.vm.uuid) if event.vm_id else None
        node = vm_id
        if node is None and event.kind in (RunEvent.Kind.WORM_DEPLOYED, RunEvent.Kind.WORM_STARTED):
            node = start_node
        events.append({"t": _seconds(event.ts, zero), "kind": event.kind, "node": node, "vm": vm_id, "data": event.data})
    captures = [
        {"t": e["t"], "vm": e["vm"], "team": e["data"].get("team")}
        for e in events
        if e["kind"] == RunEvent.Kind.CAPTURED
    ]

    window_start, window_end = run_window(run, [e.ts for e in run_events])

    def endpoint(address):
        address = str(address)
        if address in by_address:
            return by_address[address], "sandbox", labels[by_address[address]]
        hit = infra.get(address)
        if hit:
            return hit["node"], hit["role"], hit["name"]
        return f"ip:{address}", "unknown", address

    flows, truncated = [], {"flows": False, "flag_submissions": False, "logs": False}
    if window_start and by_address:
        qs = (
            NetworkFlow.objects.filter(ts__gte=window_start, ts__lte=window_end)
            .filter(Q(src_ip__in=list(by_address)) | Q(dst_ip__in=list(by_address)))
            .order_by("ts", "id")[: flow_limit + 1]
        )
        for flow in qs:
            if len(flows) == flow_limit:
                truncated["flows"] = True
                break
            src, src_role, src_name = endpoint(flow.src_ip)
            dst, dst_role, dst_name = endpoint(flow.dst_ip)
            if src_role == "sandbox" and dst_role == "sandbox":
                kind = "lan"
            else:
                other = dst_role if src_role == "sandbox" else src_role
                kind = other if other in SERVICE_ROLES else "other"
            flows.append(
                {
                    "t": _seconds(flow.ts, zero),
                    "d": round(flow.duration or 0.0, 3),
                    "src": src,
                    "dst": dst,
                    "src_role": src_role,
                    "dst_role": dst_role,
                    "src_name": src_name,
                    "dst_name": dst_name,
                    "kind": kind,
                    "src_port": flow.src_port,
                    "dst_port": flow.dst_port,
                    "transport": flow.transport,
                    "service": flow.service or None,
                    "conn_state": flow.conn_state or None,
                    "bytes": (flow.src_ip_bytes or 0) + (flow.dst_ip_bytes or 0),
                    "pkts": (flow.src_pkts or 0) + (flow.dst_pkts or 0),
                    "uid": flow.uid,
                }
            )

    # Scoring / logging see only the gateway's address: the submitting VM is
    # the one whose connection to that server was open at the time (the
    # latest such connection; a keep-alive connection may carry several).
    by_role_flows = {}
    for f in flows:
        if f["src_role"] == "sandbox" and f["dst_role"] in ("scoring", "logging"):
            times, spans = by_role_flows.setdefault(f["dst_role"], ([], []))
            times.append(f["t"])
            spans.append((f["t"] + f["d"], f["src"]))

    def inferred_source(role, t):
        times, spans = by_role_flows.get(role, ([], []))
        i = bisect.bisect_right(times, t + ATTRIBUTION_SLACK) - 1
        for j in range(i, max(i - ATTRIBUTION_SCAN, -1), -1):
            end, src = spans[j]
            if end + ATTRIBUTION_SLACK >= t:
                return src
        return None

    if window_start:
        attempts = list(
            FlagCaptureAttempt.objects.filter(
                team_uuid=team_uuid, devbox=False, created_at__gte=window_start, created_at__lte=window_end
            ).order_by("created_at", "id")[: ATTEMPT_LIMIT + 1]
        )
        truncated["flag_submissions"] = len(attempts) > ATTEMPT_LIMIT
        captures_by_attempt = {
            c.flag_capture_attempt_id: c
            for c in FlagCapture.objects.filter(flag_capture_attempt__in=attempts[:ATTEMPT_LIMIT]).select_related("vm")
        }
        for attempt in attempts[:ATTEMPT_LIMIT]:
            t = _seconds(attempt.created_at, zero)
            result, target_vm = classify_submission(
                attempt, captures_by_attempt.get(attempt.id), by_hash.get(attempt.vm_flag_hash), captured_at
            )
            target = str(target_vm.uuid) if target_vm is not None else None
            source = inferred_source("scoring", t)
            events.append(
                {
                    "t": t,
                    "kind": "flag_submitted",
                    "node": "role:scoring",
                    "vm": source,
                    "data": {
                        "result": result,
                        "flag_of": target,
                        "flag_of_label": labels.get(target),
                        "hash_prefix": (attempt.vm_flag_hash or "")[:16],
                        "processed": attempt.processed,
                        "source_vm": source,
                        "source_label": labels.get(source),
                        "source_inferred": source is not None,
                    },
                }
            )

        logs = list(
            LoggingAttempt.objects.filter(
                team_uuid=team_uuid, devbox=False, created_at__gte=window_start, created_at__lte=window_end
            ).order_by("created_at", "id")[: ATTEMPT_LIMIT + 1]
        )
        truncated["logs"] = len(logs) > ATTEMPT_LIMIT
        stored = set(
            Logging.objects.filter(logging_attempt__in=logs[:ATTEMPT_LIMIT], round=run.round).values_list("logging_attempt_id", flat=True)
        )
        for attempt in logs[:ATTEMPT_LIMIT]:
            t = _seconds(attempt.created_at, zero)
            source = inferred_source("logging", t)
            events.append(
                {
                    "t": t,
                    "kind": "log_received",
                    "node": "role:logging",
                    "vm": source,
                    "data": {
                        "uuid": str(attempt.uuid),
                        "stored": attempt.id in stored,
                        "processed": attempt.processed,
                        "log": _preview(attempt.log_data),
                        "source_vm": source,
                        "source_label": labels.get(source),
                        "source_inferred": source is not None,
                    },
                }
            )

    events.sort(key=lambda e: e["t"])
    for i, e in enumerate(events):
        e["id"] = i

    duration = _seconds(run.ends_at, zero) if run.ends_at else None
    times = [e["t"] for e in events] + [f["t"] for f in flows] + [f["t"] + f["d"] for f in flows]
    t_min = min([0.0] + times)
    t_max = max([duration or 0.0] + times)

    return {
        "run": {
            "uuid": str(run.uuid),
            "team": run.team.name,
            "team_uuid": team_uuid,
            # The team's devbox VMs are named devbox-<slug>-a/b/c
            # (orchestrator provision_devboxes.team_slug).
            "team_slug": f"t{run.team_id}",
            "round": run.round.name,
            "state": run.state,
            "position": run.position,
            "worm_start_at": run.worm_start_at.isoformat() if run.worm_start_at else None,
            "ends_at": run.ends_at.isoformat() if run.ends_at else None,
            "start_vm": start_vm,
            "duration": duration,
            "t_min": t_min,
            "t_max": t_max,
        },
        "nodes": nodes,
        "networks": network_list,
        "edges": edges,
        "vm_status": _vm_status(vms, events, t_min),
        "events": events,
        "flows": flows,
        "captures": captures,
        # The full generator graph saved at prepare time (every layer, not
        # just the sandbox VMs).
        "architecture": run.topology or {},
        "truncated": truncated,
    }
