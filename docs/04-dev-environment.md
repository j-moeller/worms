# 4. Dev environment: the devbox

Each team gets three always-on VMs, **A–B–C**, to develop against. They correspond to the dashboard's **Dev** view.

- **Services.** Devbox VMs reach the same scoring, logging and private internet as a round, attributed to the team's devbox and never to a round.
- **Dev uploads.** A dev upload resets the devbox and runs the worm on A.
- **What teams see.** The team's side of this is in [8 User guide](08-user-guide.md#devbox).

## What a team gets

| VM | Name | Addresses (team index *i*) |
|---|---|---|
| A | `devbox-t<team id>-a` | link 1: 10.128.2i.11 · SSH fabric 10.7.i.11 · mgmt 10.6.i.11 |
| B | `…-b` | link 1: 10.128.2i.12, link 2: 10.128.(2i+1).12 · 10.7.i.12 · 10.6.i.12 |
| C | `…-c` | link 2: 10.128.(2i+1).13 · 10.7.i.13 · 10.6.i.13 |
| Gateway | `…-gw` | .1 on both links · services 10.2.0.(32+i) · mgmt 10.6.i.2 |

Sources: [`scripts/generate_devbox.py`](../scripts/generate_devbox.py), [`opentofu/stacks/devbox/main.tf`](../opentofu/stacks/devbox/main.tf).

- **VMs:**
  - Debian 13, 1 vCPU (weight 50), CPU type `x86-64-v3` (no `/dev/kvm`), 512 MB, 5 GB.
  - Limits: NIC 10 MB/s, disk 200 IOPS / 20 MB/s.
  - No guest agent and no display.
- **Team access.** Members log in as **root**. Their registered keys are in `/root/.ssh/authorized_keys_worms_team` on A, B and C ([role `devbox`](../ansible/roles/applications/devbox/)).
- **Packages.** `python3`, `python3-pip`, `unzip`; apt and pip point at the private internet.
- **Flags.** Each VM holds a token at `/etc/worms/flag` (`FLAG{<32 hex>}`, mode 0444).
- **Traffic.** All ports are open between a team's own VMs on its two links. The gateway forwards to the services and the LLM endpoint only, never between the links, so B is the only way from A to C. Devbox traffic is **not** recorded by the monitor.
- **Limit.** At most 16 teams (index 0–15, inside 10.128.0.0/19).

## Access: one SSH gateway

```
laptop ──22 (prod) / 2222 (dev)──► node DNAT ──► devbox-ssh-gateway 10.7.0.2 ──22──► A (10.7.i.11)
```

- **Account.** Teams log in to the gateway as the shared user `team`. Each registered key gets one line: `restrict,port-forwarding,command="/usr/sbin/nologin",permitopen="<A>:22"` ([`authorized_keys.j2`](../ansible/roles/applications/ssh_gateway/templates/authorized_keys.j2)). That allows no shell and only the team's own A.
- **sshd.** No agent, X11 or tunnel forwarding; no root, no passwords; `MaxStartups 10:30:60` ([`ssh-gateway.tftpl`](../opentofu/stacks/devbox/templates/)).
- **Firewall.** The gateway accepts 22 only from the node's DNAT address. A devbox VM's fabric NIC accepts 22 only from the gateway.
- **Recording.** The node's collector writes every gateway login, with the key's fingerprint and the client's public address, to `ssh_gateway_logins` once a minute (`worms-audit-gateway.timer`).

**Key lifecycle** ([`ssh_keys.py`](../projects/dashboard/core/ssh_keys.py), [`views/devbox.py`](../projects/dashboard/dashboard/views/devbox.py), [`membership.py`](../projects/dashboard/dashboard/membership.py)). Members add keys on their profile; operators with `worms key add` and `worms key remove`, under the same rules:
- Validation:
  - one OpenSSH public key line of a known type;
  - the comment matches `[A-Za-z0-9._@/+:,-]`;
  - at most 64 keys per team;
  - a key may belong to one team only.
- Adding or removing a key, or a member leaving (which removes the keys they added), enqueues `devbox_reconfigure`. The round agent then writes only the key files ([`DevboxKeys.yml`](../ansible/DevboxKeys.yml): no package, no internet), on the SSH gateway (`/home/team/.ssh/authorized_keys`, lines ending in `team=<slug>`) and on the team's VMs A, B and C (`/root/.ssh/authorized_keys_worms_team`). `worms key` shows whether they arrived; `worms key sync` copies them all again, directly over SSH, and shows them.

## Build and reset

| Action | Who | Host job | What the round agent does ([`handlers.py`](../scripts/round_agent/handlers.py)) |
|---|---|---|---|
| Provision | operator: `worms apply devbox` | `devbox_provision` | Generates `generated_devboxes.tf` for every team, applies the stack, snapshots each new VM as `clean` after its first boot (before any team key), configures, writes tokens and the attribution map |
| Reset | team: **Reset devbox** button; a dev upload; operator: `worms vm reset <team>` | `devbox_rebuild` | Rolls A, B and C back to `clean` (VMs without a snapshot are replaced), configures again (the keys first; packages from the offline apt cache, skipping and naming one it lacks), writes new tokens, updates the map |
| Rebuild | operator: `worms vm rebuild devboxes [--team NAME]` | `devbox_rebuild` (`fresh`) | Replaces A, B and C, snapshots them after their first boot, and goes on as a reset |
| Keys | dashboard, on any key or membership change | `devbox_reconfigure` | Pushes the keys alone (no package, no internet) and the attribution map |
| Dev run | a dev upload | `devbox_deploy` | Places the zip on A and arms `main.sh` |

- **Version.** A reset bumps the team's `devbox_version` at once, so the dev score and logs start over. Nothing is deleted.
- **Queueing.** A rebuild already queued or running is reused, not stacked ([`orchestrator/rounds/devbox.py`](../projects/dashboard/orchestrator/rounds/devbox.py)).
- **Rounds.** While a round builds its sandbox, the devbox VMs are shut down and started again afterwards: a reboot, not a reset. A round with **Shut down every devbox while its attempts run** keeps them off from its first attempt to its last run; resets, key updates and dev uploads wait until then ([5](05-live-environment.md#devboxes-during-a-round)).
- **REST API.** The dashboard reaches the orchestrator over HTTPS with a bearer token (`/devbox/<team>/reset/`, `/devbox/<team>/reconfigure/`; and `/gameplans/<file>/simulation/` for staff's Simulations page, [5](05-live-environment.md#simulation)). The orchestrator writes the host jobs; the dashboard never does.

## Dev uploads and runs

[`devbox_runs.py`](../projects/dashboard/orchestrator/rounds/devbox_runs.py) advances each run on every pass of `process_attempts`:

```
PENDING → RESETTING → DEPLOYING → ARMED → RUNNING → FINISHED
                                        └→ FAILED, REPLACED (a newer upload or a manual reset)
```

- **Which upload runs.** Only a team's newest dev upload runs.
- **Waiting.** A run waits while any round attempt is between PREPARING and TEARING_DOWN, and while a round holds the devboxes shut down. The agent also takes round jobs before devbox jobs.
- **Deploy.** The zip goes to VM A and is started as `bash main.sh` (user `debian`) after a 10 s fuse. systemd stops it after 3600 s (`RuntimeMaxSec`). Processes it started on B or C keep running until the next reset.
- **Errors.** The error text is for staff only. Teams see a status line on the Upload page.

## Scoring and logging

- **Attribution.** By source address: each devbox-gw's services address (10.2.0.32+i) is mapped to its team in `/etc/sandbox-api/devbox-teams` on scoring and logging. The round agent writes this file and restores it after the service VMs roll back.
- **Captures.** A capture counts once per team, VM and `devbox_version`, only for the team's own tokens, and with no time window ([`captures.py`](../projects/dashboard/orchestrator/rounds/captures.py)).
- **Visibility.** Results are visible at once: the devbox has no publication gate.

## Operations

```sh
worms apply devbox                      # new teams; idempotent
worms team                   # every team's devbox and keys
worms key add alpha ~/alpha.pub        # a key for team alpha (removed again: worms key remove alpha <name>)
worms vm reset alpha                # its VMs back to their snapshot, as its Reset button
worms key sync                         # copy every team's keys to its devbox and the gateway, show them
worms key                              # are every team's keys on the gateway and its VMs?
worms show alpha                        # its four VMs (a, b, c, gw): vmids, addresses, states, keys
worms login alpha/gw                    # a shell on team alpha's NAT gateway, devbox-t<id>-gw (`worms login --help` lists the VMs)
tail -F /var/log/worms/jobs/latest.log  # on the node: the running job
```

Data: `devboxes`, `devbox_vms`, `devbox_ssh_keys`, `devbox_runs`, `devbox_captures`, `devbox_logs` ([`core/models/devbox.py`](../projects/dashboard/core/models/devbox.py)).

## Known gaps

- **Reset window.** The version is bumped when the reset is requested, but the old tokens stay valid until the rebuild finishes.
- **Isolation.**
  - Port isolation on vmbr6/vmbr7 is not active: teams share layer 2 there, and only the per-VM firewall separates them (S4).
  - The gateways run CPU type `qemu64`, which nothing checks.
- **SSH agent.** The dashboard tells teams to use `ssh -A`, which forwards their agent into VMs that are rooted by design ([7](07-security.md#findings)).
