# 3. manage.py: the `worms` CLI and Django's management commands

There are two `manage.py` files:

| File | What it is | Where it runs |
|---|---|---|
| [`src/manage.py`](../src/manage.py) | The `worms` CLI: builds, deploys and operates the platform | the controller |
| [`projects/dashboard/manage.py`](../projects/dashboard/manage.py) | Django's: its commands run as services on the orchestrator, or behind `worms` subcommands | the orchestrator VM |

## The `worms` CLI

```sh
worms() { /root/app/.venv-test/bin/python /root/app/src/manage.py "$@"; }
worms-gui() { /root/app/.venv-test/bin/python /root/app/src/worms_gui.py "$@"; }   # the dashboard below
eval "$(worms completion bash)"   # tab completion; or once: worms completion bash > /etc/bash_completion.d/worms
worms --help              # the commands, grouped by purpose
worms apply --help        # the components, what each needs
```

<a id="tab-completion"></a>**Tab completion** ([`completion.py`](../src/worms_cli/completion.py)). `worms completion bash` prints a bash completion script, written by hand (no `argcomplete`: production has no internet). What does not change with the platform is in the script, taken from the CLI's own tables when it is generated: the commands, the nouns' verbs and every flag (the argparse parsers), the topics of `show`, the components and groups of `apply` and `vm rebuild`, the steps, the services of `log` and `service restart`. Generate it again after an update. What changes, the VM names and the `<team>/<role>` targets of `run`, `login`, `show`, `log`, `rsync` and `vm start|stop|restart`, and the teams of `check`, `trace`, `vm reset`, `vm rebuild`, `key add`, `key remove` and `team delete`, comes from the hidden `worms __complete <kind>` (`targets`, `teams`). It answers before the CLI's own imports and reads local files only, no Proxmox API, SSH or database: the generated inventories (this checkout's, and on the host the round agent's workspace's) and `~/.worms/team-map.json`, which the team map writes (0600) whenever it was read from the database. About 0.15 s a TAB on the host. After `--` the shell's own file completion applies. Only words of `A-Za-z0-9._+-` (two around a `/` for a target) are offered: by `__complete`, in `team-map.json`, and again by the script, which matches them as plain strings and never hands one to `compgen -W` (it expands its words). A team names itself, and a name like `$(...)` must not run on a TAB; a team whose name is no such word is offered by its slug.

**Modes.** `WORMS_SSH` chooses the mode ([`session.py`](../src/worms_cli/session.py)).

| `WORMS_SSH` | Mode | What runs where |
|---|---|---|
| empty | on the host | Everything runs locally. |
| `root@<host>` | laptop | OpenTofu and Ansible run locally. Ansible reaches the VMs through the host. |

In both modes:
- **The host's own stacks** (sandbox, devbox) and playbooks always run in the round agent's workspace `/opt/worms`, through `/usr/local/sbin/worms-host-run`. That workspace is synced from the checkout first: `SetupRoundAgent.yml --tags agent-sync`.
- **What gets deployed** is the controller's working tree, uncommitted edits included.

**Configuration** ([`config.py`](../src/worms_cli/config.py)). Settings are read in this order, first one wins:
1. process environment (`WORMS_*` only)
2. `--config FILE`
3. `~/.worms/config` (`KEY=value` lines)
4. defaults

| Key | Meaning |
|---|---|
| `WORMS_SSH` | The host, as an ssh destination. A non-default port goes in `~/.ssh/config` |
| `WORMS_REPO_ROOT` | The checkout. Default: the one the CLI is in |
| `WORMS_PROXMOX_URL` | Default `https://<WORMS_SSH host>:8006/` |
| `WORMS_DB_*`, `WORMS_PROXMOX_*` | Override the vault-derived connections (development, tests) |
| `NO_COLOR` | No colours (read from the environment only) |

**Conventions**
- The grammar is git's: a few verbs to look (`status`, `show`, `log`, `check`, `trace`) and to reach a VM (`login`, `run`, `rsync`); `apply` and `destroy` for the platform as a whole; and a noun for each kind of thing that changes, with its verbs: `worms vm reset alpha`, `worms jobs pause`, `worms db backup`. A noun alone lists its things: `worms vm`, `worms jobs`, `worms round`, `worms team`, `worms key`, `worms user`, `worms db`, `worms service`, `worms secret` (`worms show <list>` too: `worms show jobs`).
- Every command first prints `internet access: <state>` on stderr.
- Results go to stdout; `error:`/`warning:` go to stderr.
- <a id="json"></a>`--json` (on `status`, `show`, `check`, `trace`): what the command shows, as one JSON document on stdout instead: `{"command", "exit", "sections": [{"heading", "tables", "lines"}]}`, each table a list of rows keyed by its column headers (a table without headers: lists), colours and dots left out ([`output.py`](../src/worms_cli/output.py)). `worms show jobs --json | jq '.sections[0].tables[0][] | select(.State == "queued")'`; `worms trace alpha --json` gives each step as `{state, step, detail}`. `worms check --json` adds a field `checks`, its results as data: per team, check and role (`a`, `b`, `c`, `gw`) each cell's `state` (ok, warn, error, stopped, unknown, n/a), `text` and `reason` (and `from_vm`: the reason holds what the VM, or ssh to it, printed, escaped; not the CLI's own words); a login cell also which of the team's keys the VM takes (by name and fingerprint) and whether it answered the SSH gateway; the team's jobs and upload; the SSH gateway's lines ([`validate.py`](../src/worms_cli/validate.py) has the shape). `worms check --json | jq '.checks.teams[] | {team: .label, flag: .checks.flag}'`.
- Each tool call is echoed as `$ <cmd>  [locally | on root@host | in the agent's workspace]`.
- <a id="lock"></a>**One change at a time.** A command that changes the platform (`apply`, `destroy`, `internet on|off`, `vm start|stop|restart|reset|rebuild`, `service restart`, `db restore|reset`, `key sync`, `step`) holds `/run/worms/worms.lock` on the host while it runs (from a laptop: `flock` on the host over SSH). A second one is refused, saying who holds it: pid, user, command, since when. Never `vm kill`, `jobs ...`, `round ...`: they work while a build runs. Not the round agent's own runs, and not a raw `ansible-playbook` or `tofu` ([`lock.py`](../src/worms_cli/lock.py)).
- A command that changes something keeps its whole output, the tools' too, in a run log: `~/.worms/runs/<YYYYmmdd-HHMMSS>-<command>-<what>.log` on the machine it ran on (directory 0700, files 0600: Ansible's output can hold secrets; the newest 200 are kept, without colours). The log never stops or holds up a command: one that cannot be opened is a warning and the run goes on unlogged; one that cannot be written any more (a full disk) is closed, said once, and the output goes on to the terminal. It says where at its start, and again when it fails. `worms log run [N]` shows the newest (or N-th newest). Not in the round agent's workspace, whose jobs have their own logs; never for a command whose output is a secret (`user add`, `user password`) or a VM's (`run`, `login`) ([`runlog.py`](../src/worms_cli/runlog.py)).
- What the round agent does for a command (a devbox reset or rebuild, a new devbox) is followed live: while a job is queued, why it waits; then its log as it is written; then how it ended. `--detach` queues and returns; Ctrl-C leaves the job running ([`jobs.py`](../src/worms_cli/jobs.py)).

**Exit codes**

| Code | Meaning |
|---|---|
| 0 | success; a check found nothing wrong |
| 1 | failure (build, vault, token, refused confirmation), or a check found a problem |
| 2 | usage error, or no database configured |
| 130 | Ctrl-C (a followed job goes on) |

**Privileges**
- The CLI logs in everywhere with the WORMS key only, held in a private `ssh-agent` per process. Your own keys are never offered: `IdentityFile=none`.
- Database commands connect as the PostgreSQL superuser `admin`. That role may only connect from the host; from a laptop the CLI opens an SSH tunnel.

## Commands

As `worms --help` groups them. Commands marked † ask for confirmation unless given `--yes`; a command run by the round agent never asks.

**Targets.** A VM is named by its name (`dashboard`, `devbox-t2-a`), an alias (`ssh-gateway`: the shared SSH gateway `devbox-ssh-gateway`), or `<team>/<role>`: `alpha/a`, `alpha/b`, `alpha/c`, `alpha/gw` (the team's NAT gateway), with the team's name or its slug `t<id>` (`t2/a`). A team stands for its devbox VMs (`worms vm stop alpha`), and `devboxes` for every team's (`worms vm reset devboxes`). Groups: `infrastructure`, `services`, `private-internet` (platform VMs only). The team map ([`teams.py`](../src/worms_cli/teams.py)) comes from the database (teams, devboxes); when it cannot be reached, from the VMs' names (`devbox-t<id>-a|b|c|gw`) and the inventory's `devbox_team`, and a team is then known by its slug only.

<a id="trace"></a>**A team reports a problem.** Where does it come from?

| Command | Answers |
|---|---|
| `worms check alpha` | Is its devbox set up as it should be, link by link: login through the SSH gateway, DNS, scoring, logging, apt, PyPI, the flag, the firewall on A, B, C and its NAT gateway (and the gateway's nftables); then its jobs (queued: why), and where its newest upload is |
| `worms trace alpha` | Its newest dev uploads on their way to the devbox: picked up, the reset (its job), put on VM A (its job), its hour; what each step waits for and why |
| `worms log alpha` | Its host jobs, the attempt processor's lines about its uploads, what scoring and logging refused from its devbox's address |
| `worms show job <n>`, `worms log <n> -f` | One job: what it is, why it waits, its log |
| `worms login alpha/a` | Then look yourself |

<a id="see"></a>

### Look

| Command | Does |
|---|---|
| `worms status` | First what needs attention: the internet window open; devbox VMs held by a round or stopped by the window (the host's records); service VMs without a `clean` snapshot; migrations of this checkout the database lacks; host jobs failed in the last 24 h; host services (`worms-round-agent`, the host firewall, its watchdog's timer, the mirror) not active; the platform's services on their own VMs (the orchestrator's processor and API, the dashboard, scoring, logging, the database, ...: one SSH command a VM) not active; host jobs marked running that run nowhere (`worms jobs cancel`); a round session that runs, or one that ended and left the orchestrator, the round agent or the devboxes stopped (with the commands that start them); an attempt under way that no session runs (every dev upload waits for it: `worms round teardown`). A service VM's `clean` snapshot is its configuration (every reset and round build returns it to it): **snapshot behind deployed** when the snapshot's commit (its description, `worms <commit>`) is not `deployed.json`'s for its component, **configured after its snapshot** when the VM history has a `configured`/`packages` later than it (the next reset undoes that); both repaired by `worms apply <component>`, which snapshots at its end. It exits 1 when it found something, 0 otherwise (also when it could check nothing: an unconfigured checkout). Each source when it answers, and "not checked" for one that does not. Before all of it, **the version**: `system: on <commit>` when every component's record in `deployed.json` (and the workspace's), every service VM's `clean` snapshot and what the dashboard's and orchestrator's units started with (T8) are this checkout's commit, and the checkout is clean; else `system: mixed`, one line per commit with what is on it (`unknown`: no record), and the checkout's. Then the system, the firewall, the internet window, every VM (kind, team, state, uptime, memory, when it was created and last updated, with the commit: [VM history](#vm-history); a service VM's `clean` snapshot, its commit and age), the competition |
| `worms show <vm>` | One VM: state, resources, disks, networks, management address and login user, OpenTofu address, snapshots, its team, the services that run there (the registry), whether it answers over SSH ([`vms.py`](../src/worms_cli/vms.py)); then its history, newest first ([VM history](#vm-history)) |
| `worms show <team>` | A team (its name or slug `t<id>`): its devbox's VMs `a`, `b`, `c` and `gw` with vmid, management address and state, and its keys on each (as `worms key` reads them), with the shared SSH gateway's lines for it |
| `worms show job <n>` | One host job: kind, team, run, state, its times, parameters, error; while queued, why it waits (the agent paused or not running, a round holding the devboxes: the agent takes no devbox job then, a job running ahead, jobs queued ahead); its log's last lines |
| `worms show table <name> [--schema] [--limit 20]` | A table's newest rows, or its columns, indexes and constraints ([`database.py`](../src/worms_cli/database.py)). `worms db`: the tables |
| `worms show leaderboard` | The leaderboard, with the dashboard's own scoring |
| `worms show gameplans` | Each gameplan as the orchestrator and the host found it (with its current version's SHA-256), and why one is invalid (this checkout's dry run) |
| `worms show audit [--hours 24] [--kind PREFIX] [--limit 200]` | The event tables ([7](07-security.md#audit-trail)) |
| `worms log [<n>\|<name>\|<vm>\|<team>] [-f] [-n N] [--since T]` | Every log from one place ([`logs.py`](../src/worms_cli/logs.py)): a host job's log by its number (`/var/log/worms/jobs/<uuid>.log` on the host), a service's by its name in the service registry ([`services.py`](../src/worms_cli/services.py), `worms service`: `agent`, `firewall`, `processor`, `api`, `topology`, `dashboard`, `scoring`, `logging`, `database`, `monitor`, `audit`), any platform VM's whole journal by its name (`host` too), or a team's (above). `worms log round [<r>] [<team>] [-f]`: a round's sessions (`worms round start`, with how many failures each had), the newest one's `round.log`, or that team's latest attempt's steps; `worms log round [<r>] summary`: the newest session's `summary.log`, what failed, why and in which file the rest is (`~/.worms/rounds/latest/summary.log` is always the newest). `worms log latest [-f]`: the newest host job's log (`-f` moves on to the next job). `worms log run [N] [-f]`: the newest (N-th newest) run log on this machine. `-f` follows. Alone: the run logs, the names and the newest jobs. Not the team VMs' journals |
| `worms check [<team>...]` | Whether each team (default: every team with a devbox) can work on its devbox, checked as the team works and changing nothing ([`validate.py`](../src/worms_cli/validate.py)). A table per team, columns A, B, C and GW (its NAT gateway). **login:** the SSH gateway takes the team's registered keys and forwards to its A; A, B and C take them as root (asked with the public keys alone: no private key, no login). **DNS:** scoring, logging, apt-cache, pypi resolve. **scoring**, **logging:** a `POST /submit` with `{}` answers 400 (reached, the team's gateway recognised, nothing stored). **apt:** a fresh index through the apt cache in a directory of its own, then one package (on the gateway: the cache's index answers). **PyPI:** one package through the VM's `pip.conf`. **flag:** `/etc/worms/flag` there and well-formed (never printed). **firewall:** the VM's Proxmox firewall on, DROP in and out, `ipfilter`, its security groups. **NAT** (gateway): nftables loaded, forwarding on. Under the table: the team's queued, running and (24 h) failed jobs, a queued one with why it waits, and where its newest dev upload is. Then, for the SSH gateway: whether OpenSSH's per-source penalties can shut out the one address every team comes from. About a minute; exit 1 on a problem |
| `worms check packages` | Whether the caches' package lists can be filled ([`caches.py`](../src/worms_cli/caches.py)). **apt:** offline, on the apt-cache VM: `apt-get install -s` of the whole list against the index the cache holds (the one its last fill kept), nothing installed: every unknown name and every conflict, as apt says them, and how many packages it resolves to. **pypi:** each name on PyPI, with internet here (else: not checked, said so). The lists are the roles' defaults (`apt_cache_seed_packages`, `devpi_seed_packages`): edit them in this checkout |
| `worms check tokens` | Asks Proxmox what each token may do: `ok`, `broader than`, `missing from`, `invalid`, `not set` |
| `worms trace <team>\|upload <n>` | A dev upload's way to the devbox, link by link ([`trace.py`](../src/worms_cli/trace.py)): picked up by the orchestrator (its devbox run), the reset (its `devbox_rebuild` job), put on VM A (its `devbox_deploy` job: the zip unpacked in `/home/debian/worm`), its hour. A link that waits says why: what the orchestrator waits for before the reset (a round's attempt under way, a round holding the devboxes: [`devbox_runs.py`](../projects/dashboard/orchestrator/rounds/devbox_runs.py), `reset_waits_for`, which the processor logs once and the dashboard shows the team), and for a queued job why the agent has not taken it. A team's newest three uploads |

### Reach

| Command | Does |
|---|---|
| `worms login <vm>` | A shell over the management SSH, as `debian` (`sudo -i` for root). `worms login --help` lists the VMs there are now, with their team. The agent is never forwarded. `worms login <vm> -- <command>` runs that command there instead, as `worms run <vm> -- <command>`. On a service VM (`login`, `run`, `rsync` onto it) one line says that changes made there are lost at the next reset or round build: make them through Ansible |
| `worms run <target>... [--root] -- <command>` | A command on VMs without a login ([`remote.py`](../src/worms_cli/remote.py)): `host`, a VM, or a group (platform VMs only; a team's VM runs a command only when it is named itself). After `--`, several words are the command and its arguments, each arriving as typed (quoted for the VM's shell); one word is a command line for the VM's shell (`worms run dns -- 'journalctl -n 50 \| grep named'`). `--root` runs it with `sudo -n sh -c`. Input: what is piped in reaches one VM (`worms run alpha/a -- bash -s < script.sh`); a terminal's only a platform VM (what is typed could be a secret); several VMs never. Never interactive, the WORMS key never forwarded. With several VMs each line gets a `[<vm>]` prefix and a summary follows; what a devbox or sandbox VM prints is escaped. A stopped VM is not started. The exit code is the worst of them |
| `worms rsync <source>... <target> [-n] [--delete] [--preview] [-- <rsync options>]` | `rsync -avz` between here and a VM, in rsync's own syntax with a VM's login name for the host: `worms rsync team-1-backup/ devbox-t1-a:` up, `worms rsync devbox-t2-a: team-2-backup/` down; a team's VM also as `alpha/a:`. A ":" with no "/" before it names a VM (`./a:b` is a local file), a later source `:path` is on the first source's VM, a trailing "/" copies a directory's contents, `<vm>:` alone is the home. Over the same SSH as `worms login`; the VM's rsync runs as `<user>@` (sudo) from that user's home: root's on a devbox and on the host, `debian`'s elsewhere. `--preview`: rsync's counts, nothing sent. With a devbox or sandbox VM (assumed rooted) this end is guarded: no setuid bits, owner or device files, inert symbolic links, escaped output ([`rsync.py`](../src/worms_cli/rsync.py)) |

<a id="build"></a>

### Deploy

| Command | Does |
|---|---|
| `worms apply [--plan] [--packages]` † | The platform up to this checkout: only what changed since what runs ([below](#worms-apply)) |
| `worms apply <component>... [--plan] [--packages] [-- <ansible args>]` † | These components (`worms apply --help` lists them, and the groups): configured when they exist, built when they do not, with what they need first ([below](#worms-apply)). `worms apply devbox`: a devbox for every team without one (a `devbox_provision` job for the round agent, followed) |
| `worms apply --all` † | Everything, end to end, packages too: a new host (= `make`, [below](#worms-apply)) |
| `worms destroy` † | Destroys the sandbox, devbox, infrastructure and private-internet stacks (the database's records with them) |
| `worms destroy --all` † | Stops, unlocks and destroys **every VM on the host**, including VMs WORMS did not create; then the firewall and platform stacks |
| `worms internet [on\|off]` | The internet window by hand; alone, whether it is open. `on` † is refused during a round (a sandbox VM exists): every devbox VM that is not stopped is shut down first, after asking, forced after 120 s, and written down on the host (`/var/lib/worms/window-stopped-devboxes.json`). `off` closes it, switches DNS and the caches back to offline mode, and starts those devboxes again; not while a sandbox VM exists or a round holds the devboxes: they stay off and written down, and it says so. `apply` opens and closes the window itself when it needs it |

`make`, `make destroy` and `make destroy_all` call `worms apply --all`, `worms destroy --yes` and `worms destroy --all --yes` ([`Makefile`](../Makefile)).

### Machines

The lifecycle of a VM: built by `worms apply` (a team's devbox: `worms apply devbox`), configured again by `worms apply`, on and off with `worms vm start`, `stop` and `restart`, back to its clean snapshot with `worms vm reset`, from nothing with `worms vm rebuild`, gone with `worms destroy`.

| Command | Does |
|---|---|
| `worms vm` | Every VM, as `worms status` lists them |
| `worms vm start\|stop\|restart <target>...` | `qm start`, or `qm shutdown` forced after 120 s, or both, on the host ([`power.py`](../src/worms_cli/power.py)): VMs, groups, a team (its four devbox VMs), `devboxes` (every team's). No team VM starts while the internet window is open; a devbox held by a round is left to its release; sandbox VMs are the round agent's. `worms vm start --held`: the devbox VMs a round holds (`/var/lib/worms/held-devboxes.json`), once no sandbox VM exists |
| `worms vm reset <team>...\|devboxes [--detach]` † | Each team's devbox back to its clean snapshot, through the orchestrator as the dashboard's reset is: a rollback, then the internal CA, the team's keys and new tokens over SSH; its version goes up, its dev score starts over. Follows the jobs live (Conventions); `--detach` queues only |
| `worms vm reset services` † | Rolls the 11 service VMs back to `clean` (refused while a sandbox exists); then pushes the host's devbox map onto scoring and logging again, itself (no host job: the round agent may be stopped) |
| `worms vm rebuild <team>...\|devboxes [--detach]` † | Their devbox VMs destroyed and built again from nothing, fresh disks, through the orchestrator; followed live |
| `worms vm rebuild <vm>... [--packages]` † | Platform VMs (or a group of them) destroyed and built again, in the internet window, as `worms apply` builds them |
| `worms vm kill` † | `qm stop` on every running VM, database and dashboard included: the emergency stop. Destroys nothing; never waits for the lock |

### Operate

The round agent's queue, the rounds' schedule ([`control.py`](../src/worms_cli/control.py)), the services. `worms jobs` and `worms round` give their numbers.

| Command | Does |
|---|---|
| `worms jobs [--limit 30]` | The round agent's latest host jobs, and why one failed |
| `worms jobs pause [--reason TEXT]` | The round agent finishes the job under way and takes no other: a file on the host (`/var/lib/worms/agent-paused`, its text who and why) it looks at before each job, and says so once in its log. A queued job then says so (`worms show job`). Rounds' jobs too: pausing during a round stops it between steps |
| `worms jobs resume` | The agent takes jobs again |
| `worms jobs cancel <n>...`, `worms jobs cancel --team <t>` | Queued jobs, never run: `failed`, "cancelled by the operator". A running one is refused: it is a process on the host, half-way through something (`jobs pause`, then it ends); unless it runs nowhere any more, and is marked failed, saying why: the `worms round start` that ran it has ended (its pid, kept in the job while it runs, is gone), the round agent is stopped and no session runs it, or its heartbeat stopped over 5 minutes ago. A cancelled devbox reset fails its upload, saying why |
| `worms round` | The rounds (id first, ON HOLD when paused): window, gameplan and its check, attempts by state |
| `worms round start <r> [--resume \| --team <t>]... [--yes]` | Runs the round here, on the host, team by team, in this terminal ([`rounds.py`](../src/worms_cli/rounds.py), [11](11-orchestrator.md#a-round-worms-round-start)). Before its start it asks to move it (the upload deadline) to now. It asks, one by one, with the command that does the same and the one that undoes it: stop the orchestrator, stop the round agent and cancel its queued jobs, stop every devbox VM and gateway; checks each, runs the host's preflight and asks once more. A failed attempt is written down and the round goes on; nothing is retried by itself. At the end: run the failed teams again?, publish? (once every team finished without an error), destroy the sandbox?, start the devboxes, the agent and the orchestrator again?. Everything goes to `~/.worms/rounds/<round>-<time>/`, what failed, why and the file with the rest to its `summary.log` (`worms log round <r> summary`); each step's log is named when the step starts and when it ends. A round that ran before needs `--resume` (every team that has not finished) or `--team`. The round's sandbox is built once, before its first attempt (`build`, then `snapshot`), and rolled back to its snapshot before every attempt but the first after it; at the end (every team over without an error) its snapshot deleted and the sandbox destroyed, asked. A failed build is kept (asked), and `--resume` goes on with it (asked) after OpenTofu's apply instead of building anew; a failed rollback or prepare stops the round, the sandbox and its snapshot kept. Ctrl-C fails the attempt under way; twice, out at once. It holds the host's lock to its end. On the host only, as root; a `WORMS_SSH` there (laptop mode, in `~/.worms/config` or the environment) is set aside for it |
| `worms round publish <r> [--anyway] [--yes]` | Its results to the teams and the leaderboard (`rounds.results_published_at`): refused while a team's latest attempt is not finished without an error, unless `--anyway` (asked) |
| `worms round unpublish <r> [--yes]` | Its results hidden from the teams again |
| `worms round teardown [--yes]` | The round's sandbox destroyed (asked): one a session kept (powered off) for another attempt, an unfinished build, or what OpenTofu still holds after its VMs were deleted by hand. The next session builds it again. First, an attempt a killed session left under way is closed (asked): until then every dev upload waits for it; and scoring's and logging's current team is cleared. A destroy leaves nothing: the sandbox's definition (`generated_vms.tf`) empty, so that a `tofu apply` there builds nothing, the runs' secrets (`/var/lib/worms/runs/`) and the reset record gone. Last, each VM of the sandbox's pool that no OpenTofu state holds (an interrupted build's) is named and destroyed if you say so (`qm stop`, `qm destroy --purge`) |
| `worms service restart <name>... [--timeout 900] [--during-round]` | Restarts services by their names, in a safe order: `agent`, `processor`, `api`, `topology`, `dashboard`, `scoring`, `logging`, `monitor`; then checks they are active. `agent` waits until no host job runs (systemd would kill a devbox rebuild at its stop timeout; a job whose heartbeat stopped does not count) and refuses after `--timeout` seconds. `agent`, `processor`, `scoring` and `logging` are refused while a sandbox VM exists, unless `--during-round`. `firewall`, `database` and `audit` are not restarted from here (it says what instead) |
| `worms service stop\|start <name>...` | Stops or starts services (or `orchestrator`: `api`, `processor`, `topology`), then checks them. `stop agent` pauses the agent first, waits for its job under way, stops it, and lifts the pause |

### Data and accounts

Teams, students and rounds are managed on the dashboard (/staff/); these are the console's part.

| Command | Does |
|---|---|
| `worms db` | The database's tables (rows, size); a table's rows: `worms show table <name>` |
| `worms db backup [-o FILE]` | `pg_dump -Fc` of the platform's database on the database VM, streamed over its management SSH into a new local file, mode 0600, in `~/.worms/backups/` (0700) by default; prints the path. Nothing is written on the VM; a failed dump leaves no file |
| `worms db restore <dump>` † | DANGER: the database back to a dump of `worms db backup`, in one transaction (`pg_restore --clean`); the dashboard, the orchestrator's services and the round agent are stopped meanwhile and started again. Everything written since the dump is lost; the grants are the dump's (this checkout's: `worms apply database`). Refused during a round |
| `worms db reset` † | DANGER: every table emptied but the event tables and the gameplan versions, then initialised again (`reset_database`) |
| `worms db dev-reset` | DEV, for worms-dev: `worms db reset`, then `admin` (password `admin`), `alpha@example.org` (`alpha`) in team TeamA and `beta@example.org` (`beta`) in team TeamB, none asked to change its password. Always asked, never `--yes`: on another system first whether to run a development command there [y/N], then whether to wipe [y/N]. Then for each of alpha and beta: an upload zip? [Y/n] and its path; checked as the dashboard checks an upload (`main.sh` at its top level, at most 100 MiB), stored in the dashboard's media folder and recorded as the team's latest upload, its entry for the next round. The teams have no devbox yet: `worms apply devbox` (`src/worms_cli/dev_reset.py`) |
| `worms team` | The teams, their members and who has signed in; every team's devbox (its slug `t<id>` and VM names) and SSH keys |
| `worms team delete <name>` † | A team and everything it owns, by database cascade; its users stay. Its VMs are not touched |
| `worms key` | Whether every team's devbox SSH keys are in place: for each team, the SSH gateway and its devbox VMs A, B and C (✓, or how many keys are missing or old there; `off` for a stopped VM), read over the management SSH and compared by fingerprint with the dashboard's; then the last key push job and one verdict. Exit 1 if a team's keys are not in place ([`keys.py`](../src/worms_cli/keys.py)) |
| `worms key add <team> <key\|file\|->` | Adds an SSH key to a team's devbox with the dashboard's checks ([`ssh_keys.py`](../projects/dashboard/core/ssh_keys.py)), then copies the keys as `worms key sync` does |
| `worms key remove <team> <name\|fingerprint>` † | Removes one, copied the same way |
| `worms key sync` | Copies every team's SSH keys from the database over SSH: its key file on its devbox VMs A, B and C, its lines on the SSH gateway; stopped VMs are skipped. Then `worms key`. No round agent, no internet |
| `worms user` | Every user: team, whether they have signed in, a one-time password still to change |
| `worms user add <email>` | A staff account with a one-time password, printed once. The dashboard cannot create staff |
| `worms user password [<user>]` | A 20-character one-time password for the admin (or `<user>`, an admin account), printed once. Creates the admin if missing |

### Set up

| Command | Does |
|---|---|
| `worms init` | Once per controller, with your own login: creates the vault and every missing generated secret (their states, never values), then lets the WORMS key in for root on the host and on VMs older than it. Then `worms secret issue` the three Proxmox tokens |
| `worms secret` | What the vault holds: every secret's state, never a value (creates the missing generated ones) |
| `worms secret set <name>` | Stores a token, or `ssh-key` (`worms secret set ssh-key < key`), by hand |
| `worms secret issue <token>` | Creates or **rotates** `proxmox-token`, `orchestrator-token` or `topology-token` with `pveum` on the host, and stores it in the vault. Consumers get the new value at the next `worms apply` |
| `worms completion bash` | The tab completion script (above) |
| `worms test [-- <pytest args>]` | The offline suite ([Testing](#testing)) |
| `worms test --live [-- <ansible args>]` | The end-to-end simulation on the deployed platform |

`worms step <name> [-- <ansible args>]` is the round agent's, not in the help: one pipeline step ([`pipeline.py`](../src/worms_cli/pipeline.py), `STEPS`): `init`, `inventory`, `show-config`, `ping`, `offline`, `snapshot-services`, `devbox-map` (the host's devbox map pushed onto scoring and logging again: what `worms status` names when theirs differs), and the round agent's `round-configure-sandbox`, `devbox-apply`, `devbox-configure`, `devbox-keys`. The last two are refused without the agent's `-e @<varfile>`: the roles' defaults would write every team's key files empty (`worms key sync` writes the keys; `worms vm reset <team>` or `worms apply devbox` a devbox). `worms step debug HOST`: why Ansible cannot reach a host (address, TCP 22, SSH, Python, sudo, cloud-init).

<a id="worms-build"></a>

## `worms apply`

Sources: [`components.py`](../src/worms_cli/components.py) (what a component is and needs), [`changes.py`](../src/worms_cli/changes.py) (what changed), [`pipeline.py`](../src/worms_cli/pipeline.py) (the steps).

**Components.** One per service; `worms apply --help` lists them in build order, with what each needs:

| Kind | Components | Built by | "Exists" when |
|---|---|---|---|
| a stack | `platform`, `firewall` | `tofu apply` of its stack | its OpenTofu state has resources |
| a host play | `host-network`, `round-agent` | its playbook on the host | its service is active (`worms-host-firewall`, `worms-round-agent`) |
| a VM | `dns`, `syslog`, `database`, `orchestrator`, `dashboard`, `scoring`, `logging`, `monitor`, `apt-cache`, `devpi`, `docker-registry`, `manpages`, `ntp`, `cve-search`, `wikipedia` | `tofu apply -target=<its module>`, then `Site.yml --limit <its group>` (below) | Proxmox has a VM of that name |
| devboxes | `devbox` | the round agent (a host job) | — |
| gameplans | `gameplans` | `SetupRoundAgent.yml --tags agent-sync` (the host's copy), then `Site.yml --limit orchestrator --tags gameplans` (the orchestrator's); then the host's copy checked (the dashboard's list). No packages, no internet; refused during a round | — |

Groups: `infrastructure` (the platform's own VMs), `private-internet`, `services` (the VMs a reset returns to their snapshot).

**`worms apply` alone: what changed since what runs.**
- **What runs:** per component, the commit it was last brought up to: the host's record `/var/lib/worms/deployed.json` alone (written after every successful apply of it; `workspace` by every sync of the round agent's workspace), else unknown. The VM history is an audit log, shown, never a record of what runs (until 2026-11-26 a VM's last configuration stood in when the record lacked it). A commit with `-dirty` had uncommitted changes nobody can see now; such a component, and one whose commit is unknown, is configured again.
- **What changed:** `git diff` from that commit to this checkout's working tree (an untracked file counts once it is newer than the record).
- **What it touches:** the files each component is made from, read from the repository (the reading the tests check against every host): the roles `Site.yml` gives its group with what those include and depend on, its groups' group_vars, the inventory, and what a role is made from besides its directory ([`roles/sources.yml`](../ansible/roles/sources.yml): `django_app`'s `projects/dashboard/` and the gameplans); the round agent's role and code, and its workspace (a copy of `ansible/`, `opentofu/`, `scripts/`, `src/`, `gameplans/`); the gameplans. Not deployed: the CLI, `docs/`, `tests/`; a changed file no component is made from is said.
- **Packages:** a component's package names (apt, pip, and every list named `*packages*`, the caches' seed lists too) at both commits: different needs `--packages` (the window), else it is refused before anything.
- **OpenTofu:** each stack's own plan, saved; a plan of in-place changes and new resources that are no VM is applied as shown, one that replaces, destroys or makes a VM anew is not (it says which: `worms vm rebuild <vm>` or `worms apply <stack>` does it, by name).
- **The devboxes** are never touched: they run what their teams made of them. A rebuild gives them the change (`worms vm rebuild devboxes`); a reset only rolls them back to their snapshot.
- Then, once you agree: the restore point (below), OpenTofu's saved plans, the components configured.

**`worms apply <component>...`** (`worms apply dashboard`):
1. **The plan.** Each component requested: *create* (missing), *configure again* (exists), *configure again, in the window* (exists, unfinished: below), or *recreate* (`worms vm rebuild`). Each missing dependency: *create*. Each line says what it touches (the OpenTofu stack and its `-target`/`-replace`, or `Site.yml` and its group); under the table, what the whole run touches: `Site.yml` and its `--limit`, whether the internet window opens, whether the devboxes are shut down, which service VMs are rolled back and snapshotted again, what restarts. `--plan` stops here, after what each VM of it runs now (the commit of its last configuration) against this checkout, and OpenTofu's own plan of the stacks and VMs concerned (the devbox and sandbox stacks are the round agent's).
2. **Asks** †, when the plan builds a dependency nobody asked for or recreates something (`worms vm rebuild database` loses its records). `platform` and `firewall` are never rebuilt alone: everything stands on them. Refused while a sandbox VM exists, unless `--during-round` (it restarts the round agent and the orchestrator).
3. **Builds:**
   - A VM that is created or recreated needs packages, so the internet window opens and the devboxes are shut down meanwhile. So does an *unfinished* one: it exists, but the build that created it stopped before its configuration's end ([VM history](#vm-history): no `Site.yml` after its creation; before 2026-11-21, `SetupAudit.yml`), for example a new apt-cache VM whose "Install apt-cacher-ng" had no internet. Configured offline, its apt would wait on the closed firewall. A VM that exists otherwise is configured without packages (`--skip-tags packages`) and without the window, unless `--packages`. The plan says *configure again, in the window* and names it after "internet window: OPEN".
   - A fill of the caches (`worms apply apt-cache devpi --packages`) checks their lists first, as `worms check packages`: a bad name fails before the window opens, not mid-window with the devboxes down.
   - Service VMs are rolled back to `clean` first and snapshotted again at the end, with the devboxes off meanwhile (as `worms apply --all` does).
   - **All or nothing.** When a step between the rollback and the new snapshot fails or is interrupted (Ctrl-C), the VMs it rolled back go back to `clean` once more before the window closes (`rolled_back`, "clean: apply failed", in the VM history), and it says so: "… are back at their snapshot (worms <commit>): fix the cause and run the same apply again". A service VM is never left half-applied while its snapshot and `deployed.json` say otherwise. One the build created has no snapshot to go back to: it is named and left.
   - Only the orchestrator migrates and the database VM grants: configuring the orchestrator includes the database's grants; while the database lacks a migration of the checkout, building the dashboard or the database brings the orchestrator (and the database) into the plan ([`build.py`](../src/worms_cli/build.py), `pending_migrations`).
   - The orchestrator and the round agent bring the gameplans along (unless a round is on), and are followed by a restart of the round agent once no host job runs, then a check of the host's gameplans.

<a id="site"></a>**How the configuration runs** ([`Site.yml`](../ansible/Site.yml)). One playbook says which VM gets which role; the roles are the intents (`nginx_tls`, `django_app`, `ca_distribute`, ...). Four phases, one after the other:

| Phase | Where | What |
|---|---|---|
| 1 | the controller | the CA (every certificate is signed with it) |
| 2 | `dns` | DNS: every other VM resolves through it |
| 3 | every VM, all at once (`strategy: free`) | each VM's roles in order, not waiting for the others: `apt_updates`, `timesync`, `sshd_listen`, `ca_distribute`, its services (`nginx_tls`, `setup_db_base`, `django_app` up to its migrations, `sandbox_api`, `network_monitor`, the private internet's), `syslog_server`, `auditd`, `log_limits` |
| 4 | the VMs concerned, in step | the migrations on the orchestrator, then the dashboard, then the grants on the database |

- **A failure stops the run** before the next phase. Within phase 3 the other VMs finish what they do (Ansible's `any_errors_fatal` does not hold there), then phase 4 starts by stopping the run when any VM failed.
- **Each VM's log is a file of its own**, `~/.worms/runs/<run>/<vm>.log` next to the run log: every task in the order it ran there, with how long it took, and a change or failure with its result as YAML. `worms log run [N] <vm>` shows one. The terminal gets each phase's header, a failure in full the moment it happens (VM, task, `file:line`, `msg`, `rc`, stdout and stderr as lines), and at the end one line per VM: roles run and unchanged, changes, failures, time, its file. Under the output, while a phase runs, one line per VM with its current role and task; the VMs done on one line ([`worms.py`](../ansible/callback_plugins/worms.py), [`live.py`](../src/worms_cli/live.py)). Without `worms` (the round agent's jobs, a run by hand), one line per VM and task, failures as YAML.
- **How long** (worms-dev, 2026-11-21, 15 VMs): every role on every VM 12 min (the 13 playbooks before took 18 min); with nothing changed 4 min; one VM, nothing changed, 30 s. The VMs of phase 3 share the host's 8 cores with Ansible itself: a task takes ~2 s on one VM alone, ~6 s with all 15 at once, so the stamps save more than the parallelism.
- **A role that did not change is skipped** on a VM ([`worms_role_gate.py`](../ansible/action_plugins/worms_role_gate.py), [`tasks/role.yml`](../ansible/tasks/role.yml)). After a role ran on a VM to its end, its handlers too, its stamp is written there (`/var/lib/worms/stamps/<role>`): a hash of the role's files, the sources [`roles/sources.yml`](../ansible/roles/sources.yml) names for it, the values of the variables its files name, the VM's groups. The next run reads a VM's stamps at once and skips a role whose stamp is the same: one gate instead of every task. Never skipped: with packages (the window), with `--full` (every role, its stamp written again), in a run of a part (`--tags`). What a stamp cannot see: the VM drifting by itself (a hand edit), its facts, the Proxmox CA the orchestrator fetches from the host: `worms apply <component> --full`.

**The restore point and the way back.** When the orchestrator (it migrates) or the database is among what changes, a dump of the database is taken first, as `worms db backup` takes one; if it cannot be, it asks whether to go on without. A successful apply ends with the way back: the commit the components ran before (`git checkout <it>`, then `worms apply`), and the dump (`worms db restore <dump>`: what was written since is lost).

<a id="release"></a>**From dev to production: `worms upgrade`.** On dev: commit, `worms apply`, `worms check`, then `git bundle create /root/worms-update.bundle main`, and copy it to production. There, with no round under way: `worms upgrade --plan /root/worms-update.bundle` (the steps, what stands in the way, nothing changed), then `worms upgrade /root/worms-update.bundle`. It refuses while a sandbox exists, a host job is queued or running, a build is unfinished, the window is open, the checkout has changes or the bundle does not verify; then it dumps the database and tars the git-ignored state (the vault, its key, the PKI, every OpenTofu state) into `~/.worms/backups/`, fast-forwards the checkout (never a merge), and goes on with the new code: `worms apply orchestrator` (the migrations first), `worms apply`, `worms apply round-agent gameplans`, each asking its own questions; last it checks `worms status`: `system: on <commit>` and no error. Each step done is recorded in `/var/lib/worms/upgrade.json`: after a failure or Ctrl-C, mend it and `worms upgrade --resume`. The end says the way back (the commit before, the dump, the tar). Packages need the host's egress for the window. Right after a `git pull` by hand, every database command says first when the database is behind the checkout's migrations (`worms upgrade --resume` or `worms apply orchestrator`), not a bare `ProgrammingError`.

<a id="site-upgrade"></a>**The first apply with `Site.yml`** (a platform configured by the 13 playbooks before it, 2026-11-21). Nothing about it is special to run, but on a platform that ran the older commit it does more than an ordinary apply, once:
1. Outside a round, nobody else changing the platform (`pgrep -af 'manage.py|ansible-playbook'`: nothing; the lock refuses a second `worms` anyway, not a raw `ansible-playbook`).
2. `git pull`, then `worms apply --plan`. Every VM component shows as changed (`Site.yml` is new, every VM is made from it); a component whose commit is unknown (no record yet) says its packages cannot be compared. Look at the packages column: a component with packages needs `--packages` and the host's egress for the window; without egress, leave it out and fill it later.
3. `worms apply`. It takes a dump of the database first (the orchestrator is among the changes), configures every VM with every role (no stamp is there yet: none is skipped), writes the stamps, and restarts the agent. On dev: 24 min for the 15 VMs, of which `Site.yml` 12 min; once: every VM's sshd drop-in gets its new comment (a reload), Django's code gets its owner and modes from rsync (a restart).
4. `worms check`, `worms status`. Each VM's log of the run: `worms log run 1 <vm>`.
5. From then on an unchanged role is skipped on a VM; `worms apply <component> --full` runs every role of it again (after a hand edit on the VM, say).


**`worms apply --all` (everything, a new host), in order:**
1. secrets → init → platform → firewall → host network.
2. Devboxes are shut down. The service VMs roll back to `clean`.
3. The **internet window** opens. The infrastructure is applied, the inventory written, `Site.yml` runs, then `SetupRoundAgent.yml`.
4. The window closes. Offline mode runs. The service VMs are snapshotted as `clean`.
5. The devboxes start again.

A failed step ends the build. Before exiting, it still closes the window, switches DNS and the caches back to offline mode (best effort: `!!! DNS AND THE CACHES MAY STILL BE IN ONLINE MODE` if that fails too, then `worms internet off`) and starts the devboxes.

**The internet window** ([`Internet.yml`](../ansible/Internet.yml), [`host_network/tasks/internet.yml`](../ansible/roles/host/host_network/tasks/internet.yml)) lets the infrastructure and private-internet VMs reach the internet (80, 443, 53) through the host.

- **Refused while** any sandbox VM exists or any devbox VM runs.
- **Opening it:**
  - writes the flag `/run/worms/internet-allowed` (tmpfs, so a reboot closes the window);
  - fills a runtime nft set and the `provisioning` security group;
  - records `platform_events`.
- **Closing it** is guaranteed: the build closes the window on success, on failure and on interrupt.
- **While it is open:**
  - the round agent starts no team VM;
  - the firewall watchdog stops any team VM that runs.

**Arguments after `--`** go to every playbook: `worms apply orchestrator -- -vvv`.

<a id="worms-gui"></a>
## worms-gui

An htop-like dashboard of the whole platform in the terminal, live, with the CLI's commands on what
you mark ([`src/worms_cli/gui/`](../src/worms_cli/gui/), started by [`src/worms_gui.py`](../src/worms_gui.py);
curses, nothing to install). `?` in it lists every key.

```sh
worms-gui                  # the platform this host (or WORMS_SSH's) runs
worms-gui --dry-run        # every action is shown, none runs (the devbox checks do: they change nothing)
worms-gui --no-auto-check  # a devbox check only on c or C, never by itself
worms-gui --no-watchers    # no SSH to the platform VMs: the host's view only
worms-gui --tab teams      # start on a tab: platform, teams, jobs, rounds, services, components, tasks, log
```

- **Header:** the host's CPU, memory and memory pressure, the load, KSM and the ZFS ARC; the
  system (production in red), the internet window, the cluster and host firewalls, the watchdog's
  last run, the round agent (active, PAUSED), devboxes held, the round and the run under way.
- **Tabs:** Platform, Teams, Jobs, Rounds, Services, Components, Tasks (its own commands'
  output), Log. Sort (`<` `>`, F6, a click on a column's head), filter (`/`, `f` for quick
  filters), mark (Space, `a` all shown, `u` none), Enter for a VM's or a job's details.
- **Platform** (the platform's own VMs: infrastructure, services, the private internet, the
  shared SSH gateway): state, uptime, CPU, memory, network and disk rates (the Proxmox API),
  whether the firewall is on (the VM's option, every NIC's `firewall=1` and the host's guest
  rules), the `clean` snapshot, and what it is doing. A platform VM's activity comes from a
  watcher: one SSH per VM running a small read-only loop (load, CPU, the busiest process, failed
  units, its services). One GUI on a machine runs the watchers; a second one reads theirs.
- **Teams** (every team's devbox VMs A, B, C and its NAT gateway, by team; the round's sandbox
  VMs after them, each by its name): state, uptime, CPU, memory, firewall, and what the checks
  say: login (n/m of the team's keys), DNS, apt, PyPI, flag, scoring, logging, NAT, other (the
  rest: `worms check`'s firewall verdict, a sandbox VM's other records), as ✓ ✗ ! ? —, with their
  age; an old one (older than 5 minutes, or a VM of the team restarted since) keeps its colour,
  dimmed. A devbox VM's are `worms check <team> --json`'s: when the cursor rests on one (or its
  pane is open) and its team has no check younger than 5 minutes, or one of its VMs restarted
  since (once it is up a minute), that runs in the background, one at a time, the others queued
  (`checking… 12s`, `queued`; only the newest the cursor asked for waits); each team's result is
  kept. `c` checks the marked VMs' teams now, `C` every team in one run; their end is said in the
  status line. Even with `--dry-run` (they run as root on the team's VMs, and change nothing);
  `--no-auto-check`: only on `c` and `C`. Its pane: the flag distributed, apt, PyPI and DNS
  reachable, reachable from the SSH gateway, the team's SSH keys on it (which are missing),
  scoring, logging, firewall (and the host's view when that is not on), the reasons, the team's
  devbox jobs, when it was checked.
  A sandbox VM's: what the newest `check` host job of the run that built the sandbox recorded (the
  database): `start`, `no-kvm`, `init` and its `init:<what>` (sandbox-gw has those too), then the
  probes; nothing is run for it. Team VMs and gateways are assumed rooted: worms-gui logs in to
  none and runs nothing there; only `worms check` does what it does. What a VM printed is shown
  escaped, on one line, after `the VM says:`; no flag or secret is shown. `--demo` makes the
  checks up.
- **Commands** are the CLI's own (`worms ...`), with its checks, lock and run log: `m` (F2) the
  menu for the marked VMs, jobs, rounds, services or components; `l` login and back, `!` run,
  `L` log; Jobs: cancel the marked queued ones, pause or resume the agent; Rounds: pause, resume,
  start, cancel; Components: apply or plan; `i` the internet window. What changes something asks
  first, naming its targets; on production the destructive ones want a typed `yes`. Login, apply,
  reset and rebuild run on the terminal and come back to the GUI; the rest run in the background.

## Django's management commands

To run one by hand on the orchestrator:

```sh
cd /opt/orchestrator/src && set -a && . /etc/orchestrator/django.env && set +a && /opt/orchestrator/venv/bin/python manage.py <command>
```

| Command | App | Runs as | Does |
|---|---|---|---|
| `conduct_rounds [--once\|--plan-only] [--interval 5]` | orchestrator | service `orchestrator-conduct-rounds` | Plans due rounds, runs one attempt at a time ([5](05-live-environment.md)) |
| `process_attempts [--once] [--interval 2] [--grace]` | orchestrator | service `orchestrator-process-attempts` | Single writer of captures and logs; devbox jobs and runs |
| `process_captures [--grace]` | orchestrator | on demand | One pass of round capture and log processing |
| `collect_topology` | orchestrator | timer, every 2 min | Proxmox VMs, bridges and firewall into `topology_snapshots` (read-only token) |
| `provision_devboxes` | orchestrator | `worms apply devbox` | Devbox rows and indexes (at most 16 teams), then one `devbox_provision` job |
| `init` | core | after migrations (Ansible), `reset_database` | Idempotent: `admin` (no usable password) and the competition `WORMS <year>` |
| `reset_admin_password` | core | `worms user password` | One-time password, ends sessions |
| `reset_database --yes [--no-init]` | core | `worms db reset` | Truncate, keep the event tables and the gameplan versions, `init` |
| `create_users CSV [--staff]` | core | by hand | Accounts from a CSV; prints `email,password` |

Ansible runs `migrate --database=migrator` (on the orchestrator only, with its services stopped) and `collectstatic` (on both VMs).

## Testing

| What | How |
|---|---|
| Offline suite | `worms test` or `worms test -- -k <pattern>`. Creates `.venv-test` if missing. A missing tool (tofu, ansible, PostgreSQL, root) skips its tests with the reason |
| Database tests | Need PostgreSQL server binaries (`initdb`). Without a server package, unpack `postgresql-17` with `dpkg -x` and put its `bin` on `PATH` ([`tests/pg.py`](../tests/pg.py)) |
| `worms test --live` | On a deployed host ([`Simulate.yml`](../ansible/Simulate.yml)): a temporary team and token, scoring, logging, monitor, DNS, isolation checks. PASS/FAIL per check |
| Live suite | `.venv-test/bin/pytest tests/live --live -s`: three teams and their devboxes end to end. It leaves them in place |
| Browser tests | `tests/browser/*.js`; they skip unless Playwright is installed |
| Rehearsal without a host | [`tests/docker/Dockerfile`](../tests/docker/Dockerfile): the dashboard with nginx, gunicorn, the orchestrator and real grants, in a container ([`scripts/dashboard_linux.py`](../scripts/dashboard_linux.py)) |
| Docs | [`tests/test_docs.py`](../tests/test_docs.py): every `worms ...` command in these docs must parse |

## Logs

Every one is shown by `worms log` ([See](#see)).

| What | Where | `worms` |
|---|---|---|
| A `worms` run that changed something | Here: `~/.worms/runs/<time>-<command>.log` (0600). The newest 200 | `worms log run [N]` |
| A host job (devbox build, round prepare, …) | On the host: `/var/log/worms/jobs/<uuid>.log`, `latest.log`. Kept 30 days | `worms log <#>`, `worms log latest -f` |
| A round, capture processing | The session's folder on the host, `~/.worms/rounds/`; on the orchestrator `journalctl -u orchestrator-process-attempts` | `worms log round`, `worms log processor` |
| Dashboard | `journalctl -u dashboard`, nginx logs | `worms log dashboard` |
| Audit logs of the infrastructure VMs | The syslog VM: `/var/log/remote/<ip>/audit.log` | `worms log audit` |
| Security events | The database | `worms show audit` |
| A long Ansible task's own output (the caches' fills, package installs) | On its VM: the file it names (`worms_live_log`: `/var/log/worms/devpi-seed.log`, `/var/log/worms/apt-cache-seed.log`), or apt's `/var/log/apt/term.log` | shown live during the run (below); `worms run <vm> --root -- tail -n 50 <file>` |

**Live output.** Ansible shows a task's output only once the task is over. So during a `worms` run
that writes a run log, a task that runs longer than 3 s and writes its output to a file on its VM
gets a window under it: the file's last 10 lines, how long the task has run, and how long ago its
last line came. A "last line 4 min ago" means it waits on something; pip's "Retrying …
ConnectTimeoutError" means no internet. The window goes when the task ends, and the run log never
gets it. Without a terminal, a line every 30 s instead, which the run log does get. A task names its
file with `vars: worms_live_log: <path>` and writes its output there itself; package installs with
the apt module follow apt's log without being told ([`live.py`](../src/worms_cli/live.py),
[`worms_live.py`](../ansible/callback_plugins/worms_live.py)).

## What `worms status` compares

**The version block.** `system: on <commit>` when every record of what runs agrees with this checkout's HEAD:
each component's entry in `/var/lib/worms/deployed.json` (and `workspace`, the host's copy), each service VM's
`clean` snapshot, and the commit each Django app's units started with. A record at an older commit counts as on
HEAD when nothing that component is made from changed since (the test `worms apply` makes before it leaves a
component alone), so `on <commit>` means `worms apply` has nothing to do; otherwise `system: mixed`, one line per
commit. A service VM without a snapshot, a component without a record and a dirty checkout are never on one
commit.

Each fact has one source; `worms status` compares its copies with it, from what it read once (the host probe, the VMs, the database, SSH to the platform VMs it names), and names the repair. Each check is a pure function in [`consistency.py`](../src/worms_cli/consistency.py) (the older ones in `manage.py`). `--deep` adds the slow one; `--json` has them as data.

| Row | Source | Compared with | Repair |
|---|---|---|---|
| versions | `deployed.json`, snapshots, running apps (the version block) | this checkout | `worms apply` |
| migrations | this checkout | the database | `worms apply orchestrator` |
| snapshot behind deployed / configured after its snapshot | a service VM's `clean` snapshot | `deployed.json`, the VM history | `worms apply <component>` |
| devbox map | `/var/lib/worms/devbox-teams` | its sha256 on scoring and logging | `worms step devbox-map` |
| current team | a round session that runs | `/etc/sandbox-api/current-team` on scoring and logging | `worms round teardown` |
| round run | an attempt in an active state | a round session that runs (its record's pid) | `worms round teardown` |
| host jobs | `services.left_behind` | jobs marked running | `worms jobs cancel <n>` |
| round session | `/var/lib/worms/round-stopped.json` | the units and VMs it stopped | the commands it names |
| sandbox | the `sandbox` pool | the sandbox stack's state, `generated_vms.tf`, the unfinished-build record | `worms round teardown` |
| orphan VMs | Proxmox's VMs in WORMS's pools (or named `sandbox-`/`devbox-`) | every OpenTofu state (the checkout's, the host's) | the sandbox's: `worms round teardown`; else look first |
| backup job | Proxmox's backup jobs | the VM IDs they name | `worms apply` |
| platform services | the units each service VM runs | active | `worms service start <name>` |
| drift (`--deep`) | the HCL | `tofu plan -detailed-exitcode -lock=false` of each controller stack | `worms apply --plan` |

## VM history

Each VM's history is kept on the host, in `/var/lib/worms/vm-history.jsonl` (root's, 0600, append-only; [`history.py`](../src/worms_cli/history.py)): one JSON line per event, `{"time", "vmid", "name", "event", "commit", "by", "detail"}`, time in UTC. `worms status` shows each VM's creation and last update (date and commit) in the VM table; `worms show <vm>` its whole history, newest first, one run's events of a kind on one line.

| Event | Written by | When |
|---|---|---|
| `created`, `recreated`, `destroyed` | the CLI's pipeline; the round agent | after an OpenTofu apply or destroy, by comparing the VMs before and after (`recreated`: a `-replace`, `--rebuild`); the round's sandbox (built, destroyed); a devbox rebuilt from nothing |
| `configured`, `packages` | the CLI's pipeline (also when the round agent runs it) | after each configure playbook, for the VMs of its `--limit` (else all it configures): `Site.yml` (not a run of a part, `--tags`), `SetupSandbox.yml`, `SetupDevbox.yml`, `SetupSSHGateway.yml`. `UpdateVMs.yml` with packages is `packages` |
| `snapshot`, `rolled_back` | the CLI's pipeline; the round agent | a service VM's `clean` snapshot taken or rolled back to (`worms apply`, `worms vm reset services`, a round's prepare); a sandbox's or devbox's snapshot, a devbox reset |

- **commit**: the controller's checkout, `git rev-parse --short=7 HEAD`, with `-dirty` when tracked files differ from it. The round agent's workspace has no `.git`: its sync writes the controller's commit into it (`/opt/worms/.worms-commit`, [`workspace.yml`](../ansible/roles/host/round_agent/tasks/workspace.yml)).
- **by**: the command (`worms apply dashboard`, `worms apply`), or the round agent's job (`round agent: devbox_rebuild #1834`), which the CLI it runs inherits (`WORMS_HISTORY_BY`).
- **Only on the host.** Nothing a VM says about itself is taken: sandbox and devbox VMs are assumed rooted by their team.
- **Two writers at once**, the CLI and the round agent: each takes an exclusive `flock` on the file and appends whole lines in one write; from a laptop over the host session (`flock(1)`, the same lock).
- **Best effort.** A build never fails for its history; a write that fails is a warning (in a job: a line in its log).
- **Older VMs** have no `created` line: Proxmox's creation time (`meta: ... ctime=`) stands in, commit `?`.
- `worms status` reads the last 20000 lines, in the one command it runs on the host anyway; `worms show <vm>` only the VM's lines (`grep -F '"vmid":<n>,'`).
