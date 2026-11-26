# 11. A round, the orchestrator: `worms round start`, processor, gameplans, resets

What runs a round and a dev upload, and how the parts talk to each other. Since 2026-11-23 a round runs only from `worms round start` on the host, in the operator's terminal: no daemon starts, retries or stops anything of it. Gameplans are written as [5](05-live-environment.md) says, devboxes are [4](04-dev-environment.md)'s; this document is the machinery between them.

| Part | What it is | Where | Its log |
|---|---|---|---|
| **API** | the dashboard's way to the orchestrator: devbox resets and key pushes, gameplan simulations | the orchestrator VM, unit `orchestrator` (gunicorn, 2 workers) | `worms log api` |
| **Round session** | `worms round start`: plans the round, runs its attempts one after the other, counts their submissions; asks before every step | the host, the operator's terminal ([`rounds.py`](../src/worms_cli/rounds.py), the attempt engine [`conductor.py`](../projects/dashboard/orchestrator/rounds/conductor.py)) | `worms log round [<r>] [<team>\|summary]`: `~/.worms/rounds/<round>-<time>/`, what failed in its `summary.log` |
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

`worms round start <r>` ([`rounds.py`](../src/worms_cli/rounds.py)) on the host, as root. It holds the host's lock to its end (no `worms apply` meanwhile) and writes everything it prints to `~/.worms/rounds/<round>-<time>/round.log`, each step's commands and output to a file of its own next to it (`<position>-<team>-attempt<n>/<nnn>-<kind>.log`), each VM's Ansible log to `round/<vm>.log`.

**`summary.log`**, in the same folder, is the one file to read when something failed. First every failure, the newest first: what failed (the team, its attempt, the step and its phase), why (the cause found in the step's whole output by [`failure.py`](../scripts/round_agent/failure.py): Ansible's failed task with its VM, file:line and msg; OpenTofu's `Error:` with its resource and file:line; where an exception was raised, in a gameplan or in our code; never the tail of a deprecation warning, as on 2026-11-23), the step's log, each failed VM's own Ansible log, and what to do next. Then every step, one line each: how it ended, how long it took, its log. `~/.worms/rounds/latest` points at the newest session, so `~/.worms/rounds/latest/summary.log` is always the newest summary (`worms log round summary`; one round's newest: `worms log round <r> summary`). The terminal says each step's log, absolute, when the step starts and again when it ends, and for a failure its cause, the excerpt, the log and `summary.log`. A failed attempt's account is kept on its row too (`round_runs.failure`): the staff round page shows it under **Failures**, never to a team; a failed host job's is in its result, on the same page under **Host jobs**. A step that loads the gameplan or applies it (build, rollback, prepare, preflight, the gameplan's files: `_quiet` in [`handlers.py`](../scripts/round_agent/handlers.py)) may print the gameplan's content (a failed setup command with its password), which the dashboard must never read: the database gets where it failed only (an Ansible role and its file:line, the VMs; OpenTofu's module file and line; an exception's type and where it was raised), the terminal and `summary.log` get what it said. Run it inside `tmux` (it says so when it is not): a dropped SSH connection ends the session (SIGHUP), and the attempt under way fails; its step is marked failed on the way out, as on SIGTERM or Ctrl-C twice.

0. **This checkout's code on the host**, before anything is stopped: when the workspace (`/opt/worms/.worms-commit`) is at another commit, the session syncs it (`SetupRoundAgent.yml --tags agent-sync`, as `worms apply gameplans` does; the generated definitions and OpenTofu's state stay), checks the host's gameplans and records the workspace in `deployed.json`: `workspace synced: <old> → <new>`. A checkout with uncommitted changes to tracked files is refused: a round runs committed code only. `worms round teardown` does the same.
1. **The round:** its start is the teams' upload deadline; before it, the session asks to move it to now. Its runs planned (below) if it has none; each team's upload shown.
2. **What a session runs:** a round's first session, every team; `--resume`, every team that has not finished (a new attempt after a failed one); `--team <t>`, the named ones. A round that ran before refuses a plain start.
3. **The gameplan pinned:** chosen, valid at the round's per-team duration on the host's copy (a `gameplans` step, which also brings the dashboard's list up to date), the same version as the round's earlier attempts that still count. Its SHA-256 goes into every attempt still to come. Before it, a round staff changed since its attempts (gameplan, version, duration per team, start) has the teams whose attempt no longer matches run again, asked: their earlier attempts kept as superseded (`stale`, `replan`; docs/05).
4. **Asked, one by one, each with the command that does the same and the one that undoes it:** the orchestrator stopped (`worms service stop orchestrator`: api, processor, topology), the round agent stopped (`worms service stop agent`: paused, its job under way waited for, stopped), its queued jobs cancelled (`worms jobs cancel <n>...`), any job marked running that runs nowhere any more marked failed (an earlier session's that ended half-way, a killed agent's: each with why; a session closes its own steps however it ends, but for a SIGKILL), every devbox VM and gateway stopped (`worms vm stop devboxes`). A "no" anywhere: the round does not start.
5. **Checked again,** not assumed: the orchestrator's units and the agent inactive, no host job queued or running, every devbox VM stopped, the internet window closed; then the host's preflight (memory, disk, the services, the gameplan's packages cached: [5](05-live-environment.md#an-attempt)). Parts of the private internet that are off (`cve-search`, `docker-registry`, `manpages`, `ntp`, `wikipedia`; never DNS, apt-cache or devpi, which the build and the check need) are asked about, with `worms vm start <names>` to undo it: a yes holds for the session's later preflights. The preflight runs here and again before every attempt but the first after the build. Then asked once more.
6. **The round's sandbox, once:** a `build` step (OpenTofu's apply and the configuration, `SetupSandbox.yml`; the old sandbox destroyed first, the devboxes off, the services reset to their snapshot), then a `snapshot` step (every VM's `clean` snapshot, before any team's code; the devboxes on again). Both belong to the round, not to an attempt (their logs in the session's folder, `round` in their params for the staff page). A sandbox of this gameplan with its snapshot (a later session of the round) is not built again. A failed build or snapshot: the round does not start (no attempt used up) and its VMs are kept, asked; the next `--resume` asks to go on with that build (OpenTofu's apply, which changes nothing, and the configuration again; or only the snapshot, when the configuration had finished), never a build from nothing. The host records a build from its start until its snapshot (`/var/lib/worms/sandbox-build.json`, `configured` once the configuration ran through): only such a build, on which no worm can have run, is continued.
7. **Each attempt in turn:** a team without an upload is **skipped**; otherwise the host's preflight (not for the first attempt: the one before the build covers it), then **the attempt** (below): a `rollback` to the snapshot first, except for the first attempt after the snapshot was taken. A failed preflight stops the round there (no attempt used up). An attempt that ends with an error (it failed, or its end did) stops the round there: the next team's would most likely meet the same error. Nothing is retried by itself; the sandbox and its snapshot stay, and `--resume` runs a new attempt of that team from the snapshot, then the teams still to come.
8. **At the end:** once every team's attempt finished without an error, the results may be published (`worms round publish <r>`) and the sandbox goes: a `delete_snapshot` step, then a `teardown` (asked, yes by default; before, the sandbox and its snapshot stay for `--resume`, no by default; later: `worms round teardown`); then the devboxes, the round agent and the orchestrator started again. Each asked.

**Ctrl-C** fails the attempt under way at its next step (its VMs stopped, its sandbox kept); no other starts. Twice (or SIGTERM, SIGHUP, an error of the session's own): out, but the attempt under way is closed first, failed and not counted against the team (`Conductor.interrupted`: its VMs stopped, the teardown clears scoring's team; Ctrl-C again cuts that short, the run is marked over all the same). Only a SIGKILL leaves one under way: the next session or `worms round teardown` closes it (`abandon`). **What the session stopped** (the orchestrator, the round agent, the devboxes) is in `/var/lib/worms/round-stopped.json` until it is started again: at the end (`_afterwards`), when the round does not run after all (a no, a failed check or preflight: asked as at the end), or by hand. A session that ends otherwise says what stays stopped and its commands, and `worms status` says it until then. **The results** are hidden from the teams until `worms round publish` (`Round.results_published_at`): no team sees anything of its attempt, nor the leaderboard its points; staff see everything. `worms round unpublish` hides them again.

**Planning** ([`planner.py`](../projects/dashboard/orchestrator/rounds/planner.py)): one run per team (attempt 0), each with the team's latest upload made before the round's start (dev uploads do not count), in an order shuffled by the round's seed, with the round's per-team duration.

**An attempt** (a `RoundRun`):

```
            build, snapshot (the round's, once, before its first attempt)
PENDING ─▶ PREPARING ─▶ CHECKING ─▶ RUNNING ─▶ ENDING ─▶ TEARING_DOWN ─▶ FINISHED
   │      (rollback,     (check)    (execute())  (VMs off,   (teardown keep:
   │       prepare)                               drain 120 s) the attempt's end)
   └─ no upload: SKIPPED
            delete_snapshot, teardown (the round's, once, after its last attempt)
any of them ─▶ FAILED (the operator may run a new one: --resume, attempt + 1)
```

- **Preparing:** the gameplan loaded by its pinned SHA-256 (a mismatch fails the attempt); a `rollback` job (every VM back to its snapshot, started: nothing of the team before is left), except for the first attempt after the snapshot was taken; a `prepare` job: the run's secrets, files and flags on the sandbox as its snapshot holds it (it builds nothing: a sandbox not this gameplan's, without its snapshot or with a VM down is refused); its topology, VMs and flag hashes recorded.
- **Checking:** a `check` job: the host's firewall units and every VM's CPU; every VM and sandbox-gw started in this attempt (in the pool, running, its QEMU younger than the attempt's `prepare`) and came up as it should, from inside (cloud-init done, the boot finished, its addresses, default route, sshd, the gameplan's users and services; the gateway's nftables chains, IP forwarding and `nf_conntrack_max`, as the cloud-init it was built with sets them (a different `nf_conntrack_max` is a warning: capacity, not routing)); then from inside every sandbox VM that did, before the worm: the flag, DNS, scoring and logging, apt, pypi, forbidden destinations unreachable, the gameplan's connections allowed and refused, the files ([5](05-live-environment.md#an-attempt)). Only these decide (`GATES` in [`handlers.py`](../scripts/round_agent/handlers.py), the operator's choice of 2026-11-24): every VM running and booted in this attempt, the probe that answered, the flag, DNS, scoring, logging, apt and PyPI reachable, and the isolation the platform's rules demand (the host's two firewall units, no team VM with the host's CPU, nothing forbidden reachable). The rest (the boot's details, the gameplan's own connections and files, the mirror) are warnings: in the check's log, each with why, and in its result. The VMs' clocks are not checked. A failed check fails the attempt, and stops the round.
- **Running:** the gameplan's `execute()` ([`src/gameplan/runtime.py`](../src/gameplan/runtime.py)) with the session as its back end: a `deploy` job puts the worm on its entry VM (the zip fetched from the dashboard, its hash checked) and arms it; the worm starts 15 s after (`worm_start_at`); then the gameplan's flag, file and power steps (power through the Proxmox API). The attempt ends at `worm_start_at` + the duration. **The watch:** every 10 s each sandbox VM's and the gateway's state from the Proxmox API against what the gameplan left; a VM off that should run makes an `observe` job ask the host why. The guest's own power-off is the worm's doing (an event); an OOM kill, a crash, a Proxmox task, an unknown cause or the gateway down fail the attempt. Every 60 s an `observe` job for every VM logs the host's memory and a devbox found running (an error in the round's log; nothing stops it). After every look the session counts the flag submissions and log records so far (what the processor does otherwise: it is stopped).
- **Ending:** the sandbox VMs stopped, the monitor's capture drained for 120 s.
- **Tearing down:** the attempt's end, a `teardown` job with `keep`: the run's secrets gone, its VMs off, the sandbox and its snapshot kept for the next attempt's rollback. The round's own teardown (the sandbox destroyed) comes after its last attempt, its snapshot deleted first.
- **A failure** (a step that failed): the run fails, its VMs stop, its sandbox is kept, powered off, and the error goes to the attempt, its events and the round's log. The next attempt rolls the sandbox back to its snapshot; nothing is built again.

## The processor

[`process_attempts`](../projects/dashboard/orchestrator/management/commands/process_attempts.py) passes again at once while there was something to do, else every 2 s. Each pass: the flags the sandboxes reported (scoring) and the worm logs (logging) become captures and log records, attributed to the run whose window they fall in (± 120 s of grace, `--grace`); then the same for the devboxes; then the devbox jobs' results (tokens, flag hashes, addresses); then the dev uploads. It is the **single writer** of captures and logs: exactly one runs (a second could count an attempt twice).

**A dev upload** ([`devbox_runs.py`](../projects/dashboard/orchestrator/rounds/devbox_runs.py)), from the dashboard's `Upload(devbox=True)` to the team's devbox:

```
PENDING ─▶ DEPLOYING ─▶ ARMED ─▶ RUNNING ─▶ FINISHED
any of them ─▶ FAILED, or REPLACED (a newer upload, or a reset by hand)
```

1. Only a team's newest upload runs; older ones are replaced.
2. It **waits** while a round's attempt is under way, or a reset of the team's devbox is not done (`deploy_waits_for`: the processor's log says it once, the dashboard shows the team, `worms trace <team>` too); during a round the processor is stopped anyway.
3. **No reset** (since 2026-11-24; RESETTING is an older run's): the devbox as it is, the dev score going on. The team resets it by hand for fresh VMs and a new score; a reset after the upload began replaces it.
4. **Deploying:** a `devbox_deploy` job: VM A must be running (the host's word, else the reason, not a bare SSH error), an earlier upload's worm on it stopped, the zip onto it, the fuse lit (10 s) and checked armed, as a round's deploy is, a run time of an hour.
5. Armed, running, finished: the VMs stay as the worm left them. These follow the clock once the fuse is checked armed; nothing reads `main.sh`'s own state.

A deploy job **still queued** after `job_timeout` (30 min; the round agent stopped, paused, or held up) is taken off the queue and the run fails with why (needs the orchestrator's UPDATE on `host_jobs` state: `worms apply database`). **Why a run failed** is on the team's staff page (Staff → Teams), never the team's: it can name internals. A job the round agent was running when it was killed or stopped is marked failed when the agent starts again (`Agent.close_left_behind`): before, a team's devbox rebuild left that way held up its uploads and resets until `worms jobs cancel`.

## The gameplans

- **The files** are [`gameplans/*.py`](../gameplans): one `Gameplan` subclass each. **The framework** is [`src/gameplan`](../src/gameplan): the loader (a name, a file read without following links, its SHA-256), the runtime (`Runner`, a dry run, `validate`), the rendering of a VM's files with the run's secrets, the OpenTofu definition and the Ansible variables of a sandbox, the simulation.
- **Two copies:** the host's (the agent's workspace, `/opt/worms/gameplans`), which a round runs and checks, and the orchestrator's (`/opt/orchestrator/gameplans`, root's, readable by the app), which the staff's simulations run. `worms apply gameplans` deploys both and checks the host's (the dashboard's list). The dashboard holds none (any copy there is removed).
- **The records:** `gameplans` (each file: valid or not, its current SHA-256), `gameplan_versions` (every version, its bytes, immutable), `gameplan_changes`. **The dashboard never reads a gameplan's content:** no file on its VM, no read grant on the content column, and why a gameplan is invalid goes only to the round's log and the step's log.
- **The simulation** (the staff's Simulate, through the API): one at a time (an advisory lock), in a child process with limits (CPU, memory, writes) and a 20 s timeout, its result cached (20 per file).

## The resets

| What | When | How |
|---|---|---|
| **The service VMs** (ephemeral, private internet) | before a round's sandbox is built (its `build` step), and by hand (`worms vm reset services`) | all of them back to their `clean` snapshot (all or none), wait for SSH, the devbox map written again (`devbox_reconfigure`). Refused while a sandbox exists |
| **The sandbox** | built once per round, before its first attempt; rolled back before every attempt but the first after its snapshot | `build`: the old one destroyed, the devboxes off, the services reset, OpenTofu, `SetupSandbox.yml`; `snapshot`: every VM's `clean` snapshot, the devboxes on; `rollback`: every VM back to it, started. A sandbox of the same definition with every VM's snapshot is not built again; after the round, `delete_snapshot` and `teardown` |
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
| a running job silent for (no agent any more: `worms jobs cancel` closes it; a session's step beats every 30 s, and one whose session ended is closed at once: `services.left_behind`) | 300 s | `services.SILENT_AFTER` |
| the watch: the Proxmox API; an observe job; its answer within | 10 s; 60 s; 120 s | `ROUND_CONDUCTOR["watch_interval"]`, `["observe_interval"]`, `["observe_timeout"]` |
| captures' grace around a run | 120 s | `ROUND_CAPTURE_GRACE`, `--grace` |
| the worm's fuse, the drain after | 15 s, 120 s | `ROUND_TIMING` ([`config/settings/base.py`](../projects/dashboard/config/settings/base.py)) |

## Following it

`worms round` (the rounds, their attempts by state), `worms log round [<r>] summary` (what failed, why, which file has the rest), `worms log round [<r>] [<team>] [-f]` (a session's log, an attempt's steps), `worms jobs` and `worms show job <n>` (the queue, why a job waits), `worms log processor`, `worms log latest -f` (the job the agent runs now), `worms trace <team>` (a dev upload's way).
