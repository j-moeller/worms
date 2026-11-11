# 1. Components

WORMS runs a competition for self-spreading programs ("worms") on a single Proxmox VE node.

- **Teams** develop against their own three VMs (the *devbox*, the dashboard's **Dev** view).
- They upload a zip.
- In each **round**, every team's worm runs in turn in a sandbox built from a *gameplan*. This is the dashboard's **Live** view.

Every sandbox and devbox VM is treated as malicious and rooted ([`docs/IMPORTANT`](IMPORTANT)).

| Doc | Topic |
|---|---|
| [1 Components](01-components.md) | this page |
| [2 Installation](02-installation.md) | from a bare Proxmox node to a running platform |
| [3 manage.py](03-manage-py.md) | the `worms` CLI and Django's management commands, testing |
| [4 Dev environment](04-dev-environment.md) | devboxes |
| [5 Live environment](05-live-environment.md) | rounds, gameplans, the round agent |
| [6 Background processes](06-background-processes.md) | every service, timer and loop |
| [7 Security](07-security.md) | threat models, privileges, secrets, firewalls, findings |
| [8 User guide](08-user-guide.md) | for teams: the dashboard, APIs, the worm contract |

## Overview

```
                      internet
                         │ 443 (dashboard), 22/2222 (devbox SSH gateway), 2222/22 + 8006 (operators)
┌────────────────────────┴───────────────────────────────────────────────────────────────┐
│ Proxmox node: nftables zone firewall, traffic mirror, firewall watchdog, round agent   │
│                                                                                        │
│  dmz        dashboard ──────────┐                                                      │
│  core       orchestrator ── database ── (ingest) scoring, logging, monitor             │
│  siem       syslog ◄── audit logs of every infrastructure VM                           │
│  services   scoring, logging, DNS, 7 private-internet VMs (apt, pypi, …)               │
│                ▲ SNAT                         ▲ SNAT                                   │
│            sandbox-gw                     devbox-gw (one per team)                     │
│                │                              │                                        │
│  sandbox    gameplan VMs (per round)      devbox A–B–C (per team) ◄── SSH gateway      │
│                └── mirrored to the monitor (Zeek)                                      │
└────────────────────────────────────────────────────────────────────────────────────────┘
```

## Components

| Component | Where | What it does | Source |
|---|---|---|---|
| `worms` CLI | the controller: the node itself, or a laptop with `WORMS_SSH` | Builds, deploys and operates everything | [`src/manage.py`](../src/manage.py), [`src/worms_cli/`](../src/worms_cli/) |
| OpenTofu stacks | controller; sandbox and devbox stacks on the node | Bridges, VMs, per-VM firewalls, security groups | [`opentofu/stacks/`](../opentofu/stacks/) |
| Ansible | controller; sandbox and devbox playbooks on the node | Configures the node and every VM | [`ansible/`](../ansible/) |
| Host network | node | Bridges' sysctls, nftables zone firewall, DNAT, internet window, traffic mirror, firewall watchdog | role [`host_network`](../ansible/roles/host/host_network/) |
| Round agent | node, as root | Runs a fixed menu of validated host jobs: sandbox build, flags, worm deploy, devbox jobs | [`scripts/round_agent/`](../scripts/round_agent/), role [`round_agent`](../ansible/roles/host/round_agent/) |
| Dashboard | VM `dashboard` (dmz) | Django and gunicorn behind nginx: teams, uploads, results, staff pages, team API | [`projects/dashboard/dashboard/`](../projects/dashboard/dashboard/) |
| Orchestrator | VM `orchestrator` (core) | The same Django project with other settings: runs migrations, the round conductor, capture processing, topology collection, the REST API (devbox resets, gameplan simulations) | [`projects/dashboard/orchestrator/`](../projects/dashboard/orchestrator/) |
| Database | VM `database` (core) | PostgreSQL. TLS only; one role per component with column-level grants | roles [`database/*`](../ansible/roles/database/) |
| Scoring, logging | VMs `scoring`, `logging` | `POST /submit` for flags and log records from team VMs. Teams are identified by source address | role [`sandbox_api`](../ansible/roles/applications/sandbox_api/) |
| Monitor | VM `monitor` | Zeek on a receive-only copy of all sandbox traffic, shipped into `network_flows`/`network_events` | role [`network_monitor`](../ansible/roles/applications/network_monitor/) |
| Syslog | VM `syslog` (siem) | rsyslog; receives every infrastructure VM's auditd log | roles [`audit/*`](../ansible/roles/audit/) |
| DNS | VM `dns` (services) | BIND, authoritative for `worms.internal` plus the LLM name; no recursion while offline | role [`setup_dns`](../ansible/roles/dns/setup_dns/) |
| Private internet | 7 VMs (services) | Offline caches and mirrors: apt-cacher-ng, devpi, Docker registry, man pages, NTP, cve-search, Wikipedia (kiwix) | roles [`private_internet/*`](../ansible/roles/private_internet/) |
| Devbox | per team: VMs A, B, C and a gateway | A team's development VMs ([4](04-dev-environment.md)) | [`opentofu/stacks/devbox`](../opentofu/stacks/devbox/), [`scripts/generate_devbox.py`](../scripts/generate_devbox.py) |
| SSH gateway | VM `devbox-ssh-gateway` | Teams' only way into their devbox: port forwarding to VM A only | role [`ssh_gateway`](../ansible/roles/applications/ssh_gateway/) |
| Sandbox | per round: gameplan VMs and `sandbox-gw` | Where a round's worms run ([5](05-live-environment.md)) | [`opentofu/stacks/sandbox`](../opentofu/stacks/sandbox/), [`src/gameplan/`](../src/gameplan/), [`gameplans/`](../gameplans/) |
| LLM endpoint | outside: `teachllm.mlsec.tu-berlin.de` (141.23.117.34:443) | The one outside address that team VMs can reach | `llm_access_ip` in [`group_vars/all.yml`](../ansible/group_vars/all.yml) |

## Networks

All addresses are defined once, in [`opentofu/shared/shared-variables.tf`](../opentofu/shared/shared-variables.tf), which is symlinked into every stack. They are Linux bridges, not SDN.

| Bridge | Zone | CIDR | Node address | Members |
|---|---|---|---|---|
| vmbr20 | dmz | 10.10.0.0/24 | .1 | dashboard (.18) |
| vmbr21 | core | 10.11.0.0/24 | .1 | database (.16), orchestrator (.19) |
| vmbr22 | ingest | 10.12.0.0/24 | .1 | scoring (.14), logging (.15), monitor (.100), database leg |
| vmbr24 | siem | 10.13.0.0/24 | .1 | syslog (.30) |
| vmbr23 | services | 10.2.0.0/24 | none | sandbox-gw (.2), dns (.11), scoring (.14), logging (.15), private internet (.20–.26), devbox-gws (.32+i) |
| vmbr1000–1029 | sandbox | 10.64.0.0/10, one /24 per gameplan network | none | sandbox VMs, sandbox-gw (.1) |
| vmbr9NNN | devbox | 10.128.0.0/19, two /24 per team | none | devbox VMs, devbox-gw (.1) |
| vmbr3 | mirror | none | none | the monitor's capture NIC (receive-only) |
| vmbr2 | mgmt-trusted | 10.3.0.0/24 | .1 | SSH to database, orchestrator |
| vmbr5 | mgmt-exposed | 10.5.0.0/24 | .1 | SSH to the other infrastructure VMs, private internet, SSH gateway (.40) |
| vmbr4 | mgmt-sandbox | 10.4.0.0/16 | .1 | SSH to sandbox VMs (10.4.0.10+), sandbox-gw (.2) |
| vmbr6 | mgmt-devbox | 10.6.0.0/16 | .1 | SSH to devbox VMs (10.6.i.x) |
| vmbr7 | devbox-ssh | 10.7.0.0/16 | .1 | the SSH gateway (.2) ↔ devbox VMs (10.7.i.11–13) |

VMIDs are not fixed: Proxmox assigns them. The flows allowed between zones are in [7 Security](07-security.md#firewalls).

## OpenTofu stacks

Every stack keeps its state locally, in `terraform.tfstate` next to it, and runs with `-parallelism=1`. Each one pins its own `bpg/proxmox` provider version.

| Stack | Owns | Applied by |
|---|---|---|
| `platform` | OS images, cloud-init snippets, bridges, pools (`ephemeral`, `persistent`, `sandbox`, `devbox`, `private_internet`), the hookscript | `worms build` on the controller |
| `firewall` | cluster and node firewall options, every security group | `worms build` |
| `infrastructure/persistent` | database, dashboard, orchestrator, syslog. No snapshot; an optional vzdump job | `worms build` |
| `infrastructure/ephemeral` | dns, scoring, logging, monitor. Snapshot `clean` | `worms build` |
| `private_internet` | the 7 private-internet VMs. Snapshot `clean` | `worms build` |
| `devbox` | per-team bridges, VMs, gateways; the SSH gateway | the round agent, in `/opt/worms` on the node |
| `sandbox` | one round's networks and VMs, generated from the gameplan | the round agent, in `/opt/worms` on the node |

Order: `platform` → `firewall` → the VM stacks. The VM stacks read the first two through `terraform_remote_state`.

Sources: [`src/worms_cli/pipeline.py`](../src/worms_cli/pipeline.py), [`src/worms_cli/session.py`](../src/worms_cli/session.py) (`NODE_STACKS`).

## Ansible

- **Inventory.** [`inventory.yml`](../ansible/inventory.yml) defines the groups. [`inventory.generated.yml`](../scripts/generate_inventory.py) holds the hosts, written from the stacks' `ansible_hosts` outputs.
- **Configure order.** `worms build` runs 14 playbooks:
  1. `WaitForVMs`
  2. `SetupDNS`
  3. `UpdateVMs`
  4. `HardenSSH`
  5. `CreateCA`
  6. `DistributeCA`
  7. `ConfigureNginx`
  8. `ConfigureNginxDjango`
  9. `SetupDbAndDashboard`
  10. `SetupSandboxApi`
  11. `SetupNetworkMonitor`
  12. `SetupPrivateInternet`
  13. `SetupAudit`
  14. `SetupRoundAgent`
- **Playbooks the round agent runs on the node.** `SetupSandbox`, `SetupDevbox` and `SetupSSHGateway`.
- **Secrets.** An ansible-vault file, `ansible/group_vars/vault.yml` ([7](07-security.md#secrets)).
- **Which system.** `worms_environments` in [`group_vars/all.yml`](../ansible/group_vars/all.yml), keyed by the node's hostname:

  | Node | System | Operators' SSH port | Teams' SSH gateway port | Public name |
  |---|---|---|---|---|
  | `worms` | production | 2222 | 22 | `worms.mlsec.tu-berlin.de` |
  | `worms-dev` | development | 22 | 2222 | `worms-dev.mlsec.tu-berlin.de` |

  Any other hostname is refused. Django's `DEBUG` is off on both.

## Repository layout

| Path | Contents |
|---|---|
| `src/` | `manage.py` (the `worms` CLI), `worms_cli/`, `gameplan/` (the gameplan framework) |
| `gameplans/` | the gameplans: one Python file each ([5](05-live-environment.md#writing-a-gameplan)) |
| `scripts/` | `round_agent/`, the generators for the inventory and the devbox, local dashboard rehearsals |
| `projects/dashboard/` | one Django project: apps `core` (models), `dashboard` (web), `orchestrator` (background work) |
| `opentofu/`, `ansible/` | infrastructure as code |
| `tests/` | the offline suite, `live/` (against a node), `browser/`, `docker/` |
| `worm.sh` | a minimal worm: rename it to `main.sh`, zip it, upload it |

## Resources

- **Measured on worms-dev, 2026-11-06, three teams:**
  - 8 infrastructure VMs: about 8 GB RAM.
  - 7 private-internet VMs: about 10.5 GB RAM. cve-search alone takes 4 GB.
  - 3 devboxes plus the SSH gateway: about 6.5 GB RAM (512 MB per devbox VM).
  - Ballooning lets the whole set run on 32 GB.
- **A round** adds its gameplan's VMs: 256–8192 MB each, defaults in [`src/gameplan/definition.py`](../src/gameplan/definition.py).
- **Minimum:** 32 GB RAM; 48 GB if a round should run too.
- **Disks:**
  - Team VMs' disks are thick, on storage `local-zfs-sandbox`.
  - Infrastructure disks are thin, on `local-zfs`.
- **Hard limits:**
  - 16 teams: the devbox range is a /19.
  - 32 VMs and 30 networks per gameplan.
