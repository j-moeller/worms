# Devbox: team-personalised development environments

A **devbox** is a team's own, always-on clone of the sandbox VMs. Teams develop
and test their worm against it between (and during) rounds, over SSH, without
touching the competition. It is built from the same resources as the sandbox
and is held to the same containment — but it is reachable **only by its team**,
and devbox traffic can **never** reach the sandbox (or vice versa), even though
they share the hypervisor and the scoring/logging/private-internet services.

This document is the design and the security analysis. Status and open items
are at the end.

## What a team gets

Per team, three untrusted VMs **A, B, C** wired in a chain **A–B, B–C** (B is
the only pivot between the two segments), plus a per-team router (**devbox-gw**)
for egress. The VMs are the same as sandbox VMs (Debian image, internal CA
installed, assumed rooted) and carry the same per-VM disk and network limits.
Each VM holds one **token**; the team's **dev score** is the number of distinct
tokens its worm has submitted to scoring at the current devbox version.

```
            team's laptop
                 │  ssh -J team@<gateway> root@<devbox VM>
                 ▼
        devbox SSH gateway (trusted, hardened)      vmbr7 (devbox SSH fabric, port-isolated)
                 │  jump, SSH tty only                         │
                 ▼                                             ▼
   A ─────── B ─────── C        (A on vmbr9xx0, B on both, C on vmbr9xx1)
    \        │        /
     \       ▼       /          default route
      ──► devbox-gw ◄──  ──SNAT──►  services bridge ──►  scoring / logging / private internet
          (per team)              (10.2.0.32+i)         (<team>.scoring.worms.internal, …)
```

## The dashboard: development vs production view

The nav toggles between the **production** view (the competition: leaderboard,
captures, logs) and the **development** view (the devbox). This reuses the
existing `is_dev_environment` session flag; the home page dispatches
(`dashboard/views/devbox.py`):

- **Production** shows the leaderboard (and the usual CAPTURES/LOGS pages).
- **Development** shows **no leaderboard**. It shows the team's devbox: the
  current **score** (tokens found in A/B/C at the current version), the VMs,
  the devbox **logs**, the team's **SSH keys** (add/remove), how to connect, and
  a **Reset** button.

Both views show scoring and logging feedback for their own domain: the
production view from `flag_captures`/`logging`, the development view from
`devbox_captures`/`devbox_logs`.

## Reset without deleting data: the devbox version

Resetting rebuilds the three VMs and "clears the score", but we keep every
record. Each team has a `devbox_version` (on `teams`). A reset:

1. goes through the orchestrator (the dashboard role may not change the
   version): `POST /devbox/<team>/reset/` **increments** `devbox_version` and
   enqueues a `devbox_rebuild` host job;
2. the round agent rebuilds the team's VMs (fresh disks) and writes new tokens.

Devbox captures and logs are **tagged with the version in force** when they are
recorded (`devbox_captures.devbox_version`, `devbox_logs.devbox_version`). The
dashboard only shows the **current** version, so the score restarts at zero
while the old rows stay for inspection. Nothing is deleted.

## Attribution without interfering with the round

The scoring and logging servers used to assume only one team runs at a time
(the round), attributed via `/etc/sandbox-api/current-team`. The devbox breaks
that assumption: any team's devbox can submit at any time, including during a
round. The two must not be confused.

Solution — **attribution by source address**:

- each team's **devbox-gw SNATs** its VMs to **one** services-bridge address
  from the block `10.2.0.32/27` (`Devbox.services_source_ip`, fixed by the
  team's stable index);
- the scoring/logging servers read a map `/etc/sandbox-api/devbox-teams`
  (`<source-ip> <team-uuid>`, written when devboxes are provisioned). A request
  whose source (passed by nginx as `X-Client-IP`) is in the map is a **devbox**
  submission **for that team**; anything else falls through to the single
  running round (the current-team file). The `devbox` column on the attempt
  records which it was;
- the attempt processor turns devbox attempts into `DevboxCapture`s of the
  team's **own** VMs at the **current** version (and `DevboxLog`s), never into
  round captures.

Why this can't be forged or crossed:

- a devbox-gw's services NIC is **ipfilter-pinned** to its one address, so a
  team cannot source-spoof another team's identity;
- the gateways and sandbox-gw sit in the same services subnet, so the host
  nftables **drops gateway↔gateway** traffic on the services bridge — a devbox
  can reach the service VMs, never another gateway;
- devbox and round submissions use **separate rate-limit buckets**, so one
  cannot starve the other.

### `<team>.scoring.worms.internal`

dnsmasq's `address=/scoring.worms.internal/<ip>` already answers every
subdomain, so `<team>.scoring.worms.internal` resolves to the scoring server
with no per-team DNS records. The scoring/logging certificates carry a wildcard
SAN (`*.scoring.worms.internal`, `*.logging.worms.internal`), so a devbox
reaches its per-team name over **verified** HTTPS. The hostname is per-team for
clarity and TLS; the enforced attribution is the ipfilter-pinned SNAT source
above (a name alone is not a network control).

## SSH access: a locked-down gateway

Teams never reach a devbox VM directly. A single **trusted, hardened** SSH
gateway (OpenTofu, published by host DNAT on port `2222`) is the only way in:

```
ssh -J team@<proxmox-wan>:2222 root@<devbox VM>
```

The gateway is deliberately minimal (`ssh-gateway.tftpl`): agent, X11, stream
and TCP listen forwarding are all off; root login and passwords are off. The
real confinement is **per key**: the `ssh_gateway` Ansible role writes the
team account's `authorized_keys` with, for every registered key,

```
restrict,port-forwarding,permitopen="<the team's own devbox VMs>:22" <key>
```

`restrict` disables every feature; `port-forwarding` re-enables only TCP
forwarding, and `permitopen` limits it to **that team's** VM addresses on the
devbox SSH fabric. So a key can only `ProxyJump` into its own team's devbox and
forward nothing else — the SSH tty is the only thing that flows. The gateway
reaches the VMs over **vmbr7**, which is **port-isolated** (hookscript), so the
VMs can reach the gateway and the host but never each other across teams.

Teams manage their keys on the dashboard dev view (`DevboxSSHKey`); the keys are
installed both on the gateway (for the jump) and on the devbox VMs (for the
final auth) on the next provision/rebuild.

## Isolation: devbox ⇔ sandbox can never communicate

Enforced in depth, by construction:

| Layer | Why they cannot meet |
|---|---|
| Networks | devbox `10.128.0.0/10` and sandbox `10.64.0.0/10` are disjoint; VMs on different, dedicated bridges (`vmbr9*` vs `vmbr1*`). |
| Host routing | the host has no address on either family and never routes `vmbr9*`↔`vmbr1*`; the FORWARD policy is drop and only *same-bridge* traffic is accepted. |
| Services bridge (shared) | both environments reach scoring/logging/PI here, but the host drops **gateway↔gateway**, and each gateway's Proxmox firewall accepts no inbound service connections — so neither can reach the other's SNAT address. |
| Per-VM firewall | the `devbox`/`sandbox` security groups scope traffic to their own network; a devbox VM cannot even address a sandbox VM. |
| Reset budgets | separate nft rate sets and rate buckets. |

What a devbox **can** reach: its own team's devbox VMs (the experiment),
scoring and logging (team-attributed), the private internet, and DNS — all via
its devbox-gw. What it **cannot** reach: the Proxmox host, the database, the
dashboard, the orchestrator, the monitor, wazuh, the sandbox, and other teams'
devboxes.

## Resources, limits, maintenance

Devbox VMs live on the same untrusted storage as the sandbox
(`local-zfs-sandbox`) with the same CPU weight, NIC rate limits and disk
IOPS/bandwidth caps, and the same per-VM host-side rate limits (new TCP
connections, UDP packet rates). They are reachable for maintenance (Ansible)
only from the host on the **untrusted devbox maintenance bridge** `vmbr6`
(port-isolated, host SSH only), exactly like the sandbox management bridge.

## Data model (`core/models/devbox.py`)

- `Devbox` (one per team): `index` (stable 0..31), `services_source_ip`, the
  SSH-gateway address/port, the gateway VMID.
- `DevboxVM` (A/B/C): identifier, role, maintenance IP, VMID, addresses,
  `ctf_flag_hash` (the token; plaintext only ever on the VM).
- `DevboxSSHKey`: a team's registered public key.
- `DevboxCapture` / `DevboxLog`: tagged with `devbox_version`; unique per
  (team, VM, version) / per attempt.
- `Team.devbox_version`; `devbox` flag on `flag_capture_attempts` /
  `logging_attempts`.

Least privilege: the public dashboard role may only INSERT/DELETE
`devbox_ssh_keys`; the orchestrator role writes the devbox tables and bumps the
version; the scoring/logging roles only add the `devbox` column to their one
INSERT. (`tests/test_devbox.py`.)

## OpenTofu and provisioning

- `opentofu/stacks/devbox` (+ `opentofu/modules/devbox_vm`): the per-team
  bridges, VMs and gateway, and the shared SSH gateway. `generated_devboxes.tf`
  is written by `scripts/generate_devbox.py` (one block per team, deterministic
  addressing from the index). Validated with `tofu validate`.
- Provisioning happens **when teams are created**: `worms teams create`
  (and `manage.py provision_devboxes`) ensures a `Devbox` per team and enqueues
  a `devbox_provision` host job; the round agent (`scripts/round_agent`,
  handlers `devbox_provision` / `devbox_rebuild`) generates + applies the stack,
  configures the VMs and the gateway (CA, team keys), writes the tokens and
  publishes the source→team map. `worms build devbox-apply` / `worms build devbox-configure`
  are the underlying targets.

## Operating it

```
worms build devbox                 # make devbox-apply + devbox-configure
worms teams create "Neural Net"    # also enqueues devbox provisioning
# a team resets from the dashboard dev view (Reset devbox), or:
#   POST https://orchestrator/devbox/<team-uuid>/reset/   (dashboard token)
```

A team connects with `ssh -J team@<gateway>:2222 root@<their devbox VM>` using a
key they registered on the dashboard.

## Status / not done here

Built and covered offline (`worms test`): the data model, scoring/logging
attribution and the processor (`tests/test_devbox.py`,
`tests/test_sandbox_api.py`), the generator (`tests/test_generate_devbox.py`),
the agent parameter validation (`tests/test_round_agent.py`), the CLI
provisioning trigger (`tests/test_manage_cli.py`), and the host nftables render.
The OpenTofu (firewall + devbox stack + module) passes `tofu validate` with
bpg/proxmox 0.118.0.

**Not yet run on a live Proxmox host** (none here) — the same caveat the
sandbox and round orchestration carry (`docs/findings.md`): VM creation, the
`-replace` rebuild, the Ansible key plumbing end to end, and the SSH gateway's
`permitopen` confinement in practice. See the security TODO in
`docs/findings.md`.
