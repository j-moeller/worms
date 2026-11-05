# Round orchestration

How a round runs: every team's worm is evaluated, one team after another, in the round's sandbox,
rolled back to the same clean snapshot for each team, with its own game plan. **None of this has
run on a real node**: the conductor and the agent are tested offline, with a fake Proxmox and a
real PostgreSQL. How to operate a round: `docs/management.md`. What a team uploads:
`docs/worm-contract.md`.

## Decide on the orchestrator, execute on the node

Building a sandbox needs bridges, VMs, firewall rules, snapshots and SSH into the VMs: a token
that can do all that is root on the node. So the orchestrator VM decides and the node executes:

```
 orchestrator VM (low privilege)                       Proxmox node (root)
 conductor (manage.py conduct_rounds)       jobs       worms-round-agent
  - run order, seeds, game plan            ─────▶      - fixed menu of jobs, every parameter validated
  - state machine per team run           host_jobs     - OpenTofu, Ansible, qm, ssh
  - VM power for the game plan                         - writes results back
    (token: VM.PowerMgmt + VM.Audit on pool sandbox)
```

- The **conductor** decides everything: which team runs next, the seeds, the game plan, when to
  stop a VM or release a secret. It powers sandbox VMs itself with a pool-scoped token and asks
  for everything else as a row in `host_jobs`.
- The **round agent** pulls jobs (LISTEN/NOTIFY plus polling): the only new flow is node →
  database:5432. It runs a fixed menu (`prepare`, `check`, `deploy`, `release_secrets`,
  `teardown`, and the devbox jobs) with strictly validated parameters, never a string from the
  database. Its database role can only read jobs and write their results. Its workspace,
  `/opt/worms`, holds no vault and no CA key.
- **Secrets never reach the database in plaintext.** The agent generates a VM's flag when it
  releases it, writes it to `/etc/worms/flag` on the VM and reports only its SHA-256.
- **The worm is verified.** The agent fetches the zip from the dashboard VM's media root and
  checks its SHA-256 against the upload record before copying it to the start VM.

The trust is one-way: the agent trusts nothing it reads from the database.

## The run lifecycle

One `RoundRun` per team and round, one at a time. The conductor advances the active run one step
per tick, so a crash resumes from the database.

| State | Who | What |
|---|---|---|
| PENDING | | Waiting. No upload, or not enough time left before the round's end → SKIPPED |
| PREPARING | agent `prepare` | The round's sandbox: built and snapshotted for the first run, rolled back for later ones |
| CHECKING | agent `check` | Firewalls up, mirror receive-only, every VM running, CPU exactly `x86-64-v3` |
| DEPLOYING | agent `deploy` | Fetch and verify the worm, unpack it on the start VM, release the initial secrets, arm the fuse |
| ARMED | conductor | Wait for `worm_start_at` |
| RUNNING | conductor | The game plan: stop/start VMs, request secret releases |
| ENDING | conductor | Stop every VM, let the monitor drain |
| TEARING_DOWN | agent `teardown` | Clear the running team on scoring/logging; keep the sandbox powered off if another run of the round is to come, else destroy it |
| FINISHED / FAILED / SKIPPED | | Terminal. FAILED stops every VM and tears down; the next run waits for that |

The planner (`orchestrator/rounds/planner.py`) makes one run per team in a seed-shuffled order,
each with the team's latest Live upload before the round's start, and starts a run only if it can
finish before the round's end. A round's results are therefore final at its end.

## One sandbox per round, from snapshots

All runs of a round share one sandbox, generated from the round's seed and parameters, so every
team meets the same network (`scripts/round_agent/snapshots.py`):

1. **The first run** finds no sandbox for this seed. `prepare` destroys whatever is there, shuts
   the running devbox VMs down, rolls the service VMs back to the snapshot `worms build` took,
   writes the devbox map again, applies and configures the sandbox, snapshots every sandbox VM
   (stopped first: no guest agent runs on them), and starts the devboxes again. No team code runs
   anywhere it could touch what the sandbox installs from.
2. **Every later run** rolls each sandbox VM back to that snapshot: a boot instead of a build.
3. **`teardown`** keeps the sandbox, powered off, while another run with an upload is pending, and
   destroys it otherwise.

No internet window is needed: the sandbox installs from the apt cache, which the builds fill with
what team VMs install (`apt_cache_seed_packages`). `prepare` reports under `sandbox` and
`services` what it did. Without a snapshot on every service VM, the services are left as they are.

## Defining a round

Staff create rounds on the dashboard (`dashboard/views/staff_rounds.py`). A round is a database
record: its window, seed and `sandbox_params` (`core/schedule.py`), validated again by the agent:

| Part | Keys | Used by |
|---|---|---|
| sandbox | `vms`, `networks`, `sites`, `max_interfaces`, `vm_ram` | the agent's `prepare` (`scripts/generate_sandbox.py`) |
| game plan | `initial_fraction`, `first_stop_after`, `stop_interval`, `min_alive`, `restart_after` | the conductor (`gameplan.py`) |
| timing | `worm_start_delay`, `run_duration` (seconds) | the conductor; defaults in `ROUND_TIMING` |

## The game plan

`orchestrator/rounds/gameplan.py` turns a run's seed into a deterministic plan that rewards
worms over single-VM scripts:

- A **start VM** gets the worm. Its secret and a share of the others' are released up front;
  the rest are **delayed**.
- VMs are **stopped** on a schedule (the start VM first) while at least `min_alive` keep running.
  A program that drives everything from its start VM dies with it.
- A **delayed secret** is released only after a VM has been stopped, so only a worm still alive
  elsewhere can capture it. Optionally, stopped VMs are restarted later.

## Captures and attribution

The worm captures a VM by submitting its flag to scoring. No token is used: only one team runs at
a time, so `deploy` writes the running team's uuid to `/etc/sandbox-api/current-team` on scoring
and logging, and `teardown` deletes it (round submissions between runs get `409`). Devbox
submissions are told apart by their source address (`docs/devbox.md`).

`manage.py process_attempts` on the orchestrator is the single writer of results: an attempt
becomes a capture when its hash is a released secret of a VM in that team's live run, within
`[worm_start_at, ends_at]` widened by `capture_grace`. A submission made before the hash reached
the database is credited afterwards.

## The worm on the start VM

`deploy` copies the zip to `/home/debian/user-worm.zip`, unpacks it with Python's `zipfile` into
`/home/debian/worm/` (entries cannot leave it) and arms a one-shot systemd timer that runs
`bash main.sh` there as `debian`. Every command the agent runs on a VM is one quoted command line
(`Executor.remote_command`).

## Timeline

`orchestrator/rounds/timeline.py` assembles what the replay shows on one clock (seconds from the
worm's start): the sandbox's nodes and networks, each VM's power state over time, events (state
changes, stops, secrets, captures, submissions, logs by reference), the monitor's flows, and the
generator's architecture. The format and the pages: `docs/progression.md`.

## What is not done

- **A way from one sandbox VM to the next.** Sandbox VMs accept SSH with the operator's key only
  and run nothing else, so a worm can capture only its start VM. Seeded vulnerabilities are not
  built (`docs/status.md`).
- **A run on a real node.** No sandbox has been built, rolled back or torn down live, and
  `bash main.sh`, the LLM endpoint and the CPU check have only run on devboxes.
- **Pause or abort a round** from the dashboard.
- **Parallel jobs.** The agent does one job at a time. Dev uploads wait for a round, but a manual
  devbox reset does not, so it can delay a round's jobs by its length (`docs/status.md`, decisions).
