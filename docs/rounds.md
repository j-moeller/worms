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
| PREPARING | agent (`prepare`) | rebuild ephemeral/PI (optional), generate + apply the sandbox, install the internal CA into the sandbox guests (SetupSandbox.yml), register its VMs and topology |
| CHECKING | agent (`check`) | firewalls up, mirror receive-only, every sandbox VM running |
| DEPLOYING | agent (`deploy`) | fetch + verify the worm, put it on the start VM, release the initial secrets, arm the fuse |
| ARMED | conductor | wait until `worm_start_at` (the worm starts itself on the VM) |
| RUNNING | conductor | run the game plan: stop/start VMs (its own token), request secret releases (agent) |
| ENDING | conductor | stop every VM, let the monitor drain |
| TEARING_DOWN | agent (`teardown`) | destroy the sandbox |
| FINISHED / FAILED / SKIPPED | — | terminal; the next run starts |

The deadline, uploads and the running order are the planner's
(`orchestrator/rounds/planner.py`): one run per team in a seed-shuffled order,
each with the team's latest upload before `round.round_start`.

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

A VM's secret (the flag) is generated by the **agent** when it releases it
(`release_secrets` / the initial batch in `deploy`), written into the VM at
`/etc/worms/flag`, and only its SHA-256 is returned. The plaintext never
reaches the database, so a compromised orchestrator or dashboard cannot hand a
team its flags. The conductor stores the hash on the VM row and emits a
`secret_released` timeline event.

A worm captures a VM by submitting its flag to the scoring server. **No access
token is used at scoring/logging**: only one team runs at a time, so the round
agent writes the running team's uuid into `/etc/sandbox-api/current-team` on
the scoring and logging servers at `deploy`, and they record that uuid on every
attempt (`team_uuid`). Access tokens still exist for teams in general; they are
just not used by these servers anymore, which no longer touch `access_tokens`
at all.

A dedicated continuous process on the orchestrator, `manage.py
process_attempts` (daemon, role `django_app`), is the **single writer** of
results: it turns a `flag_capture_attempt` into a `flag_capture` when the
submitted hash is a released secret of a VM in **that team's own live run** and
the VM was not already captured by the team, and records a `captured` timeline
event (`orchestrator/rounds/captures.py`). Logging attempts are attributed the
same way. It runs independently of the conductor (the conductor no longer
processes attempts), so captures appear within a second or two of a
submission.

## Timeline (the animated view's data)

`GET /rounds/<uuid>/timeline/` (staff) returns, from
`orchestrator/rounds/timeline.py`, everything the animated view replays on a
single clock (seconds from the worm's start):

- `nodes` (sandbox VMs, which is the start VM, which are captured),
  `networks`, `edges` — the architecture to draw;
- `events` — state changes, VMs stopped/started, secrets released, worm
  deployed/started, captures;
- `flows` — connections from the monitor (`network_flows`) between sandbox
  VMs in the run's window: the dots to move between nodes, with transport,
  detected protocol and size;
- `captures` — when each VM was captured;
- `architecture` — the full generator graph saved at prepare time (every
  layer: internet, edge/core/distribution/access switches, networks and VMs,
  with all edges): the backdrop to draw behind the sandbox VMs.

The run also stores `sandbox_definition`, the generated OpenTofu
configuration for the sandbox (the whole stack definition for this run), kept
for reference and reproducibility; it is not part of the timeline JSON.

The player itself (drawing the graph, moving the dots, the scrubber) is
front-end work still to come; `/rounds/` lists the runs and links the JSON.

## Operating it

```
# One-time, part of `worms build provision`:
#   - orchestrator runs conduct_rounds AND process_attempts as daemons (role django_app)
#   - the Proxmox host runs the round agent (SetupRoundAgent.yml)
#   - the orchestrator's power token gets VM.PowerMgmt+VM.Audit on pool sandbox

worms build provision

# The conductor plans and runs rounds on its own once a round's deadline
# (round_start) has passed. To watch:
ssh debian@10.3.0.19 'journalctl -u orchestrator-conduct-rounds -f'    # conductor
ssh debian@10.3.0.19 'journalctl -u orchestrator-process-attempts -f'  # captures/logs
ssh root@<proxmox>  'journalctl -u worms-round-agent -f'              # host agent

# Inspect state:
SELECT team_id, state, position FROM round_runs ORDER BY round_id, position;
SELECT kind, state, error FROM host_jobs ORDER BY id DESC LIMIT 10;
```

Timings (`ROUND_CONDUCTOR` in `config/settings/orchestrator.py`, env-tunable):
`worm_start_delay` (the fuse, 300 s), `round_duration` (3600 s),
`end_drain`, `job_timeout`, `capture_grace`.

Rebuilding the ephemeral and private-internet VMs each round (a clean slate)
is opt-in (`round_agent_reset_services`), because it needs an internet window
and so can only happen while no sandbox exists; the conductor tears the
previous sandbox down first.

`worms simulate` (`docs/testing.md`) checks the scoring path end to end: a flag
submitted through the sandbox API becomes a `FlagCapture` and a `captured`
timeline event after `manage.py process_captures` runs, and it checks that the
conductor daemon and the round agent are up. The conductor and agent
themselves are covered offline in `tests/test_rounds.py` and
`tests/test_round_agent.py`.

## What is not done here

- **The animation front-end** (the player). The data contract above is ready.
- **Sandbox guest config beyond the CA.** `prepare` installs the internal CA
  into the sandbox guests (SetupSandbox.yml), so a worm reaches scoring/logging
  over verified HTTPS; anything else the guests need (the worm start assumes
  `python3` and `unzip` in the image) is not configured yet.
- **The dashboard media path.** The agent reads uploads from
  `round_agent_dashboard_media_root` on the dashboard VM; the dashboard's
  `default_storage` must write there (`MEDIA_ROOT` is not set yet — see
  `docs/findings.md`).
- **Scoring/standing display** of captures over the round.
- **Running it on a real Proxmox host.** Everything here was built and tested
  offline (`worms test`): the conductor and agent are exercised end to end with
  a fake Proxmox and a fake agent / fake executor against a real PostgreSQL,
  but no sandbox has been built on a live node.
