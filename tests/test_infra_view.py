"""The topology view's infrastructure model (dashboard/infra_view.py).

A pure module, fed a snapshot shaped like the orchestrator's read-only Proxmox
collection (tests/infra_fixture.py). Checks what the view promises
(docs/visualization.md): VMs as first-class nodes with their status, roles that
fix the layout, no management network ever drawn as a connection, the
database's ingress derived from its firewall, the untrusted groups' reach
(scoring, logging, private internet) derived from the services' firewalls, the
devbox A-B / B-C links, and hover data (groups, rules, bridges).
"""

from __future__ import annotations

import sys

import infra_fixture
from conftest import REPO

sys.path.insert(0, str(REPO / "projects" / "dashboard"))

from dashboard import infra_view  # noqa: E402


def model(data=None):
    return infra_view.analyze(data if data is not None else infra_fixture.snapshot())


def node(m, name):
    return next(n for n in m["nodes"] if n["name"] == name)


def test_every_vm_is_a_node_with_its_role_and_status():
    m = model()
    roles = {n["name"]: n["role"] for n in m["nodes"]}
    assert roles["worms-pve"] == "proxmox"
    for name in ("database", "orchestrator", "dashboard", "scoring", "logging", "monitor", "wazuh"):
        assert roles[name] == name
    assert roles["dns"] == "dns"
    assert roles["apt-cache"] == roles["devpi"] == "private_internet"
    assert roles["sandbox-gw"] == "sandbox_gw"
    assert {roles[f"sandbox-vm{i}"] for i in range(4)} == {"sandbox"}
    assert roles["devbox-t1-gw"] == "devbox_gw"
    assert roles["devbox-t1-b"] == "devbox"
    assert roles["devbox-ssh-gateway"] == "ssh_gateway"

    # Proxmox status: running = active; anything else inactive.
    assert node(m, "database")["active"] is True
    devpi = node(m, "devpi")
    assert devpi["status"] == "stopped" and devpi["active"] is False

    b = node(m, "devbox-t2-b")
    assert (b["team"], b["devbox_role"]) == ("t2", "b")
    # Gateways and wazuh are auxiliary (hidden unless asked for).
    assert node(m, "sandbox-gw")["aux"] and node(m, "wazuh")["aux"]
    assert not node(m, "database")["aux"]


def test_management_networks_are_never_connections():
    m = model()
    management = {b["name"] for b in m["bridges"] if b["kind"] in ("management", "mirror")}
    assert {"vmbr2", "vmbr3", "vmbr4", "vmbr5", "vmbr6", "vmbr7"} <= management
    # No network, no edge and no operational address is on a management bridge.
    assert not {n["bridge"] for n in m["networks"]} & management
    assert all(e.get("label") not in management for e in m["edges"])
    db = node(m, "database")
    assert db["addresses"] == ["10.11.0.16"]  # not the 10.3.0.16 management address
    # ... but the hover card still lists the management NIC, marked as such.
    assert {(nic["bridge"], nic["kind"]) for nic in db["nics"]} == {("vmbr21", "routed"), ("vmbr2", "management")}


def test_database_ingress_is_read_from_its_firewall():
    m = model()
    db = node(m, "database")["id"]
    sources = {e["source"] for e in m["edges"] if e["kind"] == "db" and e["target"] == db}
    by_name = {n["id"]: n["name"] for n in m["nodes"]}
    assert {by_name[s] for s in sources} == {"worms-pve", "orchestrator", "dashboard", "scoring", "logging", "monitor"}
    assert all(e["verified"] for e in m["edges"] if e["kind"] == "db")
    assert m["unresolved"]["database_sources"] == []


def test_database_ingress_follows_the_rules_not_a_fixed_list():
    """Remove a rule and the edge disappears; add one for a VM and it appears."""
    data = infra_fixture.snapshot()
    group = next(g for g in data["cluster_firewall"]["groups"] if g["name"] == "database")
    group["rules"] = [r for r in group["rules"] if r["source"] != "10.12.0.100"]  # drop monitor
    group["rules"].append(infra_fixture._in("5432", "10.2.0.11"))  # a (wrong) rule for dns
    m = model(data)
    names = {n["id"]: n["name"] for n in m["nodes"]}
    sources = {names[e["source"]] for e in m["edges"] if e["kind"] == "db"}
    assert "monitor" not in sources and "dns" in sources


def test_unresolvable_database_sources_are_reported():
    data = infra_fixture.snapshot()
    group = next(g for g in data["cluster_firewall"]["groups"] if g["name"] == "database")
    group["rules"].append(infra_fixture._in("5432", "192.0.2.7"))
    assert model(data)["unresolved"]["database_sources"] == ["192.0.2.7"]


def test_groups_and_their_operational_networks():
    m = model()
    groups = {g["id"]: g for g in m["groups"]}
    assert set(groups) == {"group:sandbox", "group:devbox:t1", "group:devbox:t2"}
    names = {n["id"]: n["name"] for n in m["nodes"]}
    assert names[groups["group:sandbox"]["gateway"]] == "sandbox-gw"
    assert names[groups["group:devbox:t1"]["gateway"]] == "devbox-t1-gw"
    assert [names[i] for i in groups["group:devbox:t1"]["members"]] == ["devbox-t1-a", "devbox-t1-b", "devbox-t1-c"]

    nets = {n["bridge"]: n for n in m["networks"]}
    assert set(nets) == {"vmbr1000", "vmbr1001", "vmbr9000", "vmbr9001", "vmbr9002", "vmbr9003"}
    # Gateways are not members (they are the group's anchor, not a LAN host).
    assert {names[i] for i in nets["vmbr1000"]["members"]} == {"sandbox-vm0", "sandbox-vm1", "sandbox-vm2"}
    assert nets["vmbr9000"]["team"] == "t1" and nets["vmbr9000"]["cidr"] == "10.128.0.0/24"


def test_devbox_links_are_a_b_and_b_c():
    m = model()
    names = {n["id"]: n["name"] for n in m["nodes"]}
    lan = {frozenset((names[e["source"]], names[e["target"]])) for e in m["edges"] if e["kind"] == "lan"}
    assert lan == {
        frozenset(("devbox-t1-a", "devbox-t1-b")), frozenset(("devbox-t1-b", "devbox-t1-c")),
        frozenset(("devbox-t2-a", "devbox-t2-b")), frozenset(("devbox-t2-b", "devbox-t2-c")),
    }
    # No link ever crosses teams.
    for e in m["edges"]:
        if e["kind"] == "lan":
            assert names[e["source"]].split("-")[1] == names[e["target"]].split("-")[1]


def test_sandbox_networks_with_more_hosts_are_hubs():
    m = model()
    names = {n["id"]: n["name"] for n in m["nodes"]}
    hub = {(names[e["source"]], e["target"]) for e in m["edges"] if e["kind"] == "lan_hub"}
    assert ("sandbox-vm2", "net:vmbr1000") in hub and ("sandbox-vm2", "net:vmbr1001") in hub
    assert ("sandbox-vm0", "net:vmbr1001") not in hub


def test_groups_reach_scoring_logging_and_the_private_internet():
    m = model()
    names = {n["id"]: n["name"] for n in m["nodes"]}
    service = {(e["source"], names.get(e["target"], e["target"])): e for e in m["edges"] if e["kind"] == "service"}
    for group in ("group:sandbox", "group:devbox:t1", "group:devbox:t2"):
        for target in ("scoring", "logging", "pi"):
            edge = service[(group, target)]
            # Verified against the firewall: sandbox_api admits sandbox-gw,
            # devbox_api the devbox-gw block, private_internet the services net.
            assert edge["verified"], (group, target)
    assert set(service[("group:sandbox", "pi")]["targets"]) == {"dns", "apt-cache", "devpi"}


def test_service_edges_without_a_firewall_rule_are_marked_unverified():
    data = infra_fixture.snapshot()
    group = next(g for g in data["cluster_firewall"]["groups"] if g["name"] == "devbox_api")
    group["rules"] = []
    m = model(data)
    names = {n["id"]: n["name"] for n in m["nodes"]}
    edges = {(e["source"], names.get(e["target"], e["target"])): e for e in m["edges"] if e["kind"] == "service"}
    assert edges[("group:devbox:t1", "scoring")]["verified"] is False
    assert edges[("group:sandbox", "scoring")]["verified"] is True


def test_no_live_sandbox_still_yields_an_empty_sandbox_group():
    data = infra_fixture.snapshot()
    data["guests"] = [g for g in data["guests"] if g["pool"] != "sandbox"]
    m = model(data)
    sandbox = next(g for g in m["groups"] if g["id"] == "group:sandbox")
    assert sandbox["members"] == [] and sandbox["gateway"] is None
    # The by-design reach is still drawn, unverified (no gateway to check).
    edge = next(e for e in m["edges"] if e["source"] == "group:sandbox" and e["kind"] == "service")
    assert edge["verified"] is False


def test_hover_data_groups_rules_and_bridges():
    m = model()
    scoring = node(m, "scoring")
    assert [(g["name"], g["iface"]) for g in scoring["firewall"]["groups"]] == [
        ("sandbox_api", "net0"), ("devbox_api", "net0"), ("db_client", "net1"), ("ssh", "net2"),
    ]
    rules = scoring["firewall"]["rules"]
    assert {"direction": "in", "action": "ACCEPT", "proto": "tcp", "dport": "443", "source": "10.2.0.2",
            "group": "sandbox_api", "iface": "net0"}.items() <= next(r for r in rules if r["group"] == "sandbox_api").items()
    assert [nic["bridge"] for nic in scoring["nics"]] == ["vmbr23", "vmbr22", "vmbr5"]
    assert scoring["firewall"]["policy_in"] == "DROP" and scoring["firewall"]["enabled"]
    # Security groups are exported for the round view's historical sandbox VMs.
    assert m["security_groups"]["sandbox"]["rules"][0]["source"] == "10.64.0.0/10"


def test_address_index_maps_service_addresses_to_nodes():
    index = infra_view.address_index(model())
    assert index["10.2.0.14"]["role"] == "scoring"
    assert index["10.2.0.15"]["role"] == "logging"
    assert index["10.2.0.20"]["name"] == "apt-cache"
    assert index["10.11.0.1"]["role"] == "proxmox"
    # Management addresses of VMs are not operational: not in the index.
    assert "10.5.0.14" not in index


def test_firewall_spec_resolution():
    by_ip = {"10.2.0.2": "gw", "10.2.0.33": "d1", "10.2.0.40": "d2", "10.11.0.16": "db"}
    r = infra_view.Resolver(by_ip, aliases={"gws": "10.2.0.32/27"}, ipsets={"svc": [{"cidr": "10.2.0.2"}, {"cidr": "10.2.0.40", "nomatch": True}]})
    assert r.nodes("10.2.0.2")[0] == {"gw"}
    assert r.nodes("10.2.0.32/27")[0] == {"d1", "d2"}
    assert r.nodes("dc/gws")[0] == {"d1", "d2"}
    assert r.nodes("+dc/svc")[0] == {"gw"}
    assert r.nodes("10.2.0.30-10.2.0.35")[0] == {"d1"}
    assert r.nodes("10.2.0.2,10.11.0.16")[0] == {"gw", "db"}
    assert r.nodes("nonsense")[1] == ["nonsense"]


def test_admits_ports_ranges_and_macros():
    rule = lambda **kw: {"direction": "in", "action": "ACCEPT", "proto": "tcp", "dport": "", "macro": "", **kw}  # noqa: E731
    assert infra_view.admits(rule(dport="5432"), 5432)
    assert infra_view.admits(rule(dport="5000:6000"), 5432)
    assert infra_view.admits(rule(dport="22,5432"), 5432)
    assert not infra_view.admits(rule(dport="443"), 5432)
    assert not infra_view.admits(rule(proto="udp", dport="5432"), 5432)
    assert infra_view.admits(rule(proto="", macro="PostgreSQL"), 5432)
    assert not infra_view.admits(rule(dport="5432", action="DROP"), 5432)
    assert not infra_view.admits(rule(dport="5432", direction="out"), 5432)


def test_bridge_kinds_follow_the_comment():
    kind = infra_view.bridge_kind
    assert kind("vmbr21", "Core: database and orchestrator.") == "routed"
    assert kind("vmbr7", "Management: devbox SSH fabric (SSH gateway <-> devbox VMs). Ports isolated.") == "management"
    assert kind("vmbr1003", "") == "sandbox"
    assert kind("vmbr9010", "") == "devbox"
    assert kind("vmbr23", "Sandbox services: scoring/logging") == "services"
    # Renaming the comment moves the bridge, no code change.
    assert kind("vmbr21", "Management: repurposed") == "management"
    assert kind("vmbr0", "") == "uplink"


def test_empty_snapshot_is_harmless():
    m = infra_view.analyze({})
    assert [n["id"] for n in m["nodes"]] == ["proxmox"]
    assert m["nodes"][0]["active"] is False
    assert m["edges"] == [] and m["networks"] == []
