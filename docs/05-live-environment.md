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
orchestrator: conduct_rounds ──HostJob rows──► round agent (root, host) ──► OpenTofu, Ansible, SSH into VMs
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
  - `remove_flags`, `place_flags`, `write_file`, `read_file`, `teardown`
  - `devbox_provision`, `devbox_rebuild`, `devbox_reconfigure`, `devbox_deploy`
- Each job logs to `/var/log/worms/jobs/<uuid>.log`.

**Where gameplans live**
- **Orchestrator.** `/opt/orchestrator/gameplans/{plans,lib}`, readable by the service only. Set by `WORMS_GAMEPLAN_DIR` and `WORMS_GAMEPLAN_LIB`.
- **Host.** `/opt/worms/gameplans` and `/opt/worms/src/gameplan`.
- **Dashboard VM.** It holds no copy; Ansible deletes one if present.
- **Database.** Every version the orchestrator has seen, bytes included ([Versions](#versions)). Only the orchestrator and the admin may read the bytes.
- **What the dashboard sees.** The files and whether each is valid, every version's SHA-256, file name, size and `description` for teams, which runs pinned it and the files' history; a round's `gameplan_status`; and the simulations staff ask for ([Simulation](#simulation)). Never a version's bytes. Why a gameplan is invalid goes to the orchestrator's journal and the host's job log, never to the database.

### Versions

A gameplan version is its bytes, named by their SHA-256. Once recorded, a version never changes. A round runs one version from its first attempt to its last.

- **Recorded.** Each time the orchestrator checks the gameplans (when the conductor starts, then every 10 minutes), it reads each file once and records it ([`gameplans.py`](../projects/dashboard/orchestrator/rounds/gameplans.py), `refresh`):
  - **A new version**, in `gameplan_versions`: the SHA-256, the bytes, their size, the file name it had, its `description`, when it was first seen and the database account that recorded it.
  - **A change**, in `gameplan_changes`: the file appeared, became another version, or went.
  - **The files now**, in `gameplans`: each one's current version, valid or not.
- **Immutable.** The database itself keeps the record whole ([migration 0019](../projects/dashboard/core/migrations/0019_gameplan_versions.py)):
  - A CHECK computes each version's SHA-256 from its bytes, and its size.
  - No role may UPDATE or DELETE a row of either table, and a trigger refuses both to the owner too.
  - The time and the recording account are filled by the database; the orchestrator may insert only the other columns.
  - `worms db reset` keeps both tables.
- **Pinned.** When a round's first attempt starts, every run of the round gets the version its file has then: `round_runs.gameplan_sha256`, and `gameplan_version` pointing at the record (a constraint keeps the two equal). Retries keep it. Before that, the round shows its file's current version.
- **Run from the files.** The host and the orchestrator run the file in their copy, after checking it is exactly the pinned SHA-256. Neither ever runs code from the database: whoever could write to the database could otherwise put code on the host, where it runs as root. The record is for the audit.
- **Edits.** A file edited after a round pinned it is a new version. The record keeps the old one. The round's attempts fail until both copies hold the pinned version again, so edit a gameplan only when no round has pinned the old version and still has attempts to run.
- **Older runs.** Runs pinned before versions were recorded are linked to their version when a file with that SHA-256 is checked. A hash whose file is gone stays known by hash only: its bytes were never recorded.
- **Where to look.**
  - **Staff → Gameplans:** the files now, every version (newest first) with the rounds and attempts that ran it, the history, and the hashes known only as hashes.
  - **A round's staff page:** its version, pinned or current.
  - **`worms show gameplans`:** each file's current version.
  - **The bytes:** for the admin only, on the database VM (`worms login database`):

    ```sh
    sudo -u postgres psql mydb -Atc "SELECT convert_from(content, 'UTF8') FROM gameplan_versions WHERE sha256 = '<sha256>'"
    ```

## A round, step by step

1. **Staff create the round** on **Staff → Rounds** ([`forms.py`](../projects/dashboard/dashboard/forms.py)).
   - Fields: name, start (in the future), end, seed, a gameplan (optional: one of the valid ones, or none yet), and minutes per team (1–10080, default 60).
   - Overlapping rounds are refused. So is a window shorter than teams × slot.
   - A slot is 15 s fuse + duration + 120 s drain + 900 s overhead ([`core/schedule.py`](../projects/dashboard/core/schedule.py)). Retries are not in the estimate.
   - Before its start a round can be edited fully. After it, its end, its gameplan and its duration per team can change until its first attempt starts. A team skipped for want of an upload does not count as a start.
   - Every staff page warns of a round not over whose gameplan is missing or not valid.
2. **Validation, every 10 minutes** ([`orchestrator/rounds/gameplans.py`](../projects/dashboard/orchestrator/rounds/gameplans.py)).
   - The conductor asks the host for its view (the `gameplans` job: SHA-256 and validity, with the packages checked against the apt cache). It validates its own copy.
   - A file is valid only if both copies are identical and both find nothing wrong.
   - Each upcoming round gets `gameplan_status` `ok` or `invalid`, checked at its duration per team.
3. **Planning.** Once `round_start` has passed, the round is planned ([`planner.py`](../projects/dashboard/orchestrator/rounds/planner.py)), with or without a gameplan.
   - Every team of the competition gets a run, at attempt 0, in an order shuffled by the round's seed.
   - Each run uses the team's latest Live upload from **before** `round_start`, however late the gameplan comes. A team without one is SKIPPED.
4. **Pin, or wait alone** ([`conductor.py`](../projects/dashboard/orchestrator/rounds/conductor.py), `_next` and `_pin`).
   - Before a round's first attempt, its gameplan's SHA-256 is written into every run of that round, so every team runs the same version.
   - The gameplan must be chosen, and valid on the orchestrator and the host at the round's duration per team.
   - If it is not, the round waits, **alone**: the rounds after it go ahead. The conductor looks at it again every `gameplan_poll` seconds (60) and records each look (`gameplan_checked_at`). Once staff choose a valid gameplan, the round begins at the next look, as soon as no other round is under way.
   - A round whose teams all lack an upload needs no gameplan: each run is skipped.
5. **Attempts.** One round at a time. The round under way (a pinned attempt still to come) goes on until every team's run is over, retries included. Then the next round that can begin, earliest start first. Within a round, attempts run by (position, attempt) (next section).
6. **Finish.** A round is finished when its end has passed **and** no run is still open. Runs and retries may go past `round_end`, and so may a round that waited for its gameplan.

## An attempt

```
PENDING → PREPARING → CHECKING → RUNNING → ENDING → TEARING_DOWN → FINISHED
                 └──────────────── any failure ────────────────→ FAILED (+ retry)       SKIPPED
```

**Before an attempt** (it is still PENDING; [`conductor.py`](../projects/dashboard/orchestrator/rounds/conductor.py), `_ready`):
1. A round that holds the devboxes: job `hold_devboxes` again, before every attempt (it shuts down any devbox that runs). If it fails, the attempt does not start.
2. Job `preflight` ([`handlers.py`](../scripts/round_agent/handlers.py)), which changes nothing: the internet window closed; the host's firewall units and the cluster firewall; every service VM running and the scoring and logging APIs active; the devboxes off (when held); no sandbox VM running; **memory**: MemAvailable plus what the ZFS ARC gives back must hold every ceiling of the gameplan's VMs and the gateway, 40 MB per QEMU and a 1024 MB reserve (a rooted guest can touch all of its memory; `round_one` needs 12 616 MB); every package the gameplan installs cached in apt-cacher-ng. Warnings only: service VMs without a `clean` snapshot, and how far the VMs that run could still grow.
3. A failed hold or preflight is logged as an error with every failed check, and tried again after 60 s. After 5 failures in a row the round is **put on hold** (`worms round resume <r>` once fixed). No attempt is used up.

| State | What happens |
|---|---|
| PREPARING | Job `prepare` (below). The conductor stores the topology and the entry VM, creates the attempt's `virtual_machines` rows and records the flag hashes |
| CHECKING | Job `check`: `proxmox-firewall`, `worms-host-firewall`, the watchdog timer and `worms-mirror` are active; the mirror drops; every VM is registered, running and has exactly `cpu: x86-64-v3`. Then, from inside every sandbox VM at once, before the worm (the VMs are still ours; [`probe.py`](../scripts/round_agent/probe.py)): the flag is there; DNS; scoring and logging over HTTPS verified by the internal CA (a GET, never a submission); `apt-get update` through the cache; pypi; the clock synced; the internet, the dashboard, the database, the orchestrator and the devboxes unreachable; every connection the gameplan allows works and the ones it does not are refused; every file has its owner and mode. Every sandbox and devbox QEMU gets `oom_score_adj` 1000: an OOM kill takes a team VM, never the database |
| RUNNING | The gameplan's `execute()` runs against the real backend; then the rest of the duration is waited out. The run is watched (below) |
| ENDING | Every sandbox VM is hard-stopped; 120 s drain for late submissions |
| TEARING_DOWN | Job `teardown`: clears the current-team file, deletes the run's secrets. If another attempt of the round follows, the sandbox is stopped and kept; otherwise it is destroyed |

**`prepare`** ([`handlers.py`](../scripts/round_agent/handlers.py)):
1. The host loads its own copy of the gameplan, pinned to the SHA-256, and validates it again.
2. It builds the sandbox, or reuses it:
   - **First attempt of a round, or another gameplan.** It refuses while the internet window is open. It destroys the old sandbox, shuts down the devboxes and rolls the 11 service VMs back to `clean`. Then it writes `generated_vms.tf`, applies the sandbox stack and runs [`SetupSandbox.yml`](../ansible/SetupSandbox.yml): the CA, apt, and role [`sandbox_vm`](../ansible/roles/applications/sandbox_vm/) for the gameplan's packages, users, files, setup commands and services. It snapshots every sandbox VM as `clean` and starts the devboxes again.
   - **Later attempts.** `qm rollback … clean` of the same sandbox.
3. It generates **this run's** secrets on the host: SSH keys, passwords and TOTP seeds.
4. It sets the passwords, installs the keys and writes the files marked `at_reset`. Documents, archives and vaults are made on the host at this point, with the secrets in them ([`render.py`](../src/gameplan/render.py)).
5. It places a fresh flag on **every** VM, reads it back and reports only its hash.
6. It keeps the secrets in `/var/lib/worms/runs/<run>.json` (root, 0600) until teardown.

**Errors of the gameplan jobs.** `prepare`, `write_file`, `read_file` and `gameplans` report "the job failed: its log says why" to the database. The reason, which may quote the gameplan, stays in the job log.

**The worm.** `deploy_worm()` sends a `deploy` job, which:
1. writes the team's uuid to `/etc/sandbox-api/current-team` on scoring and logging;
2. fetches the zip from the dashboard VM and checks its SHA-256;
3. unpacks it into `/home/debian/worm` on the entry VM;
4. arms `systemd-run --on-active=15 --unit=worms-start --uid debian bash main.sh`.

The run's clock starts at `armed_at + 15 s` and ends after the round's duration per team.

### Failures, retries, publication

- **Before `deploy_worm()`.** Every guest step (flags, files) must succeed; any failure fails the attempt.
- **After it.** A guest step that fails is recorded (staff only) and skipped: the worm is root on the VM and may have caused it.
- **Always fatal.** Power and API failures fail the attempt (API calls are tried 3 times first), and so does a `RoundError` that `execute()` catches.
- **The watch.** Only the host's view of a sandbox VM counts once the worm is there: every VM is assumed rooted. Every sandbox VM and the gateway have an expected state (running after the check, then what each power action of the gameplan left). Every 10 s the conductor reads every VM's state from the Proxmox API. A VM not in its expected state: job `observe` asks the host why ([`host_state.py`](../scripts/round_agent/host_state.py): the kernel's and systemd's lines of that VM's QEMU, Proxmox's tasks).
  - Stopped by its guest after the deploy (a power-off from inside, a guest that halted its QEMU): the worm's doing. An event (`vm_stopped`, how: guest), not an error; the VM stays stopped.
  - Anything else fails the attempt, which runs again: an OOM kill, a crashed QEMU, a Proxmox task that stopped it (an operator, the firewall watchdog), a cause the host cannot tell, a VM running or paused that should not be, the gateway not running, an observe job not answered within 120 s. Raised so that `execute()` cannot catch it.
  - Every 60 s, and after every start, an observe job for every VM: the host's memory goes to the orchestrator's journal (a warning below 2048 MB available), team VMs' QEMUs get their OOM priority again, and a devbox found running while the round holds them is logged as an error and held again.
- **Twice in a row before the deploy.** An attempt that fails before its deploy job, right after another attempt of the round that did too, puts the round on hold: a fault of ours that trying again does not mend stops after two attempts, not after every team's four. A team's upload cannot trigger it (a zip that does not unpack fails at the deploy), nor can a conductor restart.
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
| `gameplan_poll` (a round waiting for its gameplan) | 60 s | `ROUND_GAMEPLAN_POLL` |
| `preflight_retry`, `preflight_attempts` (then the round on hold) | 60 s, 5 | — |
| `watch_interval` (the Proxmox API), `observe_interval` (an observe job) | 10 s, 60 s | — |
| `observe_timeout` (not answered: the attempt fails) | 120 s | — |
| `memory_warn_mb` (the journal warns below it) | 2048 MB | — |
| The round agent's `memory_reserve_mb` (preflight) | 1024 MB | — |

## Writing a gameplan

One file in `gameplans/`, named `[a-z][a-z0-9_]*.py`, holding exactly one `Gameplan` subclass. Commit it like any other file and deploy it with `worms apply gameplans` ([Running a round](#running-a-round)).

- **Who runs it.** The code runs as root on the host and as the orchestrator's service user, so gameplan authors are trusted.
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
  - `BasicVM(cores=1, memory=512, disk=5)`: Debian 13. Class attributes `packages`, `files`, `setup` (root shell commands), `remove`, `purge`, `services` and `ssh_port`; per instance, `install()`, `uninstall()`, `add_setup()`, `add_service()`, `add_user()` and `set_ssh_port()`.
  - `DjangoVM`: a Django project served by gunicorn on :8000.
  - `PostgreVM`: PostgreSQL on :5432, password logins from the sandbox networks.
  - Subclass `BasicVM` for your own.
- **The build, in order** ([`sandbox_vm`](../ansible/roles/applications/sandbox_vm/tasks/main.yml)): packages, users, files, setup commands, removals, services. The snapshot every run starts from is taken after that.
- **Packages.** Every package must be in `apt_cache_seed_packages` ([`apt_cache/defaults`](../ansible/roles/private_internet/apt_cache/defaults/main.yml)): VMs install from the offline apt cache. After adding one, refill the cache: `worms apply apt-cache --packages`, outside a round.
- **Removing packages.** `vm.uninstall(*packages, purge=False)`, or the class's `remove` and `purge`.
  - Removed after the setup commands, so a setup step can still use what goes: `install("gcc", "libc6-dev")`, compile in `add_setup`, `uninstall("gcc", "libc6-dev", purge=True)`. `purge` also removes the configuration files.
  - Nothing a VM needs can go: `openssh-server`, `sudo`, `python3`, `systemd`, `ca-certificates` and the rest of `PROTECTED_PACKAGES` ([`definition.py`](../src/gameplan/definition.py)), nor what one of the gameplan's logins needs (`vsftpd` for an FTP login, ...). Validation refuses those names.
  - The build asks apt first what a removal would take with it (its dependents) and fails before removing anything if that includes a protected package. A package that is not installed is skipped.
- **Users.** `add_user(vms, user, shell=None, groups=None, sudo=None)` (or `vm.add_user(user, …)`): made when the sandbox is built, locked, with no key. A login's user is made by the login; call `add_user` as well for the options.
  - `shell`: `/bin/bash` (default), `/bin/sh`, `/bin/rbash` (restricted: no `cd`, no `/` in commands), or `/usr/sbin/nologin` for a user nothing logs in as (validation refuses it for a user with a login).
  - `groups`: supplementary groups, made if missing. In `sudo`, the user may sudo with its own password.
  - `sudo`: `True` (anything, no password), or a list of commands run as root without a password (`["/usr/bin/find"]`): a step up for a worm that knows its GTFOBins.
  - Called again for the same user, the groups add up; a different shell or sudo rule is an error. `root` and `debian` are the image's and cannot be changed.
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
- **Secrets, new for every run.** Each stands for its value in a file's content (a marker, `@@worms-secret:…@@`, filled in on the host). They are allowed nowhere else.

  | Call | Gives | In a file |
  |---|---|---|
  | `generate_ssh_key(name="id_ed25519", type="ed25519"\|"rsa"\|"ecdsa", passphrase=None)` | a key pair; `name` is its file name in `~/.ssh`. With a passphrase the private key is encrypted with it (OpenSSH's format, as `ssh-keygen -p` writes it) | `key.private`, `key.public`, `key.passphrase` |
  | `generate_login(user=None, length=None, charset=None)` (also `generate_ssh_pwd_login`) | a user and a password: long and random, or as weak as `length`/`charset` say, for a worm to guess (`length=4, charset="digits"`) | `login.user`, `login.pwd` |
  | `generate_password(length=16, charset="alnum"\|"digits"\|"hex")` | a password of no user, itself a string | `pw` |
  | `generate_totp(account, issuer="worms", digits=6, period=30, algorithm="SHA1")` | an authenticator's seed | `totp.secret`, `totp.uri` (`otpauth://…`) |

- **Logins.** Each makes its user on the VM if it is new; `add_user` sets its shell, groups and sudo. A password login admits only its own users: a user with a password for `su` does not get in over telnet or FTP. The host's own SSH access stays key-only. Open the ports with firewall rules.

  | Call | The worm needs |
  |---|---|
  | `add_ssh_key_login(vms, key, user="debian")` | the private key (`add_public_ssh_key` alone, for an existing user) |
  | `add_ssh_pwd_login(vms, login)` | the password; only these users may use one over SSH |
  | `add_ssh_totp_login(vms, key, totp, user)` | the private key **and** the current code ([`pam_google_authenticator`](https://github.com/google/google-authenticator-libpam), ±8 codes). Only `user` is asked for one |
  | `add_telnet_login(vms, login)` | the password, on port 23 (`inetutils-telnetd` under inetd; PAM service `remote`, with a list of the telnet users) |
  | `add_ftp_login(vms, login)` | the password, on port 21 (`vsftpd`), passive mode on `FTP_PASSIVE_PORTS` (40000–40019: open them too). Read and write wherever the user may, not chrooted: fetch a file, or put `~/.ssh/authorized_keys` and log in over SSH |
  | `add_local_login(vms, login)` | the password, from the VM itself: `su`, and `sudo` for a user in the group `sudo`. No way in from outside |
  | `add_private_ssh_key(vms, key, user="debian")` | nothing: puts `~/.ssh/<name>` (0600) there for the worm to find |

  - **A key's passphrase** is text: fixed, for a worm to guess from hints, the run's own (`generate_password()`, a login's `pwd`), or both. It may hold the run's passwords and TOTP secrets, not a key; it is one line. The host encrypts the key with it in Python, so the passphrase is never a command's argument there. `key.passphrase` stands for it in a file.
  - **SSH on another port.** `vm.set_ssh_port(2222)` (or `ssh_port = 2222` on a VM class): sshd answers there, and every SSH login of the VM with it. It keeps listening on 22, which the host logs in on over the management NIC; the gameplan's firewall decides which port the sandbox networks see, so open the new one and leave 22 closed. Validation refuses a port one of the VM's services takes (23 for telnet, 21 and 40000–40019 for FTP, 8000 for `DjangoVM`, 5432 for `PostgreVM`).

  ```python
  pin = self.generate_password(length=4, charset="digits")
  key = self.generate_ssh_key("id_backup", passphrase=f"rex{pin}")    # the dog's name, then a PIN
  self.add_ssh_key_login(self.vault, key)
  self.vault.set_ssh_port(2222)
  self.register_fw_rule(self.vault, FirewallRule(Allow, 2222, Incoming, peers=[self.files]))
  self.register_fw_rule(self.files, FirewallRule(Allow, 2222, Outgoing, peers=[self.vault]))
  self.add_private_ssh_key(self.files, key)
  self.files.write_file("~/todo.txt", "the backup key: rex + the PIN of the garage door\n")
  ```

  Users, logins and sudo rules make steps up on one VM. The numbers are the worm's steps:

  ```python
  pi = self.generate_login("pi", length=4, charset="digits")         # 1. guess pi's PIN over telnet
  self.add_telnet_login(self.box, pi)
  self.add_user(self.box, "pi", shell="/bin/rbash", sudo=["/usr/bin/cat /root/notes"])  # 2. read one root file
  admin = self.generate_login("admin")
  self.box.write_file("/root/notes", f"admin: {admin.pwd}\n", owner="root", mode=0o600)
  self.add_user(self.box, "admin", groups=["sudo"])
  self.add_local_login(self.box, admin)                                # 3. su admin, then sudo: root
  ```

- **`vm.write_file(path, content, mode=0o644, owner="debian", at_reset=True)`** (or `self.write_file(vm, path, …)`) returns a `FileHandle`.
  - `~` is the owner's home. The owner is root, debian, or a user of that VM.
  - **Give a file to the user the worm arrives as on that VM.** A home is 0700 on the image, so only its owner can read what is in it. The spec refuses a file or private key in the home of a user nothing logs in as, on a VM that is not the entry point, unless root, debian or a user with sudo logs in there. Root's files and paths outside `/home` are not checked.
  - With `at_reset=False` the file is written only when `execute()` calls `self.write_file(handle)`.
  - `content` is text or one of these ([`content.py`](../src/gameplan/content.py)). Any text in them, a zip's password and a vault's password can be secrets. The host makes the file when it writes it ([`render.py`](../src/gameplan/render.py)); it never passes the database.

  | Content | File |
  |---|---|
  | `"text"`, f-strings | text (UTF-8) |
  | `Doc(paragraphs, title="")` | Word 97–2003 `.doc` (one paragraph per item or per line) |
  | `Docx(paragraphs, title="")` | Word `.docx` |
  | `Ods({sheet: rows})` or `Ods(rows)` | OpenDocument spreadsheet `.ods`; a cell is text or a number |
  | `Zip({name: content}, password=None)` | `.zip`; entries are text or any of these. With a password, ZipCrypto |
  | `KeePass(password, [Entry(title, username, password, url, notes, otp=totp)])`, or `{group: entries}` | KeePass `.kdbx` (KDBX 4, AES-256, AES-KDF) |
  | `TotpExport(totps, format="aegis"\|"uri")` | an authenticator's plain export: Aegis JSON, or `otpauth://` lines |

- **`vm.watch_file(path)`** declares a path that `execute()` may `read_file()` without writing it.
- **Entry point.** `set_worm_entrypoint(vm)`, exactly once.
- **`description`** (class attribute, default `""`): what teams read about the round. The round timer in the dashboard's header opens `/round/`, which shows it while the round is the next or the current one; staff see it on the round's page.
  - Plain text, a blank line between paragraphs; a triple-quoted string's indentation is removed. At most 20,000 characters.
  - It belongs to the version ([Versions](#versions)): the orchestrator records it with the version's bytes, and the dashboard reads only the description. A round shows the description of the version it runs: the pinned one once its first attempt starts, otherwise its file's current one. A file that does not describe a sandbox has none.
  - It is published as written, before the round: no run's secrets (validation refuses them), and nothing that gives away more than the round should. `""` shows the round's times only.
- **Limits.**
  - 32 VMs, 30 networks, 8 NICs per VM.
  - 1–4 cores, 256–8192 MB, 5–64 GB.
  - 64 KiB of text per file, also inside a document or archive; a rendered file at most 4 MiB.
  - Duration 60 s – 7 days.

A chain of them, from [`gameplans/credential_chain.py`](../gameplans/credential_chain.py):

```python
archivist = self.generate_ssh_pwd_login("archivist")
self.add_telnet_login(self.files, archivist)                      # sandbox-files: telnet
ops_key, ops_totp = self.generate_ssh_key("id_ops"), self.generate_totp("ops@sandbox-vault")
self.add_ssh_totp_login(self.vault, ops_key, ops_totp, user="ops")  # sandbox-vault: key + code
vault_pw, zip_pw = self.generate_password(), self.generate_password(length=12)

self.entry.write_file("~/handover.doc", Doc(["Logins are in passwords.kdbx.", f"Master password: {vault_pw}"]))
self.entry.write_file("~/passwords.kdbx", KeePass(vault_pw, [Entry("file server", archivist.user, archivist.pwd)]))
self.files.write_file("~/accounts.ods", Ods([["backup.zip", zip_pw]]), owner="archivist")
self.files.write_file("~/backup.zip", Zip({"ops/id_ops": ops_key.private,
                                          "ops/aegis-export-plain.json": TotpExport(ops_totp)}, password=zip_pw),
                      owner="archivist")
```

### `execute()`: the round

| Call | Does |
|---|---|
| `deploy_worm()` | Exactly once. Deploys the team's upload to the entry VM; the run's clock starts 15 s later |
| `wait(seconds)` | Sleeps. At the end of the run, `execute()` is stopped |
| `remove_flag(vm)` → `Flag` | Deletes the VM's flag; a later submission of it no longer counts |
| `add_flag(flag_or_vm)` | A new flag on a VM that has none |
| `write_file(handle)` | Writes a file declared with `at_reset=False` |
| `vm.read_file(path)` → `FileState` | Whether the file is there (`exists`), its `size` and `sha256`, never its content (that would reach the database). Compare two reads to see a change. After the worm, a VM that cannot be read gives `error`. Readable: the files written with `write_file`, and paths declared in `__init__` with `vm.watch_file(path)`. The job names the file by number, so no path reaches the database |
| `vm.start()`, `stop()`, `shutdown(timeout=60)`, `suspend()`, `resume()`, `status()` | Proxmox power, awaited |
| `with FreezeState(vms) as fs: fs.unfreeze(vm)` | Suspends the running VMs, resumes them on exit. A VM the worm powered off is left as it is, with a note: not running when the block begins, powering off as its suspend comes, or stopped by the gameplan in the block (`unfreeze` of it does nothing). A suspended VM found stopped that the gameplan did not stop was stopped by the host: the attempt fails. Only `unfreeze` of a VM not given to it is a `GameplanError` |
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

### Simulation

**Staff → Simulations** shows how a gameplan plays out before a round uses it: its attempt, replayed on the sandbox it describes in the round player.

- **The attempt.** [`gameplan/simulation.py`](../src/gameplan/simulation.py) runs it as the conductor would: prepare (every VM gets its flag, the files with `at_reset` are written), check, `execute()` under the Runner's rules, the rest of the run's time, every VM stopped, the drain, the teardown.
- **The clock is pretend.** `wait(600)` moves it by 600 s and is recorded. Every other step takes a nominal time (`TIMING`: a host job 3–5 s, start 5 s, shutdown up to 10 s, prepare 60 s), plus the conductor's fuse and drain.
- **There is no worm.** Nothing is captured, and `read_file` sees a file change only when the gameplan writes it. What a real round would refuse (resuming a running VM, a second `deploy_worm()`) fails the simulation the same way.
- **Where it runs.** On the orchestrator, which holds the gameplans ([`simulations.py`](../projects/dashboard/orchestrator/rounds/simulations.py)), one at a time, in a child process limited to 20 s, 10 s of CPU and 1 GiB. The dashboard asks through the orchestrator API (`/gameplans/<file>/simulation/?duration=`, [`api.py`](../projects/dashboard/orchestrator/api.py)).
- **Kept.** One document per version of the file (SHA-256), duration per team and `SIMULATION_VERSION`, in `gameplan_simulations`; up to 20 per file. A changed file is simulated again on the next request.
- **What it shows.** The VMs, networks, addresses and each VM's firewall rules, and the timeline: the run's states, power, flags, the worm's deployment, waits, and files written and read **by number**. Files are numbered in the order `__init__` writes them, then the `watch_file` paths. The sandbox is drawn as a tree from the entry VM down, one row per hop (VMs and networks alternate), siblings in name order: no two cards overlap, and only an edge that closes a cycle can cross another ([`topology.js`](../projects/dashboard/dashboard/static/dashboard/js/topology.js), `sandboxLayout`). The round player draws a run's sandbox the same way.
- **What stays on the orchestrator.** Paths, file contents, the gameplan's log lines and why a simulation failed: `journalctl -u orchestrator` lists them when a simulation is made.
- **Outcomes.** `ok`; `cut` (`execute()` is still going when the run ends); `failed` (the attempt would fail); `invalid` (the gameplan does not load).

## Running a round

1. **Create the round** on **Staff → Rounds**: its window (the start is the upload deadline; teams' latest Live uploads before it are used) and its duration per team. The gameplan is optional here. Tick **Shut down every devbox while its attempts run** when the host needs the memory ([Devboxes during a round](#devboxes-during-a-round)).
2. **Commit and deploy** the gameplan: `worms apply gameplans`, outside a round. It copies `gameplans/` and the framework (`src/gameplan`) to the host, then to the orchestrator, and restarts the conductor, which checks them at once. Nothing else is configured and no package installed, so it needs no internet and takes seconds. The round agent reads gameplan files fresh for every job, and restarts itself, between jobs, when the framework or its own code changed on disk ([`agent.py`](../scripts/round_agent/agent.py), `LoadedCode`).
3. **Check it is valid, and the version.** **Staff → Rounds** marks it valid. The gameplan choice, the rounds list, the round's page and **Staff → Simulations** name each gameplan's version by the first 12 hex digits of its SHA-256: compare them with `sha256sum gameplans/<file> | cut -c1-12` in your checkout. A file missing from the choice is not valid. If not, read `journalctl -u orchestrator-conduct-rounds` on the orchestrator and the `gameplans` job log on the host. **Staff → Simulations** (or **Simulate** on the round's page) replays its attempt at the round's duration per team ([Simulation](#simulation)).
4. **Choose it** on the round's page. That works until the round's first attempt starts: a round without a valid gameplan waits alone, and the conductor looks again every minute ([`conductor.py`](../projects/dashboard/orchestrator/rounds/conductor.py), `_pin`). Then the gameplan's version is pinned for every attempt.
5. **Watch it** (next section). `worms status` shows the active run. On the host, `tail -F /var/log/worms/jobs/latest.log`.

### Devboxes during a round

A round runs with every devbox shut down, for the host's memory, unless staff turn it off: the switch **Shut down every devbox while its attempts run** (`rounds.devboxes_off`, on by default since migration `0023`, which also set it on every round that had not begun).
- **Until the round's start** staff can set it, on creating the round or on its page. After the start it is no longer on the form.
- **When the round begins** (its gameplan pinned, [`conductor.py`](../projects/dashboard/orchestrator/rounds/conductor.py), `_pin`), the conductor records it once, in `rounds.devbox_hold`: `no`, or `wanted`. From then on only the record counts, whatever the switch says. The round's page shows it.
- **Before each of its attempts** the conductor asks the round agent (`hold_devboxes`): every devbox VM and gateway that runs (the Proxmox pool `devbox`) is shut down, each one recorded on the host first (`/var/lib/worms/held-devboxes.json`); the round is `held`. If that fails, the attempt does not start: tried again after 60 s, and after 5 failures (with the preflight's) the round goes on hold. While an attempt runs, a devbox found running is shut down again.
- **Meanwhile** the agent takes no `devbox_*` job (resets, key updates, dev uploads wait in the queue) and dev uploads wait to start.
- **Once no round that holds them has a run to come or under way** (after the last run's teardown), the agent starts again exactly the VMs it recorded (`release_devboxes`) and the round is `released`. A failed release (the internet window open, ...) is tried again every minute; a next round that holds them too keeps them off until it is over.
- **By hand**, if the conductor is gone: `qm start` the VMs listed in that file on the host, then delete the file (the agent takes devbox jobs again).

### Following a round

**Staff → Rounds** shows the round under way, or else the one that waits or is about to begin. Each round's page shows its own. The panel refreshes every 30 s. It is read from the runs, their events, host jobs and captures ([`round_progress.py`](../projects/dashboard/dashboard/views/round_progress.py)); it writes nothing and never reads the gameplan.

- **Phase.** scheduled, planning, waiting for a gameplan, gameplan not valid, queued (behind the round under way), starting, running or done.
- **Summary.** Teams done of all, with a progress bar; the flags counted (finished attempts only); restarts; skipped teams; and an estimate of when the last team is done, flagged when that is after the round's end.
- **Timeline.** One time axis from the round's start:
  - every attempt as a block: finished, failed (run again) or under way;
  - the teams to come as dashed blocks (an estimate, one slot each);
  - the wait before the first attempt;
  - the skipped teams, the round's end and now.
  Hovering over a block shows its team, attempt, times, flags or error.
- **Teams.** One card per team in running order: finished with its flags, skipped, under way, next or to come, its restarts and why each failed attempt failed.
- **The attempt under way.**
  - Its steps (build, checks, gameplan, drain, teardown, done) with the time it reached each.
  - The worm's run time as a progress bar, and the flags so far (they count only if the attempt finishes).
  - The host job it waits for.
  - Its own timeline, and a log of what it did, timed from the worm's start: states, flags removed and placed, power, captures, files by number, skipped steps and errors.

Every staff page lists the rounds not over whose gameplan is missing or not valid. A scheduled round gets a warning; one that already waits gets an error, with the time of the conductor's last look.

### Telling the teams

**Staff → Notifications** sends the teams a message under the bell in their header, next to
Upload and Progression ([`staff_notifications.py`](../projects/dashboard/dashboard/views/staff_notifications.py)):
a severity (info, warning, urgent, error), the text, every team or the teams chosen, and whether it
is shown now. Staff edit, hide and delete them there; each change is recorded in
`competition_events` (`notification_*`). The bell counts a team's active notifications, in the
colour of the most severe.

## Known gaps

As of 2026-11-08. The gameplan rework is uncommitted and not yet deployed.

- **Testing.** Rounds with `round_one` ran on worms-dev from 2026-11-12. The preflight, the in-VM check and the watch (2026-11-22) are tested against a fake Proxmox and a fake round agent ([`tests/test_rounds.py`](../tests/test_rounds.py), [`tests/test_round_agent.py`](../tests/test_round_agent.py)), and the host side read-only on worms-dev; no round has run with them yet.
- **A QEMU that aborts** (SIGABRT, an assertion) reads as the guest's own power-off: the host has no `systemd-coredump`, so nothing records it. An OOM kill, a segfault and a Proxmox task are told apart.
- **Memory.** The preflight counts the sandbox at its ceilings; the service and devbox VMs that run can still grow towards theirs (a warning). If the host still runs out, the OOM killer takes a team VM and that attempt runs again.
- **Shared services.** The service VMs are rolled back only when a sandbox is built, i.e. the first attempt of a round. What one team's worm does to them stays for later attempts.
- **Operations.**
  - Nothing clears a failed-teardown hold.
  - The round agent restarts itself between jobs when its code changed on disk; gameplan files are read fresh. An agent started before 2026-11-19 does not, and needs `systemctl restart worms-round-agent` once.
