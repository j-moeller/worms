"""Assemble a run's timeline: the data the dashboard's animated view replays.

One run -> one JSON document:

    {
      "run": {uuid, team, round, state, worm_start_at, ends_at, start_vm},
      "nodes": [{id, label, networks, start_vm}],         # the sandbox VMs
      "networks": [{id, label, cidr}],                    # the sandbox bridges
      "edges": [{source, target, network}],               # VM <-> network
      "events": [{t, kind, vm, data}],                    # t = seconds from worm start
      "flows":  [{t, src, dst, src_port, dst_port,        # packets as dots
                  transport, service, bytes}],
      "captures": [{t, vm, team}]
    }

Times are seconds relative to the worm's start (negative during deployment),
so the view has a single clock. Flows come from network_flows (the monitor),
matched to this run by VM address and the run's time window. Pure read model:
this does not touch Proxmox and writes nothing.
"""

from __future__ import annotations

from datetime import timedelta

from core.models.monitoring import NetworkFlow
from core.models.rounds import RoundRun, RunEvent


def _seconds(ts, zero):
    return round((ts - zero).total_seconds(), 2)


def build(run: RoundRun, flow_limit=20000) -> dict:
    vms = list(run.virtual_machines.all())
    zero = run.worm_start_at or run.created_at
    start_vm = run.sandbox_params.get("start_vm")

    # Address -> VM id, to turn a flow's src/dst IP into a node.
    by_address = {}
    networks = {}
    for vm in vms:
        for address in vm.addresses or []:
            by_address[address] = vm.uuid
        for net in vm.networks or []:
            networks.setdefault(net.get("name") or net.get("cidr"), net)

    nodes = [
        {
            "id": str(vm.uuid),
            "label": vm.vm_identifier,
            "networks": [n.get("name") or n.get("cidr") for n in (vm.networks or [])],
            "start_vm": vm.vm_identifier == start_vm,
            "captured": vm.ctf_flag_hash is not None
            and vm.flag_captures.filter(team=run.team).exists(),
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

    events = [
        {
            "t": _seconds(event.ts, zero),
            "kind": event.kind,
            "vm": str(event.vm.uuid) if event.vm_id else None,
            "data": event.data,
        }
        for event in run.events.select_related("vm").order_by("ts")
    ]
    captures = [
        {"t": e["t"], "vm": e["vm"], "team": e["data"].get("team")}
        for e in events
        if e["kind"] == RunEvent.Kind.CAPTURED
    ]

    flows = []
    if run.worm_start_at and run.ends_at and by_address:
        window_end = run.ends_at + timedelta(seconds=300)
        qs = (
            NetworkFlow.objects.filter(ts__gte=run.worm_start_at, ts__lte=window_end)
            .filter(src_ip__in=list(by_address), dst_ip__in=list(by_address))
            .order_by("ts")[:flow_limit]
        )
        for flow in qs:
            src, dst = by_address.get(str(flow.src_ip)), by_address.get(str(flow.dst_ip))
            if not src or not dst:
                continue
            flows.append(
                {
                    "t": _seconds(flow.ts, zero),
                    "src": str(src),
                    "dst": str(dst),
                    "src_port": flow.src_port,
                    "dst_port": flow.dst_port,
                    "transport": flow.transport,
                    "service": flow.service or None,
                    "bytes": (flow.src_ip_bytes or 0) + (flow.dst_ip_bytes or 0),
                }
            )

    return {
        "run": {
            "uuid": str(run.uuid),
            "team": run.team.name,
            "round": run.round.name,
            "state": run.state,
            "position": run.position,
            "worm_start_at": run.worm_start_at.isoformat() if run.worm_start_at else None,
            "ends_at": run.ends_at.isoformat() if run.ends_at else None,
            "start_vm": start_vm,
            "duration": _seconds(run.ends_at, zero) if run.ends_at else None,
        },
        "nodes": nodes,
        "networks": network_list,
        "edges": edges,
        "events": events,
        "flows": flows,
        "captures": captures,
        # The full generator graph saved at prepare time (every layer, not
        # just the sandbox VMs): the backdrop the animated view can draw.
        "architecture": run.topology or {},
    }
