# 5. Live environment: rounds and gameplans

This is the dashboard's **Live** view.

- **Round.** A round has a name, a start, an end, a duration per team, and a **gameplan** chosen by file name.
- **Gameplan.** A Python file in [`gameplans/`](../gameplans/) that defines two things:
  - the sandbox (VMs, networks, firewall rules, credentials, files);
  - what happens while a team's worm runs (`execute()`).
- **Turns.** When the round starts, every team's latest upload runs in turn in a fresh copy of that sandbox.
- **Publication.** A team's result is published only when its run succeeded.

## Architecture

```
dashboard ──(round: name, dates, gameplan file name, minutes per team)──► database
orchestrator: conduct_rounds ──HostJob rows──► round agent (root, node) ──► OpenTofu, Ansible, SSH into VMs
             └─ Proxmox API (power token: VM.Audit + VM.PowerMgmt on /pool/sandbox) ──► start/stop/suspend sandbox VMs
```

**The conductor** ([`conductor.py`](../projects/dashboard/orchestrator/rounds/conductor.py), service `orchestrator-conduct-rounds`)
- It runs on the low-privilege orchestrator VM and drives one attempt at a time.
- It powers sandbox VMs through the Proxmox API ([`power.py`](../projects/dashboard/orchestrator/rounds/power.py)). It refuses any VM that is not registered to the run, or whose Proxmox name does not match.

**Host jobs**
- Everything else is a `host_jobs` row: the conductor inserts it and sends `NOTIFY host_jobs`.
- The orchestrator's role may only insert jobs.
- The agent's role may only read them and update their state, result and error.

**The round agent** ([`scripts/round_agent/`](../scripts/round_agent/), service `worms-round-agent`)
- It runs as root and claims one queued job at a time, round jobs before devbox jobs.
- It validates every parameter against a fixed menu ([`validation.py`](../scripts/round_agent/validation.py)) and builds commands from literals and validated values only. It sends a heartbeat every 30 s.
- Job menu:
  - `gameplans`, `prepare`, `check`, `deploy`
  - `remove_flags`, `place_flags`, `write_file`, `teardown`
  - `devbox_provision`, `devbox_rebuild`, `devbox_reconfigure`, `devbox_deploy`
- Each job logs to `/var/log/worms/jobs/<uuid>.log`.

**Where gameplans live**
- **Orchestrator.** `/opt/orchestrator/gameplans/{plans,lib}`, readable by the service only. Set by `WORMS_GAMEPLAN_DIR` and `WORMS_GAMEPLAN_LIB`.
- **Node.** `/opt/worms/gameplans` and `/opt/worms/src/gameplan`.
- **Dashboard VM.** It holds no copy; Ansible deletes one if present.
- **What the dashboard sees.** Only the `gameplans` table (file name, valid or not) and a round's `gameplan_status`. Why a gameplan is invalid goes to the orchestrator's journal and the node's job log, never to the database.

## A round, step by step

1. **Staff create the round** on **Staff → Rounds** ([`forms.py`](../projects/dashboard/dashboard/forms.py)).
   - Fields: name, start (in the future), end, seed, a **valid** gameplan, and minutes per team (1–10080, default 60).
   - Overlapping rounds are refused. So is a window shorter than teams × slot.
   - A slot is 15 s fuse + duration + 120 s drain + 900 s overhead ([`core/schedule.py`](../projects/dashboard/core/schedule.py)). Retries are not in the estimate.
   - A round can be edited while it has no runs; after its start only the end can move.
2. **Validation, every 10 minutes** ([`orchestrator/rounds/gameplans.py`](../projects/dashboard/orchestrator/rounds/gameplans.py)).
   - The conductor asks the node for its view (the `gameplans` job: SHA-256 and validity, with the packages checked against the apt cache). It validates its own copy.
   - A file is valid only if both copies are identical and both find nothing wrong.
   - Each upcoming round gets `gameplan_status` `ok` or `invalid`, checked at its duration per team.
3. **Planning.** Once `round_start` has passed, the round is planned ([`planner.py`](../projects/dashboard/orchestrator/rounds/planner.py)).
   - Every team of the competition gets a run, at attempt 0, in an order shuffled by the round's seed.
   - Each run uses the team's latest Live upload from **before** `round_start`. A team without one is SKIPPED.
4. **Pin.** Before a round's first attempt, the gameplan's SHA-256 is written into every run of that round, so every team runs the same version. If the gameplan is invalid, the round, and every round after it, waits.
5. **Attempts.** The conductor runs the attempts one by one, ordered by (round, position, attempt) (next section).
6. **Finish.** A round is finished when its end has passed **and** no run is still open. Runs and retries may go past `round_end`.

## An attempt

```
PENDING → PREPARING → CHECKING → RUNNING → ENDING → TEARING_DOWN → FINISHED
                 └──────────────── any failure ────────────────→ FAILED (+ retry)       SKIPPED
```

| State | What happens |
|---|---|
| PREPARING | Job `prepare` (below). The conductor stores the topology and the entry VM, creates the attempt's `virtual_machines` rows and records the flag hashes |
| CHECKING | Job `check`: `proxmox-firewall`, `worms-host-firewall`, the watchdog timer and `worms-mirror` are active; the mirror drops; every VM is registered, running and has exactly `cpu: x86-64-v3` |
| RUNNING | The gameplan's `execute()` runs against the real backend; then the rest of the duration is waited out |
| ENDING | Every sandbox VM is hard-stopped; 120 s drain for late submissions |
| TEARING_DOWN | Job `teardown`: clears the current-team file, deletes the run's secrets. If another attempt of the round follows, the sandbox is stopped and kept; otherwise it is destroyed |

**`prepare`** ([`handlers.py`](../scripts/round_agent/handlers.py)):
1. The node loads its own copy of the gameplan, pinned to the SHA-256, and validates it again.
2. It builds the sandbox, or reuses it:
   - **First attempt of a round, or another gameplan.** It refuses while the internet window is open. It destroys the old sandbox, shuts down the devboxes and rolls the 11 service VMs back to `clean`. Then it writes `generated_vms.tf`, applies the sandbox stack and runs [`SetupSandbox.yml`](../ansible/SetupSandbox.yml): the CA, apt, and role [`sandbox_vm`](../ansible/roles/applications/sandbox_vm/) for the gameplan's packages, users, files, setup commands and services. It snapshots every sandbox VM as `clean` and starts the devboxes again.
   - **Later attempts.** `qm rollback … clean` of the same sandbox.
3. It generates **this run's** secrets on the node: SSH keys and passwords.
4. It sets the passwords, installs the keys and writes the files marked `at_reset`.
5. It places a fresh flag on **every** VM, reads it back and reports only its hash.
6. It keeps the secrets in `/var/lib/worms/runs/<run>.json` (root, 0600) until teardown.

**Errors of the gameplan jobs.** `prepare`, `write_file` and `gameplans` report "the job failed: its log says why" to the database. The reason, which may quote the gameplan, stays in the job log.

**The worm.** `deploy_worm()` sends a `deploy` job, which:
1. writes the team's uuid to `/etc/sandbox-api/current-team` on scoring and logging;
2. fetches the zip from the dashboard VM and checks its SHA-256;
3. unpacks it into `/home/debian/worm` on the entry VM;
4. arms `systemd-run --on-active=15 --unit=worms-start --uid debian bash main.sh`.

The run's clock starts at `armed_at + 15 s` and ends after the round's duration per team.

### Failures, retries, publication

- **Before `deploy_worm()`.** Every guest step (flags, files) must succeed; any failure fails the attempt.
- **After it.** A guest step that fails is recorded (staff only) and skipped: the worm is root on the VM and may have caused it.
- **Always fatal.** Power and API failures fail the attempt, and so does a `RoundError` that `execute()` catches.
- **What a failure does** ([`conductor.py`](../projects/dashboard/orchestrator/rounds/conductor.py), `fail`):
  - The attempt becomes FAILED and its VMs stop.
  - If `attempt < max_retries` (3), attempt + 1 is created with the same upload, gameplan and duration. That makes **at most 4 attempts**, and only the failed team's run repeats.
  - The retry runs right after the teardown.
- **Publication.** Only a FINISHED attempt counts.
  - Its captures are `FlagCapture.published()`; its logs are published too.
  - A team sees nothing of an unfinished run: its progression is `withheld`, and the API returns no submissions or logs.
  - Points of failed attempts are discarded.
- **Restart.** If the conductor restarts, an attempt in PREPARING, CHECKING or RUNNING fails and is retried; ENDING and TEARING_DOWN are completed. **Do not redeploy the orchestrator during a round:** each deploy costs the running team an attempt.
- **Teardown hold.** If the teardown of a FAILED attempt fails, the queue holds until it is fixed by hand.

### Scoring

[`captures.py`](../projects/dashboard/orchestrator/rounds/captures.py) runs in `process_attempts`, every 2 s. A submitted flag counts if all of these hold:
- its hash is a sandbox VM's current flag;
- that VM belongs to the submitting team's run;
- it arrived between `worm_start_at − 120 s` and `ends_at + 120 s`;
- the team has not captured that VM before.

Flags released late are credited for submissions up to 900 s earlier. The round score is the number of VMs captured. The leaderboard turns places into points (25, 18, 15, …).

### Timing settings

[`config/settings/orchestrator.py`](../projects/dashboard/config/settings/orchestrator.py), `ROUND_CONDUCTOR`:

| Setting | Default | Environment |
|---|---|---|
| `worm_start_delay` | 15 s | `ROUND_WORM_START_DELAY` |
| `end_drain` | 120 s | `ROUND_END_DRAIN` |
| `slot_overhead` | 900 s | `ROUND_SLOT_OVERHEAD` |
| `job_timeout` (no heartbeat) | 1800 s | `ROUND_JOB_TIMEOUT` |
| `capture_grace` | 120 s | `ROUND_CAPTURE_GRACE` |
| `max_retries` | 3 | `ROUND_MAX_RETRIES` |
| `gameplan_check_interval` | 600 s | — |

## Writing a gameplan

One file in `gameplans/`, named `[a-z][a-z0-9_]*.py`, holding exactly one `Gameplan` subclass. Commit it like any other file and deploy it with `worms build`.

- **Who runs it.** The code runs as root on the node and as the orchestrator's service user, so gameplan authors are trusted.
- **Teams never see it.**
- **Reference.** [`src/gameplan/definition.py`](../src/gameplan/definition.py) (API, limits), [`runtime.py`](../src/gameplan/runtime.py) (`execute()`, validation), [`loader.py`](../src/gameplan/loader.py), [`build.py`](../src/gameplan/build.py).

```python
from gameplan import Allow, BasicVM, DjangoVM, FirewallRule, FreezeState, Gameplan, Incoming, Outgoing, PostgreVM


class RoundOne(Gameplan):                       # gameplans/round_one.py
    def __init__(self):                         # the sandbox: recorded, never run as a round
        self.vm0, self.vm1, self.vm2 = BasicVM(), DjangoVM(), PostgreVM()   # sandbox-vm0, -vm1, -vm2
        key = self.generate_ssh_key("id_rsa")   # new for every run
        login = self.generate_ssh_pwd_login()
        vms = [self.vm0, self.vm1, self.vm2]
        self.register_fw_rule(vms, FirewallRule(Allow, 22, Incoming))
        self.register_fw_rule(vms, FirewallRule(Allow, 22, Outgoing))
        self.add_private_ssh_key(self.vm0, key)
        self.add_public_ssh_key([self.vm0, self.vm1], key)
        self.add_ssh_pwd_login(self.vm2, login)
        self.write_file(self.vm1, "~/note", f"user: {login.user}, pwd: {login.pwd}\n")
        self.register_network([self.vm0, self.vm1])
        self.register_network([self.vm1, self.vm2])
        self.set_worm_entrypoint(self.vm0)

    def execute(self):                          # the round
        f1, f2 = self.remove_flag(self.vm1), self.remove_flag(self.vm2)
        self.deploy_worm()
        self.wait(600)
        with FreezeState(self.vms) as fs:
            fs.unfreeze(self.vm0)
            self.vm0.shutdown()
        self.add_flag(f1)
        self.add_flag(f2)
```

### `__init__`: the sandbox

- **VMs.** Assign them to `self.<attr>`; the VM is then named `sandbox-<attr>`, with `_` becoming `-`. A list becomes `sandbox-<attr>-<i>`. `gw` is reserved.
- **VM classes.**
  - `BasicVM(cores=1, memory=512, disk=5)`: Debian 13. Class attributes `packages`, `remove`, `files`, `setup` (root shell commands) and `services`; per instance, `install()` and `uninstall()`.
  - `DjangoVM`: a Django project served by gunicorn on :8000.
  - `PostgreVM`: PostgreSQL on :5432, password logins from the sandbox networks.
  - Subclass `BasicVM` for your own.
- **Packages.** Every package must be in `apt_cache_seed_packages` ([`apt_cache/defaults`](../ansible/roles/private_internet/apt_cache/defaults/main.yml)): VMs install from the offline apt cache.
- **`register_network(vms, name=None)`.**
  - The N-th network gets the N-th /24 of 10.64.0.0/10 and bridge `vmbr<1000+N>`.
  - Addresses are .10–.249, in a fixed shuffle.
  - A VM's first network carries its default route, via sandbox-gw (.1).
  - **sandbox-gw does not route between sandbox networks.**
- **`register_fw_rule(vms, FirewallRule(Allow|Deny, port, Incoming|Outgoing, proto="tcp", peers=None))`.**
  - Every VM drops everything both ways, so a connection needs an Outgoing rule on the sender **and** an Incoming rule on the receiver.
  - `port` is an int, a `(low, high)` range, or `None`.
  - `peers` are VMs or networks; the default is every network the VM is on.
  - Scoring, logging, DNS, the private internet and the LLM endpoint are always allowed.
- **Credentials, new for every run.**
  - `generate_ssh_key(name, type="ed25519"|"rsa"|"ecdsa")` and `add_private_ssh_key(vms, key, user="debian")` (`~/.ssh/<name>`, 0600). `add_public_ssh_key(vms, key, user)` appends to `authorized_keys`.
  - `generate_ssh_pwd_login(user=None)` and `add_ssh_pwd_login(vms, login)`. Only these users may log in with a password.
  - The node's own SSH access stays key-only.
- **`write_file(vm, path, content, mode=0o644, owner="debian", at_reset=True)` returns a `FileHandle`.**
  - `content` may use `login.pwd`, `login.user`, `key.private` and `key.public`, for instance in an f-string. They are markers (`@@worms-secret:…@@`), filled in on the node per run. They are allowed nowhere else.
  - With `at_reset=False` the file is written only when `execute()` calls `self.write_file(handle)`.
- **Entry point.** `set_worm_entrypoint(vm)`, exactly once.
- **Limits.**
  - 32 VMs, 30 networks, 8 NICs per VM.
  - 1–4 cores, 256–8192 MB, 5–64 GB.
  - 64 KiB per file.
  - Duration 60 s – 7 days.

### `execute()`: the round

| Call | Does |
|---|---|
| `deploy_worm()` | Exactly once. Deploys the team's upload to the entry VM; the run's clock starts 15 s later |
| `wait(seconds)` | Sleeps. At the end of the run, `execute()` is stopped |
| `remove_flag(vm)` → `Flag` | Deletes the VM's flag; a later submission of it no longer counts |
| `add_flag(flag_or_vm)` | A new flag on a VM that has none |
| `write_file(handle)` | Writes a file declared with `at_reset=False` |
| `vm.start()`, `stop()`, `shutdown(timeout=60)`, `suspend()`, `resume()`, `status()` | Proxmox power, awaited |
| `with FreezeState(vms) as fs: fs.unfreeze(vm)` | Suspends the running VMs, resumes them on exit |
| `log(msg)`, `elapsed`, `remaining` | The orchestrator's journal; time |

- **Starting state.** Every VM starts with a flag.
- **Errors.** `GameplanError` means the gameplan is wrong; `RoundError` means the infrastructure failed. Both fail the attempt.

### Validation

[`runtime.validate`](../src/gameplan/runtime.py) runs before a round is scheduled:
1. `__init__` is run twice. The two specs must be identical: no randomness, no clock.
2. Every rule, path, name and limit is checked.
3. `execute()` runs on a fake clock (`DryRun`). Power transitions must be legal, `deploy_worm()` must be called, and `execute()` must finish before the run ends.

The dry run costs no time for jobs and boots, so leave margin. To check a file locally:

```sh
PYTHONPATH=src python3 -c "from gameplan import loader, runtime; print(runtime.validate(loader.load('gameplans', 'round_one.py').cls, duration=3600))"
```

## Running a round

1. **Create the round** on **Staff → Rounds**: its window (the start is the upload deadline; teams' latest Live uploads before it are used) and its duration per team. The gameplan is optional here.
2. **Commit and deploy** the gameplan: `worms build orchestrator round-agent` (both copies), outside a round. The conductor restarts; the agent reads gameplan files fresh.
3. **Check it is valid.** **Staff → Rounds** marks it valid. If not, read `journalctl -u orchestrator-conduct-rounds` on the orchestrator and the `gameplans` job log on the node.
4. **Choose it** on the round's page. That works until the round's first attempt starts: a round without a valid gameplan waits ([`conductor.py`](../projects/dashboard/orchestrator/rounds/conductor.py), `_pin`), and then the gameplan's version is pinned for every attempt.
5. **Watch it.** **Staff → Rounds → round** shows the attempts, their errors and host jobs, and a replay of each run. `worms status` shows the active run. On the node, `tail -F /var/log/worms/jobs/latest.log`.

## Known gaps

As of 2026-11-08. The gameplan rework is uncommitted and not yet deployed.

- **Testing.** No round has run on a real node yet. The conductor's tests run it against a fake Proxmox and a fake round agent ([`tests/test_rounds.py`](../tests/test_rounds.py)).
- **Shared services.** The service VMs are rolled back only when a sandbox is built, i.e. the first attempt of a round. What one team's worm does to them stays for later attempts.
- **Operations.**
  - Nothing clears a failed-teardown hold.
  - New agent code needs `systemctl restart worms-round-agent` on the node; gameplan files are read fresh.
