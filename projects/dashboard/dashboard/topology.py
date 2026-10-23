"""Turn a raw Proxmox topology snapshot into a graph the frontend can draw.

The snapshot (core.models.TopologySnapshot.data) is collected by the
orchestrator (orchestrator/proxmox_inventory.py). This module is pure: it
parses guest NICs, expands firewall security groups and evaluates the Proxmox
firewall to answer "which endpoint may open a connection to which other
endpoint on which port".

Evaluation model (mirrors the Proxmox VE firewall, simplified):

- A guest NIC is filtered only if the datacenter firewall is enabled, the
  guest firewall is enabled AND the NIC has firewall=1. Otherwise everything
  passes that NIC.
- Rules are evaluated top-down, first match wins; security groups are expanded
  in place; no match falls through to the guest's policy_in / policy_out.
- A connection A -> B needs A's egress AND B's ingress to accept it.
- The Proxmox host ingress is evaluated from the node + datacenter rules and
  the datacenter policy_in. Proxmox additionally lets the `management` IPSet
  (or, if absent, the auto-detected `local_network`) reach 22/8006/... ; the
  latter is not visible through the API, so such verdicts become "unknown".
- Only stateful TCP/UDP/ICMP connection setup is modelled (no sport, no
  ICMP types, no IPv6, no VLAN-aware bridge semantics beyond the tag).
"""

import ipaddress
import re

HOST = "host"

ALLOW = "allow"
DENY = "deny"
UNKNOWN = "unknown"

# Proxmox firewall macros (subset of `pve-firewall` macros, enough for the
# rules used in this platform plus common ones). name -> [(proto, dport)]
MACROS = {
    "ssh": [("tcp", "22")],
    "http": [("tcp", "80")],
    "https": [("tcp", "443")],
    "web": [("tcp", "80"), ("tcp", "443")],
    "dns": [("udp", "53"), ("tcp", "53")],
    "ntp": [("udp", "123")],
    "ping": [("icmp", "")],
    "postgresql": [("tcp", "5432")],
    "mysql": [("tcp", "3306")],
    "smtp": [("tcp", "25")],
    "smtps": [("tcp", "465")],
    "submission": [("tcp", "587")],
    "imap": [("tcp", "143")],
    "imaps": [("tcp", "993")],
    "pop3": [("tcp", "110")],
    "pop3s": [("tcp", "995")],
    "ftp": [("tcp", "21")],
    "telnet": [("tcp", "23")],
    "rdp": [("tcp", "3389")],
    "vnc": [("tcp", "5900:5999")],
    "ldap": [("tcp", "389")],
    "ldaps": [("tcp", "636")],
    "snmp": [("udp", "161:162"), ("tcp", "161")],
    "syslog": [("udp", "514"), ("tcp", "514")],
    "rsync": [("tcp", "873")],
    "squid": [("tcp", "3128")],
    "git": [("tcp", "9418")],
}

# Service names Proxmox accepts in dport (from /etc/services), subset.
SERVICE_PORTS = {
    "ssh": 22,
    "http": 80,
    "https": 443,
    "domain": 53,
    "ntp": 123,
    "postgresql": 5432,
    "smtp": 25,
    "ftp": 21,
    "telnet": 23,
}

# Ports Proxmox opens on the host for the management IPSet / local_network.
MANAGEMENT_PORTS = [("tcp", "8006"), ("tcp", "22"), ("tcp", "5900:5999"), ("tcp", "3128")]

NET_KEY = re.compile(r"^net(\d+)$")

# Network security zones. Each infrastructure bridge carries a comment whose
# leading phrase names its zone (set in opentofu/modules/network/main.tf);
# sandbox bridges are generated per round as vmbr1NNN with a "Sandbox network …"
# comment (opentofu/stacks/sandbox/main.tf). The zone of a bridge is therefore
# derived from the *live* Proxmox data (comment, then name) rather than from a
# hardcoded map, so the view always tracks what is actually configured: change
# a bridge's comment and its zone label follows. Zones that span several bridges
# (management has three, sandbox one per generated network) are grouped under a
# single zone. See docs/visualization.md.
#
# (leading word of the comment, lowercased) -> (zone id, label, kind)
ZONE_BY_COMMENT = (
    ("dmz", "dmz", "DMZ", "routed"),
    ("core", "core", "Core", "routed"),
    ("ingest", "ingest", "Ingest", "routed"),
    ("siem", "siem", "SIEM", "routed"),
    ("sandbox services", "services", "Sandbox services", "services"),
    ("mirror", "mirror", "Mirror", "passive"),
    ("management", "management", "Management", "management"),
)
# Display order for the known zones (others sort after, alphabetically).
ZONE_ORDER = {z[1]: i for i, z in enumerate(
    (("", "dmz"), ("", "core"), ("", "ingest"), ("", "siem"),
     ("", "services"), ("", "sandbox"), ("", "mirror"), ("", "management"))
)}


def zone_of(bridge):
    """Classify a bridge into a security zone from the live data it carries.

    Returns (zone_id, label, kind). `kind` drives only the colour/role shown in
    the view: routed (host-routed infrastructure), services, sandbox, mirror
    (passive), management, uplink (has a physical port) or isolated.
    """
    name = bridge.get("id", "")
    comment = (bridge.get("comment") or "").strip()
    head = comment.split(":", 1)[0].strip().lower()
    # Sandbox networks: vmbr1NNN (host firewall matches the vmbr1* prefix), or a
    # "Sandbox network …" comment. Grouped into one zone across all networks.
    if (name.startswith("vmbr1") and len(name) >= 8) or head.startswith("sandbox network"):
        return "sandbox", "Sandbox", "sandbox"
    for needle, zid, label, kind in ZONE_BY_COMMENT:
        if head.startswith(needle):
            return zid, label, kind
    # No comment / unrecognised: the bridge stands alone as its own zone.
    if bridge.get("cidr"):
        return name, name, "routed"
    return name, name, "uplink" if bridge.get("ports") else "isolated"


def zones_of(bridges):
    """Group bridges into zones, mutating each bridge with its `zone` id and
    returning the ordered list of zones."""
    zones = {}
    for b in bridges:
        zid, label, kind = zone_of(b)
        b["zone"] = zid
        z = zones.get(zid)
        if z is None:
            z = zones[zid] = {"id": zid, "label": label, "kind": kind, "bridges": [], "comment": ""}
        z["bridges"].append(b["id"])
        if not z["comment"] and b.get("comment"):
            z["comment"] = b["comment"]
    return sorted(zones.values(), key=lambda z: (ZONE_ORDER.get(z["id"], 99), z["label"]))


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def parse_kv(value):
    """Parse a Proxmox property string ('virtio=AA:..,bridge=vmbr2,firewall=1')."""
    result = {}
    for part in str(value or "").split(","):
        if not part:
            continue
        key, sep, val = part.partition("=")
        result[key.strip()] = val.strip() if sep else ""
    return result


def flag(value, default=False):
    if value is None or value == "":
        return default
    return str(value) not in ("0", "false", "False")


def interface(cidr):
    try:
        return ipaddress.ip_interface(cidr)
    except ValueError:
        return None


def parse_ports(spec):
    """'80,443' / '1000:2000' / 'ssh' -> [(lo, hi)], or None for "any" / unknown."""
    spec = str(spec or "").strip()
    if not spec:
        return None
    ranges = []
    for part in spec.split(","):
        lo, _, hi = part.strip().partition(":")
        try:
            lo = int(lo) if lo.isdigit() else SERVICE_PORTS[lo.lower()]
            hi = (int(hi) if hi.isdigit() else SERVICE_PORTS[hi.lower()]) if hi else lo
        except KeyError:
            return None
        ranges.append((lo, hi))
    return ranges


def tags(value):
    return [t for t in re.split(r"[;, ]+", str(value or "")) if t]


# ---------------------------------------------------------------------------
# Address specs (source/dest of a rule)
# ---------------------------------------------------------------------------


class AddressResolver:
    """Resolve rule source/dest specs (IP, CIDR, range, alias, +ipset) to networks."""

    def __init__(self, cluster_aliases, cluster_ipsets, guest_aliases=None, guest_ipsets=None):
        self.aliases = {"dc": cluster_aliases or {}, "guest": guest_aliases or {}}
        self.ipsets = {"dc": cluster_ipsets or {}, "guest": guest_ipsets or {}}

    def _lookup(self, table, name):
        scope, sep, bare = name.partition("/")
        if sep and scope in ("dc", "guest"):
            return table[scope].get(bare)
        # Unscoped: guest-level definitions shadow datacenter ones.
        found = table["guest"].get(name)
        return found if found is not None else table["dc"].get(name)

    def resolve(self, spec):
        """Return (matchers, unknown): matchers is a list of (include, network|range)."""
        spec = str(spec or "").strip()
        if not spec:
            return None, False  # any

        matchers, unknown = [], False
        for item in spec.split(","):
            item = item.strip()
            negate = item.startswith("!")
            item = item.lstrip("!")

            if item.startswith("+"):
                members = self._lookup(self.ipsets, item[1:])
                if members is None:
                    unknown = True
                    continue
                for member in members:
                    sub, sub_unknown = self.resolve(member["cidr"])
                    unknown |= sub_unknown
                    for include, net in sub or []:
                        matchers.append((include != (negate or member["nomatch"]), net))
                continue

            if "-" in item:
                lo, _, hi = item.partition("-")
                try:
                    matchers.append(
                        (not negate, (ipaddress.ip_address(lo), ipaddress.ip_address(hi)))
                    )
                except ValueError:
                    unknown = True
                continue

            try:
                matchers.append((not negate, ipaddress.ip_network(item, strict=False)))
                continue
            except ValueError:
                pass

            alias = self._lookup(self.aliases, item)
            if alias is None:
                unknown = True
                continue
            sub, sub_unknown = self.resolve(alias)
            unknown |= sub_unknown
            matchers += [(include != negate, net) for include, net in sub or []]

        return matchers, unknown


def address_match(matchers, unknown, ip):
    """True / False / None (cannot tell) for an IP against a resolved spec."""
    if matchers is None and not unknown:
        return True
    ip = ipaddress.ip_address(ip)
    included = False
    for include, net in matchers or []:
        if isinstance(net, tuple):
            hit = net[0].version == ip.version and net[0] <= ip <= net[1]
        else:
            hit = ip.version == net.version and ip in net
        if hit and not include:
            return False
        included |= hit and include
    if included:
        return True
    return None if unknown else False


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------


def expand_rules(rules, groups, origin):
    """Flatten guest/host rules: expand `group` references, split macros.

    Every resulting rule carries `origin` (where it was defined) and `group`
    (the security group it came from, or None).
    """
    expanded = []
    for rule in rules or []:
        if not flag(rule.get("enable"), default=True):
            continue
        if rule.get("type") == "group":
            name = rule.get("action")
            for inner in expand_rules(groups.get(name, {}).get("rules", []), {}, origin):
                if rule.get("iface") and not inner.get("iface"):
                    inner["iface"] = rule["iface"]
                inner["group"] = name
                expanded.append(inner)
            continue
        if rule.get("type") not in ("in", "out"):
            continue

        base = {
            "direction": rule["type"],
            "action": str(rule.get("action", "")).upper(),
            "source": rule.get("source", ""),
            "dest": rule.get("dest", ""),
            "iface": rule.get("iface", ""),
            "comment": rule.get("comment", ""),
            "macro": rule.get("macro", ""),
            "origin": origin,
            "group": None,
            "unknown_service": False,
        }
        macro = str(rule.get("macro") or "").lower()
        if macro:
            services = MACROS.get(macro)
            if services is None:
                # Unknown macro: treat as "any service" but flag it.
                services = [("", "")]
                base["unknown_service"] = True
        else:
            services = [(rule.get("proto", ""), rule.get("dport", ""))]

        for proto, dport in services:
            ports = parse_ports(dport)
            expanded.append(
                {
                    **base,
                    "proto": str(proto or "").lower(),
                    "dport": str(dport or ""),
                    "ports": ports,
                    "unknown_service": base["unknown_service"]
                    or (bool(dport) and ports is None),
                }
            )
    return expanded


def service_match(rule, proto, port):
    if rule["proto"] and rule["proto"] != proto:
        return False
    if rule["ports"] is None:
        return True
    if port is None:
        return False
    return any(lo <= port <= hi for lo, hi in rule["ports"])


def public_rule(rule):
    return {
        key: rule[key]
        for key in (
            "direction",
            "action",
            "proto",
            "dport",
            "source",
            "dest",
            "iface",
            "macro",
            "comment",
            "origin",
            "group",
            "unknown_service",
        )
    }


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


class Endpoint:
    def __init__(self, id, kind, name):
        self.id = id
        self.kind = kind
        self.name = name
        self.nics = []
        self.rules = []  # expanded rules
        self.resolver = None
        self.enforced_in = {}  # nic name -> bool
        self.enforced_out = {}
        self.policy_in = "ACCEPT"
        self.policy_out = "ACCEPT"
        self.extra = {}
        self._resolved = {}

    def resolved(self, spec):
        if spec not in self._resolved:
            self._resolved[spec] = self.resolver.resolve(spec)
        return self._resolved[spec]

    def evaluate(self, direction, nic, peer_ip, own_ip, proto, port):
        """Verdict for one packet on one NIC. Returns (verdict, rule|None)."""
        enforced = (self.enforced_in if direction == "in" else self.enforced_out).get(
            nic["name"], False
        )
        if not enforced:
            return ALLOW, None

        # First definite match wins. A rule that *might* match (unresolvable
        # alias/IPSet, unknown macro) before it turns the verdict into UNKNOWN
        # if it would have decided differently.
        maybe = None
        for rule in self.rules:
            if rule["direction"] != direction:
                continue
            if rule["iface"] and rule["iface"] != nic["name"]:
                continue
            if not service_match(rule, proto, port):
                continue
            peer_spec, own_spec = (
                (rule["source"], rule["dest"]) if direction == "in" else (rule["dest"], rule["source"])
            )
            peer_hit = address_match(*self.resolved(peer_spec), peer_ip)
            own_hit = address_match(*self.resolved(own_spec), own_ip)
            if peer_hit is False or own_hit is False:
                continue
            accept = rule["action"] == "ACCEPT"
            if peer_hit is None or own_hit is None or rule["unknown_service"]:
                maybe = maybe or (accept, rule)
                continue
            if maybe and maybe[0] != accept:
                return UNKNOWN, maybe[1]
            return (ALLOW if accept else DENY), rule

        accept = (self.policy_in if direction == "in" else self.policy_out) == "ACCEPT"
        if maybe and maybe[0] != accept:
            return UNKNOWN, maybe[1]
        return (ALLOW if accept else DENY), None

    def to_json(self):
        return {
            "id": self.id,
            "kind": self.kind,
            "name": self.name,
            "nics": self.nics,
            "policy_in": self.policy_in,
            "policy_out": self.policy_out,
            "rules": [public_rule(r) for r in self.rules],
            **self.extra,
        }


def guest_nics(guest):
    config = guest.get("config", {})
    nics = []
    for key in sorted(config, key=lambda k: (len(k), k)):
        match = NET_KEY.match(key)
        if not match:
            continue
        index = match.group(1)
        net = parse_kv(config[key])

        if guest.get("type") == "lxc":
            ipcfg = net
            mac = net.get("hwaddr", "")
        else:
            ipcfg = parse_kv(config.get(f"ipconfig{index}", ""))
            mac = next(
                (v for k, v in net.items() if re.fullmatch(r"([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}", v)),
                "",
            )
        ip = ipcfg.get("ip", "")
        iface = interface(ip) if ip and ip not in ("dhcp", "manual") else None

        nics.append(
            {
                "name": key,
                "bridge": net.get("bridge", ""),
                "vlan": net.get("tag", ""),
                "mac": mac,
                "firewall": flag(net.get("firewall")),
                "link_down": flag(net.get("link_down")),
                "ip": str(iface.ip) if iface else "",
                "cidr": str(iface) if iface else ip,
                "gateway": ipcfg.get("gw", ""),
            }
        )
    return nics


def build_endpoints(data):
    cluster_fw = data.get("cluster_firewall", {})
    cluster_options = cluster_fw.get("options", {})
    cluster_enabled = flag(cluster_options.get("enable"))
    groups = {g["name"]: g for g in cluster_fw.get("groups", [])}
    cluster_aliases = cluster_fw.get("aliases", {})
    cluster_ipsets = cluster_fw.get("ipsets", {})

    endpoints = []

    # --- Proxmox host ---------------------------------------------------------
    host = Endpoint(HOST, "host", data.get("node") or "proxmox")
    for net in data.get("networks", []):
        iface = interface(net.get("cidr") or "") or (
            interface(f"{net['address']}/{net.get('netmask', '32')}") if net.get("address") else None
        )
        if not iface:
            continue
        host.nics.append(
            {
                "name": net.get("iface"),
                "bridge": net.get("iface") if net.get("type") in ("bridge", "OVSBridge") else "",
                "vlan": "",
                "mac": "",
                "firewall": True,
                "link_down": not flag(net.get("active"), default=True),
                "ip": str(iface.ip),
                "cidr": str(iface),
                "gateway": net.get("gateway", ""),
            }
        )

    node_fw = data.get("node_firewall", {})
    node_enabled = cluster_enabled and flag(node_fw.get("options", {}).get("enable"), default=True)
    host.resolver = AddressResolver(cluster_aliases, cluster_ipsets)
    host.policy_in = str(cluster_options.get("policy_in") or "DROP").upper()
    host.policy_out = str(cluster_options.get("policy_out") or "ACCEPT").upper()
    host.rules = expand_rules(node_fw.get("rules"), groups, "node") + expand_rules(
        cluster_fw.get("rules"), groups, "datacenter"
    )
    # Implicit management access, evaluated after the explicit rules.
    management_source = "+dc/management" if "management" in cluster_ipsets else "local_network"
    for proto, dport in MANAGEMENT_PORTS:
        host.rules.append(
            {
                "direction": "in",
                "action": "ACCEPT",
                "proto": proto,
                "dport": dport,
                "ports": parse_ports(dport),
                "source": management_source,
                "dest": "",
                "iface": "",
                "macro": "",
                "comment": "Implicit Proxmox management access",
                "origin": "implicit",
                "group": None,
                "unknown_service": False,
            }
        )
    for nic in host.nics:
        host.enforced_in[nic["name"]] = node_enabled
        host.enforced_out[nic["name"]] = node_enabled
    host.extra = {
        "firewall_enabled": node_enabled,
        "api_port": (data.get("host") or {}).get("api_port", 8006),
        "services": data.get("services", []),
        "version": data.get("version"),
    }
    endpoints.append(host)

    # --- Guests ---------------------------------------------------------------
    for guest in data.get("guests", []):
        fw = guest.get("firewall", {})
        options = fw.get("options", {})
        enabled = cluster_enabled and flag(options.get("enable"))

        ep = Endpoint(f"vm:{guest['vmid']}", guest.get("type", "qemu"), guest.get("name") or str(guest["vmid"]))
        ep.nics = guest_nics(guest)
        ep.resolver = AddressResolver(
            cluster_aliases, cluster_ipsets, fw.get("aliases", {}), fw.get("ipsets", {})
        )
        ep.policy_in = str(options.get("policy_in") or "DROP").upper()
        ep.policy_out = str(options.get("policy_out") or "ACCEPT").upper()
        ep.rules = expand_rules(fw.get("rules"), groups, "guest")
        for nic in ep.nics:
            ep.enforced_in[nic["name"]] = enabled and nic["firewall"]
            ep.enforced_out[nic["name"]] = enabled and nic["firewall"]
        ep.extra = {
            "vmid": guest["vmid"],
            "status": guest.get("status"),
            "pool": guest.get("pool") or "",
            "tags": tags(guest.get("tags")),
            "firewall_enabled": enabled,
            "firewall_options": options,
            "security_groups": sorted(
                {r["action"] for r in fw.get("rules", []) if r.get("type") == "group"}
            ),
        }
        endpoints.append(ep)

    return endpoints


# ---------------------------------------------------------------------------
# Reachability
# ---------------------------------------------------------------------------


def paths(src, dst, host):
    """Yield (src_nic, dst_nic, dst_ip, path_label) for every L3 path src -> dst."""
    host_ips = {nic["ip"] for nic in host.nics}
    host_bridges = {
        nic["bridge"]: interface(nic["cidr"]) for nic in host.nics if nic["bridge"]
    }

    for dnic in dst.nics:
        if not dnic["ip"] or dnic["link_down"]:
            continue
        dip = ipaddress.ip_address(dnic["ip"])
        best = None
        for snic in src.nics:
            if not snic["ip"] or snic["link_down"]:
                continue
            siface = interface(snic["cidr"])

            # Same broadcast domain: same bridge + VLAN and the target is on-link.
            if (
                snic["bridge"]
                and snic["bridge"] == dnic["bridge"]
                and snic["vlan"] == dnic["vlan"]
                and dip in siface.network
            ):
                best = (snic, dnic, dnic["ip"], f"L2 {snic['bridge']}")
                break

            if best:
                continue
            if dst.id == HOST and snic["gateway"] in host_ips:
                # Every host address is local to the gateway (the host itself).
                best = (snic, dnic, dnic["ip"], f"via host ({snic['bridge']})")
            elif snic["gateway"] in host_ips and dnic["bridge"] in host_bridges:
                hnet = host_bridges[dnic["bridge"]]
                if hnet and dip in hnet.network:
                    best = (snic, dnic, dnic["ip"], f"routed via host ({snic['bridge']} -> {dnic['bridge']})")
        if best:
            yield best


def probes(endpoints, api_port):
    """Every distinct (proto, port, label) worth testing: one per ACCEPT rule service."""
    seen = {("tcp", api_port): ("tcp", api_port, f"tcp/{api_port}")}
    for ep in endpoints:
        for rule in ep.rules:
            if rule["action"] != "ACCEPT":
                continue
            proto = rule["proto"] or None
            if rule["ports"]:
                for lo, hi in rule["ports"]:
                    label = f"{proto or 'any'}/{lo}" + (f"-{hi}" if hi != lo else "")
                    seen.setdefault((proto, lo), (proto, lo, label))
            else:
                seen.setdefault((proto, None), (proto, None, f"{proto or 'any'}/*" if proto else "any"))
    return sorted(seen.values(), key=lambda p: (p[1] is None, p[1] or 0, p[0] or ""))


def combine(a, b):
    if DENY in (a, b):
        return DENY
    if UNKNOWN in (a, b):
        return UNKNOWN
    return ALLOW


def rule_ref(ep, rule, direction):
    policy = ep.policy_in if direction == "in" else ep.policy_out
    if rule is None:
        return {"endpoint": ep.id, "direction": direction, "policy": policy, "rule": None}
    return {"endpoint": ep.id, "direction": direction, "policy": policy, "rule": public_rule(rule)}


def evaluate(src, dst, host, proto, port):
    """Best verdict over all paths for src -> dst on (proto, port)."""
    best = None
    rank = {ALLOW: 2, UNKNOWN: 1, DENY: 0}
    for snic, dnic, dip, label in paths(src, dst, host):
        # Probe with a concrete protocol; "any" probes use tcp.
        p = proto or "tcp"
        out_v, out_r = src.evaluate("out", snic, dip, snic["ip"], p, port)
        in_v, in_r = dst.evaluate("in", dnic, snic["ip"], dip, p, port)
        verdict = combine(out_v, in_v)
        candidate = {
            "verdict": verdict,
            "path": label,
            "src_nic": snic["name"],
            "dst_nic": dnic["name"],
            "dst_ip": dip,
            "egress": {"verdict": out_v, **rule_ref(src, out_r, "out")},
            "ingress": {"verdict": in_v, **rule_ref(dst, in_r, "in")},
        }
        # Prefer the more permissive verdict, then a direct (L2) path.
        if (
            best is None
            or rank[verdict] > rank[best["verdict"]]
            or (rank[verdict] == rank[best["verdict"]] and label.startswith("L2") and not best["path"].startswith("L2"))
        ):
            best = candidate
    return best


def analyze(data):
    endpoints = build_endpoints(data)
    host = endpoints[0]
    api_port = host.extra["api_port"]

    flows = []
    for proto, port, label in probes(endpoints, api_port):
        for src in endpoints:
            for dst in endpoints:
                if src is dst:
                    continue
                result = evaluate(src, dst, host, proto, port)
                if result is None or result["verdict"] == DENY:
                    continue
                groups = sorted(
                    {
                        side["rule"]["group"]
                        for side in (result["egress"], result["ingress"])
                        if side["rule"] and side["rule"]["group"]
                    }
                )
                flows.append(
                    {
                        "id": f"{src.id}>{dst.id}>{label}",
                        "from": src.id,
                        "to": dst.id,
                        "service": label,
                        "proto": proto or "any",
                        "port": port,
                        "groups": groups,
                        **result,
                    }
                )

    # Who can open a connection to the Proxmox API?
    api_access = []
    for src in endpoints[1:]:
        result = evaluate(src, host, host, "tcp", api_port)
        if result is None:
            api_access.append({"endpoint": src.id, "name": src.name, "verdict": DENY, "path": None})
        else:
            api_access.append({"endpoint": src.id, "name": src.name, **result})

    bridges = []
    for net in data.get("networks", []):
        if net.get("type") not in ("bridge", "OVSBridge"):
            continue
        bridges.append(
            {
                "id": net["iface"],
                "cidr": net.get("cidr") or net.get("address") or "",
                "ports": net.get("bridge_ports") or "",
                "comment": net.get("comments", "").strip(),
                "active": flag(net.get("active"), default=True),
                "vlan_aware": flag(net.get("bridge_vlan_aware")),
            }
        )
    known = {b["id"] for b in bridges}
    for ep in endpoints:
        for nic in ep.nics:
            if nic["bridge"] and nic["bridge"] not in known:
                known.add(nic["bridge"])
                bridges.append({"id": nic["bridge"], "cidr": "", "ports": "", "comment": "", "active": False, "vlan_aware": False, "missing": True})

    bridges.sort(key=lambda b: (len(b["id"]), b["id"]))
    zones = zones_of(bridges)

    cluster_fw = data.get("cluster_firewall", {})
    warnings = []
    if not flag(cluster_fw.get("options", {}).get("enable")):
        warnings.append("Datacenter firewall is disabled: no guest firewall rule is enforced.")
    for ep in endpoints[1:]:
        loose = [n["name"] for n in ep.nics if not ep.enforced_in.get(n["name"])]
        if loose:
            warnings.append(f"{ep.name}: firewall not enforced on {', '.join(loose)}.")

    return {
        "node": data.get("node"),
        "version": data.get("version"),
        "api_port": api_port,
        "bridges": bridges,
        "zones": zones,
        "endpoints": [ep.to_json() for ep in endpoints],
        "groups": [
            {
                "name": g["name"],
                "comment": g.get("comment", ""),
                "rules": [public_rule(r) for r in expand_rules(g.get("rules"), {}, "group")],
                "members": [ep.id for ep in endpoints[1:] if g["name"] in ep.extra["security_groups"]],
            }
            for g in cluster_fw.get("groups", [])
        ],
        "cluster_firewall": {
            "options": cluster_fw.get("options", {}),
            "aliases": cluster_fw.get("aliases", {}),
            "ipsets": cluster_fw.get("ipsets", {}),
        },
        "flows": flows,
        "api_access": api_access,
        "warnings": warnings,
        "errors": data.get("errors", []),
    }
