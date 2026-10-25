"""Fixture JSON for the round player's browser tests (tests/test_topology_page.py):
the endpoints the player reads.

Staff (the TOPOLOGY page):
- model():       the live infrastructure model, from the real analyzer
                 (dashboard/infra_view.py) on tests/infra_fixture.py's snapshot;
- runs():        /topology/runs/ (one round, two teams);
- timeline():    one run's timeline in orchestrator/rounds/timeline.py's schema
                 (test_rounds.py checks the real builder emits the same keys);
- progression(): that run's progression for staff (orchestrator/rounds/progression.py),
                 its flows (flows()) and its log record (log_record()).
Team (the PROGRESSION page):
- team_rounds(), team_progression(round_id): a live round (team-a's run: the
  two VMs it captured) and a dev round (its devbox); test_progression.py
  checks the real builder emits the same keys.

The log record carries an XSS payload: the page must show it as text.
"""

from __future__ import annotations

import sys

import infra_fixture
from conftest import REPO

sys.path.insert(0, str(REPO / "projects" / "dashboard"))

from dashboard import infra_view  # noqa: E402

RUN_UUID = "6b1f2c3d-0000-4000-8000-00000000a001"
LOG_REF = "7d7d0000-0000-4000-8000-000000000001"
TEAM_UUID = "5c5c0000-0000-4000-8000-000000000001"
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
    event(60.4, "log_received", node="role:logging", vm=v[0], stored=True, processed=True,
          log={"ref": LOG_REF, "size": 48}, source_vm=v[0], source_label="sandbox-vm0", source_inferred=True)
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
            "uuid": RUN_UUID, "team": "team-a", "team_uuid": TEAM_UUID, "team_slug": "t1",
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


def log_record() -> dict:
    return {"ref": LOG_REF, "received_at": "2026-10-24T10:06:00+00:00", "data": {"msg": XSS}}


def _envelope(audience, environment, rnd, clock, scene, tl, logs_url, network):
    return {
        "format": "worms-progression", "version": 1, "generated_at": "2026-10-24T12:00:00+00:00",
        "audience": audience, "environment": environment, "round": rnd,
        "team": {"uuid": TEAM_UUID, "name": "team-a"}, "clock": clock, "scene": scene, "timeline": tl,
        "logs": {"count": sum(1 for e in tl["events"] if e["kind"] == "log_received"), "truncated": False,
                 "included": False, "path": None, "url": logs_url},
        "network": network,
    }


R1 = {"id": 1, "name": "R1", "competition": "main", "start": "2026-10-24T10:00:00+00:00", "end": "2026-10-24T18:00:00+00:00"}


def progression() -> dict:
    """The staff progression of RUN_UUID: the timeline without its flows
    (those are at network.url), logs by reference (logs.url)."""
    tl = timeline()
    flows_ = tl.pop("flows")
    tl["run"].update(uuid=RUN_UUID)
    return _envelope(
        "staff", "live", R1,
        {"t0": "2026-10-24T10:05:00+00:00", "round_start": -300.0, "round_end": 28500.0, "now": None, "finished": True,
         "t_min": -60.0, "t_max": 640.0},
        None, tl, f"/rounds/{RUN_UUID}/progression/logs/{{ref}}/",
        {"available": True, "reason": None, "count": len(flows_), "truncated": False, "included": False, "path": None,
         "url": f"/rounds/{RUN_UUID}/progression/flows/"},
    )


def flows() -> dict:
    f = timeline()["flows"]
    return {"count": len(f), "truncated": False, "available": True, "reason": None, "flows": f}


def team_rounds() -> dict:
    return {
        "environment": "live", "team": {"uuid": TEAM_UUID, "name": "team-a"}, "default": 1,
        "rounds": [
            {"id": 1, "name": "R1", "start": R1["start"], "end": R1["end"], "state": "active", "run": "finished"},
            {"id": 2, "name": "R2", "start": "2026-10-25T10:00:00+00:00", "end": "2026-10-25T18:00:00+00:00", "state": "upcoming", "run": None},
            {"id": 3, "name": "Dev week", "start": "2026-10-20T00:00:00+00:00", "end": "2026-10-27T00:00:00+00:00", "state": "active", "run": None},
        ],
    }


def _scene(group, extra_nodes=(), extra_edges=()):
    """What progression.team_scene builds: scoring, logging, the private internet, one group."""
    node = lambda i, name, role, **kw: {"id": i, "name": name, "role": role, "status": "running", "active": True, **kw}  # noqa: E731
    nodes = [node("role:scoring", "scoring", "scoring"), node("role:logging", "logging", "logging"),
             node("pi:dns", "dns", "dns"), node("pi:apt-cache", "apt-cache", "private_internet"), *extra_nodes]
    gid = group["id"]
    edges = [{"id": f"service:{gid}->role:{r}", "source": gid, "target": f"role:{r}", "kind": "service", "verified": True, "label": r}
             for r in ("scoring", "logging")]
    edges.append({"id": f"service:{gid}->pi", "source": gid, "target": "pi", "kind": "service", "verified": True,
                  "label": "private internet", "targets": ["dns", "apt-cache"]})
    return {"host": {}, "nodes": nodes, "groups": [group], "networks": [], "edges": edges + list(extra_edges)}


def team_progression(round_id) -> dict | None:
    """Round 1: team-a's run as the team sees it (two captured VMs on
    "net-1"; the far ends remapped); round 3: its devbox in a dev round;
    round 2 (upcoming): fails (404)."""
    if round_id == 1:
        v0, v1 = _uuid(0), _uuid(1)
        nodes = [
            {"id": v0, "label": "sandbox-vm0", "group": "sandbox", "addresses": ["10.64.0.10"],
             "networks": [{"name": "net-1", "cidr": "10.64.0.0/24"}], "start_vm": True, "captured": True, "captured_at": 40.0},
            {"id": v1, "label": "sandbox-vm1", "group": "sandbox", "addresses": ["10.64.0.11"],
             "networks": [{"name": "net-1", "cidr": "10.64.0.0/24"}], "start_vm": False, "captured": True, "captured_at": 95.0},
        ]
        events = []

        def event(t, kind, node=None, vm=None, **data):
            events.append({"t": t, "kind": kind, "node": node, "vm": vm, "data": data})

        for t, state in [(-60, "preparing"), (-12, "armed"), (0, "running"), (600, "ending"), (640, "finished")]:
            event(t, "state", state=state)
        event(0, "worm_started", node=v0, vm=v0)
        event(40.0, "captured", node=v0, vm=v0)
        event(40.0, "flag_submitted", node="role:scoring", vm=v0, source_vm=v0, source_label="sandbox-vm0", source_inferred=True,
              result="captured", flag_of=v0, flag_of_label="sandbox-vm0", hash_prefix="3f2a9c0d1e2b3c4d", processed=True)
        event(60.4, "log_received", node="role:logging", vm=v0, source_vm=v0, source_label="sandbox-vm0", source_inferred=True,
              stored=True, processed=True, log={"ref": LOG_REF, "size": 48})
        event(95.0, "captured", node=v1, vm=v1)
        event(95.0, "flag_submitted", node="role:scoring", vm=None, source_vm=None, source_label=None, source_inferred=False,
              result="not_counted", flag_of=None, flag_of_label=None, hash_prefix="0000000000000000", processed=True)
        events.sort(key=lambda e: e["t"])
        for i, e in enumerate(events):
            e["id"] = i
        tl = {
            "run": {"uuid": None, "team": "team-a", "round": "R1", "state": "finished", "position": 0,
                    "worm_start_at": "2026-10-24T10:05:00+00:00", "ends_at": "2026-10-24T10:15:00+00:00",
                    "start_vm": "sandbox-vm0", "duration": 600.0, "t_min": -60.0, "t_max": 640.0},
            "nodes": nodes,
            "networks": [{"id": "net-1", "label": "net 1", "cidr": "10.64.0.0/24"}],
            "edges": [{"source": v, "target": "net-1", "network": "net-1"} for v in (v0, v1)],
            "vm_status": {v: [[-40, "running"], [600, "stopped"], [640, "destroyed"]] for v in (v0, v1)},
            "events": events,
            "captures": [{"t": 40.0, "vm": v0}, {"t": 95.0, "vm": v1}],
            "truncated": {"flows": False, "flag_submissions": False, "logs": False},
        }
        return _envelope(
            "team", "live", R1,
            {"t0": "2026-10-24T10:05:00+00:00", "round_start": -300.0, "round_end": 28500.0, "now": 900.0, "finished": False,
             "t_min": -60.0, "t_max": 640.0},
            _scene({"id": "group:sandbox", "kind": "sandbox", "team": None, "label": "Sandbox", "gateway": None, "members": []}),
            tl, "/progression/rounds/1/logs/{ref}/",
            {"available": True, "reason": None, "count": len(team_flows()["flows"]), "truncated": False, "included": False,
             "path": None, "url": "/progression/rounds/1/flows/"},
        )
    if round_id == 3:
        a, b = "d0d00000-0000-4000-8000-00000000000a", "d0d00000-0000-4000-8000-00000000000b"
        devbox = [{"id": a, "name": "devbox-t1-a", "role": "devbox", "status": "running", "active": True, "devbox_role": "a", "team": None},
                  {"id": b, "name": "devbox-t1-b", "role": "devbox", "status": "running", "active": True, "devbox_role": "b", "team": None}]
        events = [
            {"t": 3600.0, "kind": "captured", "node": a, "vm": a, "data": {}},
            {"t": 3600.0, "kind": "flag_submitted", "node": "role:scoring", "vm": None,
             "data": {"result": "captured", "flag_of": a, "flag_of_label": "devbox-t1-a", "hash_prefix": "aaaaaaaaaaaaaaaa",
                      "processed": True, "source_vm": None, "source_label": None, "source_inferred": False}},
            {"t": 7200.0, "kind": "log_received", "node": "role:logging", "vm": None,
             "data": {"stored": True, "processed": True, "log": {"ref": LOG_REF, "size": 48}, "source_vm": None, "source_label": None,
                      "source_inferred": False}},
            {"t": 9000.0, "kind": "captured", "node": b, "vm": b, "data": {}},
        ]
        for i, e in enumerate(events):
            e["id"] = i
        tl = {
            "run": None,
            "nodes": [{"id": n["id"], "label": n["name"], "group": "devbox", "devbox_role": n["devbox_role"], "addresses": [],
                       "networks": [], "start_vm": False, "captured": True, "captured_at": t}
                      for n, t in ((devbox[0], 3600.0), (devbox[1], 9000.0))],
            "networks": [], "edges": [],
            "vm_status": {a: [[0.0, "running"]], b: [[0.0, "running"]]},
            "events": events,
            "captures": [{"t": 3600.0, "vm": a}, {"t": 9000.0, "vm": b}],
            "truncated": {"flows": False, "flag_submissions": False, "logs": False},
        }
        group = {"id": "group:devbox:own", "kind": "devbox", "team": None, "team_name": "team-a", "label": "Devbox team-a",
                 "gateway": None, "members": [a, b]}
        link = {"id": f"lan:{a}->{b}", "source": a, "target": b, "kind": "lan", "verified": True, "label": None}
        return _envelope(
            "team", "dev", {"id": 3, "name": "Dev week", "competition": "development", "start": "2026-10-20T00:00:00+00:00",
                            "end": "2026-10-27T00:00:00+00:00"},
            {"t0": "2026-10-20T00:00:00+00:00", "round_start": 0.0, "round_end": 604800.0, "now": 14400.0, "finished": False,
             "t_min": 0.0, "t_max": 14400.0},
            _scene(group, devbox, [link]), tl, "/progression/rounds/3/logs/{ref}/",
            {"available": False, "reason": "The monitor records the sandbox only: devbox traffic is not recorded.", "count": 0,
             "truncated": False, "included": False, "path": None, "url": None},
        )
    return None


def team_flows() -> dict:
    v0, v1 = _uuid(0), _uuid(1)
    f = []
    for k in range(40):
        f.append({"t": 1 + k * 15, "d": 0.4, "src": v0, "dst": v1, "src_role": "sandbox", "dst_role": "sandbox",
                  "src_name": "sandbox-vm0", "dst_name": "sandbox-vm1", "kind": "lan", "src_port": 40000 + k, "dst_port": 22,
                  "transport": "tcp", "service": "ssh", "conn_state": "SF", "bytes": 900, "pkts": 6, "uid": f"T{k:05d}"})
    f.append({"t": 39.5, "d": 0.6, "src": v0, "dst": "role:scoring", "src_role": "sandbox", "dst_role": "scoring",
              "src_name": "sandbox-vm0", "dst_name": "scoring", "kind": "scoring", "src_port": 41000, "dst_port": 443,
              "transport": "tcp", "service": "ssl", "conn_state": "SF", "bytes": 700, "pkts": 9, "uid": "Tscore"})
    f.append({"t": 70.0, "d": 0.2, "src": v1, "dst": "pi:dns", "src_role": "sandbox", "dst_role": "dns",
              "src_name": "sandbox-vm1", "dst_name": "dns", "kind": "dns", "src_port": 41001, "dst_port": 53,
              "transport": "udp", "service": "dns", "conn_state": "SF", "bytes": 120, "pkts": 2, "uid": "Tdns"})
    f.sort(key=lambda x: x["t"])
    return {"count": len(f), "truncated": False, "available": True, "reason": None, "flows": f}
