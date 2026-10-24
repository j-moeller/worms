"""A Proxmox snapshot shaped like what orchestrator/proxmox_inventory.py
collects from a fully built platform, for the topology view's tests.

Addresses, bridges and security groups follow opentofu/shared/
shared-variables.tf and opentofu/stacks/firewall (abridged to the rules the
view interprets): the infrastructure VMs, the private internet, a running
sandbox (sandbox-gw + four VMs on two networks) and two teams' devboxes
(devbox-gw + A/B/C wired A-B, B-C) with the trusted SSH gateway.
"""

from __future__ import annotations

import copy

SERVICES_NET = "10.2.0.0/24"
SANDBOX_NET = "10.64.0.0/10"
DEVBOX_NET = "10.128.0.0/10"

BRIDGES = [
    ("vmbr2", "10.3.0.1/24", "Management (SSH host -> VM) for database and orchestrator. Ports isolated."),
    ("vmbr3", "", "Mirror of the sandbox networks for the monitor VM. No host IP."),
    ("vmbr4", "10.4.0.1/16", "Management (SSH host -> VM) for sandbox VMs and sandbox-gw. Ports isolated."),
    ("vmbr5", "10.5.0.1/24", "Management (SSH host -> VM) for dashboard, scoring, logging, DNS, private internet. Ports isolated."),
    ("vmbr6", "10.6.0.1/16", "Management (SSH host -> VM) for devbox VMs and devbox gateways. Ports isolated."),
    ("vmbr7", "10.7.0.1/16", "Management: devbox SSH fabric (SSH gateway <-> devbox VMs). Ports isolated."),
    ("vmbr20", "10.10.0.1/24", "DMZ: dashboard (published to the internet by host DNAT on 443)."),
    ("vmbr21", "10.11.0.1/24", "Core: database and orchestrator."),
    ("vmbr22", "10.12.0.1/24", "Ingest: database leg of scoring/logging (routed to database:5432 only)."),
    ("vmbr23", "", "Sandbox services: scoring/logging (sandbox leg), private internet, DNS, sandbox-gw. No host IP, no uplink."),
    ("vmbr24", "10.13.0.1/24", "SIEM: wazuh (receives syslog from every infrastructure VM, routed tcp 514 only)."),
    ("vmbr1000", "", "Sandbox network dmz (no host IP)"),
    ("vmbr1001", "", "Sandbox network core (no host IP)"),
    ("vmbr9000", "", "Devbox network t1 A-B (no host IP)"),
    ("vmbr9001", "", "Devbox network t1 B-C (no host IP)"),
    ("vmbr9002", "", "Devbox network t2 A-B (no host IP)"),
    ("vmbr9003", "", "Devbox network t2 B-C (no host IP)"),
]


def _in(dport, source, proto="tcp", comment=""):
    return {"type": "in", "action": "ACCEPT", "proto": proto, "dport": dport, "source": source, "comment": comment, "enable": 1}


def _out(dport, dest, proto="tcp", comment=""):
    return {"type": "out", "action": "ACCEPT", "proto": proto, "dport": dport, "dest": dest, "comment": comment, "enable": 1}


GROUPS = {
    "database": [
        _in("5432", ip, comment=f"Allow {name}")
        for name, ip in [("dashboard", "10.10.0.18"), ("orchestrator", "10.11.0.19"), ("scoring", "10.12.0.14"),
                         ("logging", "10.12.0.15"), ("monitor", "10.12.0.100")]
    ] + [_in("5432", "10.11.0.1", comment="Allow the Proxmox host (round agent)")],
    "dashboard": [_in("443", "", comment="HTTPS (public, via host DNAT)"), _out("5432", "10.11.0.16"), _out("443", "10.11.0.19")],
    "orchestrator": [_in("443", "10.10.0.18"), _out("8006", "10.11.0.1"), _out("5432", "10.11.0.16")],
    "db_client": [_out("5432", "10.11.0.16")],
    "sandbox_api": [_in("443", "10.2.0.2", comment="Sandbox (via sandbox-gw) -> REST API")],
    "devbox_api": [_in("443", "10.2.0.32/27", comment="Devbox gateways -> REST API")],
    "dns": [_in("53", SERVICES_NET, proto="udp"), _in("53", SERVICES_NET)],
    "private_internet": [_in("53,80,443,3141,3142,5000,8888", SERVICES_NET), _in("53,123", SERVICES_NET, proto="udp")],
    "pi_apt_client": [_out("3142", "10.2.0.20")],
    "services_client": [_out("53,80,443,3141,3142,5000,8888", "10.2.0.11,10.2.0.14,10.2.0.15,10.2.0.20")],
    "sandbox": [
        {"type": "in", "action": "ACCEPT", "source": SANDBOX_NET, "dest": SANDBOX_NET, "enable": 1},
        {"type": "out", "action": "ACCEPT", "source": SANDBOX_NET, "dest": SANDBOX_NET, "enable": 1},
        _out("53,80,443,3141,3142,5000,8888", SERVICES_NET),
    ],
    "sandbox_gw": [
        {"type": "in", "action": "ACCEPT", "source": SANDBOX_NET, "dest": SANDBOX_NET, "enable": 1},
        {"type": "in", "action": "ACCEPT", "proto": "tcp", "dport": "53,80,443", "source": SANDBOX_NET, "dest": SERVICES_NET, "enable": 1},
    ],
    "devbox": [
        {"type": "in", "action": "ACCEPT", "source": DEVBOX_NET, "dest": DEVBOX_NET, "enable": 1},
        {"type": "out", "action": "ACCEPT", "source": DEVBOX_NET, "dest": DEVBOX_NET, "enable": 1},
    ],
    "devbox_gw": [{"type": "in", "action": "ACCEPT", "source": DEVBOX_NET, "dest": DEVBOX_NET, "enable": 1}],
    "devbox_ssh_target": [_in("22", "10.7.0.2")],
    "devbox_ssh_gateway": [_in("22", "", comment="Teams (host DNAT :2222)")],
    "ssh": [_in("22", "10.3.0.1,10.5.0.1,10.4.0.1,10.6.0.1", comment="SSH from the Proxmox host")],
    "provisioning": [_out("3142", "10.11.0.1")],
    "log_client": [_out("514", "10.13.0.30")],
    "wazuh": [_in("514", "10.0.0.0/8")],
}


def _guest(vmid, name, nics, groups, pool="persistent", tags="", status="running", ipfilter=False):
    config = {"name": name}
    for i, (bridge, cidr) in enumerate(nics):
        config[f"net{i}"] = f"virtio=BC:24:11:00:{vmid % 256:02X}:{i:02X},bridge={bridge},firewall=1"
        config[f"ipconfig{i}"] = f"ip={cidr}" if cidr else "ip=manual"
    return {
        "vmid": vmid,
        "type": "qemu",
        "name": name,
        "status": status,
        "pool": pool,
        "tags": tags,
        "config": config,
        "firewall": {
            "options": {"enable": 1, "policy_in": "DROP", "policy_out": "DROP", "ipfilter": int(ipfilter)},
            "rules": [
                {"pos": pos, "type": "group", "action": group, "iface": iface, "enable": 1}
                for pos, (group, iface) in enumerate(groups)
            ],
            "aliases": {},
            "ipsets": {},
        },
    }


def snapshot() -> dict:
    guests = [
        _guest(101, "database", [("vmbr21", "10.11.0.16/24"), ("vmbr2", "10.3.0.16/24")],
               [("database", "net0"), ("provisioning", "net0"), ("log_client", "net0"), ("ssh", "net1")]),
        _guest(102, "orchestrator", [("vmbr21", "10.11.0.19/24"), ("vmbr2", "10.3.0.19/24")],
               [("orchestrator", "net0"), ("ssh", "net1")]),
        _guest(103, "dashboard", [("vmbr20", "10.10.0.18/24"), ("vmbr5", "10.5.0.18/24")],
               [("dashboard", "net0"), ("ssh", "net1")]),
        _guest(104, "scoring", [("vmbr23", "10.2.0.14/24"), ("vmbr22", "10.12.0.14/24"), ("vmbr5", "10.5.0.14/24")],
               [("sandbox_api", "net0"), ("devbox_api", "net0"), ("db_client", "net1"), ("ssh", "net2")], pool="ephemeral"),
        _guest(105, "logging", [("vmbr23", "10.2.0.15/24"), ("vmbr22", "10.12.0.15/24"), ("vmbr5", "10.5.0.15/24")],
               [("sandbox_api", "net0"), ("devbox_api", "net0"), ("db_client", "net1"), ("ssh", "net2")], pool="ephemeral"),
        _guest(106, "monitor", [("vmbr22", "10.12.0.100/24"), ("vmbr5", "10.5.0.100/24"), ("vmbr3", "")],
               [("db_client", "net0"), ("ssh", "net1")], pool="ephemeral"),
        _guest(107, "wazuh", [("vmbr24", "10.13.0.30/24"), ("vmbr5", "10.5.0.30/24")],
               [("wazuh", "net0"), ("ssh", "net1")]),
        _guest(110, "dns", [("vmbr23", "10.2.0.11/24"), ("vmbr5", "10.5.0.11/24")],
               [("dns", "net0"), ("ssh", "net1")], pool="private_internet", tags="private_internet"),
        _guest(120, "apt-cache", [("vmbr23", "10.2.0.20/24"), ("vmbr5", "10.5.0.20/24")],
               [("private_internet", "net0"), ("ssh", "net1")], pool="private_internet", tags="private_internet"),
        _guest(121, "devpi", [("vmbr23", "10.2.0.21/24"), ("vmbr5", "10.5.0.21/24")],
               [("private_internet", "net0"), ("pi_apt_client", "net0"), ("ssh", "net1")],
               pool="private_internet", tags="private_internet", status="stopped"),
        _guest(200, "sandbox-gw", [("vmbr1000", "10.64.0.1/24"), ("vmbr1001", "10.64.1.1/24"), ("vmbr23", "10.2.0.2/24"), ("vmbr4", "10.4.0.2/16")],
               [("sandbox_gw", ""), ("services_client", "net2"), ("ssh", "net3")], pool="sandbox", tags="sandbox-gw", ipfilter=True),
        _guest(300, "devbox-ssh-gateway", [("vmbr7", "10.7.0.2/16"), ("vmbr5", "10.5.0.40/24")],
               [("devbox_ssh_gateway", "net0"), ("ssh", "net1")], tags="devbox-ssh-gateway"),
    ]
    # The sandbox: vm0, vm1 on vmbr1000; vm2 on both; vm3 on vmbr1001.
    sandbox = [
        (900, "sandbox-vm0", [("vmbr1000", "10.64.0.10/24")]),
        (901, "sandbox-vm1", [("vmbr1000", "10.64.0.11/24")]),
        (902, "sandbox-vm2", [("vmbr1000", "10.64.0.12/24"), ("vmbr1001", "10.64.1.12/24")]),
        (903, "sandbox-vm3", [("vmbr1001", "10.64.1.13/24")]),
    ]
    for vmid, name, nics in sandbox:
        mgmt = ("vmbr4", f"10.4.0.{vmid - 880}/16")
        guests.append(_guest(vmid, name, nics + [mgmt], [("sandbox", ""), ("ssh", f"net{len(nics)}")],
                             pool="sandbox", tags="untrusted", ipfilter=True))
    # Two teams' devboxes (scripts/generate_devbox.py addressing).
    for index, team in enumerate(["t1", "t2"]):
        ab, bc = f"vmbr9{2 * index:03d}", f"vmbr9{2 * index + 1:03d}"
        base = 400 + 10 * index
        guests.append(_guest(base, f"devbox-{team}-gw",
                             [(ab, f"10.128.{2 * index}.1/24"), (bc, f"10.128.{2 * index + 1}.1/24"),
                              ("vmbr23", f"10.2.0.{32 + index}/24"), ("vmbr6", f"10.6.{index}.2/16")],
                             [("devbox_gw", ""), ("services_client", "net2"), ("ssh", "net3")],
                             pool="devbox", tags="devbox-gw", ipfilter=True))
        for offset, (role, nics) in enumerate([
            ("a", [(ab, f"10.128.{2 * index}.11/24")]),
            ("b", [(ab, f"10.128.{2 * index}.12/24"), (bc, f"10.128.{2 * index + 1}.12/24")]),
            ("c", [(bc, f"10.128.{2 * index + 1}.13/24")]),
        ]):
            host = 11 + offset
            nics = nics + [("vmbr7", f"10.7.{index}.{host}/16"), ("vmbr6", f"10.6.{index}.{host}/16")]
            n = len(nics)
            guests.append(_guest(base + 1 + offset, f"devbox-{team}-{role}", nics,
                                 [("devbox", ""), ("devbox_ssh_target", f"net{n - 2}"), ("ssh", f"net{n - 1}")],
                                 pool="devbox", tags="devbox", ipfilter=True))

    return copy.deepcopy(
        {
            "schema": 1,
            "node": "worms-pve",
            "version": "8.4.1",
            "services": [{"name": "pve-firewall", "state": "running"}],
            "networks": [
                {"iface": name, "type": "bridge", "cidr": cidr or None, "address": cidr.split("/")[0] if cidr else None,
                 "comments": comment, "active": 1}
                for name, cidr, comment in BRIDGES
            ] + [{"iface": "eno1", "type": "eth", "active": 1}],
            "guests": guests,
            "cluster_firewall": {
                "options": {"enable": 1, "policy_in": "DROP", "policy_out": "ACCEPT"},
                "rules": [],
                "groups": [{"name": name, "comment": "", "rules": rules} for name, rules in GROUPS.items()],
                "aliases": {},
                "ipsets": {},
            },
            "node_firewall": {"options": {"enable": 1}, "rules": []},
            "host": {"api_port": 8006},
            "errors": [],
        }
    )
