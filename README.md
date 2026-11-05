# WORMS platform

A competition platform for self-spreading programs ("worms"), on one Proxmox node. Teams sign
in to a dashboard, develop against their own three VMs (the **devbox**), upload a zip, and in
**rounds** every team's worm runs, one team after another, in a generated **sandbox**. Sandbox
and devbox VMs are assumed malicious and rooted: they never reach the internet, the
infrastructure or each other (`docs/IMPORTANT`).

Built with OpenTofu (`opentofu/`), Ansible (`ansible/`), the `worms` CLI and a round agent in
Python (`src/`, `scripts/`), and a Django dashboard (`projects/dashboard/`).

**Where it stands, and what is missing: [`docs/status.md`](docs/status.md).**

## Documentation

| Doc | For | What |
|---|---|---|
| [`docs/status.md`](docs/status.md) | everyone | What works, what is missing, open decisions, open security items |
| [`docs/management.md`](docs/management.md) | operators | Every task: build, accounts, devboxes, rounds, status, teardown |
| [`docs/manual-devbox-run.md`](docs/manual-devbox-run.md) | operators | The devbox journey by hand, command by command |
| [`docs/worm-contract.md`](docs/worm-contract.md) | teams | What to upload, how a worm runs, captures, limits, the devbox |
| [`docs/devbox.md`](docs/devbox.md) | developers | Devbox design: SSH gateway, attribution, reset, uploads |
| [`docs/rounds.md`](docs/rounds.md) | developers | Round orchestration: conductor, round agent, lifecycle, game plan |
| [`docs/security-assessment.md`](docs/security-assessment.md) | everyone | Threat models, containment layers and their state, findings |
| [`docs/accounts.md`](docs/accounts.md) | operators | Proxmox tokens, database roles, dashboard accounts, the vault |
| [`docs/progression.md`](docs/progression.md) | developers | The replay format, the PROGRESSION and TOPOLOGY pages |
| [`docs/team_api.md`](docs/team_api.md) | teams | The read-only team API |
| [`docs/monitoring.md`](docs/monitoring.md) | developers | Zeek network monitor, audit logs |
| [`docs/resources.md`](docs/resources.md) | operators | Memory budget of the node |
| [`docs/testing.md`](docs/testing.md) | developers | Offline suite, `worms simulate`, the live suite |
| `docs/archive/` | | Dated run logs and research notes, kept as they were written |

## Quick start

From the node, or from a laptop with root SSH to the node (`docs/accounts.md` §4):

```sh
alias worms='python3 /path/to/worms/src/manage.py'   # on the node: /root/app/.venv-test/bin/python
echo 'WORMS_SSH=root@<node>' >> ~/.worms/config        # laptop only
worms secrets issue proxmox-token                      # OpenTofu
worms secrets issue orchestrator-token                 # sandbox VM power, for the game plan
worms secrets issue topology-token                     # topology view and `worms status`
worms build                                            # = make: everything, idempotent
worms admin reset-password                             # one-time password for `admin`
```

`worms build` applies the platform and firewall stacks and the host firewall, opens the
**internet window**, creates and configures the infrastructure and private-internet VMs, closes
the window again (also on failure) and switches DNS and the caches to offline mode. The service
VMs are rolled back to their snapshot before they are configured and snapshotted again afterwards.
Running devbox VMs are shut down while the window is open. `worms --help` lists every command by
purpose; `worms build --list` the build's steps.

**Secrets:** one ansible-vault file on the controller, `ansible/group_vars/vault.yml` (key
`ansible/.vault_pass`, both git-ignored). The three Proxmox tokens are issued into it; everything
else is generated. `worms secrets` shows what is set, never a value.

**Addresses:** one source, `opentofu/shared/shared-variables.tf`, symlinked into every stack. The
Ansible inventory is generated from OpenTofu's state (`worms build inventory`); sandbox and devbox
addresses come from their own generated stacks.

## Network design

Risk zones, each on its own bridge:

| Bridge | Zone | Members | Host IP |
|---|---|---|---|
| vmbr20 | dmz | dashboard (internet via host DNAT :443) | 10.10.0.1 |
| vmbr21 | core | database, orchestrator | 10.11.0.1 |
| vmbr22 | ingest | scoring/logging/monitor, database leg | 10.12.0.1 |
| vmbr24 | siem | syslog | 10.13.0.1 |
| vmbr23 | services | scoring/logging sandbox leg, private internet, DNS, sandbox-gw, devbox-gws | none |
| vmbr1xxx | sandbox | generated networks (10.64.0.0/10), sandbox VMs, sandbox-gw | none |
| vmbr9xxx | devbox | per-team networks (10.128.0.0/10), devbox VMs, devbox-gws | none |
| vmbr3 | mirror | copy of all sandbox traffic, monitor capture NIC (receive-only) | none |
| vmbr2 | mgmt-trusted | SSH: database, orchestrator | 10.3.0.1 |
| vmbr5 | mgmt-exposed | SSH: dashboard, scoring, logging, monitor, DNS, private internet, syslog, SSH gateway | 10.5.0.1 |
| vmbr4 | mgmt-sandbox | SSH: sandbox VMs, sandbox-gw | 10.4.0.1 |
| vmbr6 | mgmt-devbox | SSH: devbox VMs, devbox-gws | 10.6.0.1 |
| vmbr7 | devbox-ssh | the SSH gateway ↔ devbox VMs | 10.7.0.1 |

Allowed flows. Everything else is dropped by the per-VM Proxmox firewall (policy DROP both ways,
`ipfilter`) and, for routed traffic, by the host's nftables:

- internet → dashboard:443 and → SSH gateway:2222 (host DNAT)
- dashboard → database:5432, orchestrator:443; orchestrator → database, Proxmox API:8006
- scoring, logging, monitor → database:5432, each INSERT-only into its own table
- every infrastructure VM → syslog:514
- sandbox VMs ↔ each other; sandbox and devbox VMs → their gateway → scoring, logging, DNS and the
  private internet on their service ports only, SNATed to the gateway's address
- sandbox and devbox VMs → `teachllm.mlsec.tu-berlin.de:443`, the one outside exception
  (`docs/security-assessment.md`)
- host → every VM:22 on its management NIC

The database is the choke point between the untrusted side and the infrastructure: rows written
by scoring and logging are attacker-controlled data. The host has no IPv4 address on any bridge a
sandbox or devbox VM reaches except its management and SSH-fabric bridges. Whether all of this
holds live, and what does not: `docs/security-assessment.md`.

**The internet window** (`worms build internet-allow` / `internet-deny`, `ansible/Internet.yml`)
gives the infrastructure and private-internet VMs host NAT for package installs and cache
filling. It is refused while a sandbox VM exists or a devbox VM runs, the round agent starts no
team VM while it is open, and the firewall watchdog stops any that does. The state lives in
`/run/worms/internet-allowed` and runtime-only rules, so a reboot comes back denied.

## Where things are configured

| What | Where |
|---|---|
| Bridges, VMs, per-VM firewall, security groups | OpenTofu: `opentofu/stacks/{platform,firewall,infrastructure/*,private_internet,sandbox,devbox}` |
| VM configuration | Ansible roles by component, `ansible/roles/README.md` |
| The node: sysctls, nftables zone firewall, internet gate, traffic mirror, firewall watchdog | Ansible role `host_network` (`SetupHostNetwork.yml`, `Internet.yml`) |
| The round agent on the node | `scripts/round_agent/`, role `round_agent` |
| The build's order and the internet window | `src/worms_cli/pipeline.py` |
| The dashboard and orchestrator | `projects/dashboard/` (one Django project, two settings modules) |

Stacks read each other's state through `terraform_remote_state`: `platform` and `firewall` first,
then the VM stacks. The sandbox and devbox stacks live in the round agent's workspace on the node,
`/opt/worms`.

## Features in one paragraph each

- **Dashboard** (`https://worms-dev.mlsec.tu-berlin.de/` here): sign-in with a forced password
  change, teams, a Live view (leaderboard, upload, progression) and a Dev view (the devbox, its
  score, logs and replay). Staff pages manage students, teams and rounds; the admin can view the
  dashboard as another user, read only (`docs/accounts.md`).
- **Devbox:** three VMs per team (A–B–C), reached over `ssh -A -J team@<host>:2222 root@<A>`. A
  dev upload resets them and runs the worm on A; a reset rolls them back to a snapshot
  (`docs/devbox.md`).
- **Rounds:** the conductor on the orchestrator plans and drives every team's run; the round
  agent on the node does the privileged work from a fixed, validated job menu
  (`docs/rounds.md`). **Never run on a real node yet.**
- **Monitor:** Zeek records every sandbox connection into the database; every infrastructure VM
  ships its audit log to the syslog VM (`docs/monitoring.md`).
- **Progression:** a team replays its rounds and its devbox; staff replay any run on the
  TOPOLOGY page; teams download them through the team API (`docs/progression.md`,
  `docs/team_api.md`).

## Tests

```sh
worms test          # offline suite: no Proxmox needed
worms simulate      # on the node: the deployed platform end to end, PASS/FAIL report
```

Details and the live suite: `docs/testing.md`.
