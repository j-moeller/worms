# 11. The orchestrator: conductor, processor, gameplans, resets

What runs a round and a dev upload, and how the parts talk to each other. Gameplans are written as [5](05-live-environment.md) says, devboxes are [4](04-dev-environment.md)'s; this document is the machinery between them.

| Part | What it is | Where | Its log |
|---|---|---|---|
| **API** | the dashboard's way to the orchestrator: devbox resets and key pushes, gameplan simulations | the orchestrator VM, unit `orchestrator` (gunicorn, 2 workers) | `worms log api` |
| **Conductor** | plans each round, runs its attempts one after the other, checks the gameplans | unit `orchestrator-conduct-rounds` (`manage.py conduct_rounds`) | `worms log conductor` |
| **Processor** | turns what the sandboxes reported (flags, worm logs) into captures and log records; runs the dev uploads | unit `orchestrator-process-attempts` (`manage.py process_attempts`) | `worms log processor` |
| **Topology** | the Proxmox VMs, bridges and firewall, for the staff's topology page (read-only token) | timer `orchestrator-collect-topology`, every 2 min | its unit's journal |
| **Round agent** | does on the Proxmox host what the others ask for: builds and resets VMs, deploys worms, places flags | the host, unit `worms-round-agent` (`scripts/round_agent`) | `worms log agent`, a job's own: `worms log <n>` |

There is no part called a gameplanner or a VM resetter: planning is the conductor's (`plan_round`, then pinning the gameplan), resets are spread over the conductor, the processor and the round agent (below).

## How they talk

- **Through the database only.** The conductor and the processor create **host jobs** (table `host_jobs`: a kind, its parameters, a state `queued` → `running` → `done` | `failed`, a result, an error); the round agent takes them one at a time and writes the result back. The orchestrator's role may only insert and read them; the agent's may only update their state and result ([`setup_db_permissions`](../ansible/roles/database/setup_db_permissions/defaults/main.yml)).
- **A notification** (`NOTIFY host_jobs`) when the conductor queues a job, so the agent takes it at once; the agent also looks every 5 s (the devbox jobs come that way: they are queued without one).
- **The dashboard** reaches the orchestrator over its API (a bearer token, compared in constant time; POST only) and never the host. The CLI does neither: it calls the same functions through Django on the controller (`worms vm reset alpha` is the dashboard's reset).
- **Files on the host** that the agent keeps: the operator's pause (`/var/lib/worms/agent-paused`), the devboxes a round holds off (`held-devboxes.json`), a run's secrets (`/var/lib/worms/runs/<run>.json`).

Job kinds ([`core/models/rounds.py`](../projects/dashboard/core/models/rounds.py)): `prepare`, `check`, `deploy`, `place_flags`, `remove_flags`, `write_file`, `read_file`, `teardown` (an attempt); `gameplans` (the host's view of the gameplan files); `hold_devboxes`, `release_devboxes` (a round that wants the devboxes off); `devbox_provision`, `devbox_rebuild`, `devbox_reconfigure`, `devbox_deploy` (the devboxes).

## The conductor

[`conduct_rounds`](../projects/dashboard/orchestrator/management/commands/conduct_rounds.py) loops; it sleeps 5 s only when a tick did nothing. Each loop first plans every round whose start has passed and that has no runs yet, then ticks ([`conductor.py`](../projects/dashboard/orchestrator/rounds/conductor.py)):

1. **The gameplans' check**, at its first tick and then every 10 minutes (and when a waiting round's check is older than a minute): a `gameplans` job (the host's copy and what it finds), then the orchestrator's own copy, the table `gameplans` and each upcoming round's gameplan status.
2. **Recovery** of a run the last conductor left half-way (after a restart): one ending or tearing down is finished off, any other fails.
3. **Holding the queue** while a teardown is still due.
4. **Releasing the devboxes** of a round that is over (`release_devboxes`).
5. **The next attempt**: the round already under way first, else the next round by its start; a round on hold (`worms round pause`) is passed over.
6. **Pinning** the round on its first attempt: its gameplan must be chosen, valid on both copies (the orchestrator's and the host's, by SHA-256), and its check must pass at the round's per-team duration. The SHA-256, the version and the duration are written into the round's runs; the round asks for its devboxes off (`devboxes_off`). Otherwise the round waits, and is looked at again a minute later.
7. A run without an upload is **skipped**; the devboxes are held off when the round wants it (`hold_devboxes`); then **the attempt**.
8. A sandbox kept for a next run nobody has is destroyed.

**Planning** ([`planner.py`](../projects/dashboard/orchestrator/rounds/planner.py)): one run per team (attempt 0), each with the team's latest upload made before the round's start (dev uploads do not count), in an order shuffled by the round's seed, with the round's per-team duration.

**An attempt** (a `RoundRun`):

```
PENDING ─▶ PREPARING ─▶ CHECKING ─▶ RUNNING ─▶ ENDING ─▶ TEARING_DOWN ─▶ FINISHED
   │          (prepare)    (check)    (execute())  (VMs off,   (teardown)
   └─ no upload: SKIPPED                            drain 120 s)
any of them ─▶ FAILED ─▶ the same team again, attempt + 1 (at most 4 attempts)
```

- **Preparing:** the gameplan loaded by its pinned SHA-256 (a mismatch fails the attempt); a `prepare` job: the sandbox built or rolled back (below), the run's secrets, files and flags; its topology, VMs and flag hashes recorded.
- **Checking:** a `check` job; a failed check fails the attempt.
- **Running:** the gameplan's `execute()` ([`src/gameplan/runtime.py`](../src/gameplan/runtime.py)) with the conductor as its back end: a `deploy` job puts the worm on its entry VM (the zip fetched from the dashboard, its hash checked) and arms it; the worm starts 15 s after (`worm_start_at`); then the gameplan's flag, file and power steps (power through the Proxmox API). The attempt ends at `worm_start_at` + the duration.
- **Ending:** the sandbox VMs stopped, the monitor's capture drained for 120 s.
- **Tearing down:** a `teardown` job: the sandbox kept (powered off) when another run of the round still has an upload, else destroyed.
- **A failure** (a job that failed, or one silent for `job_timeout`, 30 min, measured from its last heartbeat): the run fails, its VMs stop, a teardown is queued, and the same team runs again while attempts are left (3 retries). A teardown that fails holds the queue: nothing starts on a sandbox in an unknown state.
- **On SIGTERM** (a restart) the conductor stops when idle; an attempt under way fails at its next wait, and the next conductor recovers it (step 2). Hence: no deploy during a round ([6](06-background-processes.md)).

**Holds:** `worms round pause` and `worms round resume` set the round's `on_hold`; `worms round start` and `worms round cancel` move its start to now or skip its remaining attempts ([`control.py`](../src/worms_cli/control.py)); `worms jobs pause` stops the agent from taking jobs, rounds' too.

## The processor

[`process_attempts`](../projects/dashboard/orchestrator/management/commands/process_attempts.py) passes again at once while there was something to do, else every 2 s. Each pass: the flags the sandboxes reported (scoring) and the worm logs (logging) become captures and log records, attributed to the run whose window they fall in (± 120 s of grace, `--grace`); then the same for the devboxes; then the devbox jobs' results (tokens, flag hashes, addresses); then the dev uploads. It is the **single writer** of captures and logs: exactly one runs (a second could count an attempt twice).

**A dev upload** ([`devbox_runs.py`](../projects/dashboard/orchestrator/rounds/devbox_runs.py)), from the dashboard's `Upload(devbox=True)` to the team's devbox:

```
PENDING ─▶ RESETTING ─▶ DEPLOYING ─▶ ARMED ─▶ RUNNING ─▶ FINISHED
any of them ─▶ FAILED, or REPLACED (a newer upload, or a reset by hand)
```

1. Only a team's newest upload runs; older ones are replaced.
2. It **waits** while a round's attempt is under way, or a round holds (or wants) the devboxes off (`reset_waits_for`: the processor's log says it once, the dashboard shows the team, `worms trace <team>` too).
3. **Resetting:** the team's devbox reset, as the dashboard's reset is (a `devbox_rebuild` job: its version goes up, its dev score starts over).
4. **Deploying:** a `devbox_deploy` job: the zip onto VM A, the fuse lit (10 s), a run time of an hour.
5. Armed, running, finished: the VMs stay as the worm left them.

A queued devbox job never times out here (unlike an attempt's).

## The gameplans

- **The files** are [`gameplans/*.py`](../gameplans): one `Gameplan` subclass each. **The framework** is [`src/gameplan`](../src/gameplan): the loader (a name, a file read without following links, its SHA-256), the runtime (`Runner`, a dry run, `validate`), the rendering of a VM's files with the run's secrets, the OpenTofu definition and the Ansible variables of a sandbox, the simulation.
- **Two copies**, each checked: the orchestrator's (`/opt/orchestrator/gameplans`, root's, readable by the app: `Site.yml` copies it, `worms apply gameplans`) and the host's (the agent's workspace). A gameplan is valid only when both have the same SHA-256 and both find it valid. The dashboard holds none (any copy there is removed).
- **The records:** `gameplans` (each file: valid or not, its current SHA-256), `gameplan_versions` (every version, its bytes, immutable), `gameplan_changes`. **The dashboard never reads a gameplan's content:** no file on its VM, no read grant on the content column, and why a gameplan is invalid goes only to the conductor's journal and the job's log.
- **The simulation** (the staff's Simulate, through the API): one at a time (an advisory lock), in a child process with limits (CPU, memory, writes) and a 20 s timeout, its result cached (20 per file).

## The resets

| What | When | How |
|---|---|---|
| **The service VMs** (ephemeral, private internet) | before a round's sandbox is built (its first attempt), and by hand (`worms vm reset services`) | all of them back to their `clean` snapshot (all or none), wait for SSH, the devbox map written again (`devbox_reconfigure`). Refused while a sandbox exists |
| **The sandbox** | between the attempts of a round | the same definition with every VM's `clean` snapshot: rolled back; otherwise built anew: the old one destroyed, the devboxes off, the services reset, OpenTofu, `SetupSandbox.yml`, a snapshot, the devboxes on |
| **A team's devbox** | the dashboard's reset, a dev upload, `worms vm reset <team>` | a `devbox_rebuild` job: back to the clean snapshot (from before any team key), then the CA, the team's keys and new tokens over SSH: seconds, no Ansible. An older snapshot gets the devbox's setup once, without network, and a new snapshot |
| **A devbox from nothing** | `worms vm rebuild <team>`, a VM without a snapshot | the same job, `fresh`: OpenTofu `-replace` of the three VMs, configured, snapshotted |

A queued or running rebuild of the same team is reused, not doubled. What a reset gives a devbox is what its snapshot holds: a change of the devbox role reaches it with a rebuild, not a reset.

## The round agent

[`agent.py`](../scripts/round_agent/agent.py), [`handlers.py`](../scripts/round_agent/handlers.py):

- **One job at a time**, taken with one `UPDATE … FOR UPDATE SKIP LOCKED`: devbox jobs after the others; none while the operator's pause file exists; no `devbox_*` job while a round holds the devboxes off (`hold_devboxes` and `release_devboxes` are named so that they are not caught by this).
- A heartbeat every 30 s while a job runs; its whole output in `/var/log/worms/jobs/<uuid>.log` (and `latest.log`), kept 30 days.
- Its Ansible runs in its own workspace, `/opt/worms`, with the host's Ansible ([9](09-vm-configuration.md)).
- **New code:** between jobs, when a module it loaded changed on disk, it exits and systemd starts it again on the new code; the gameplan files are read afresh for every job.

## Settings

| | Default | Where |
|---|---|---|
| conductor idle interval | 5 s | `conduct_rounds --interval` |
| processor idle interval | 2 s | `process_attempts --interval` |
| a job silent for | 1800 s, then the attempt fails | `ROUND_JOB_TIMEOUT` |
| retries of a team's attempt | 3 | `ROUND_MAX_RETRIES` |
| a waiting round looked at again | 60 s | `ROUND_GAMEPLAN_POLL` |
| the gameplans checked again | 600 s | `ROUND_CONDUCTOR["gameplan_check_interval"]` |
| captures' grace around a run | 120 s | `ROUND_CAPTURE_GRACE`, `--grace` |
| the worm's fuse, the drain after | 15 s, 120 s | `ROUND_TIMING` ([`config/settings/base.py`](../projects/dashboard/config/settings/base.py)) |

## Following it

`worms round` (the rounds, their attempts by state), `worms jobs` and `worms show job <n>` (the queue, why a job waits), `worms log conductor -f`, `worms log processor`, `worms log latest -f` (the job the agent runs now), `worms trace <team>` (a dev upload's way).
