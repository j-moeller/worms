"""Unit tests for the topology analyzer's network-security-zone derivation.

`dashboard/topology.py` is a pure module (no Django, no database), so these
run everywhere: they feed it bridge dicts shaped like the orchestrator's
read-only Proxmox snapshot and check that zones are grouped the way the
infrastructure actually defines them (opentofu/modules/network/main.tf and
opentofu/stacks/sandbox/main.tf). The derivation is from the live comment/name,
not a hardcoded map, so renaming a comment must move the zone with it.
"""

from __future__ import annotations

import sys

from conftest import REPO

sys.path.insert(0, str(REPO / "projects" / "dashboard"))

from dashboard import topology  # noqa: E402

# The bridge comments exactly as opentofu/modules/network/main.tf sets them,
# plus a generated sandbox bridge (opentofu/stacks/sandbox/main.tf) and the
# three management bridges that must collapse into one "management" zone.
INFRA_BRIDGES = [
    {"id": "vmbr20", "cidr": "10.10.0.1/24", "ports": "", "comment": "DMZ: dashboard (published to the internet by host DNAT on 443)."},
    {"id": "vmbr21", "cidr": "10.11.0.1/24", "ports": "", "comment": "Core: database and orchestrator."},
    {"id": "vmbr22", "cidr": "10.12.0.1/24", "ports": "", "comment": "Ingest: database leg of scoring/logging (routed to database:5432 only)."},
    {"id": "vmbr24", "cidr": "10.13.0.1/24", "ports": "", "comment": "SIEM: wazuh (receives syslog from every infrastructure VM, routed tcp 514 only)."},
    {"id": "vmbr23", "cidr": "", "ports": "", "comment": "Sandbox services: scoring/logging (sandbox leg), private internet, DNS, sandbox-gw. No host IP, no uplink."},
    {"id": "vmbr3", "cidr": "", "ports": "", "comment": "Mirror of the sandbox networks for the monitor VM. No host IP."},
    {"id": "vmbr2", "cidr": "10.3.0.1/24", "ports": "", "comment": "Management (SSH host -> VM) for database and orchestrator. Ports isolated."},
    {"id": "vmbr5", "cidr": "10.5.0.1/24", "ports": "", "comment": "Management (SSH host -> VM) for dashboard, scoring, logging, DNS, private internet. Ports isolated."},
    {"id": "vmbr4", "cidr": "10.4.0.1/16", "ports": "", "comment": "Management (SSH host -> VM) for sandbox VMs and sandbox-gw. Ports isolated."},
    {"id": "vmbr1000", "cidr": "", "ports": "", "comment": "Sandbox network dmz (no host IP)"},
    {"id": "vmbr1001", "cidr": "", "ports": "", "comment": "Sandbox network core (no host IP)"},
]


def test_zone_of_matches_the_configured_comment():
    cases = {
        "vmbr20": ("dmz", "DMZ", "routed"),
        "vmbr21": ("core", "Core", "routed"),
        "vmbr22": ("ingest", "Ingest", "routed"),
        "vmbr24": ("siem", "SIEM", "routed"),
        "vmbr23": ("services", "Sandbox services", "services"),
        "vmbr3": ("mirror", "Mirror", "passive"),
        "vmbr2": ("management", "Management", "management"),
        "vmbr5": ("management", "Management", "management"),
        "vmbr4": ("management", "Management", "management"),
        "vmbr1000": ("sandbox", "Sandbox", "sandbox"),
        "vmbr1001": ("sandbox", "Sandbox", "sandbox"),
    }
    by_id = {b["id"]: b for b in INFRA_BRIDGES}
    for bid, expected in cases.items():
        assert topology.zone_of(by_id[bid]) == expected, bid


def test_zones_of_groups_and_tags_bridges():
    bridges = [dict(b) for b in INFRA_BRIDGES]
    zones = topology.zones_of(bridges)
    by_id = {z["id"]: z for z in zones}

    # The three management bridges collapse into one zone; the two sandbox
    # networks into one; everything else is its own zone.
    assert set(by_id) == {"dmz", "core", "ingest", "siem", "services", "mirror", "management", "sandbox"}
    assert by_id["management"]["bridges"] == ["vmbr2", "vmbr5", "vmbr4"]
    assert by_id["sandbox"]["bridges"] == ["vmbr1000", "vmbr1001"]

    # Every bridge is tagged with its zone id (the frontend maps NIC -> zone).
    for b in bridges:
        assert b["zone"] == topology.zone_of(b)[0]

    # The canonical display order is honoured.
    assert [z["id"] for z in zones] == ["dmz", "core", "ingest", "siem", "services", "sandbox", "mirror", "management"]


def test_zone_follows_the_comment_not_the_name():
    """The whole point of deriving from live data: change the comment and the
    zone moves, with no code change."""
    renamed = {"id": "vmbr20", "cidr": "10.10.0.1/24", "ports": "", "comment": "Core: repurposed."}
    assert topology.zone_of(renamed)[0] == "core"


def test_unknown_bridge_stands_alone():
    isolated = {"id": "vmbr9", "cidr": "", "ports": "", "comment": ""}
    uplinked = {"id": "vmbr0", "cidr": "", "ports": "eno1", "comment": ""}
    routed = {"id": "vmbr8", "cidr": "192.168.1.1/24", "ports": "", "comment": ""}
    assert topology.zone_of(isolated) == ("vmbr9", "vmbr9", "isolated")
    assert topology.zone_of(uplinked) == ("vmbr0", "vmbr0", "uplink")
    assert topology.zone_of(routed) == ("vmbr8", "vmbr8", "routed")


def test_analyze_emits_zones_and_per_bridge_zone():
    """End to end through analyze(): zones appear in the output and every
    bridge carries its zone id."""
    data = {
        "node": "worms-pve",
        "host": {"api_port": 8006},
        "networks": [
            {"iface": b["id"], "type": "bridge", "address": b["cidr"].split("/")[0] if b["cidr"] else None,
             "cidr": b["cidr"], "comments": b["comment"], "active": True}
            for b in INFRA_BRIDGES
        ],
        "guests": [],
        "cluster_firewall": {"options": {"enable": 1, "policy_in": "DROP", "policy_out": "ACCEPT"}},
    }
    out = topology.analyze(data)
    zone_ids = {z["id"] for z in out["zones"]}
    assert "sandbox" in zone_ids and "management" in zone_ids
    assert all("zone" in b for b in out["bridges"])
    sandbox = next(z for z in out["zones"] if z["id"] == "sandbox")
    assert sandbox["kind"] == "sandbox" and len(sandbox["bridges"]) == 2
