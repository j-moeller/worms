# Round orchestration

How a round is run: every team's worm is evaluated, one team after another,
in a freshly built sandbox with its own game plan. This document grows with the
implementation (see "Status" at the end).

## The problem: privileges

Building a sandbox is a host-level operation:

| Step | Needs |
|---|---|
| sandbox bridges (`vmbr1xxx`) | changing the host's network configuration (Proxmox `Sys.Modify` on the node) |
| cloud-init snippets, hookscript | root SSH to the node (the OpenTofu provider uploads them) |
| VMs, disks, firewall rules, pools | `VM.Allocate`, `Datastore.AllocateSpace`, `VM.Config.*`, firewall rights |
| resetting ephemeral / private-internet VMs | the same, or snapshot rollback |
| internet window, host nftables | root on the host |
| secrets and the worm into the sandbox VMs | SSH on the sandbox management bridge (only the host is there) |

A Proxmox token that can do all that is equivalent to root on the host. The
orchestrator is a VM; handing it such a token would make "orchestrator
compromised" equal to "host compromised". Only **power control** of
sandbox VMs (start/stop) fits a least-privilege token (`VM.PowerMgmt` +
`VM.Audit` on the pool `sandbox`).

## Design: decide on the orchestrator, execute on the host

```
          orchestrator VM (low privilege)                     Proxmox host (root)
 ┌──────────────────────────────────────────┐        ┌──────────────────────────────────┐
 │ conductor (manage.py conduct_rounds)     │        │ worms-round-agent                │
 │  - run order, seeds, game plan           │  jobs  │  - fixed menu of jobs            │
 │  - state machine per team run            │ ─────▶ │  - validates every parameter     │
 │  - game plan: stop/start VMs  ───────────┼──┐     │  - prepare / check / deploy /    │
 │    (Proxmox token: power, pool sandbox)  │  │     │    release_secrets / teardown    │
 │  - captures, timeline events             │  │     │  - OpenTofu, Ansible, qm, ssh    │
 └──────────────────────────────────────────┘  │     └──────────────────────────────────┘
                  │  INSERT job, read result    │            │ pulls jobs, writes results
                  ▼                             ▼            ▼
             database (host_jobs, round_runs, ...)     Proxmox API (power only)
```

- The **conductor** on the orchestrator decides everything: which team runs
  next, the sandbox's seed and size, the game plan, when to stop which VM and
  when to release which secret. It controls VM power itself, through the
  Proxmox API with a token restricted to power on the `sandbox` pool.
  Everything that needs more than that, it **requests** as a row in
  `host_jobs`.
- The **round agent** on the Proxmox host **pulls** those jobs (LISTEN/NOTIFY
  plus polling). No new path into the host: the only new flow is host →
  database:5432. It executes only a fixed menu of jobs (`prepare`, `check`,
  `deploy`, `release_secrets`, `teardown`) with strictly validated
  parameters (integers in ranges, UUIDs, hashes, a storage path matching a
  pattern) and never runs a string taken from the database. Its database role
  can only read jobs and write their results.
- **Secrets never exist in the database in plaintext.** The agent generates a
  VM's secret when it releases it, writes it into the VM (`/etc/worms/flag`)
  and reports only its SHA-256. The scoring server compares hashes. A
  compromised orchestrator or dashboard cannot hand flags to a team.
- **The worm artifact is verified.** The agent fetches the team's zip from the
  dashboard VM and checks its SHA-256 against the upload record before
  copying it to the start VM.

The trust direction is one-way: the agent trusts nothing it reads from the
database; the conductor trusts the agent's results (it is the host).

## Status

Design. Implementation follows in the next commits.
