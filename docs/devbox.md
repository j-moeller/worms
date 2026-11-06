# Devbox: a team's development environment

A **devbox** is a team's own, always-on set of three untrusted VMs to develop and test its worm
on, between and during rounds, over SSH. It is built like the sandbox and held to the same
containment, but it is reachable **only by its team**, and devbox traffic never reaches the
sandbox or another team's devbox, although all of them share the node and the services. How to
operate it: `docs/management.md`. What it means for a team: `docs/worm-contract.md`. Whether the
containment holds: `docs/security-assessment.md`.

## What a team gets

Three VMs **A, B, C** in a chain (A–B, B–C; B is the only way from A to C), plus a per-team router
**devbox-gw**. Debian, the internal CA trusted, `x86-64-v3` CPU (no `/dev/kvm`), 512 MB RAM, the
sandbox's disk and NIC limits. Each VM holds a **token** in `/etc/worms/flag`; the team's dev
score is the number of distinct tokens submitted to scoring at the current devbox version.

```
 team's laptop ── ssh -A -J team@<public name> root@<A> ──▶ SSH gateway (trusted, vmbr7)
                                                                     │ TCP to the team's A:22 only
   A ─────── B ─────── C        (A on vmbr9xx0, B on both, C on vmbr9xx1)
    \        │        /
     └──▶ devbox-gw ◀──┘  ── SNAT to 10.2.0.32+i ──▶ scoring / logging / DNS / private internet
                                                    + teachllm.mlsec.tu-berlin.de:443
```

## SSH access through one gateway

Teams never reach a devbox VM directly. A shared, hardened **SSH gateway** (published by host
DNAT on port 22; the node's own sshd is on 2222) is the only way in. For every registered key, its `team` account's
`authorized_keys` holds:

```
restrict,port-forwarding,command="/usr/sbin/nologin",permitopen="<the team's A>:22" <key>
```

So a key can only forward TCP to its own team's A on port 22: no shell, command, `scp` or `sftp`
on the gateway (the account's shell is `nologin` too). From A, members go on to B and C with the
forwarded agent; the VMs hold the team's public keys and no private key. The dashboard's
**Connect** panel prints each hop with the team's real addresses and tells users to check
`ssh-add -L`.

```sh
ssh -A -J team@<public name> root@<A>   # on the laptop
ssh -A root@<B on the A-B link>              # on A
ssh root@<C on the B-C link>                 # on B
```

The gateway's own protection: `authorized_keys`, `.ssh` and the home are root-owned; its firewall
accepts SSH only from the node's address on vmbr7, where the host DNAT puts every team.

**Keys** are managed on the dashboard profile. Adding or removing one enqueues a
`devbox_reconfigure` job that re-pushes every team's keys to the gateway and the VMs; leaving a
team removes the member's keys. A key is validated on the dashboard and again by the agent (one
OpenSSH line, a known type, base64, a comment from a safe character set), written to Ansible's
variables tagged `!unsafe`, and may belong to only one team (sshd uses the first matching line, so
a shared key would hand one team another's confinement). Security findings D6 and D7.

## Reset: a new version, VMs rolled back

Each team has a `devbox_version`. A reset (dashboard profile, or a dev upload):

1. goes through the orchestrator (the dashboard's role may not change the version), which
   increments `devbox_version` and enqueues a `devbox_rebuild` job carrying every team's keys;
2. the agent rolls the team's three VMs back to their snapshot `clean`, then installs the keys
   and new tokens. The snapshot is taken when `devbox_provision` creates a VM, after its first
   boot and before any team key is on it. A VM without one is replaced and snapshotted instead.

Captures and logs are tagged with the version in force; the dashboard shows only the current
one, so the score restarts at zero and nothing is deleted. **The rollback has not run on a real
devbox yet**; replacing the VMs took about 8 minutes.

## Uploads: a worm on the devbox

An upload in the dev view runs on the team's devbox the way a round runs on a sandbox, and never
enters a round.

```
dashboard      stores the upload (Upload.devbox = true). That is all it can do
orchestrator   rounds/devbox_runs.py
  PENDING      waits while a round runs, or for a reset in progress
  RESETTING    devbox_rebuild: new version, VMs back to their snapshot, new tokens
  DEPLOYING    devbox_deploy: the zip to VM A, hash checked, unpacked, fuse lit
  RUNNING      bash main.sh as debian after 10 s
  FINISHED     after devbox_run_duration (1 h): systemd stops main.sh; the VMs stay as they are
```

Only the latest upload runs (a newer one or a manual reset replaces it); only on the team's own
A (the agent refuses anything else); a failed job's error stays with staff. The time limit
bounds `main.sh` only: what it started on B or C runs until the next reset. Live on 2026-11-05:
8 min 36 s from upload to start, flag scored, log received. While a reset runs the agent does
nothing else, so uploads from several teams queue behind each other.

## Attribution without touching a round

Any team's devbox can submit at any time, including during a round. Scoring and logging tell
them apart by source address:

- each devbox-gw SNATs its VMs to **one** services address from `10.2.0.32/27`
  (`Devbox.services_source_ip`, from the team's stable index), and that NIC is ipfilter-pinned;
- scoring and logging read `/etc/sandbox-api/devbox-teams` (`<source ip> <team uuid>`), written
  by every devbox job and restored by the agent after a rollback of the services. A source in the
  map is a devbox submission for that team; only the sandbox-gw's source maps to the running
  round; any other source is refused (fail closed);
- devbox and round submissions have separate rate-limit buckets.

`<team>.scoring.worms.internal` resolves to scoring through a wildcard record and is covered by the
certificate, for verified HTTPS. The name is for clarity; the source address is what counts.

## Isolation from the sandbox and other teams

| Layer | What separates them |
|---|---|
| Networks | Devbox `10.128.0.0/10` and sandbox `10.64.0.0/10` are disjoint, on different bridges (`vmbr9*`, `vmbr1*`) |
| Node | No address on either; never routes `vmbr9*`↔`vmbr1*`; drops gateway↔gateway on the services bridge |
| Per-VM firewall | `devbox`/`devbox_gw` groups: own team's networks, the gateway, the service hosts on their ports, `teachllm:443` |
| Gateways | Accept no inbound service connections, so no one reaches another's SNAT address |
| Rate limits | Separate nftables sets and scoring/logging buckets |

**What every team shares:** vmbr6 (maintenance, SSH from the node only) and vmbr7 (the SSH
fabric). Their port isolation is **not active**, so all teams' VMs share layer 2 there, and only
the per-VM firewall keeps them apart; ICMP errors pass even that (S19). One conntrack table on the
node serves all of them (S18). Both are open findings in `docs/security-assessment.md`.

## Data model and privileges

`core/models/devbox.py`: `Devbox` (index 0–31, `services_source_ip`, gateway), `DevboxVM` (role,
addresses, the token's hash), `DevboxSSHKey`, `DevboxCapture`/`DevboxLog` tagged with the version,
`Team.devbox_version`, and a `devbox` flag on the attempts. The dashboard role may only
insert/delete `devbox_ssh_keys`; the orchestrator writes the rest; scoring and logging only add
the `devbox` column to their one INSERT.

## Build

`opentofu/stacks/devbox` with `modules/devbox_vm`; `generated_devboxes.tf` is written by
`scripts/generate_devbox.py` (deterministic addressing from the team index). `worms devbox
provision` enqueues a `devbox_provision` job; the agent generates and applies the stack,
snapshots new VMs after their first boot, configures VMs and gateway, writes tokens and the map.
Devbox traffic is not mirrored to the monitor, so a devbox progression has no network data.
