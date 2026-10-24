"""The infrastructure model behind the dashboard's TOPOLOGY view (staff only).

Pure module (no Django, no database): it turns the orchestrator's read-only
Proxmox snapshot (orchestrator/proxmox_inventory.py, stored in
`topology_snapshots`) into what the visualization draws:

    {
      "host":     {...},  # the Proxmox node: name, version, firewall daemons
      "nodes":    [...],  # every VM, plus the Proxmox host, as first-class nodes
      "groups":   [...],  # the untrusted environments: the sandbox, each team's devbox
      "networks": [...],  # the operational L2 segments inside those groups
      "edges":    [...],  # operational connections only -- never management
      "bridges":  [...],  # every bridge with its kind (for the hover cards)
      "security_groups": {...},  # cluster security groups (name -> rules)
      "index":    {...},  # address -> node, role -> nodes (for the round timeline)
    }

What is drawn (docs/visualization.md):

* nodes carry their role (which fixes their place in the layout), their
  Proxmox status (`running` = active) and, for the hover card, every NIC with
  its bridge and the bridge's kind, the attached security groups and the
  expanded firewall rules. Management NICs stay on the hover card but never
  become edges.
* edges, derived from the firewall where the firewall says it:
    - `db`: the database VM's accepted inbound tcp/5432 rules, each source
      resolved to a node (so the picture shows exactly who the database's
      firewall lets in -- including the Proxmox host's round agent);
    - `service`: each untrusted group -> scoring, logging and the private
      internet, from those VMs' inbound rules whose source resolves to the
      group's gateway (sandbox-gw, the team's devbox-gw). When no gateway is
      live (no run in progress) the edge is drawn "by design" (verified=False);
    - `lan` / `lan_hub`: the operational networks inside a group (sandbox
      networks; the devbox A-B / B-C links).

Nothing here is trusted input from the sandbox -- it is Proxmox configuration
-- but it is sensitive (addresses, rules), hence staff only.
"""

from __future__ import annotations

import ipaddress
import re

PROXMOX = "proxmox"
PI_COLUMN = "pi"  # the private-internet column (a layout target, not a VM)
DB_PORT = 5432
API_PORT = 443

NET_KEY = re.compile(r"^net(\d+)$")
SANDBOX_BRIDGE = re.compile(r"^vmbr1\d{3,}$")
DEVBOX_BRIDGE = re.compile(r"^vmbr9\d{3,}$")
DEVBOX_NAME = re.compile(r"^devbox-(?P<team>[a-z0-9-]+?)-(?P<role>[abc])$")
DEVBOX_GW_NAME = re.compile(r"^devbox-(?P<team>[a-z0-9-]+?)-gw$")

# Infrastructure VMs are recognised by their OpenTofu name (= the VM name).
INFRA_ROLES = {"orchestrator", "dashboard", "database", "scoring", "logging", "monitor", "wazuh"}
# Plumbing rather than the picture the operator asked for; the view hides
# these unless "auxiliary VMs" is switched on.
AUX_ROLES = {"wazuh", "sandbox_gw", "devbox_gw", "ssh_gateway", "other"}
UNTRUSTED_ROLES = {"sandbox", "devbox"}
GATEWAY_ROLES = {"sandbox_gw", "devbox_gw"}
PI_ROLES = {"private_internet", "dns"}

# Bridge kinds that never become edges (and whose addresses are not a node's
# operational addresses).
NON_OPERATIONAL = {"management", "mirror", "uplink"}

MACRO_PORTS = {
    "postgresql": ("tcp", "5432"),
    "ssh": ("tcp", "22"),
    "https": ("tcp", "443"),
    "http": ("tcp", "80"),
    "dns": ("", "53"),
}


# ---------------------------------------------------------------------------
# Small parsers
# ---------------------------------------------------------------------------


def parse_kv(value) -> dict:
    """'virtio=AA:..,bridge=vmbr2,firewall=1' -> {'virtio': 'AA:..', ...}."""
    result = {}
    for part in str(value or "").split(","):
        if part:
            key, sep, val = part.partition("=")
            result[key.strip()] = val.strip() if sep else ""
    return result


def flag(value, default=False) -> bool:
    if value is None or value == "":
        return default
    return str(value) not in ("0", "false", "False")


def tag_list(value) -> list[str]:
    return [t for t in re.split(r"[;, ]+", str(value or "")) if t]


def _interface(value):
    try:
        return ipaddress.ip_interface(value)
    except ValueError:
        return None


def bridge_kind(name: str, comment: str = "") -> str:
    """Classify a bridge from its live name and comment (set by OpenTofu:
    opentofu/modules/network, the sandbox and devbox stacks).

    sandbox / devbox (generated vmbr1NNN / vmbr9NNN), services, routed (dmz,
    core, ingest, siem), management (every maintenance bridge and the devbox
    SSH fabric), mirror, uplink, other."""
    c = (comment or "").strip().lower()
    if SANDBOX_BRIDGE.match(name or "") or c.startswith("sandbox network"):
        return "sandbox"
    if DEVBOX_BRIDGE.match(name or "") or c.startswith("devbox network"):
        return "devbox"
    if c.startswith("management"):
        return "management"
    if c.startswith("sandbox services"):
        return "services"
    if c.startswith("mirror"):
        return "mirror"
    if c.startswith(("dmz", "core", "ingest", "siem")):
        return "routed"
    if name == "vmbr0":
        return "uplink"
    return "other"


# ---------------------------------------------------------------------------
# Guests
# ---------------------------------------------------------------------------


def classify(guest) -> tuple[str, dict]:
    """(role, extra) for a guest; extra carries a devbox's team and A/B/C."""
    name = str(guest.get("name") or "")
    tags = set(tag_list(guest.get("tags")))
    pool = guest.get("pool") or ""

    if "sandbox-gw" in tags or name == "sandbox-gw":
        return "sandbox_gw", {}
    m = DEVBOX_GW_NAME.match(name)
    if "devbox-gw" in tags or m:
        return "devbox_gw", {"team": m.group("team") if m else None}
    if "devbox-ssh-gateway" in tags or name == "devbox-ssh-gateway":
        return "ssh_gateway", {}
    m = DEVBOX_NAME.match(name)
    if m or pool == "devbox" or "devbox" in tags:
        return "devbox", {"team": m.group("team") if m else None, "devbox_role": m.group("role") if m else None}
    if pool == "sandbox" or ("untrusted" in tags and name.startswith("sandbox")):
        return "sandbox", {}
    if name == "dns":
        return "dns", {}
    if pool == "private_internet" or "private_internet" in tags:
        return "private_internet", {}
    if name in INFRA_ROLES:
        return name, {}
    return "other", {}


def guest_nics(guest, bridges: dict) -> list[dict]:
    config = guest.get("config", {}) or {}
    nics = []
    for key in sorted(config, key=lambda k: (len(k), k)):
        match = NET_KEY.match(key)
        if not match:
            continue
        net = parse_kv(config[key])
        index = match.group(1)
        ipcfg = net if guest.get("type") == "lxc" else parse_kv(config.get(f"ipconfig{index}", ""))
        ip = ipcfg.get("ip", "")
        iface = _interface(ip) if ip and ip not in ("dhcp", "manual") else None
        bridge = net.get("bridge", "")
        nics.append(
            {
                "name": key,
                "bridge": bridge,
                "kind": bridges.get(bridge, {}).get("kind") or bridge_kind(bridge),
                "ip": str(iface.ip) if iface else "",
                "cidr": str(iface) if iface else ip,
                "network": str(iface.network) if iface else "",
                "firewall": flag(net.get("firewall")),
                "link_down": flag(net.get("link_down")),
            }
        )
    return nics


def _rule(rule, group=None, iface=None) -> dict:
    return {
        "direction": rule.get("type"),
        "action": str(rule.get("action", "")).upper(),
        "proto": str(rule.get("proto") or "").lower(),
        "dport": str(rule.get("dport") or ""),
        "sport": str(rule.get("sport") or ""),
        "source": str(rule.get("source") or ""),
        "dest": str(rule.get("dest") or ""),
        "macro": str(rule.get("macro") or ""),
        "iface": iface if iface is not None else str(rule.get("iface") or ""),
        "group": group,
        "comment": str(rule.get("comment") or ""),
    }


def expand_rules(rules, groups: dict) -> tuple[list[dict], list[dict]]:
    """Flatten a guest's rules: (attached security groups, effective rules).

    Group references (`type: group`) become the group's rules, tagged with the
    group name and the NIC the group is attached on."""
    attached, effective = [], []
    for rule in rules or []:
        if not flag(rule.get("enable"), default=True):
            continue
        if rule.get("type") == "group":
            name = rule.get("action")
            iface = str(rule.get("iface") or "")
            group = groups.get(name, {})
            attached.append({"name": name, "iface": iface, "comment": str(rule.get("comment") or group.get("comment", ""))})
            for inner in group.get("rules", []) or []:
                if flag(inner.get("enable"), default=True) and inner.get("type") in ("in", "out"):
                    effective.append(_rule(inner, group=name, iface=iface or str(inner.get("iface") or "")))
        elif rule.get("type") in ("in", "out"):
            effective.append(_rule(rule))
    return attached, effective


def admits(rule, port=None, proto="tcp") -> bool:
    """Is `rule` an inbound ACCEPT for proto/port (port None: any port)?"""
    if rule["direction"] != "in" or rule["action"] != "ACCEPT":
        return False
    rproto, dport = rule["proto"], rule["dport"]
    macro = rule["macro"].lower()
    if macro:
        if macro not in MACRO_PORTS:
            return port is None
        rproto, dport = MACRO_PORTS[macro]
    if port is None:
        return True
    if rproto and rproto != proto:
        return False
    if not dport:
        return True
    for part in dport.split(","):
        lo, _, hi = part.strip().partition(":")
        if lo.isdigit() and int(lo) <= port <= int(hi if hi.isdigit() else lo):
            return True
    return False


class Resolver:
    """Resolve a firewall source/dest spec (IP, CIDR, comma list, alias,
    +ipset) to the node ids whose operational addresses it covers."""

    def __init__(self, by_ip: dict, aliases: dict | None = None, ipsets: dict | None = None):
        self.by_ip = {ipaddress.ip_address(ip): node for ip, node in by_ip.items()}
        self.aliases = aliases or {}
        self.ipsets = ipsets or {}

    def nodes(self, spec: str, depth=0) -> tuple[set, list]:
        found, unresolved = set(), []
        if depth > 4:
            return found, [spec]
        for token in [t.strip() for t in str(spec or "").split(",") if t.strip()]:
            if token.startswith("+"):
                members = self.ipsets.get(token[1:].split("/")[-1])
                if members is None:
                    unresolved.append(token)
                for member in members or []:
                    if not member.get("nomatch"):
                        f, u = self.nodes(member.get("cidr", ""), depth + 1)
                        found |= f
                        unresolved += u
                continue
            if "-" in token and not token[0].isalpha():
                lo, _, hi = token.partition("-")
                try:
                    lo_ip, hi_ip = ipaddress.ip_address(lo), ipaddress.ip_address(hi)
                except ValueError:
                    unresolved.append(token)
                    continue
                matched = {node for ip, node in self.by_ip.items() if lo_ip <= ip <= hi_ip}
            else:
                try:
                    net = ipaddress.ip_network(token, strict=False)
                except ValueError:
                    alias = self.aliases.get(token.split("/")[-1])
                    if alias:
                        f, u = self.nodes(alias, depth + 1)
                        found |= f
                        unresolved += u
                    else:
                        unresolved.append(token)
                    continue
                matched = {node for ip, node in self.by_ip.items() if ip in net}
            if matched:
                found |= matched
            else:
                unresolved.append(token)
        return found, unresolved


# ---------------------------------------------------------------------------
# The model
# ---------------------------------------------------------------------------


def _bridges(data) -> dict:
    bridges = {}
    for net in data.get("networks", []) or []:
        name = str(net.get("iface", ""))
        if net.get("type") not in ("bridge", "OVSBridge") and not name.startswith("vmbr"):
            continue
        comment = net.get("comments", "") or ""
        bridges[name] = {
            "name": name,
            "comment": comment.strip(),
            "cidr": net.get("cidr") or net.get("address") or "",
            "kind": bridge_kind(name, comment),
            "active": flag(net.get("active"), default=True),
        }
    return bridges


def _host_node(data, bridges, groups) -> dict:
    """The Proxmox node itself. Its addresses are its bridge addresses (it is
    what connects to the database as the round agent / admin role)."""
    nics, addresses = [], []
    for b in sorted(bridges.values(), key=lambda b: b["name"]):
        iface = _interface(b["cidr"]) if b["cidr"] else None
        if iface:
            addresses.append(str(iface.ip))
            nics.append(
                {"name": b["name"], "bridge": b["name"], "kind": b["kind"], "ip": str(iface.ip),
                 "cidr": str(iface), "network": str(iface.network), "firewall": True, "link_down": False}
            )  # fmt: skip
    node_fw = data.get("node_firewall", {}) or {}
    cluster = data.get("cluster_firewall", {}) or {}
    attached, rules = expand_rules(node_fw.get("rules"), groups)
    return {
        "id": PROXMOX,
        "name": data.get("node") or "proxmox",
        "role": PROXMOX,
        "status": "running" if data.get("node") else "unknown",
        "active": bool(data.get("node")),
        "vmid": None,
        "pool": "",
        "tags": [],
        "aux": False,
        "team": None,
        "addresses": addresses,
        "nics": nics,
        "firewall": {
            "enabled": flag((cluster.get("options") or {}).get("enable"))
            and flag((node_fw.get("options") or {}).get("enable"), default=True),
            "policy_in": (cluster.get("options") or {}).get("policy_in", ""),
            "policy_out": (cluster.get("options") or {}).get("policy_out", ""),
            "groups": attached,
            "rules": rules,
        },
    }


def _guest_node(guest, bridges, groups) -> dict:
    role, extra = classify(guest)
    nics = guest_nics(guest, bridges)
    fw = guest.get("firewall", {}) or {}
    options = fw.get("options", {}) or {}
    attached, rules = expand_rules(fw.get("rules"), groups)
    status = guest.get("status") or "unknown"
    return {
        "id": f"vm:{guest.get('vmid')}",
        "name": guest.get("name") or f"vm{guest.get('vmid')}",
        "role": role,
        "status": status,
        "active": status == "running",
        "vmid": guest.get("vmid"),
        "type": guest.get("type"),
        "pool": guest.get("pool") or "",
        "tags": tag_list(guest.get("tags")),
        "aux": role in AUX_ROLES,
        "team": extra.get("team"),
        "devbox_role": extra.get("devbox_role"),
        "addresses": [n["ip"] for n in nics if n["ip"] and n["kind"] not in NON_OPERATIONAL],
        "nics": nics,
        "firewall": {
            "enabled": flag(options.get("enable")),
            "policy_in": options.get("policy_in", ""),
            "policy_out": options.get("policy_out", ""),
            "ipfilter": flag(options.get("ipfilter")),
            "macfilter": flag(options.get("macfilter"), default=True),
            "groups": attached,
            "rules": rules,
            "aliases": fw.get("aliases", {}) or {},
            "ipsets": fw.get("ipsets", {}) or {},
        },
    }


def analyze(data: dict) -> dict:
    data = data or {}
    cluster = data.get("cluster_firewall", {}) or {}
    groups = {g.get("name"): g for g in cluster.get("groups", []) or []}
    bridges = _bridges(data)

    nodes = [_host_node(data, bridges, groups)]
    nodes += [_guest_node(g, bridges, groups) for g in data.get("guests", []) or []]

    by_id = {n["id"]: n for n in nodes}
    by_role: dict[str, list[str]] = {}
    for n in nodes:
        by_role.setdefault(n["role"], []).append(n["id"])
    by_ip = {}
    for n in nodes:
        for ip in n["addresses"]:
            by_ip.setdefault(ip, n["id"])

    def resolver_for(node):
        fw = node["firewall"]
        return Resolver(
            by_ip,
            {**(cluster.get("aliases") or {}), **(fw.get("aliases") or {})},
            {**(cluster.get("ipsets") or {}), **(fw.get("ipsets") or {})},
        )

    def ingress(node, port=None, proto="tcp"):
        """[(source node id, rule)] admitted to `node` (inbound ACCEPT rules
        with an explicit source; an any-source rule is not fanned out)."""
        resolver = resolver_for(node)
        found, unresolved = [], []
        for rule in node["firewall"]["rules"]:
            if not admits(rule, port, proto) or not rule["source"]:
                continue
            sources, missing = resolver.nodes(rule["source"])
            unresolved += missing
            found += [(src, rule) for src in sorted(sources)]
        return found, unresolved

    edges, seen = [], set()

    def add_edge(source, target, kind, **extra):
        key = (source, target, kind)
        if key in seen or source == target:
            return
        seen.add(key)
        edges.append({"id": f"{kind}:{source}->{target}", "source": source, "target": target, "kind": kind, **extra})

    # --- database ingress, from the database VM's own firewall --------------
    unresolved_db = []
    for db_id in by_role.get("database", []):
        found, unresolved = ingress(by_id[db_id], DB_PORT)
        unresolved_db += unresolved
        for src, rule in found:
            add_edge(src, db_id, "db", label=f"tcp/{DB_PORT}", group=rule["group"], comment=rule["comment"], verified=True)

    # --- the untrusted groups ------------------------------------------------
    group_list = []
    gateways = {n["id"]: n for n in nodes if n["role"] in GATEWAY_ROLES}
    sandbox_gw = next((g["id"] for g in gateways.values() if g["role"] == "sandbox_gw"), None)
    group_list.append(
        {
            "id": "group:sandbox",
            "kind": "sandbox",
            "team": None,
            "label": "Sandbox",
            "gateway": sandbox_gw,
            "members": [n["id"] for n in nodes if n["role"] == "sandbox"],
        }
    )
    teams = sorted({n["team"] for n in nodes if n["role"] in ("devbox", "devbox_gw") and n.get("team")})
    for team in teams:
        members = sorted(
            (n for n in nodes if n["role"] == "devbox" and n.get("team") == team),
            key=lambda n: (n.get("devbox_role") or "", n["name"]),
        )
        gateway = next((g["id"] for g in gateways.values() if g["role"] == "devbox_gw" and g.get("team") == team), None)
        group_list.append(
            {"id": f"group:devbox:{team}", "kind": "devbox", "team": team, "label": f"Devbox {team}",
             "gateway": gateway, "members": [m["id"] for m in members]}
        )  # fmt: skip
    group_of_gateway = {g["gateway"]: g["id"] for g in group_list if g["gateway"]}

    # --- operational networks inside the groups -------------------------------
    networks = []
    for bridge in sorted(bridges.values(), key=lambda b: b["name"]):
        if bridge["kind"] not in UNTRUSTED_ROLES:
            continue
        members = [
            n for n in nodes
            if n["role"] in UNTRUSTED_ROLES and any(nic["bridge"] == bridge["name"] for nic in n["nics"])
        ]  # fmt: skip
        if not members:
            continue
        cidr = next(
            (nic["network"] for m in members for nic in m["nics"] if nic["bridge"] == bridge["name"] and nic["network"]),
            "",
        )
        team = next((m.get("team") for m in members if m.get("team")), None) if bridge["kind"] == "devbox" else None
        net_id = f"net:{bridge['name']}"
        networks.append(
            {
                "id": net_id,
                "bridge": bridge["name"],
                "kind": bridge["kind"],
                "team": team,
                "group": f"group:devbox:{team}" if bridge["kind"] == "devbox" else "group:sandbox",
                "cidr": cidr,
                "comment": bridge["comment"],
                "members": [m["id"] for m in members],
            }
        )
        # A devbox segment with two members is a direct link (A-B, B-C);
        # sandbox networks (and anything larger) are drawn as a hub.
        if bridge["kind"] == "devbox" and len(members) == 2:
            add_edge(members[0]["id"], members[1]["id"], "lan", network=net_id, label=bridge["name"], verified=True)
        else:
            for m in members:
                add_edge(m["id"], net_id, "lan_hub", network=net_id, label=bridge["name"], verified=True)

    # --- what each group reaches through its gateway -------------------------
    services = [by_id[i] for i in by_role.get("scoring", []) + by_role.get("logging", [])]
    pi_nodes = [n for n in nodes if n["role"] in PI_ROLES]
    reaches: dict[tuple, set] = {}
    for service in services:
        for src, _rule_ in ingress(service, API_PORT)[0]:
            if src in group_of_gateway:
                reaches.setdefault((group_of_gateway[src], service["id"]), set()).add(service["name"])
    for pi in pi_nodes:
        for src, _rule_ in ingress(pi, None)[0]:
            if src in group_of_gateway:
                reaches.setdefault((group_of_gateway[src], PI_COLUMN), set()).add(pi["name"])
    for group in group_list:
        for service in services:
            key = (group["id"], service["id"])
            add_edge(group["id"], service["id"], "service", label=service["role"], verified=key in reaches)
        if pi_nodes:
            key = (group["id"], PI_COLUMN)
            add_edge(
                group["id"], PI_COLUMN, "service", label="private internet",
                verified=key in reaches, targets=sorted(reaches.get(key, [])),
            )  # fmt: skip

    return {
        "host": {
            "node": data.get("node"),
            "version": data.get("version"),
            "firewall_services": data.get("services", []),
            "errors": data.get("errors", []),
        },
        "nodes": nodes,
        "groups": group_list,
        "networks": networks,
        "edges": edges,
        "bridges": sorted(bridges.values(), key=lambda b: b["name"]),
        "security_groups": {
            name: {
                "comment": g.get("comment", ""),
                "rules": [_rule(r, group=name) for r in g.get("rules", []) or [] if r.get("type") in ("in", "out")],
            }
            for name, g in groups.items()
        },
        "unresolved": {"database_sources": sorted(set(unresolved_db))},
        "index": {"by_ip": by_ip, "by_role": by_role},
    }


def address_index(model: dict) -> dict:
    """{ip: {"node": id, "role": role, "name": name}} for the timeline, which
    maps the far end of a sandbox VM's flows to an infrastructure node."""
    by_id = {n["id"]: n for n in model.get("nodes", [])}
    return {
        ip: {"node": node_id, "role": by_id[node_id]["role"], "name": by_id[node_id]["name"]}
        for ip, node_id in (model.get("index", {}).get("by_ip") or {}).items()
        if node_id in by_id
    }
