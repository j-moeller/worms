# 3. manage.py: the `worms` CLI and Django's management commands

There are two `manage.py` files:

| File | What it is | Where it runs |
|---|---|---|
| [`src/manage.py`](../src/manage.py) | The `worms` CLI: builds, deploys and operates the platform | the controller |
| [`projects/dashboard/manage.py`](../projects/dashboard/manage.py) | Django's: its commands run as services on the orchestrator, or behind `worms` subcommands | the orchestrator VM |

## The `worms` CLI

```sh
worms() { /root/app/.venv-test/bin/python /root/app/src/manage.py "$@"; }
eval "$(worms completion bash)"   # tab completion; or once: worms completion bash > /etc/bash_completion.d/worms
worms --help              # the verbs, grouped by purpose
worms apply --help        # the components, what each needs
```

<a id="tab-completion"></a>**Tab completion** ([`completion.py`](../src/worms_cli/completion.py)). `worms completion bash` prints a bash completion script, written by hand (no `argcomplete`: production has no internet). What does not change with the platform is in the script, taken from the CLI's own tables when it is generated: the verbs, their objects and flags (the argparse parsers), the lists of `show`, the components and groups of `apply`, `configure`, `plan` and `rebuild`, the steps, the services of `logs` and `restart`. Generate it again after an update. What changes, the VM names and the `<team>/<role>` targets of `run`, `login`, `show`, `logs`, `rsync`, `start` and `stop`, and the teams of `check`, `trace`, `reset`, `rebuild`, `add key`, `remove key` and `delete team`, comes from the hidden `worms __complete <kind>` (`targets`, `teams`). It answers before the CLI's own imports and reads local files only, no Proxmox API, SSH or database: the generated inventories (this checkout's, and on the node the round agent's workspace's) and `~/.worms/team-map.json`, which the team map writes (0600) whenever it was read from the database. About 0.15 s a TAB on the node. After `--` the shell's own file completion applies. Only words of `A-Za-z0-9._+-` (two around a `/` for a target) are offered: by `__complete`, in `team-map.json`, and again by the script, which matches them as plain strings and never hands one to `compgen -W` (it expands its words). A team names itself, and a name like `$(...)` must not run on a TAB; a team whose name is no such word is offered by its slug.

**Modes.** `WORMS_SSH` chooses the mode ([`session.py`](../src/worms_cli/session.py)).

| `WORMS_SSH` | Mode | What runs where |
|---|---|---|
| empty | on the node | Everything runs locally. |
| `root@<node>` | laptop | OpenTofu and Ansible run locally. Ansible reaches the VMs through the node. |

In both modes:
- **The node's own stacks** (sandbox, devbox) and playbooks always run in the round agent's workspace `/opt/worms`, through `/usr/local/sbin/worms-node-run`. That workspace is synced from the checkout first: `SetupRoundAgent.yml --tags agent-sync`.
- **What gets deployed** is the controller's working tree, uncommitted edits included.

**Configuration** ([`config.py`](../src/worms_cli/config.py)). Settings are read in this order, first one wins:
1. process environment (`WORMS_*` only)
2. `--config FILE`
3. `~/.worms/config` (`KEY=value` lines)
4. defaults

| Key | Meaning |
|---|---|
| `WORMS_SSH` | The node, as an ssh destination. A non-default port goes in `~/.ssh/config` |
| `WORMS_REPO_ROOT` | The checkout. Default: the one the CLI is in |
| `WORMS_PROXMOX_URL` | Default `https://<WORMS_SSH host>:8006/` |
| `WORMS_DB_*`, `WORMS_PROXMOX_*` | Override the vault-derived connections (development, tests) |
| `NO_COLOR` | No colours (read from the environment only) |

**Conventions**
- Every command is a verb and what it acts on: `worms <verb> [<what>...]` ([`src/manage.py`](../src/manage.py)).
- Every command first prints `internet access: <state>` on stderr.
- Results go to stdout; `error:`/`warning:` go to stderr.
- Each tool call is echoed as `$ <cmd>  [locally | on root@node | in the agent's workspace]`.
- <a id="lock"></a>**One change at a time.** A verb that changes the platform (`apply`, `configure`, `rebuild`, `reset`, `start`, `stop`, `restart`, `open`/`close window`, `refill`, `destroy`, `sync keys`, `restore db`, `step`) holds `/run/worms/worms.lock` on the node while it runs (from a laptop: `flock` on the node over SSH). A second one is refused, saying who holds it: pid, user, command, since when. Never `emergency-stop`, `pause`, `resume`, `cancel`: they work while a build runs. Not the round agent's own runs ([`release.py`](../src/worms_cli/release.py)).
- A verb that changes something keeps its whole output, the tools' too, in a run log: `~/.worms/runs/<YYYYmmdd-HHMMSS>-<verb>-<what>.log` on the machine it ran on (directory 0700, files 0600: Ansible's output can hold secrets; the newest 200 are kept, without colours). The log never stops or holds up a command: one that cannot be opened is a warning and the run goes on unlogged; one that cannot be written any more (a full disk) is closed, said once, and the output goes on to the terminal. It says where at its start, and again when it fails. `worms logs run [N]` shows the newest (or N-th newest). Not in the round agent's workspace, whose jobs have their own logs; never for a command whose output is a secret (`add staff`, `reset password`) or a VM's (`run`, `login`) ([`runlog.py`](../src/worms_cli/runlog.py)).
- What the round agent does for a command (a devbox reset or rebuild) is followed live: while a job is queued, why it waits; then its log as it is written; then how it ended. `--detach` queues and returns ([`jobs.py`](../src/worms_cli/jobs.py)).

**Exit codes**

| Code | Meaning |
|---|---|
| 0 | success; a check found nothing wrong |
| 1 | failure (build, vault, token, refused confirmation), or a check found a problem |
| 2 | usage error, or no database configured |
| 130 | Ctrl-C (a followed job goes on) |

**Privileges**
- The CLI logs in everywhere with the WORMS key only, held in a private `ssh-agent` per process. Your own keys are never offered: `IdentityFile=none`.
- Database commands connect as the PostgreSQL superuser `admin`. That role may only connect from the node; from a laptop the CLI opens an SSH tunnel.

## Commands

As `worms --help` groups them: look, reach, machines, operate, platform, teams and accounts, set up, develop. Commands marked † ask for confirmation unless given `--yes`; a command run by the round agent never asks.

**Targets.** A VM is named by its name (`dashboard`, `devbox-t2-a`), an alias (`ssh-gateway`: the shared SSH gateway `devbox-ssh-gateway`), or `<team>/<role>`: `alpha/a`, `alpha/b`, `alpha/c`, `alpha/gw` (the team's NAT gateway), with the team's name or its slug `t<id>` (`t2/a`). A team stands for its devbox VMs (`worms stop alpha`), and `devboxes` for every team's (`worms reset devboxes`). Groups: `infrastructure`, `services`, `private-internet` (platform VMs only). The team map ([`teams.py`](../src/worms_cli/teams.py)) comes from the database (teams, devboxes); when it cannot be reached, from the VMs' names (`devbox-t<id>-a|b|c|gw`) and the inventory's `devbox_team`, and a team is then known by its slug only.

<a id="trace"></a>**A team reports a problem.** Where does it come from?

| Command | Answers |
|---|---|
| `worms check alpha` | Is its devbox set up as it should be, link by link: login through the SSH gateway, DNS, scoring, logging, apt, PyPI, the flag, the firewall on A, B, C and its NAT gateway (and the gateway's nftables); then its jobs (queued: why), and where its newest upload is |
| `worms trace alpha` | Its newest dev uploads on their way to the devbox: picked up, the reset (its job), put on VM A (its job), its hour; what each step waits for and why |
| `worms logs alpha` | Its host jobs, the attempt processor's lines about its uploads, what scoring and logging refused from its devbox's address |
| `worms show job <n>`, `worms logs <n> -f` | One job: what it is, why it waits, its log |
| `worms login alpha/a` | Then look yourself |

<a id="see"></a>

### Look

| Command | Does |
|---|---|
| `worms status` | First what needs attention: the internet window open; devbox VMs held by a round or stopped by the window (the node's records); service VMs without a `clean` snapshot; migrations of this checkout the database lacks; host jobs failed in the last 24 h; node services (`worms-round-agent`, the host firewall, its watchdog's timer, the mirror) not active. Each source when it answers, and "not checked" for one that does not. Then the system, the firewall, the internet window, every VM (kind, team, state, uptime, memory, when it was created and last updated, with the commit: [VM history](#vm-history)), the competition |
| `worms show teams`, `users`, `leaderboard` | The database's view, with the dashboard's own scoring |
| `worms show audit [--hours 24] [--kind PREFIX] [--limit 200]` | The event tables ([7](07-security.md#audit-trail)) |
| `worms show rounds`, `gameplans`, `jobs`, `devboxes` | Rounds (id first, ON HOLD when paused) with their gameplan and attempts; each gameplan as the orchestrator and the node found it (with its current version's SHA-256), and why one is invalid (this checkout's dry run); the round agent's latest host jobs; every team's devbox (its slug `t<id>` and VM names) and SSH keys |
| `worms show job <n>` | One host job: kind, team, run, state, its times, parameters, error; while queued, why it waits (the agent paused or not running, a round holding the devboxes: the agent takes no devbox job then, a job running ahead, jobs queued ahead); its log's last lines |
| `worms show <vm>` | One VM: state, resources, disks, networks, management address and login user, OpenTofu address, snapshots, its team, the services that run there (the registry), whether it answers over SSH ([`vms.py`](../src/worms_cli/vms.py)); then its history, newest first ([VM history](#vm-history)) |
| `worms show <team>` | A team (its name or slug `t<id>`): its devbox's VMs `a`, `b`, `c` and `gw` with vmid, management address and state, and its keys on each (as `worms show keys` reads them), with the shared SSH gateway's lines for it |
| `worms show keys` | Whether every team's devbox SSH keys are in place: for each team, the SSH gateway and its devbox VMs A, B and C (✓, or how many keys are missing or old there; `off` for a stopped VM), read over the management SSH and compared by fingerprint with the dashboard's; then the last key push job and one verdict. Exit 1 if a team's keys are not in place ([`keys.py`](../src/worms_cli/keys.py)) |
| `worms show secrets` | Creates the vault and every missing generated secret; prints states, never values |
| `worms show db [<table>] [--schema] [--limit 20]` | The database's tables (rows, size); a table's newest rows, or its columns, indexes and constraints ([`database.py`](../src/worms_cli/database.py)). `worms show database` is the database's VM, then its tables |
| `worms logs [<n>\|<name>\|<vm>\|<team>] [-f] [-n N] [--since T]` | Every log from one place ([`logs.py`](../src/worms_cli/logs.py)): a host job's log by its number (`/var/log/worms/jobs/<uuid>.log` on the node), a service's by its name in the service registry ([`services.py`](../src/worms_cli/services.py): `agent`, `firewall`, `conductor`, `processor`, `api`, `dashboard`, `scoring`, `logging`, `database`, `monitor`, `audit`), any platform VM's whole journal by its name (`node` too), or a team's (above). `worms logs latest [-f]`: the newest host job's log (`-f` moves on to the next job). `worms logs run [N] [-f]`: the newest (N-th newest) run log on this machine. `-f` follows. Alone: the run logs, the names and the newest jobs. Not the team VMs' journals |
| `worms check [<team>...\|devboxes]` | Whether each team (default: every team with a devbox) can work on its devbox, checked as the team works and changing nothing ([`validate.py`](../src/worms_cli/validate.py)). A table per team, columns A, B, C and GW (its NAT gateway). **login:** the SSH gateway takes the team's registered keys and forwards to its A; A, B and C take them as root (asked with the public keys alone: no private key, no login). **DNS:** scoring, logging, apt-cache, pypi resolve. **scoring**, **logging:** a `POST /submit` with `{}` answers 400 (reached, the team's gateway recognised, nothing stored). **apt:** a fresh index through the apt cache in a directory of its own, then one package (on the gateway: the cache's index answers). **PyPI:** one package through the VM's `pip.conf`. **flag:** `/etc/worms/flag` there and well-formed (never printed). **firewall:** the VM's Proxmox firewall on, DROP in and out, `ipfilter`, its security groups. **NAT** (gateway): nftables loaded, forwarding on. Under the table: the team's queued, running and (24 h) failed jobs, a queued one with why it waits, and where its newest dev upload is. Then, for the SSH gateway: whether OpenSSH's per-source penalties can shut out the one address every team comes from. About a minute; exit 1 on a problem |
| `worms check tokens` | Asks Proxmox what each token may do: `ok`, `broader than`, `missing from`, `invalid`, `not set` |
| `worms check apt\|pypi\|caches` | Whether the caches' package lists can be filled ([`caches.py`](../src/worms_cli/caches.py)). **apt:** offline, on the apt-cache VM: `apt-get install -s` of the whole list against the index the cache holds (the one its last fill kept), nothing installed: every unknown name and every conflict, as apt says them, and how many packages it resolves to. **pypi:** each name on PyPI, with internet here (else: not checked, said so) |
| `worms trace <team>\|upload <n>` | A dev upload's way to the devbox, link by link ([`trace.py`](../src/worms_cli/trace.py)): picked up by the orchestrator (its devbox run), the reset (its `devbox_rebuild` job), put on VM A (its `devbox_deploy` job: the zip unpacked in `/home/debian/worm`), its hour. A link that waits says why: what the orchestrator waits for before the reset (a round's attempt under way, a round holding the devboxes: [`devbox_runs.py`](../projects/dashboard/orchestrator/rounds/devbox_runs.py), `reset_waits_for`, which the processor logs once and the dashboard shows the team), and for a queued job why the agent has not taken it. A team's newest three uploads |

### Reach

| Command | Does |
|---|---|
| `worms login <vm>` | A shell over the management SSH, as `debian` (`sudo -i` for root). `worms login --help` lists the VMs there are now, with their team. The agent is never forwarded. `worms login <vm> -- <command>` runs that command there instead, as `worms run <vm> -- <command>` |
| `worms run <target>... [--root] -- <command>` | A command on VMs without a login ([`remote.py`](../src/worms_cli/remote.py)): `node`, a VM, or a group (platform VMs only; a team's VM runs a command only when it is named itself). After `--`, several words are the command and its arguments, each arriving as typed (quoted for the VM's shell); one word is a command line for the VM's shell (`worms run dns -- 'journalctl -n 50 \| grep named'`). `--root` runs it with `sudo -n sh -c`. Input: what is piped in reaches one VM (`worms run alpha/a -- bash -s < script.sh`); a terminal's only a platform VM (what is typed could be a secret); several VMs never. Never interactive, the WORMS key never forwarded. With several VMs each line gets a `[<vm>]` prefix and a summary follows; what a devbox or sandbox VM prints is escaped. A stopped VM is not started. The exit code is the worst of them |
| `worms rsync <source>... <target> [-n] [--delete] [--preview] [-- <rsync options>]` | `rsync -avz` between here and a VM, in rsync's own syntax with a VM's login name for the host: `worms rsync team-1-backup/ devbox-t1-a:` up, `worms rsync devbox-t2-a: team-2-backup/` down; a team's VM also as `alpha/a:`. A ":" with no "/" before it names a VM (`./a:b` is a local file), a later source `:path` is on the first source's VM, a trailing "/" copies a directory's contents, `<vm>:` alone is the home. Over the same SSH as `worms login`; the VM's rsync runs as `<user>@` (sudo) from that user's home: root's on a devbox and on the node, `debian`'s elsewhere. `--preview`: rsync's counts, nothing sent. With a devbox or sandbox VM (assumed rooted) this end is guarded: no setuid bits, owner or device files, inert symbolic links, escaped output ([`rsync.py`](../src/worms_cli/rsync.py)) |

### Machines

| Command | Does |
|---|---|
| `worms start\|stop <target>...` | `qm start`, or `qm shutdown` forced after 120 s, on the node ([`power.py`](../src/worms_cli/power.py)): VMs, groups, a team (its four devbox VMs), `devboxes` (every team's). No team VM starts while the internet window is open; a devbox held by a round is left to its release; sandbox VMs are the round agent's. `worms start --held`: the devbox VMs a round holds (`/var/lib/worms/held-devboxes.json`), once no sandbox VM exists |
| `worms reset <team>...\|devboxes [--detach]` † | Each team's devbox back to its clean snapshot, through the orchestrator as the dashboard's reset is: a rollback, then the internal CA, the team's keys and new tokens over SSH; its version goes up, its dev score starts over. Follows the jobs live (Conventions); `--detach` queues only |
| `worms rebuild <team>...\|devboxes [--detach]` † | Their devbox VMs destroyed and built again from nothing, fresh disks, through the orchestrator; followed live |
| `worms rebuild <component>... [--packages]` † | Platform VMs destroyed and built again (in the internet window), as `worms apply` builds them |
| `worms reset services [--wait]` † | Rolls the 11 service VMs back to `clean` (refused while a sandbox exists); then has the agent write the devbox map again (`--wait`: follow that job) |
| `worms restart <service>... [--timeout 900] [--during-round]` | Restarts services by their names in the registry, in a safe order: `agent`, `processor`, `api`, `conductor`, `dashboard`, `scoring`, `logging`, `monitor`; then checks they are active. `agent` waits until no host job runs (systemd would kill a sandbox build at its stop timeout) and refuses after `--timeout` seconds; with the conductor it comes first. `agent`, `processor`, `conductor`, `scoring` and `logging` are refused while a sandbox VM exists, unless `--during-round`. `firewall`, `database` and `audit` are not restarted from here (it says what instead) |

### Operate

The round agent's queue and the rounds' schedule ([`control.py`](../src/worms_cli/control.py)). `worms show jobs` and `worms show rounds` give their numbers.

| Command | Does |
|---|---|
| `worms pause jobs [--reason TEXT]` | The round agent finishes the job under way and takes no other: a file on the node (`/var/lib/worms/agent-paused`, its text who and why) it looks at before each job, and says so once in its log. A queued job then says so (`worms show job`). Rounds' jobs too: pausing during a round stops it between steps |
| `worms resume jobs` | The agent takes jobs again |
| `worms cancel job <n>...`, `worms cancel jobs --team <t>` | Queued jobs, never run: `failed`, "cancelled by the operator". A running one is refused: it is a process on the node, half-way through something (`pause jobs`, then it ends). A cancelled devbox reset fails its upload, saying why |
| `worms pause round <r>` | The conductor starts no attempt of the round (`rounds.on_hold`; one under way ends as usual), and says so once; the devboxes a held round holds are started again meanwhile. Before its start too: its runs are planned at its start, then wait |
| `worms resume round <r>` | It goes on, holding the devboxes again if it wants them off |
| `worms start round <r>` | A round still to come begins now: its start, which is also its upload deadline, moves to now; its runs are planned with the uploads made until then |
| `worms cancel round <r>` † | Its attempts still to come are skipped; one under way ends as usual. A round not begun is not cancelled (pause it, or delete it on /staff/rounds/) |

<a id="build"></a>

### Platform

| Command | Does |
|---|---|
| `worms apply` | Everything, end to end (= `make`): below |
| `worms apply <component>... [--packages] [-- <ansible args>]` | Some components, and what they need first (below) |
| `worms apply devbox [--wait]` | A devbox for every team without one: a `devbox_provision` job for the round agent; `--wait` follows it |
| `worms configure [<component>...] [--packages] [--plan] [--during-round] [-- <ansible args>]` | Configures what exists, as `worms apply` configures an existing component, and does what operators did by hand after it. Never OpenTofu: a stack (`platform`, `firewall`) or `devbox` is refused, and so is a component (or a dependency) that is not there, pointing at `worms apply`. No internet window unless `--packages`. Refused while a sandbox VM exists, unless `--during-round` (it restarts the conductor, which costs the running team its attempt). When the orchestrator or the round agent is configured (or pending migrations bring the orchestrator in): the gameplans too (`worms apply gameplans`), then the round agent is restarted once no host job runs, then the conductor. Alone: every component that exists, offline |
| `worms plan [<component>...]` | What `worms apply` would do, changing nothing ([`release.py`](../src/worms_cli/release.py)): its steps and what they touch; what each VM of it runs now (the commit of its last configuration, from the VM history) against this checkout, and how far apart; OpenTofu's own plan of the stacks and VMs concerned (the devbox and sandbox stacks are the round agent's). Alone: every component. Before a release, run it on dev and then on prod: where the two differ, prod will surprise you |
| `worms refill apt\|pypi\|caches [--plan]` † | The caches' lists checked (as `worms check apt|pypi`), then filled in the internet window (`worms apply apt-cache|devpi --packages`). A bad name fails before anything is opened, not mid-window with the devboxes down |
| `worms add package apt\|pypi <name>...`, `worms remove package apt\|pypi <name>...` | A cache's list in this checkout (the roles' `apt_cache_seed_packages`, `devpi_seed_packages`): a name added only once checked; a change to commit, which the cache gets with `worms refill` |
| `worms open window [--yes]`, `worms close window` | Opens or closes the internet window by hand. Opening is refused during a round (a sandbox VM exists). Every devbox VM that is not stopped is shut down first, after asking, forced after 120 s, and written down on the node (`/var/lib/worms/window-stopped-devboxes.json`); `worms close window` starts them again once the window is closed, and switches DNS and the caches back to offline mode. Not while a sandbox VM exists or a round holds the devboxes: they stay off and written down, and it says so |
| `worms backup db [-o FILE]` | `pg_dump -Fc` of the platform's database on the database VM, streamed over its management SSH into a new local file, mode 0600, in `~/.worms/backups/` (0700) by default; prints the path. Nothing is written on the VM; a failed dump leaves no file |
| `worms restore db <dump>` † | DANGER: the database back to a dump of `worms backup db`, in one transaction (`pg_restore --clean`); the dashboard, the orchestrator's services and the round agent are stopped meanwhile and started again. Everything written since the dump is lost; the grants are the dump's (this checkout's: `worms configure database`). Refused during a round |
| `worms destroy` † | Destroys the sandbox, devbox, infrastructure and private-internet stacks (the database's records with them) |
| `worms destroy --all` † | Stops, unlocks and destroys **every VM on the node**, including VMs WORMS did not create; then the firewall and platform stacks |
| `worms emergency-stop` † | `qm stop` on every running VM, database and dashboard included. Destroys nothing |

`make`, `make destroy` and `make destroy_all` call `worms apply`, `worms destroy --yes` and `worms destroy --all --yes` ([`Makefile`](../Makefile)).

<a id="release"></a>**A release to production.** `worms plan` on dev, then on prod: the same steps and the same OpenTofu changes, or the difference explains itself. On prod: `worms backup db`, then `worms apply`/`worms configure` of what the plan names, then `worms check` and `worms status`. The way back: the previous commit checked out and `worms configure` again; a migration that has to go back: `worms restore db` of the dump taken first (what was written since is lost). The service VMs keep their `clean` snapshots (`worms reset services`).

### Teams and accounts

| Command | Does |
|---|---|
| `worms add key <team> <key\|file\|->` | Adds an SSH key to a team's devbox with the dashboard's checks ([`ssh_keys.py`](../projects/dashboard/core/ssh_keys.py)), then copies the keys as `worms sync keys` does |
| `worms remove key <team> <name\|fingerprint>` † | Removes one, copied the same way |
| `worms sync keys` | Copies every team's SSH keys from the database over SSH: its key file on its devbox VMs A, B and C, its lines on the SSH gateway; stopped VMs are skipped. Then `worms show keys`. No round agent, no internet |
| `worms add staff EMAIL` | A staff account with a one-time password. The dashboard cannot create staff |
| `worms reset password [<user>]` | A 20-character one-time password for the admin (or `<user>`, an admin account), printed once. Creates the admin if missing |
| `worms delete team NAME` † | A team and everything it owns, by database cascade; its users stay. Its VMs are not touched |

### Set up

| Command | Does |
|---|---|
| `worms issue token <token>` | Creates or **rotates** `proxmox-token`, `orchestrator-token` or `topology-token` with `pveum` on the node, and stores it in the vault. Consumers get the new value at the next `worms apply` |
| `worms set secret <name>` | Stores a token, or `ssh-key` (`worms set secret ssh-key < key`), by hand |
| `worms trust node` | Once, with your own login: root on the node, and VMs older than the key, accept the WORMS key |
| `worms completion bash` | The tab completion script (above) |

### Develop

| Command | Does |
|---|---|
| `worms test [-- <pytest args>]` | The offline suite (Testing) |
| `worms test --live [-- <ansible args>]` | The end-to-end simulation on the deployed platform |
| `worms step <name> [-- <ansible args>]` | One pipeline step ([`pipeline.py`](../src/worms_cli/pipeline.py), `STEPS`): `init`, `inventory`, `show-config`, `ping`, `offline`, `snapshot-services`, and the round agent's `round-configure-sandbox`, `devbox-apply`, `devbox-configure`, `devbox-keys`. The last two are refused without the agent's `-e @<varfile>`: the roles' defaults would write every team's key files empty (`worms sync keys` writes the keys; `worms reset <team>` or `worms apply devbox` a devbox). `worms step debug HOST`: why Ansible cannot reach a host (address, TCP 22, SSH, Python, sudo, cloud-init) |

<a id="moved"></a>**Commands that moved** keep working: their old words are rewritten before parsing, with a one-line note on stderr (`ALIASES` in [`src/manage.py`](../src/manage.py)):

| Old | New |
|---|---|
| `worms build [...] [--rebuild]` | `worms apply [...]`, `worms rebuild <component>` |
| `worms deploy` | `worms configure` |
| `worms status <topic>` (teams, users, jobs, ...) | `worms show <topic>` |
| `worms log ...`, `worms log build [N]` | `worms logs ...`, `worms logs run [N]` |
| `worms validate devbox [<team>]` | `worms check [<team>]` |
| `worms reset devbox <team> [--fresh]`, `--all-teams` | `worms reset <team>` (`worms rebuild <team>`), `worms reset devboxes` |
| `worms reset database`, `worms backup database` | `worms reset db`, `worms backup db` |
| `worms power start`, `worms power stop` | `worms start`, `worms stop` |
| `worms internet enable`, `worms internet disable` | `worms open window`, `worms close window` |
| `worms keys [status\|sync\|add\|remove]` | `worms show keys`, `worms sync keys`, `worms add key`, `worms remove key` |
| `worms secrets [issue\|set\|check]` | `worms show secrets`, `worms issue token`, `worms set secret`, `worms check tokens` |
| `worms admin-reset-password`, `admin-create-staff`, `delete-team`, `trust-node` | `worms reset password`, `worms add staff`, `worms delete team`, `worms trust node` |

## `worms apply`

Sources: [`components.py`](../src/worms_cli/components.py) (what a component is and needs), [`pipeline.py`](../src/worms_cli/pipeline.py) (the steps).

**Components.** One per service; `worms apply --help` lists them in build order, with what each needs:

| Kind | Components | Built by | "Exists" when |
|---|---|---|---|
| a stack | `platform`, `firewall` | `tofu apply` of its stack | its OpenTofu state has resources |
| a node play | `host-network`, `round-agent` | its playbook on the node | its service is active (`worms-host-firewall`, `worms-round-agent`) |
| a VM | `dns`, `syslog`, `database`, `orchestrator`, `dashboard`, `scoring`, `logging`, `monitor`, `apt-cache`, `devpi`, `docker-registry`, `manpages`, `ntp`, `cve-search`, `wikipedia` | `tofu apply -target=<its module>`, then the configure playbooks with `--limit <its group>` | Proxmox has a VM of that name |
| devboxes | `devbox` | the round agent (a host job) | — |
| gameplans | `gameplans` | `SetupRoundAgent.yml --tags agent-sync` (the node's copy), then `SetupDbAndDashboard.yml --limit orchestrator --tags gameplans` (the orchestrator's; the conductor restarts and checks them). No packages, no internet; refused during a round | — |

Groups: `infrastructure` (the platform's own VMs), `private-internet`, `services` (the VMs a reset returns to their snapshot).

**What a build of some components does** (`worms apply dashboard`):
1. **The plan.** Each component requested: *create* (missing), *configure again* (exists), *configure again, in the window* (exists, unfinished: below), or *recreate* (`--rebuild`). Each missing dependency: *create*. Each line says what it touches (the OpenTofu stack and its `-target`/`-replace`, or the playbook and its group); under the table, what the whole run touches: the configure playbooks and their `--limit`, whether the internet window opens, whether the devboxes are shut down, which service VMs are rolled back and snapshotted again, what restarts. When a component exists already, a note says that `worms configure` configures it too. `--plan` stops here.
2. **Asks** †, when the plan builds a dependency nobody asked for or recreates something (`--rebuild` of `database` loses its records). `platform` and `firewall` are never rebuilt alone: everything stands on them.
3. **Builds:**
   - A VM that is created or recreated needs packages, so the internet window opens and the devboxes are shut down meanwhile. So does an *unfinished* one: it exists, but the build that created it stopped before its last configure playbook ([VM history](#vm-history): no `SetupAudit.yml` after its creation), for example a new apt-cache VM whose "Install apt-cacher-ng" had no internet. Configured offline, its apt would wait on the closed firewall. A VM that exists otherwise is configured without packages (`--skip-tags packages`) and without the window, unless `--packages`. The plan says *configure again, in the window* and names it after "internet window: OPEN".
   - Service VMs are rolled back to `clean` first and snapshotted again at the end, with the devboxes off meanwhile (as `worms apply` does).
   - Only the orchestrator migrates and the database VM grants: configuring the orchestrator includes the database's plays; while the database lacks a migration of the checkout, building the dashboard or the database brings the orchestrator (and the database) into the plan ([`build.py`](../src/worms_cli/build.py), `pending_migrations`). Not during a round: the conductor restarts.

**`worms apply` (everything), in order:**
1. secrets → init → platform → firewall → host network.
2. Devboxes are shut down. The service VMs roll back to `clean`.
3. The **internet window** opens. The infrastructure is applied, the inventory written, the 14 configure playbooks run.
4. The window closes. Offline mode runs. The service VMs are snapshotted as `clean`.
5. The devboxes start again.

A failed step ends the build. Before exiting, it still closes the window, switches DNS and the caches back to offline mode (best effort: `!!! DNS AND THE CACHES MAY STILL BE IN ONLINE MODE` if that fails too, then `worms close window`) and starts the devboxes.

**The internet window** ([`Internet.yml`](../ansible/Internet.yml), [`host_network/tasks/internet.yml`](../ansible/roles/host/host_network/tasks/internet.yml)) lets the infrastructure and private-internet VMs reach the internet (80, 443, 53) through the node.

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
| `reset_admin_password` | core | `worms reset password` | One-time password, ends sessions |
| `reset_database --yes [--no-init]` | core | `worms reset db` | Truncate, keep the event tables and the gameplan versions, `init` |
| `create_users CSV [--staff]` | core | by hand | Accounts from a CSV; prints `email,password` |

Ansible runs `migrate --database=migrator` (on the orchestrator only, with its services stopped) and `collectstatic` (on both VMs).

## Testing

| What | How |
|---|---|
| Offline suite | `worms test` or `worms test -- -k <pattern>`. Creates `.venv-test` if missing. A missing tool (tofu, ansible, PostgreSQL, root) skips its tests with the reason |
| Database tests | Need PostgreSQL server binaries (`initdb`). Without a server package, unpack `postgresql-17` with `dpkg -x` and put its `bin` on `PATH` ([`tests/pg.py`](../tests/pg.py)) |
| `worms test --live` | On a deployed node ([`Simulate.yml`](../ansible/Simulate.yml)): a temporary team and token, scoring, logging, monitor, DNS, isolation checks. PASS/FAIL per check |
| Live suite | `.venv-test/bin/pytest tests/live --live -s`: three teams and their devboxes end to end. It leaves them in place |
| Browser tests | `tests/browser/*.js`; they skip unless Playwright is installed |
| Rehearsal without a node | [`tests/docker/Dockerfile`](../tests/docker/Dockerfile): the dashboard with nginx, gunicorn, the orchestrator and real grants, in a container ([`scripts/dashboard_linux.py`](../scripts/dashboard_linux.py)) |
| Docs | [`tests/test_docs.py`](../tests/test_docs.py): every `worms ...` command in these docs must parse |

## Logs

Every one is shown by `worms logs` ([See](#see)).

| What | Where | `worms` |
|---|---|---|
| A `worms` run that changed something | Here: `~/.worms/runs/<time>-<verb>.log` (0600). The newest 200 | `worms logs run [N]` |
| A host job (devbox build, round prepare, …) | On the node: `/var/log/worms/jobs/<uuid>.log`, `latest.log`. Kept 30 days | `worms logs <#>`, `worms logs latest -f` |
| Conductor, capture processing | On the orchestrator: `journalctl -u orchestrator-conduct-rounds`, `-u orchestrator-process-attempts` | `worms logs conductor`, `worms logs processor` |
| Dashboard | `journalctl -u dashboard`, nginx logs | `worms logs dashboard` |
| Audit logs of the infrastructure VMs | The syslog VM: `/var/log/remote/<ip>/audit.log` | `worms logs audit` |
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

## VM history

Each VM's history is kept on the node, in `/var/lib/worms/vm-history.jsonl` (root's, 0600, append-only; [`history.py`](../src/worms_cli/history.py)): one JSON line per event, `{"time", "vmid", "name", "event", "commit", "by", "detail"}`, time in UTC. `worms status` shows each VM's creation and last update (date and commit) in the VM table; `worms show <vm>` its whole history, newest first, one run's events of a kind on one line.

| Event | Written by | When |
|---|---|---|
| `created`, `recreated`, `destroyed` | the CLI's pipeline; the round agent | after an OpenTofu apply or destroy, by comparing the VMs before and after (`recreated`: a `-replace`, `--rebuild`); the round's sandbox (built, destroyed); a devbox rebuilt from nothing |
| `configured`, `packages` | the CLI's pipeline (also when the round agent runs it) | after each configure playbook, for the VMs of its `--limit` (else all it configures): the platform's 13, `SetupSandbox.yml`, `SetupDevbox.yml`, `SetupSSHGateway.yml`. `UpdateVMs.yml` with packages is `packages` |
| `snapshot`, `rolled_back` | the CLI's pipeline; the round agent | a service VM's `clean` snapshot taken or rolled back to (`worms apply`, `worms reset services`, a round's prepare); a sandbox's or devbox's snapshot, a devbox reset |

- **commit**: the controller's checkout, `git rev-parse --short=7 HEAD`, with `-dirty` when tracked files differ from it. The round agent's workspace has no `.git`: its sync writes the controller's commit into it (`/opt/worms/.worms-commit`, [`workspace.yml`](../ansible/roles/host/round_agent/tasks/workspace.yml)).
- **by**: the command (`worms apply dashboard`, `worms configure`), or the round agent's job (`round agent: devbox_rebuild #1834`), which the CLI it runs inherits (`WORMS_HISTORY_BY`).
- **Only on the node.** Nothing a VM says about itself is taken: sandbox and devbox VMs are assumed rooted by their team.
- **Two writers at once**, the CLI and the round agent: each takes an exclusive `flock` on the file and appends whole lines in one write; from a laptop over the node session (`flock(1)`, the same lock).
- **Best effort.** A build never fails for its history; a write that fails is a warning (in a job: a line in its log).
- **Older VMs** have no `created` line: Proxmox's creation time (`meta: ... ctime=`) stands in, commit `?`.
- `worms status` reads the last 20000 lines, in the one command it runs on the node anyway; `worms show <vm>` only the VM's lines (`grep -F '"vmid":<n>,'`).
