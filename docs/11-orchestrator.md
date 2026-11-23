# 11. A round, the orchestrator: `worms round start`, processor, gameplans, resets

What runs a round and a dev upload, and how the parts talk to each other. Since 2026-11-23 a round runs only from `worms round start` on the host, in the operator's terminal: no daemon starts, retries or stops anything of it. Gameplans are written as [5](05-live-environment.md) says, devboxes are [4](04-dev-environment.md)'s; this document is the machinery between them.

| Part | What it is | Where | Its log |
|---|---|---|---|
| **API** | the dashboard's way to the orchestrator: devbox resets and key pushes, gameplan simulations | the orchestrator VM, unit `orchestrator` (gunicorn, 2 workers) | `worms log api` |
| **Round session** | `worms round start`: plans the round, runs its attempts one after the other, counts their submissions; asks before every step | the host, the operator's terminal ([`rounds.py`](../src/worms_cli/rounds.py), the attempt engine [`conductor.py`](../projects/dashboard/orchestrator/rounds/conductor.py)) | `worms log round [<r>] [<team>]`: `~/.worms/rounds/<round>-<time>/` |
| **Processor** | turns what the sandboxes reported (flags, worm logs) into captures and log records; runs the dev uploads | unit `orchestrator-process-attempts` (`manage.py process_attempts`) | `worms log processor` |
| **Topology** | the Proxmox VMs, bridges and firewall, for the staff's topology page (read-only token) | timer `orchestrator-collect-topology`, every 2 min | its unit's journal |
| **Round agent** | does on the Proxmox host what the dashboard asks for: the devboxes' resets, rebuilds and keys | the host, unit `worms-round-agent` (`scripts/round_agent`) | `worms log agent`, a job's own: `worms log <n>` |

There is no part called a gameplanner or a VM resetter: planning is the session's (`plan_round`, then pinning the gameplan), resets are spread over the session, the processor and the round agent (below). The round session runs the host's steps (`scripts/round_agent/handlers.py`) itself, in its own process, with the round agent's environment and the WORMS key in an ssh-agent of its own; the round agent is stopped meanwhile.

## How they talk

- **Through the database only.** The dashboard's requests (through the API) and the processor create **host jobs** (table `host_jobs`: a kind, its parameters, a state `queued` → `running` → `done` | `failed`, a result, an error); the round agent takes them one at a time and writes the result back. The orchestrator's role may only insert and read them; the agent's may only update their state and result ([`setup_db_permissions`](../ansible/roles/database/setup_db_permissions/defaults/main.yml)).
- **A notification** (`NOTIFY host_jobs`) when a job is queued, so the agent takes it at once; the agent also looks every 5 s. A round's steps are `host_jobs` rows too (for the record, `worms show job <n>`), but the session runs them itself: they are never queued.
- **The dashboard** reaches the orchestrator over its API (a bearer token, compared in constant time; POST only) and never the host. The CLI does neither: it calls the same functions through Django on the controller (`worms vm reset alpha` is the dashboard's reset).
- **Files on the host** that the agent keeps: the operator's pause (`/var/lib/worms/agent-paused`), a run's secrets (`/var/lib/worms/runs/<run>.json`).

Job kinds ([`core/models/rounds.py`](../projects/dashboard/core/models/rounds.py)): `prepare`, `check`, `deploy`, `place_flags`, `remove_flags`, `write_file`, `read_file`, `teardown` (an attempt); `preflight` (before an attempt), `observe` (while one runs: the host's view of its VMs); `gameplans` (the host's view of the gameplan files); `hold_devboxes`, `release_devboxes` (unused since 2026-11-23: the session asks to stop the devboxes); `devbox_provision`, `devbox_rebuild`, `devbox_reconfigure`, `devbox_deploy` (the devboxes).

## A round: `worms round start`

`worms round start <r>` ([`rounds.py`](../src/worms_cli/rounds.py)) on the host, as root. It holds the host's lock to its end (no `worms apply` meanwhile) and writes everything it prints to `~/.worms/rounds/<round>-<time>/round.log`, each step's commands and output to a file of its own next to it (`<position>-<team>-attempt<n>/<nnn>-<kind>.log`).

1. **The round:** its start is the teams' upload deadline; before it, the session asks to move it to now. Its runs planned (below) if it has none; each team's upload shown.
2. **What a session runs:** a round's first session, every team; `--resume`, every team that has not finished (a new attempt after a failed one); `--team <t>`, the named ones. A round that ran before refuses a plain start.
3. **The gameplan pinned:** chosen, valid at the round's per-team duration on the host's copy (a `gameplans` step, which also brings the dashboard's list up to date), the same version as the round's earlier attempts. Its SHA-256 goes into every attempt still to come.
4. **Asked, one by one, each with the command that does the same and the one that undoes it:** the orchestrator stopped (`worms service stop orchestrator`: api, processor, topology), the round agent stopped (`worms service stop agent`: paused, its job under way waited for, stopped), its queued jobs cancelled (`worms jobs cancel <n>...`), any job marked running whose heartbeat stopped marked failed, every devbox VM and gateway stopped (`worms vm stop devboxes`). A "no" anywhere: the round does not start.
5. **Checked again,** not assumed: the orchestrator's units and the agent inactive, no host job queued or running, every devbox VM stopped, the internet window closed; then the host's preflight (memory, disk, the services, the gameplan's packages cached: [5](05-live-environment.md#an-attempt)). Then asked once more.
6. **Each attempt in turn:** a team without an upload is **skipped**; otherwise the host's preflight again, then **the attempt** (below). A failed preflight stops the round there (no attempt used up). A failed attempt is written down and the round goes on with the next team: nothing is retried by itself. A failed **prepare** stops the round too (the next team's would fail the same way). If it left the sandbox build unfinished, its VMs are kept, asked; the next `--resume` asks to continue that build on them (OpenTofu's apply and the configuration again, not the whole build). The host records a build from its start until every VM has its clean snapshot (`/var/lib/worms/sandbox-build.json`): only such a build, on which no worm can have run, is continued.
7. **At the end:** the failed teams may be run again at once (or later: `--resume`); once every team's attempt finished without an error, the results may be published (`worms round publish <r>`); then the round's sandbox destroyed (later: `worms round teardown`), the devboxes, the round agent and the orchestrator started again. Each asked.

**Ctrl-C** fails the attempt under way at its next step (its VMs stopped, its sandbox kept); no other starts. Twice: out at once; the next session fails an attempt left under way (`abandon`). **The results** are hidden from the teams until `worms round publish` (`Round.results_published_at`): no team sees anything of its attempt, nor the leaderboard its points; staff see everything. `worms round unpublish` hides them again.

**Planning** ([`planner.py`](../projects/dashboard/orchestrator/rounds/planner.py)): one run per team (attempt 0), each with the team's latest upload made before the round's start (dev uploads do not count), in an order shuffled by the round's seed, with the round's per-team duration.

**An attempt** (a `RoundRun`):

```
PENDING ─▶ PREPARING ─▶ CHECKING ─▶ RUNNING ─▶ ENDING ─▶ TEARING_DOWN ─▶ FINISHED
   │          (prepare)    (check)    (execute())  (VMs off,   (teardown)
   └─ no upload: SKIPPED                            drain 120 s)
any of them ─▶ FAILED (the operator may run a new one: --resume, attempt + 1)
```

- **Preparing:** the gameplan loaded by its pinned SHA-256 (a mismatch fails the attempt); a `prepare` job: the sandbox built or rolled back (below), the run's secrets, files and flags; its topology, VMs and flag hashes recorded.
- **Checking:** a `check` job: the host's firewall units and every VM's CPU; every VM and sandbox-gw started in this attempt (in the pool, running, its QEMU younger than the attempt's `prepare`) and came up as it should, from inside (cloud-init done, the boot finished, its addresses, default route, sshd, the gameplan's users and services; the gateway's nftables chains, IP forwarding and `nf_conntrack_max`, as the cloud-init it was built with sets them (a different `nf_conntrack_max` is a warning: capacity, not routing)); then from inside every sandbox VM that did, before the worm: the flag, DNS, scoring and logging, apt, pypi, the clock, forbidden destinations unreachable, the gameplan's connections allowed and refused, the files ([5](05-live-environment.md#an-attempt)). A failed check fails the attempt.
- **Running:** the gameplan's `execute()` ([`src/gameplan/runtime.py`](../src/gameplan/runtime.py)) with the session as its back end: a `deploy` job puts the worm on its entry VM (the zip fetched from the dashboard, its hash checked) and arms it; the worm starts 15 s after (`worm_start_at`); then the gameplan's flag, file and power steps (power through the Proxmox API). The attempt ends at `worm_start_at` + the duration. **The watch:** every 10 s each sandbox VM's and the gateway's state from the Proxmox API against what the gameplan left; a VM off that should run makes an `observe` job ask the host why. The guest's own power-off is the worm's doing (an event); an OOM kill, a crash, a Proxmox task, an unknown cause or the gateway down fail the attempt. Every 60 s an `observe` job for every VM logs the host's memory and a devbox found running (an error in the round's log; nothing stops it). After every look the session counts the flag submissions and log records so far (what the processor does otherwise: it is stopped).
- **Ending:** the sandbox VMs stopped, the monitor's capture drained for 120 s.
- **Tearing down:** a `teardown` job: the sandbox kept, powered off, for the next attempt; the session destroys it at its end (asked).
- **A failure** (a step that failed): the run fails, its VMs stop, its sandbox is kept, powered off, and the error goes to the attempt, its events and the round's log. The next attempt's `prepare` rolls the sandbox back, or builds it anew when it is not whole.

## The processor

[`process_attempts`](../projects/dashboard/orchestrator/management/commands/process_attempts.py) passes again at once while there was something to do, else every 2 s. Each pass: the flags the sandboxes reported (scoring) and the worm logs (logging) become captures and log records, attributed to the run whose window they fall in (± 120 s of grace, `--grace`); then the same for the devboxes; then the devbox jobs' results (tokens, flag hashes, addresses); then the dev uploads. It is the **single writer** of captures and logs: exactly one runs (a second could count an attempt twice).

**A dev upload** ([`devbox_runs.py`](../projects/dashboard/orchestrator/rounds/devbox_runs.py)), from the dashboard's `Upload(devbox=True)` to the team's devbox:

```
PENDING ─▶ RESETTING ─▶ DEPLOYING ─▶ ARMED ─▶ RUNNING ─▶ FINISHED
any of them ─▶ FAILED, or REPLACED (a newer upload, or a reset by hand)
```

1. Only a team's newest upload runs; older ones are replaced.
2. It **waits** while a round's attempt is under way (`reset_waits_for`: the processor's log says it once, the dashboard shows the team, `worms trace <team>` too); during a round the processor is stopped anyway.
3. **Resetting:** the team's devbox reset, as the dashboard's reset is (a `devbox_rebuild` job: its version goes up, its dev score starts over).
4. **Deploying:** a `devbox_deploy` job: the zip onto VM A, the fuse lit (10 s), a run time of an hour.
5. Armed, running, finished: the VMs stay as the worm left them.

A queued devbox job never times out here (unlike an attempt's).

## The gameplans

- **The files** are [`gameplans/*.py`](../gameplans): one `Gameplan` subclass each. **The framework** is [`src/gameplan`](../src/gameplan): the loader (a name, a file read without following links, its SHA-256), the runtime (`Runner`, a dry run, `validate`), the rendering of a VM's files with the run's secrets, the OpenTofu definition and the Ansible variables of a sandbox, the simulation.
- **Two copies:** the host's (the agent's workspace, `/opt/worms/gameplans`), which a round runs and checks, and the orchestrator's (`/opt/orchestrator/gameplans`, root's, readable by the app), which the staff's simulations run. `worms apply gameplans` deploys both and checks the host's (the dashboard's list). The dashboard holds none (any copy there is removed).
- **The records:** `gameplans` (each file: valid or not, its current SHA-256), `gameplan_versions` (every version, its bytes, immutable), `gameplan_changes`. **The dashboard never reads a gameplan's content:** no file on its VM, no read grant on the content column, and why a gameplan is invalid goes only to the round's log and the step's log.
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

- **One job at a time**, taken with one `UPDATE … FOR UPDATE SKIP LOCKED`: `observe` first, devbox jobs after the others; none while the operator's pause file exists. During a round it is stopped.
- A heartbeat every 30 s while a job runs; its whole output in `/var/log/worms/jobs/<uuid>.log` (and `latest.log`), kept 30 days.
- Its Ansible runs in its own workspace, `/opt/worms`, with the host's Ansible ([9](09-vm-configuration.md)).
- **New code:** between jobs, when a module it loaded changed on disk, it exits and systemd starts it again on the new code; the gameplan files are read afresh for every job.

## Settings

| | Default | Where |
|---|---|---|
| processor idle interval | 2 s | `process_attempts --interval` |
| a running job silent for (no agent any more: `worms jobs cancel` closes it) | 300 s | `services.SILENT_AFTER` |
| the watch: the Proxmox API; an observe job; its answer within | 10 s; 60 s; 120 s | `ROUND_CONDUCTOR["watch_interval"]`, `["observe_interval"]`, `["observe_timeout"]` |
| captures' grace around a run | 120 s | `ROUND_CAPTURE_GRACE`, `--grace` |
| the worm's fuse, the drain after | 15 s, 120 s | `ROUND_TIMING` ([`config/settings/base.py`](../projects/dashboard/config/settings/base.py)) |

## Following it

`worms round` (the rounds, their attempts by state), `worms log round [<r>] [<team>] [-f]` (a session's log, an attempt's steps), `worms jobs` and `worms show job <n>` (the queue, why a job waits), `worms log processor`, `worms log latest -f` (the job the agent runs now), `worms trace <team>` (a dev upload's way).
