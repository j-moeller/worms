"""Read-only collection of the Proxmox topology (VMs, bridges, firewall).

Runs on the orchestrator with a dedicated *read-only* API token
(settings.PROXMOX_READONLY, role PVEAuditor). Only HTTP GETs are issued: the
client below exposes nothing but `get`, so this module cannot change Proxmox
state even by mistake.

The result is a plain JSON-serialisable dict that the orchestrator stores in
the database (core.models.TopologySnapshot); the dashboard reads it from there
and never contacts Proxmox itself.

Data minimisation: the dashboard is public-facing, so only the fields needed to
draw the network are kept (whitelists below). In particular cloud-init secrets
(cipassword, sshkeys), descriptions, hookscripts and disks are dropped here,
before anything leaves the orchestrator.
"""

import re

from django.conf import settings

# Fields kept from /nodes/{node}/{qemu,lxc}/{vmid}/config. netN / ipconfigN are
# matched by pattern.
GUEST_CONFIG_FIELDS = {"name", "hostname", "tags", "cores", "memory", "ostype"}
GUEST_CONFIG_PATTERN = re.compile(r"^(net|ipconfig)\d+$")

NETWORK_FIELDS = {
    "iface",
    "type",
    "active",
    "autostart",
    "address",
    "netmask",
    "cidr",
    "gateway",
    "address6",
    "cidr6",
    "bridge_ports",
    "bridge_vlan_aware",
    "comments",
}

FIREWALL_OPTION_FIELDS = {
    "enable",
    "policy_in",
    "policy_out",
    "policy_forward",
    "ipfilter",
    "macfilter",
    "dhcp",
    "ndp",
    "radv",
    "nftables",
}

FIREWALL_RULE_FIELDS = {
    "pos",
    "type",
    "action",
    "macro",
    "proto",
    "dport",
    "sport",
    "source",
    "dest",
    "iface",
    "enable",
    "comment",
    "icmp-type",
}


class ReadOnlyProxmox:
    """proxmoxer wrapper that can only issue GET requests."""

    def __init__(self, api):
        self._api = api

    def get(self, *path, **params):
        return self._api.get(*[str(part) for part in path], **params)


def readonly_client():
    from proxmoxer import ProxmoxAPI

    config = settings.PROXMOX_READONLY
    missing = [key for key in ("user", "token_name", "token_value") if not config[key]]
    if missing:
        raise RuntimeError(
            "Read-only Proxmox credentials are not configured "
            f"(missing: {', '.join(missing)}). See PROXMOX_READONLY_* in "
            "config/settings/orchestrator.py."
        )

    return ReadOnlyProxmox(
        ProxmoxAPI(
            config["host"],
            port=config["port"],
            user=config["user"],
            token_name=config["token_name"],
            token_value=config["token_value"],
            verify_ssl=config["verify_ssl"],
            timeout=config["timeout"],
        )
    )


def _pick(record, fields):
    return {key: value for key, value in (record or {}).items() if key in fields}


def _pick_rules(rules):
    rules = [_pick(rule, FIREWALL_RULE_FIELDS) for rule in rules or []]
    return sorted(rules, key=lambda rule: rule.get("pos", 0))


class _Collector:
    def __init__(self, api: ReadOnlyProxmox, node: str, api_port: int):
        self.api = api
        self.node = node
        self.api_port = api_port
        self.errors = []

    def fetch(self, *path, default=None, required=False, **params):
        """GET a resource; record (instead of raise) per-resource failures.

        A missing privilege on one object should not hide the rest of the
        topology, so errors are collected and shown in the dashboard. Only the
        `required` top-level listings abort the collection.
        """
        try:
            return self.api.get(*path, **params)
        except Exception as e:  # proxmoxer raises ResourceException / requests errors
            if required:
                raise
            self.errors.append({"path": "/".join(str(p) for p in path), "error": str(e)})
            return default

    def ipsets(self, *prefix):
        result = {}
        for ipset in self.fetch(*prefix, "firewall", "ipset", default=[]) or []:
            name = ipset.get("name")
            members = self.fetch(*prefix, "firewall", "ipset", name, default=[]) or []
            result[name] = [
                {"cidr": m.get("cidr"), "nomatch": bool(m.get("nomatch"))}
                for m in members
            ]
        return result

    def aliases(self, *prefix):
        return {
            alias.get("name"): alias.get("cidr")
            for alias in self.fetch(*prefix, "firewall", "aliases", default=[]) or []
        }

    def guest(self, resource):
        kind = resource.get("type")  # "qemu" | "lxc"
        vmid = resource.get("vmid")
        prefix = ("nodes", self.node, kind, vmid)

        config = self.fetch(*prefix, "config", default={}) or {}
        config = {
            key: value
            for key, value in config.items()
            if key in GUEST_CONFIG_FIELDS or GUEST_CONFIG_PATTERN.match(key)
        }

        return {
            "vmid": vmid,
            "type": kind,
            "name": resource.get("name") or config.get("name") or config.get("hostname"),
            "status": resource.get("status"),
            "pool": resource.get("pool"),
            "tags": resource.get("tags") or config.get("tags") or "",
            "config": config,
            "firewall": {
                "options": _pick(
                    self.fetch(*prefix, "firewall", "options", default={}),
                    FIREWALL_OPTION_FIELDS,
                ),
                "rules": _pick_rules(self.fetch(*prefix, "firewall", "rules", default=[])),
                "aliases": self.aliases(*prefix),
                "ipsets": self.ipsets(*prefix),
            },
        }

    def collect(self):
        version = self.fetch("version", default={}) or {}

        resources = self.fetch("cluster", "resources", type="vm", required=True) or []
        guests = [
            self.guest(resource)
            for resource in sorted(resources, key=lambda r: r.get("vmid", 0))
            if resource.get("node") == self.node and not resource.get("template")
        ]

        networks = [
            _pick(interface, NETWORK_FIELDS)
            for interface in self.fetch("nodes", self.node, "network", required=True) or []
        ]

        # Rules are only enforced if the firewall daemon actually runs
        # (pve-firewall for iptables, proxmox-firewall for nftables).
        services = [
            {
                "name": service.get("name") or service.get("service"),
                "state": service.get("state"),
                "active_state": service.get("active-state"),
                "unit_state": service.get("unit-state"),
            }
            for service in self.fetch("nodes", self.node, "services", default=[]) or []
            if "firewall" in str(service.get("name") or service.get("service") or "")
        ]

        groups = []
        for group in self.fetch("cluster", "firewall", "groups", default=[]) or []:
            name = group.get("group")
            groups.append(
                {
                    "name": name,
                    "comment": group.get("comment", ""),
                    "rules": _pick_rules(
                        self.fetch("cluster", "firewall", "groups", name, default=[])
                    ),
                }
            )

        return {
            "schema": 1,
            "node": self.node,
            "version": version.get("version"),
            "services": services,
            "networks": sorted(networks, key=lambda n: n.get("iface", "")),
            "guests": guests,
            "cluster_firewall": {
                "options": _pick(
                    self.fetch("cluster", "firewall", "options", default={}),
                    FIREWALL_OPTION_FIELDS,
                ),
                "rules": _pick_rules(self.fetch("cluster", "firewall", "rules", default=[])),
                "groups": groups,
                "aliases": self.aliases("cluster"),
                "ipsets": self.ipsets("cluster"),
            },
            "node_firewall": {
                "options": _pick(
                    self.fetch("nodes", self.node, "firewall", "options", default={}),
                    FIREWALL_OPTION_FIELDS,
                ),
                "rules": _pick_rules(
                    self.fetch("nodes", self.node, "firewall", "rules", default=[])
                ),
            },
            "host": {"api_port": self.api_port},
            "errors": self.errors,
        }


def collect_topology(api: ReadOnlyProxmox, node: str, api_port: int = 8006) -> dict:
    return _Collector(api, node, api_port).collect()
