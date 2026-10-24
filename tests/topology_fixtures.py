"""Fixture JSON for the topology page's browser test (tests/test_topology_page.py):
the three endpoints the player reads.

- model():    the live infrastructure model, from the real analyzer
              (dashboard/infra_view.py) on tests/infra_fixture.py's snapshot;
- runs():     /topology/runs/ (one round, two teams);
- timeline(): one run's timeline in orchestrator/rounds/timeline.py's schema
              (test_rounds.py checks the real builder emits the same keys).

The timeline carries an XSS payload in a log record: the page must show it as
text.
"""

from __future__ import annotations

import sys

import infra_fixture
from conftest import REPO

sys.path.insert(0, str(REPO / "projects" / "dashboard"))

from dashboard import infra_view  # noqa: E402

RUN_UUID = "6b1f2c3d-0000-4000-8000-00000000a001"
OTHER_RUN_UUID = "6b1f2c3d-0000-4000-8000-00000000a002"
XSS = "<img src=x onerror=window.__xss=1>"


def model() -> dict:
    m = infra_view.analyze(infra_fixture.snapshot())
    for group in m["groups"]:
        if group["kind"] == "devbox":
            group["team_name"] = {"t1": "team-a", "t2": "team-b"}[group["team"]]
            group["label"] = f"Devbox {group['team_name']}"
    return {**m, "collected_at": "2026-10-24T12:00:00+00:00", "attempted_at": "2026-10-24T12:00:00+00:00",
            "collection_error": "", "stale": False}


def runs() -> dict:
    return {
        "competition": "WORMS 2026",
        "rounds": [
            {
                "id": 1, "name": "R1", "round_start": "2026-10-24T10:00:00+00:00", "round_end": "2026-10-24T18:00:00+00:00",
                "runs": [
                    {"uuid": RUN_UUID, "team": "team-a", "state": "finished", "position": 0, "worm_start_at": "2026-10-24T10:05:00+00:00"},
                    {"uuid": OTHER_RUN_UUID, "team": "team-b", "state": "pending", "position": 1, "worm_start_at": None},
                ],
            }
        ],
    }


def _uuid(i):
    return f"0b9c0000-0000-4000-8000-{i:012d}"


def timeline() -> dict:
    vms = []
    for i in range(6):
        net = "vmbr1000" if i < 4 else "vmbr1001"
        networks = [{"name": net, "cidr": "10.64.0.0/24" if net == "vmbr1000" else "10.64.1.0/24"}]
        addresses = [f"10.64.{0 if i < 4 else 1}.{10 + i}"]
        if i == 3:  # multi-homed: on both networks
            networks.append({"name": "vmbr1001", "cidr": "10.64.1.0/24"})
            addresses.append("10.64.1.13")
        vms.append({
            "id": _uuid(i), "label": f"sandbox-vm{i}", "vmid": 900 + i, "addresses": addresses, "networks": networks,
            "start_vm": i == 0, "captured": i == 1, "captured_at": 95.0 if i == 1 else None,
        })
    v = [vm["id"] for vm in vms]
    events = []

    def event(t, kind, node=None, vm=None, **data):
        events.append({"t": t, "kind": kind, "node": node, "vm": vm, "data": data})

    for t, state in [(-60, "preparing"), (-40, "checking"), (-20, "deploying"), (-12, "armed")]:
        event(t, "state", state=state)
    event(-12, "worm_deployed", node=v[0], worm_start_at="2026-10-24T10:05:00+00:00")
    event(0, "worm_started", node=v[0])
    event(0, "state", state="running")
    for i in range(6):
        event(0.5, "secret_released", node=v[i], vm=v[i])
    event(60.4, "log_received", node="role:logging", vm=v[0], uuid="7d7d0000-0000-4000-8000-000000000001", stored=True,
          processed=True, log={"data": {"msg": XSS}, "truncated": False}, source_vm=v[0], source_label="sandbox-vm0",
          source_inferred=True)
    event(95.0, "flag_submitted", node="role:scoring", vm=v[0], result="captured", flag_of=v[1], flag_of_label="sandbox-vm1",
          hash_prefix="3f2a9c0d1e2b3c4d", processed=True, source_vm=v[0], source_label="sandbox-vm0", source_inferred=True)
    event(95.0, "captured", node=v[1], vm=v[1], team=1, attempt=1)
    event(120, "vm_stopped", node=v[3], vm=v[3])
    event(200, "flag_submitted", node="role:scoring", vm=None, result="invalid", flag_of=None, flag_of_label=None,
          hash_prefix="0000000000000000", processed=True, source_vm=None, source_label=None, source_inferred=False)
    event(300, "vm_started", node=v[3], vm=v[3])
    for t, state in [(600, "ending"), (605, "tearing_down"), (640, "finished")]:
        event(t, "state", state=state)
    events.sort(key=lambda e: e["t"])
    for i, e in enumerate(events):
        e["id"] = i

    flows = []

    def flow(t, src, dst, kind, dport, src_role="sandbox", dst_role="sandbox", dst_name=None, src_name=None, state="SF", d=0.4):
        flows.append({
            "t": t, "d": d, "src": src, "dst": dst, "src_role": src_role, "dst_role": dst_role,
            "src_name": src_name or src, "dst_name": dst_name or dst, "kind": kind, "src_port": 40000 + len(flows),
            "dst_port": dport, "transport": "tcp", "service": None, "conn_state": state, "bytes": 1200, "pkts": 8,
            "uid": f"C{len(flows):05d}",
        })

    names = {vm["id"]: vm["label"] for vm in vms}
    for k in range(120):
        t = 1 + k * 4.5
        a, b = v[k % 4], v[(k + 1) % 4]
        flow(t, a, b, "lan", 22, src_name=names[a], dst_name=names[b])
        if k % 3 == 0:
            flow(t + 1, v[4], v[3], "lan", 445, src_name=names[v[4]], dst_name=names[v[3]], state="S0" if k % 6 == 0 else "SF")
        if k % 5 == 0:
            flow(t + 2, v[k % 6], "vm:110", "dns", 53, dst_role="dns", dst_name="dns", src_name=names[v[k % 6]], d=0.02)
        if k % 7 == 0:
            flow(t + 2.5, v[k % 6], "vm:120", "private_internet", 3142, dst_role="private_internet", dst_name="apt-cache",
                 src_name=names[v[k % 6]], d=3.5)
    flow(94.2, v[0], "vm:104", "scoring", 443, dst_role="scoring", dst_name="scoring", src_name="sandbox-vm0", d=1.2)
    flow(60.0, v[0], "vm:105", "logging", 443, dst_role="logging", dst_name="logging", src_name="sandbox-vm0", d=0.8)
    flow(150.0, v[5], "ip:8.8.8.8", "other", 53, dst_role="unknown", dst_name="8.8.8.8", src_name="sandbox-vm5", state="S0")
    flows.sort(key=lambda f: f["t"])

    vm_status = {vm["id"]: [[-40, "running"], [600, "stopped"], [640, "destroyed"]] for vm in vms}
    vm_status[v[3]] = [[-40, "running"], [120, "stopped"], [300, "running"], [600, "stopped"], [640, "destroyed"]]

    return {
        "run": {
            "uuid": RUN_UUID, "team": "team-a", "team_uuid": "5c5c0000-0000-4000-8000-000000000001", "team_slug": "t1",
            "round": "R1", "state": "finished", "position": 0, "worm_start_at": "2026-10-24T10:05:00+00:00",
            "ends_at": "2026-10-24T10:15:00+00:00", "start_vm": "sandbox-vm0", "duration": 600.0, "t_min": -60.0,
            "t_max": 640.0,
        },
        "nodes": vms,
        "networks": [{"id": "vmbr1000", "label": "vmbr1000", "cidr": "10.64.0.0/24"},
                     {"id": "vmbr1001", "label": "vmbr1001", "cidr": "10.64.1.0/24"}],
        "edges": [{"source": vm["id"], "target": n["name"], "network": n["name"]} for vm in vms for n in vm["networks"]],
        "vm_status": vm_status,
        "events": events,
        "flows": flows,
        "captures": [{"t": 95.0, "vm": v[1], "team": 1}],
        "architecture": {},
        "truncated": {"flows": False, "flag_submissions": False, "logs": False},
    }
