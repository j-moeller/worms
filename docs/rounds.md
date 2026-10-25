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
  can only read jobs and write their results. It runs from its own workspace,
  `/opt/worms` (role `round_agent`, synced from the controller's checkout):
  the sandbox and devbox stacks and their state are the node's; the
  controller's stacks, the vault and the CA key are not there.
- **Secrets never exist in the database in plaintext.** The agent generates a
  VM's secret when it releases it, writes it into the VM (`/etc/worms/flag`)
  and reports only its SHA-256. The scoring server compares hashes. A
  compromised orchestrator or dashboard cannot hand flags to a team.
- **The worm artifact is verified.** The agent fetches the team's zip from the
  dashboard VM and checks its SHA-256 against the upload record before
  copying it to the start VM. Every command the agent runs on a VM goes over
  ssh as one command line quoted for the VM's shell (`Executor.remote_command`),
  so the VM runs exactly the argument list the agent built.

The trust direction is one-way: the agent trusts nothing it reads from the
database; the conductor trusts the agent's results (it is the host).

## The run lifecycle

One `RoundRun` per team and round, run one at a time (one sandbox fits on the
host). The conductor (`manage.py conduct_rounds`, a loop) advances the active
run one step per tick and starts the next when it finishes, so a crash just
resumes from the database. States (`core/models/rounds.py`):

```
PENDING ── prepare ─▶ PREPARING ─▶ CHECKING ─▶ DEPLOYING ─▶ ARMED
  │ (no upload)                                               │ worm_start_at
  ▼                                                           ▼
SKIPPED                                                     RUNNING ── game plan
                                                              │ ends_at
FINISHED ◀── TEARING_DOWN ◀── ENDING ◀────────────────────────┘
   ▲                                                    (stop VMs, drain monitor)
   └─ FAILED (on any error / failed job) also tears the sandbox down
```

| State | Who | What |
|---|---|---|
| PENDING | — | planned, waiting its turn; no upload → SKIPPED |
| PREPARING | agent (`prepare`) | generate + apply the sandbox, install the internal CA into the sandbox guests (SetupSandbox.yml), register its VMs and topology (the ephemeral/private-internet VMs are not rebuilt per run: `worms build round-reset-services` between rounds) |
| CHECKING | agent (`check`) | firewalls up, mirror receive-only, every sandbox VM running |
| DEPLOYING | agent (`deploy`) | fetch + verify the worm, copy it to the start VM and unpack it there (a zip without `main.py` at its top level fails the run), release the initial secrets, arm the fuse; report when it was armed |
| ARMED | conductor | wait until `worm_start_at` (the worm starts itself on the VM) |
| RUNNING | conductor | run the game plan: stop/start VMs (its own token), request secret releases (agent) |
| ENDING | conductor | stop every VM, let the monitor drain |
| TEARING_DOWN | agent (`teardown`) | clear the running team on scoring/logging, destroy the sandbox |
| FINISHED / FAILED / SKIPPED | — | terminal; the next run starts |

The deadline, uploads and the running order are the planner's
(`orchestrator/rounds/planner.py`): one run per team in a seed-shuffled order,
each with the team's latest upload before `round.round_start`, and each with
the round's parameters (below).

## Defining a round (dashboard, staff)

Staff create rounds on the dashboard (Staff → Rounds;
`dashboard/views/staff_rounds.py`). A round is only a database record: its
window, its seed, and `sandbox_params`, which every run of the round gets
(`core/schedule.py`):

| | Keys | Used by |
|---|---|---|
| sandbox | `vms`, `networks`, `sites`, `max_interfaces` | the round agent's `prepare` (`scripts/generate_sandbox.py`; validated again by the agent with the same bounds) |
| game plan | `game_plan`: `initial_fraction`, `first_stop_after`, `stop_interval`, `min_alive`, `restart_after` | the conductor (`gameplan.py`) |
| timing | `timing`: `worm_start_delay`, `run_duration` (seconds) | the conductor (fuse, run length); defaults `ROUND_TIMING` |

Nothing is built when a round is created. Runs are sequential, so the form
refuses a Live round too short for every team's slot (fuse + run + drain +
`slot_overhead` for build and teardown, per team) and rounds that overlap. A
round can be edited until it starts (afterwards only its end), and deleted
until its runs exist. The page then shows the runs, their host jobs and
errors, and what each `prepare` built. A Dev round (the development
competition) is only a time window for the devbox progression.

## The game plan

`orchestrator/rounds/gameplan.py` turns the run's seed into a deterministic
plan that distinguishes worms from single-VM scripts:

- A **start VM** is chosen and gets the worm. Its secret and a share of the
  others' are released up front; the rest are **delayed**.
- VMs are **stopped** on a schedule (`first_stop_after`, then every
  `stop_interval`) while keeping at least `min_alive` running. A program that
  drives everything from its start VM dies when that VM is switched off.
- Each **delayed secret** is released only *after* a VM has been stopped (VM
  B's secret appears once VM A is off), so it can only be captured by a worm
  that is still alive elsewhere. A VM whose secret has not appeared yet is
  never stopped first.
- Optionally VMs are restarted later (`restart_after`).

Parameters come from the run's `sandbox_params.game_plan`; the defaults and
the exact rules are in the module docstring. The plan is stored as
`GamePlanStep` rows; a step is due when its offset after the worm start has
passed and every step it lists in `after` is done.

## Secrets and captures

## The worm on the start VM

`deploy` copies the zip to `/home/debian/user-worm.zip` and unpacks it with
Python's `zipfile` into `/home/debian/worm/` (python3 is in every Debian cloud
image; `unzip` is not; entries cannot leave that directory). The fuse is a
one-shot systemd timer (`systemd-run --on-active=<fuse>`) that runs
`/usr/bin/python3 main.py` in `/home/debian/worm` as `debian` -- the worm's
contract for teams is `docs/worm-contract.md`. The job reports `armed_at`, the
moment the timer was armed; the conductor dates the worm's start from it.

A failed run (any failed host job or error) stops every VM at once, closes
the run's capture window and tears the sandbox down; the next run waits for
that teardown.

A VM's secret (the flag) is generated by the **agent** when it releases it
(`release_secrets` / the initial batch in `deploy`), written into the VM at
`/etc/worms/flag` (`install -D`: the directory is created), and only its
SHA-256 is returned. The plaintext never
reaches the database, so a compromised orchestrator or dashboard cannot hand a
team its flags. The conductor stores the hash on the VM row and emits a
`secret_released` timeline event.

A worm captures a VM by submitting its flag to the scoring server. **No access
token is used at scoring/logging**: only one team runs at a time, so the round
agent writes the running team's uuid into `/etc/sandbox-api/current-team` on
the scoring and logging servers at `deploy`, and they record that uuid on every
attempt (`team_uuid`). `teardown` deletes that file again, so between runs the servers
refuse round submissions (409). Access tokens still exist for teams in
general; they are just not used by these servers anymore, which no longer
touch `access_tokens` at all.

A dedicated continuous process on the orchestrator, `manage.py
process_attempts` (daemon, role `django_app`), is the **single writer** of
results: it turns a `flag_capture_attempt` into a `flag_capture` when the
submitted hash is a released secret of a VM in **that team's own live run** and
the VM was not already captured by the team, and records a `captured` timeline
event (`orchestrator/rounds/captures.py`). Logging attempts are attributed the
same way. It runs independently of the conductor (the conductor no longer
processes attempts), so captures appear within a second or two of a
submission. The run's window is `[worm_start_at, ends_at]` widened by
`capture_grace` at both ends (the start is dated by the host's clock, the
submission by the database's). A secret's hash reaches the VM row a few
seconds after the agent wrote the secret into the VM; a submission made in
between is credited afterwards (`process_late_releases`, same daemon).

## Timeline (the round player's data)

`orchestrator/rounds/timeline.py` assembles everything the round player
replays on a single clock (seconds from the worm's start); it is served as
part of the run's progression (`docs/progression.md`: staff at
`/rounds/<uuid>/progression/`, the team — only what it captured — on the
PROGRESSION page and through the team API):

- `nodes` (sandbox VMs, which is the start VM, when each was captured),
  `networks`, `edges` — the sandbox to draw; `vm_status` — each VM's power
  state over time;
- `events` — state changes, VMs stopped/started, secrets released, worm
  deployed/started, captures, plus the team's flag submissions (on scoring)
  and logs (on logging, as references to their records), each attributed to
  the VM whose connection to that server was open at the time (inferred: the
  servers only see the gateway);
- `flows` — every connection from the monitor (`network_flows`) with an end
  in the run, the far end mapped to scoring / logging / DNS / a private
  internet VM through the live Proxmox snapshot: the dots;
- `captures` — when each VM was captured;
- `architecture` — the full generator graph saved at prepare time (every
  layer: internet, edge/core/distribution/access switches, networks and VMs,
  with all edges).

The run also stores `sandbox_definition`, the generated OpenTofu
configuration for the sandbox (the whole stack definition for this run), kept
for reference and reproducibility; it is not part of the timeline JSON.

The player is the TOPOLOGY page (pick the round, then the team;
`docs/visualization.md`); `/rounds/` lists the runs with a "play" link.

## Operating it

```
# One-time, part of `worms build provision`:
#   - orchestrator runs conduct_rounds AND process_attempts as daemons (role django_app)
#   - the Proxmox host runs the round agent (SetupRoundAgent.yml)
#   - the orchestrator's power token gets VM.PowerMgmt+VM.Audit on pool sandbox

worms build provision

# Create a round on the dashboard (Staff → Rounds). The conductor plans and
# runs it on its own once its deadline (round_start) has passed. The dashboard
# shows the runs and host jobs; to watch the services themselves:
ssh debian@10.3.0.19 'journalctl -u orchestrator-conduct-rounds -f'    # conductor
ssh debian@10.3.0.19 'journalctl -u orchestrator-process-attempts -f'  # captures/logs
ssh root@<proxmox>  'journalctl -u worms-round-agent -f'              # host agent

# Inspect state:
SELECT team_id, state, position FROM round_runs ORDER BY round_id, position;
SELECT kind, state, error FROM host_jobs ORDER BY id DESC LIMIT 10;
```

Timings: a round's own `timing` (set on the dashboard) for the fuse and the
run length; otherwise `ROUND_TIMING` (`config/settings/base.py`, env-tunable:
`worm_start_delay` 300 s, `run_duration` 3600 s, `end_drain`, `slot_overhead`)
and `ROUND_CONDUCTOR` (`job_timeout`, `capture_grace`).

Rebuilding the ephemeral and private-internet VMs for a clean slate is
`worms build round-reset-services` on the controller (it owns those stacks; the
round agent only has the sandbox and devbox stacks). It needs an internet
window, so run it between rounds, while no sandbox exists.

`worms simulate` (`docs/testing.md`) checks the scoring path end to end: a flag
submitted through the sandbox API becomes a `FlagCapture` and a `captured`
timeline event after `manage.py process_captures` runs, and it checks that the
conductor daemon and the round agent are up. The conductor and agent
themselves are covered offline in `tests/test_rounds.py` and
`tests/test_round_agent.py`.

## What is not done here

- **The dashboard media path.** The agent reads uploads from
  `round_agent_dashboard_media_root` on the dashboard VM; the dashboard's
  `default_storage` must write there (`MEDIA_ROOT` is not set yet).
- **Scoring/standing display** of captures over the round.
- **A way from one sandbox VM to the next.** The sandbox VMs accept SSH with
  the operator's key only and run no other service, so a worm has no designed
  way onto a second VM; it can capture its start VM's flag. Seeded
  vulnerabilities are not built yet (`TODO`).
- **Running it on a real Proxmox host.** Everything here was built and tested
  offline (`worms test`): the conductor and agent are exercised end to end with
  a fake Proxmox and a fake agent / fake executor against a real PostgreSQL,
  and the commands the agent sends to a VM are replayed through a real shell
  (`tests/test_round_agent.py`), but no sandbox has been built on a live node.
