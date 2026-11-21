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
worms --help              # the commands, grouped by purpose
worms apply --help        # the components, what each needs
```

<a id="tab-completion"></a>**Tab completion** ([`completion.py`](../src/worms_cli/completion.py)). `worms completion bash` prints a bash completion script, written by hand (no `argcomplete`: production has no internet). What does not change with the platform is in the script, taken from the CLI's own tables when it is generated: the commands, the nouns' verbs and every flag (the argparse parsers), the topics of `show`, the components and groups of `apply` and `vm rebuild`, the steps, the services of `log` and `service restart`. Generate it again after an update. What changes, the VM names and the `<team>/<role>` targets of `run`, `login`, `show`, `log`, `rsync` and `vm start|stop|restart`, and the teams of `check`, `trace`, `vm reset`, `vm rebuild`, `key add`, `key remove` and `team delete`, comes from the hidden `worms __complete <kind>` (`targets`, `teams`). It answers before the CLI's own imports and reads local files only, no Proxmox API, SSH or database: the generated inventories (this checkout's, and on the node the round agent's workspace's) and `~/.worms/team-map.json`, which the team map writes (0600) whenever it was read from the database. About 0.15 s a TAB on the node. After `--` the shell's own file completion applies. Only words of `A-Za-z0-9._+-` (two around a `/` for a target) are offered: by `__complete`, in `team-map.json`, and again by the script, which matches them as plain strings and never hands one to `compgen -W` (it expands its words). A team names itself, and a name like `$(...)` must not run on a TAB; a team whose name is no such word is offered by its slug.

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
- The grammar is git's: a few verbs to look (`status`, `show`, `log`, `check`, `trace`) and to reach a VM (`login`, `run`, `rsync`); `apply` and `destroy` for the platform as a whole; and a noun for each kind of thing that changes, with its verbs: `worms vm reset alpha`, `worms jobs pause`, `worms db backup`. A noun alone lists its things: `worms vm`, `worms jobs`, `worms round`, `worms team`, `worms key`, `worms user`, `worms db`, `worms service`, `worms secret` (`worms show <list>` too: `worms show jobs`).
- Every command first prints `internet access: <state>` on stderr.
- Results go to stdout; `error:`/`warning:` go to stderr.
- <a id="json"></a>`--json` (on `status`, `show`, `check`, `trace`): what the command shows, as one JSON document on stdout instead: `{"command", "exit", "sections": [{"heading", "tables", "lines"}]}`, each table a list of rows keyed by its column headers (a table without headers: lists), colours and dots left out ([`output.py`](../src/worms_cli/output.py)). `worms show jobs --json | jq '.sections[0].tables[0][] | select(.State == "queued")'`; `worms trace alpha --json` gives each step as `{state, step, detail}`.
- Each tool call is echoed as `$ <cmd>  [locally | on root@node | in the agent's workspace]`.
- <a id="lock"></a>**One change at a time.** A command that changes the platform (`apply`, `destroy`, `internet on|off`, `vm start|stop|restart|reset|rebuild`, `service restart`, `db restore|reset`, `key sync`, `step`) holds `/run/worms/worms.lock` on the node while it runs (from a laptop: `flock` on the node over SSH). A second one is refused, saying who holds it: pid, user, command, since when. Never `vm kill`, `jobs ...`, `round ...`: they work while a build runs. Not the round agent's own runs, and not a raw `ansible-playbook` or `tofu` ([`lock.py`](../src/worms_cli/lock.py)).
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
- Database commands connect as the PostgreSQL superuser `admin`. That role may only connect from the node; from a laptop the CLI opens an SSH tunnel.

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
| `worms status` | First what needs attention: the internet window open; devbox VMs held by a round or stopped by the window (the node's records); service VMs without a `clean` snapshot; migrations of this checkout the database lacks; host jobs failed in the last 24 h; node services (`worms-round-agent`, the host firewall, its watchdog's timer, the mirror) not active. Each source when it answers, and "not checked" for one that does not. Then the system, the firewall, the internet window, every VM (kind, team, state, uptime, memory, when it was created and last updated, with the commit: [VM history](#vm-history)), the competition |
| `worms show <vm>` | One VM: state, resources, disks, networks, management address and login user, OpenTofu address, snapshots, its team, the services that run there (the registry), whether it answers over SSH ([`vms.py`](../src/worms_cli/vms.py)); then its history, newest first ([VM history](#vm-history)) |
| `worms show <team>` | A team (its name or slug `t<id>`): its devbox's VMs `a`, `b`, `c` and `gw` with vmid, management address and state, and its keys on each (as `worms key` reads them), with the shared SSH gateway's lines for it |
| `worms show job <n>` | One host job: kind, team, run, state, its times, parameters, error; while queued, why it waits (the agent paused or not running, a round holding the devboxes: the agent takes no devbox job then, a job running ahead, jobs queued ahead); its log's last lines |
| `worms show table <name> [--schema] [--limit 20]` | A table's newest rows, or its columns, indexes and constraints ([`database.py`](../src/worms_cli/database.py)). `worms db`: the tables |
| `worms show leaderboard` | The leaderboard, with the dashboard's own scoring |
| `worms show gameplans` | Each gameplan as the orchestrator and the node found it (with its current version's SHA-256), and why one is invalid (this checkout's dry run) |
| `worms show audit [--hours 24] [--kind PREFIX] [--limit 200]` | The event tables ([7](07-security.md#audit-trail)) |
| `worms log [<n>\|<name>\|<vm>\|<team>] [-f] [-n N] [--since T]` | Every log from one place ([`logs.py`](../src/worms_cli/logs.py)): a host job's log by its number (`/var/log/worms/jobs/<uuid>.log` on the node), a service's by its name in the service registry ([`services.py`](../src/worms_cli/services.py), `worms service`: `agent`, `firewall`, `conductor`, `processor`, `api`, `dashboard`, `scoring`, `logging`, `database`, `monitor`, `audit`), any platform VM's whole journal by its name (`node` too), or a team's (above). `worms log latest [-f]`: the newest host job's log (`-f` moves on to the next job). `worms log run [N] [-f]`: the newest (N-th newest) run log on this machine. `-f` follows. Alone: the run logs, the names and the newest jobs. Not the team VMs' journals |
| `worms check [<team>...]` | Whether each team (default: every team with a devbox) can work on its devbox, checked as the team works and changing nothing ([`validate.py`](../src/worms_cli/validate.py)). A table per team, columns A, B, C and GW (its NAT gateway). **login:** the SSH gateway takes the team's registered keys and forwards to its A; A, B and C take them as root (asked with the public keys alone: no private key, no login). **DNS:** scoring, logging, apt-cache, pypi resolve. **scoring**, **logging:** a `POST /submit` with `{}` answers 400 (reached, the team's gateway recognised, nothing stored). **apt:** a fresh index through the apt cache in a directory of its own, then one package (on the gateway: the cache's index answers). **PyPI:** one package through the VM's `pip.conf`. **flag:** `/etc/worms/flag` there and well-formed (never printed). **firewall:** the VM's Proxmox firewall on, DROP in and out, `ipfilter`, its security groups. **NAT** (gateway): nftables loaded, forwarding on. Under the table: the team's queued, running and (24 h) failed jobs, a queued one with why it waits, and where its newest dev upload is. Then, for the SSH gateway: whether OpenSSH's per-source penalties can shut out the one address every team comes from. About a minute; exit 1 on a problem |
| `worms check packages` | Whether the caches' package lists can be filled ([`caches.py`](../src/worms_cli/caches.py)). **apt:** offline, on the apt-cache VM: `apt-get install -s` of the whole list against the index the cache holds (the one its last fill kept), nothing installed: every unknown name and every conflict, as apt says them, and how many packages it resolves to. **pypi:** each name on PyPI, with internet here (else: not checked, said so). The lists are the roles' defaults (`apt_cache_seed_packages`, `devpi_seed_packages`): edit them in this checkout |
| `worms check tokens` | Asks Proxmox what each token may do: `ok`, `broader than`, `missing from`, `invalid`, `not set` |
| `worms trace <team>\|upload <n>` | A dev upload's way to the devbox, link by link ([`trace.py`](../src/worms_cli/trace.py)): picked up by the orchestrator (its devbox run), the reset (its `devbox_rebuild` job), put on VM A (its `devbox_deploy` job: the zip unpacked in `/home/debian/worm`), its hour. A link that waits says why: what the orchestrator waits for before the reset (a round's attempt under way, a round holding the devboxes: [`devbox_runs.py`](../projects/dashboard/orchestrator/rounds/devbox_runs.py), `reset_waits_for`, which the processor logs once and the dashboard shows the team), and for a queued job why the agent has not taken it. A team's newest three uploads |

### Reach

| Command | Does |
|---|---|
| `worms login <vm>` | A shell over the management SSH, as `debian` (`sudo -i` for root). `worms login --help` lists the VMs there are now, with their team. The agent is never forwarded. `worms login <vm> -- <command>` runs that command there instead, as `worms run <vm> -- <command>` |
| `worms run <target>... [--root] -- <command>` | A command on VMs without a login ([`remote.py`](../src/worms_cli/remote.py)): `node`, a VM, or a group (platform VMs only; a team's VM runs a command only when it is named itself). After `--`, several words are the command and its arguments, each arriving as typed (quoted for the VM's shell); one word is a command line for the VM's shell (`worms run dns -- 'journalctl -n 50 \| grep named'`). `--root` runs it with `sudo -n sh -c`. Input: what is piped in reaches one VM (`worms run alpha/a -- bash -s < script.sh`); a terminal's only a platform VM (what is typed could be a secret); several VMs never. Never interactive, the WORMS key never forwarded. With several VMs each line gets a `[<vm>]` prefix and a summary follows; what a devbox or sandbox VM prints is escaped. A stopped VM is not started. The exit code is the worst of them |
| `worms rsync <source>... <target> [-n] [--delete] [--preview] [-- <rsync options>]` | `rsync -avz` between here and a VM, in rsync's own syntax with a VM's login name for the host: `worms rsync team-1-backup/ devbox-t1-a:` up, `worms rsync devbox-t2-a: team-2-backup/` down; a team's VM also as `alpha/a:`. A ":" with no "/" before it names a VM (`./a:b` is a local file), a later source `:path` is on the first source's VM, a trailing "/" copies a directory's contents, `<vm>:` alone is the home. Over the same SSH as `worms login`; the VM's rsync runs as `<user>@` (sudo) from that user's home: root's on a devbox and on the node, `debian`'s elsewhere. `--preview`: rsync's counts, nothing sent. With a devbox or sandbox VM (assumed rooted) this end is guarded: no setuid bits, owner or device files, inert symbolic links, escaped output ([`rsync.py`](../src/worms_cli/rsync.py)) |

<a id="build"></a>

### Deploy

| Command | Does |
|---|---|
| `worms apply [--plan] [--packages]` † | The platform up to this checkout: only what changed since what runs ([below](#worms-apply)) |
| `worms apply <component>... [--plan] [--packages] [-- <ansible args>]` † | These components (`worms apply --help` lists them, and the groups): configured when they exist, built when they do not, with what they need first ([below](#worms-apply)). `worms apply devbox`: a devbox for every team without one (a `devbox_provision` job for the round agent, followed) |
| `worms apply --all` † | Everything, end to end, packages too: a new node (= `make`, [below](#worms-apply)) |
| `worms destroy` † | Destroys the sandbox, devbox, infrastructure and private-internet stacks (the database's records with them) |
| `worms destroy --all` † | Stops, unlocks and destroys **every VM on the node**, including VMs WORMS did not create; then the firewall and platform stacks |
| `worms internet [on\|off]` | The internet window by hand; alone, whether it is open. `on` † is refused during a round (a sandbox VM exists): every devbox VM that is not stopped is shut down first, after asking, forced after 120 s, and written down on the node (`/var/lib/worms/window-stopped-devboxes.json`). `off` closes it, switches DNS and the caches back to offline mode, and starts those devboxes again; not while a sandbox VM exists or a round holds the devboxes: they stay off and written down, and it says so. `apply` opens and closes the window itself when it needs it |

`make`, `make destroy` and `make destroy_all` call `worms apply --all`, `worms destroy --yes` and `worms destroy --all --yes` ([`Makefile`](../Makefile)).

### Machines

The lifecycle of a VM: built by `worms apply` (a team's devbox: `worms apply devbox`), configured again by `worms apply`, on and off with `worms vm start`, `stop` and `restart`, back to its clean snapshot with `worms vm reset`, from nothing with `worms vm rebuild`, gone with `worms destroy`.

| Command | Does |
|---|---|
| `worms vm` | Every VM, as `worms status` lists them |
| `worms vm start\|stop\|restart <target>...` | `qm start`, or `qm shutdown` forced after 120 s, or both, on the node ([`power.py`](../src/worms_cli/power.py)): VMs, groups, a team (its four devbox VMs), `devboxes` (every team's). No team VM starts while the internet window is open; a devbox held by a round is left to its release; sandbox VMs are the round agent's. `worms vm start --held`: the devbox VMs a round holds (`/var/lib/worms/held-devboxes.json`), once no sandbox VM exists |
| `worms vm reset <team>...\|devboxes [--detach]` † | Each team's devbox back to its clean snapshot, through the orchestrator as the dashboard's reset is: a rollback, then the internal CA, the team's keys and new tokens over SSH; its version goes up, its dev score starts over. Follows the jobs live (Conventions); `--detach` queues only |
| `worms vm reset services [--detach]` † | Rolls the 11 service VMs back to `clean` (refused while a sandbox exists); then has the agent write the devbox map again, and follows that job |
| `worms vm rebuild <team>...\|devboxes [--detach]` † | Their devbox VMs destroyed and built again from nothing, fresh disks, through the orchestrator; followed live |
| `worms vm rebuild <vm>... [--packages]` † | Platform VMs (or a group of them) destroyed and built again, in the internet window, as `worms apply` builds them |
| `worms vm kill` † | `qm stop` on every running VM, database and dashboard included: the emergency stop. Destroys nothing; never waits for the lock |

### Operate

The round agent's queue, the rounds' schedule ([`control.py`](../src/worms_cli/control.py)), the services. `worms jobs` and `worms round` give their numbers.

| Command | Does |
|---|---|
| `worms jobs [--limit 30]` | The round agent's latest host jobs, and why one failed |
| `worms jobs pause [--reason TEXT]` | The round agent finishes the job under way and takes no other: a file on the node (`/var/lib/worms/agent-paused`, its text who and why) it looks at before each job, and says so once in its log. A queued job then says so (`worms show job`). Rounds' jobs too: pausing during a round stops it between steps |
| `worms jobs resume` | The agent takes jobs again |
| `worms jobs cancel <n>...`, `worms jobs cancel --team <t>` | Queued jobs, never run: `failed`, "cancelled by the operator". A running one is refused: it is a process on the node, half-way through something (`jobs pause`, then it ends). A cancelled devbox reset fails its upload, saying why |
| `worms round` | The rounds (id first, ON HOLD when paused): window, gameplan and its check, attempts by state |
| `worms round pause <r>` | The conductor starts no attempt of the round (`rounds.on_hold`; one under way ends as usual), and says so once; the devboxes a held round holds are started again meanwhile. Before its start too: its runs are planned at its start, then wait. The staff pages show it "on hold" |
| `worms round resume <r>` | It goes on, holding the devboxes again if it wants them off |
| `worms round start <r>` | A round still to come begins now: its start, which is also its upload deadline, moves to now; its runs are planned with the uploads made until then |
| `worms round cancel <r>` † | Its attempts still to come are skipped; one under way ends as usual. A round not begun is not cancelled (pause it, or delete it on /staff/rounds/) |
| `worms service` | The services of the registry ([`services.py`](../src/worms_cli/services.py)): where each runs, what its log says, whether it restarts from here |
| `worms service restart <name>... [--timeout 900] [--during-round]` | Restarts services by their names, in a safe order: `agent`, `processor`, `api`, `conductor`, `dashboard`, `scoring`, `logging`, `monitor`; then checks they are active. `agent` waits until no host job runs (systemd would kill a sandbox build at its stop timeout) and refuses after `--timeout` seconds; with the conductor it comes first. `agent`, `processor`, `conductor`, `scoring` and `logging` are refused while a sandbox VM exists, unless `--during-round`. `firewall`, `database` and `audit` are not restarted from here (it says what instead) |

### Data and accounts

Teams, students and rounds are managed on the dashboard (/staff/); these are the console's part.

| Command | Does |
|---|---|
| `worms db` | The database's tables (rows, size); a table's rows: `worms show table <name>` |
| `worms db backup [-o FILE]` | `pg_dump -Fc` of the platform's database on the database VM, streamed over its management SSH into a new local file, mode 0600, in `~/.worms/backups/` (0700) by default; prints the path. Nothing is written on the VM; a failed dump leaves no file |
| `worms db restore <dump>` † | DANGER: the database back to a dump of `worms db backup`, in one transaction (`pg_restore --clean`); the dashboard, the orchestrator's services and the round agent are stopped meanwhile and started again. Everything written since the dump is lost; the grants are the dump's (this checkout's: `worms apply database`). Refused during a round |
| `worms db reset` † | DANGER: every table emptied but the event tables and the gameplan versions, then initialised again (`reset_database`) |
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
| `worms init` | Once per controller, with your own login: creates the vault and every missing generated secret (their states, never values), then lets the WORMS key in for root on the node and on VMs older than it. Then `worms secret issue` the three Proxmox tokens |
| `worms secret` | What the vault holds: every secret's state, never a value (creates the missing generated ones) |
| `worms secret set <name>` | Stores a token, or `ssh-key` (`worms secret set ssh-key < key`), by hand |
| `worms secret issue <token>` | Creates or **rotates** `proxmox-token`, `orchestrator-token` or `topology-token` with `pveum` on the node, and stores it in the vault. Consumers get the new value at the next `worms apply` |
| `worms completion bash` | The tab completion script (above) |
| `worms test [-- <pytest args>]` | The offline suite ([Testing](#testing)) |
| `worms test --live [-- <ansible args>]` | The end-to-end simulation on the deployed platform |

`worms step <name> [-- <ansible args>]` is the round agent's, not in the help: one pipeline step ([`pipeline.py`](../src/worms_cli/pipeline.py), `STEPS`): `init`, `inventory`, `show-config`, `ping`, `offline`, `snapshot-services`, and the round agent's `round-configure-sandbox`, `devbox-apply`, `devbox-configure`, `devbox-keys`. The last two are refused without the agent's `-e @<varfile>`: the roles' defaults would write every team's key files empty (`worms key sync` writes the keys; `worms vm reset <team>` or `worms apply devbox` a devbox). `worms step debug HOST`: why Ansible cannot reach a host (address, TCP 22, SSH, Python, sudo, cloud-init).

<a id="worms-build"></a>

## `worms apply`

Sources: [`components.py`](../src/worms_cli/components.py) (what a component is and needs), [`changes.py`](../src/worms_cli/changes.py) (what changed), [`pipeline.py`](../src/worms_cli/pipeline.py) (the steps).

**Components.** One per service; `worms apply --help` lists them in build order, with what each needs:

| Kind | Components | Built by | "Exists" when |
|---|---|---|---|
| a stack | `platform`, `firewall` | `tofu apply` of its stack | its OpenTofu state has resources |
| a node play | `host-network`, `round-agent` | its playbook on the node | its service is active (`worms-host-firewall`, `worms-round-agent`) |
| a VM | `dns`, `syslog`, `database`, `orchestrator`, `dashboard`, `scoring`, `logging`, `monitor`, `apt-cache`, `devpi`, `docker-registry`, `manpages`, `ntp`, `cve-search`, `wikipedia` | `tofu apply -target=<its module>`, then the configure playbooks with `--limit <its group>` | Proxmox has a VM of that name |
| devboxes | `devbox` | the round agent (a host job) | — |
| gameplans | `gameplans` | `SetupRoundAgent.yml --tags agent-sync` (the node's copy), then `SetupDbAndDashboard.yml --limit orchestrator --tags gameplans` (the orchestrator's; the conductor restarts and checks them). No packages, no internet; refused during a round | — |

Groups: `infrastructure` (the platform's own VMs), `private-internet`, `services` (the VMs a reset returns to their snapshot).

**`worms apply` alone: what changed since what runs.**
- **What runs:** per component, the commit it was last brought up to: the node's record `/var/lib/worms/deployed.json` (written after every successful apply of it), else for a VM the VM history (its last configuration), else unknown. A commit with `-dirty` had uncommitted changes nobody can see now; such a component, and one whose commit is unknown, is configured again.
- **What changed:** `git diff` from that commit to this checkout's working tree (an untracked file counts once it is newer than the record).
- **What it touches:** the files each component is made from, read from the repository (the reading the tests check against every host): a VM's configure playbooks that reach its group, their roles with what those include and depend on, its groups' group_vars, the inventory, and what a role copies from the repository (`django_app`: `projects/dashboard/`, the gameplans); the round agent's role and code, and its workspace (a copy of `ansible/`, `opentofu/`, `scripts/`, `src/`, `gameplans/`); the gameplans. Not deployed: the CLI, `docs/`, `tests/`; a changed file no component is made from is said.
- **Packages:** a component's package names (apt, pip, and every list named `*packages*`, the caches' seed lists too) at both commits: different needs `--packages` (the window), else it is refused before anything.
- **OpenTofu:** each stack's own plan, saved; a plan of in-place changes and new resources that are no VM is applied as shown, one that replaces, destroys or makes a VM anew is not (it says which: `worms vm rebuild <vm>` or `worms apply <stack>` does it, by name).
- **The devboxes** are never touched: they run what their teams made of them, and a reset gives them the change (`worms vm reset devboxes`).
- Then, once you agree: the restore point (below), OpenTofu's saved plans, the components configured.

**`worms apply <component>...`** (`worms apply dashboard`):
1. **The plan.** Each component requested: *create* (missing), *configure again* (exists), *configure again, in the window* (exists, unfinished: below), or *recreate* (`worms vm rebuild`). Each missing dependency: *create*. Each line says what it touches (the OpenTofu stack and its `-target`/`-replace`, or the playbook and its group); under the table, what the whole run touches: the configure playbooks and their `--limit`, whether the internet window opens, whether the devboxes are shut down, which service VMs are rolled back and snapshotted again, what restarts. `--plan` stops here, after what each VM of it runs now (the commit of its last configuration) against this checkout, and OpenTofu's own plan of the stacks and VMs concerned (the devbox and sandbox stacks are the round agent's).
2. **Asks** †, when the plan builds a dependency nobody asked for or recreates something (`worms vm rebuild database` loses its records). `platform` and `firewall` are never rebuilt alone: everything stands on them. Refused while a sandbox VM exists, unless `--during-round` (it restarts the conductor, which costs the running team its attempt).
3. **Builds:**
   - A VM that is created or recreated needs packages, so the internet window opens and the devboxes are shut down meanwhile. So does an *unfinished* one: it exists, but the build that created it stopped before its last configure playbook ([VM history](#vm-history): no `SetupAudit.yml` after its creation), for example a new apt-cache VM whose "Install apt-cacher-ng" had no internet. Configured offline, its apt would wait on the closed firewall. A VM that exists otherwise is configured without packages (`--skip-tags packages`) and without the window, unless `--packages`. The plan says *configure again, in the window* and names it after "internet window: OPEN".
   - A fill of the caches (`worms apply apt-cache devpi --packages`) checks their lists first, as `worms check packages`: a bad name fails before the window opens, not mid-window with the devboxes down.
   - Service VMs are rolled back to `clean` first and snapshotted again at the end, with the devboxes off meanwhile (as `worms apply --all` does).
   - Only the orchestrator migrates and the database VM grants: configuring the orchestrator includes the database's plays; while the database lacks a migration of the checkout, building the dashboard or the database brings the orchestrator (and the database) into the plan ([`build.py`](../src/worms_cli/build.py), `pending_migrations`).
   - The orchestrator and the round agent bring the gameplans along (unless a round is on), and are followed by a restart of the round agent once no host job runs, then of the conductor.

**The restore point and the way back.** When the orchestrator (it migrates) or the database is among what changes, a dump of the database is taken first, as `worms db backup` takes one; if it cannot be, it asks whether to go on without. A successful apply ends with the way back: the commit the components ran before (`git checkout <it>`, then `worms apply`), and the dump (`worms db restore <dump>`: what was written since is lost).

<a id="release"></a>**From dev to production.** On dev: commit, `worms apply`, `worms check`. On production, after pulling the same commit: `worms apply --plan` shows the same changes and OpenTofu plans as on dev did, or the difference explains itself; then `worms apply`, `worms check`, `worms status`. Packages need the node's egress for the window there.

**`worms apply --all` (everything, a new node), in order:**
1. secrets → init → platform → firewall → host network.
2. Devboxes are shut down. The service VMs roll back to `clean`.
3. The **internet window** opens. The infrastructure is applied, the inventory written, the 14 configure playbooks run.
4. The window closes. Offline mode runs. The service VMs are snapshotted as `clean`.
5. The devboxes start again.

A failed step ends the build. Before exiting, it still closes the window, switches DNS and the caches back to offline mode (best effort: `!!! DNS AND THE CACHES MAY STILL BE IN ONLINE MODE` if that fails too, then `worms internet off`) and starts the devboxes.

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
| `reset_admin_password` | core | `worms user password` | One-time password, ends sessions |
| `reset_database --yes [--no-init]` | core | `worms db reset` | Truncate, keep the event tables and the gameplan versions, `init` |
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

Every one is shown by `worms log` ([See](#see)).

| What | Where | `worms` |
|---|---|---|
| A `worms` run that changed something | Here: `~/.worms/runs/<time>-<command>.log` (0600). The newest 200 | `worms log run [N]` |
| A host job (devbox build, round prepare, …) | On the node: `/var/log/worms/jobs/<uuid>.log`, `latest.log`. Kept 30 days | `worms log <#>`, `worms log latest -f` |
| Conductor, capture processing | On the orchestrator: `journalctl -u orchestrator-conduct-rounds`, `-u orchestrator-process-attempts` | `worms log conductor`, `worms log processor` |
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

## VM history

Each VM's history is kept on the node, in `/var/lib/worms/vm-history.jsonl` (root's, 0600, append-only; [`history.py`](../src/worms_cli/history.py)): one JSON line per event, `{"time", "vmid", "name", "event", "commit", "by", "detail"}`, time in UTC. `worms status` shows each VM's creation and last update (date and commit) in the VM table; `worms show <vm>` its whole history, newest first, one run's events of a kind on one line.

| Event | Written by | When |
|---|---|---|
| `created`, `recreated`, `destroyed` | the CLI's pipeline; the round agent | after an OpenTofu apply or destroy, by comparing the VMs before and after (`recreated`: a `-replace`, `--rebuild`); the round's sandbox (built, destroyed); a devbox rebuilt from nothing |
| `configured`, `packages` | the CLI's pipeline (also when the round agent runs it) | after each configure playbook, for the VMs of its `--limit` (else all it configures): the platform's 13, `SetupSandbox.yml`, `SetupDevbox.yml`, `SetupSSHGateway.yml`. `UpdateVMs.yml` with packages is `packages` |
| `snapshot`, `rolled_back` | the CLI's pipeline; the round agent | a service VM's `clean` snapshot taken or rolled back to (`worms apply`, `worms vm reset services`, a round's prepare); a sandbox's or devbox's snapshot, a devbox reset |

- **commit**: the controller's checkout, `git rev-parse --short=7 HEAD`, with `-dirty` when tracked files differ from it. The round agent's workspace has no `.git`: its sync writes the controller's commit into it (`/opt/worms/.worms-commit`, [`workspace.yml`](../ansible/roles/host/round_agent/tasks/workspace.yml)).
- **by**: the command (`worms apply dashboard`, `worms apply`), or the round agent's job (`round agent: devbox_rebuild #1834`), which the CLI it runs inherits (`WORMS_HISTORY_BY`).
- **Only on the node.** Nothing a VM says about itself is taken: sandbox and devbox VMs are assumed rooted by their team.
- **Two writers at once**, the CLI and the round agent: each takes an exclusive `flock` on the file and appends whole lines in one write; from a laptop over the node session (`flock(1)`, the same lock).
- **Best effort.** A build never fails for its history; a write that fails is a warning (in a job: a line in its log).
- **Older VMs** have no `created` line: Proxmox's creation time (`meta: ... ctime=`) stands in, commit `?`.
- `worms status` reads the last 20000 lines, in the one command it runs on the node anyway; `worms show <vm>` only the VM's lines (`grep -F '"vmid":<n>,'`).
