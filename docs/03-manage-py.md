# 3. manage.py: the `worms` CLI and Django's management commands

There are two `manage.py` files:

| File | What it is | Where it runs |
|---|---|---|
| [`src/manage.py`](../src/manage.py) | The `worms` CLI: builds, deploys and operates the platform | the controller |
| [`projects/dashboard/manage.py`](../projects/dashboard/manage.py) | Django's: its commands run as services on the orchestrator, or behind `worms` subcommands | the orchestrator VM |

## The `worms` CLI

```sh
worms() { /root/app/.venv-test/bin/python /root/app/src/manage.py "$@"; }
worms --help              # the verbs, grouped by purpose
worms build --help        # the components, what each needs
```

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
- Every command first prints `internet access: <state>` on stderr.
- Results go to stdout; `error:`/`warning:` go to stderr.
- Each tool call is echoed as `$ <cmd>  [locally | on root@node | in the agent's workspace]`.
- A command that changes something (`build`, `deploy`, `reset`, `internet`, `keys`, `devbox-*-key(s)`, `step`, `restart`, `power`, `destroy`, `emergency-stop`) keeps its whole output, the tools' too, in a run log: `~/.worms/runs/<YYYYmmdd-HHMMSS>-<verb>.log` on the machine it ran on (directory 0700, files 0600: Ansible's output can hold secrets; the newest 200 are kept, without colours). It says where at its start, and again when it fails. `worms log build [N]` shows the newest (or N-th newest). Not in the round agent's workspace, whose jobs have their own logs; never for a command whose output is a secret (`admin-reset-password`, `admin-create-staff`) or a VM's (`run`, `login`) ([`runlog.py`](../src/worms_cli/runlog.py)).

**Exit codes**

| Code | Meaning |
|---|---|
| 0 | success |
| 1 | failure (build, vault, token, refused confirmation) |
| 2 | usage error, or no database configured |
| 130 | Ctrl-C during a build |

**Privileges**
- The CLI logs in everywhere with the WORMS key only, held in a private `ssh-agent` per process. Your own keys are never offered: `IdentityFile=none`.
- Database commands connect as the PostgreSQL superuser `admin`. That role may only connect from the node; from a laptop the CLI opens an SSH tunnel.

## Commands

One verb per task ([`src/manage.py`](../src/manage.py)), grouped as `worms --help` groups them: what an operator does every day first. Commands marked † ask for confirmation unless given `--yes`; a command run by the round agent never asks.

**Targets.** A VM is named by its name (`dashboard`, `devbox-t2-a`), an alias (`ssh-gateway`: the shared SSH gateway `devbox-ssh-gateway`), or `<team>/<role>`: `alpha/a`, `alpha/b`, `alpha/c`, `alpha/gw` (the team's NAT gateway), with the team's name or its slug `t<id>` (`t2/a`). The team map ([`teams.py`](../src/worms_cli/teams.py)) comes from the database (teams, devboxes); when it cannot be reached, from the VMs' names (`devbox-t<id>-a|b|c|gw`) and the inventory's `devbox_team`, and a team is then known by its slug only. The old alias `ssh-gateway-t<id>` still works and means the team's NAT gateway `devbox-t<id>-gw`, not the shared SSH gateway. `login`, `show`, `log`, `run`, `power` and `rsync` (`alpha/a:notes`) take targets; the commands that take a team (`reset devbox`, `keys add`, ...) take its slug too.

**Moved commands** keep working: their old words are rewritten before parsing, with a one-line note on stderr (`ALIASES` in [`src/manage.py`](../src/manage.py)):

| Old | New |
|---|---|
| `worms status keys` | `worms keys status` (or `worms keys`) |
| `worms devbox-sync-keys` | `worms keys sync` |
| `worms devbox-add-key <team> <key>` | `worms keys add <team> <key>` |
| `worms devbox-remove-key <team> <key>` | `worms keys remove <team> <key>` |

<a id="see"></a><a id="operate"></a>

### Day to day

| Command | Does |
|---|---|
| `worms status` | First what needs attention: the internet window open; devbox VMs held by a round or stopped by the window (the node's records); service VMs without a `clean` snapshot; migrations of this checkout the database lacks; host jobs failed in the last 24 h; node services (`worms-round-agent`, the host firewall, its watchdog's timer, the mirror) not active. Each source when it answers, and "not checked" for one that does not. Then the system, the firewall, the internet window, every VM (kind, team, state, uptime, memory), the competition |
| `worms status teams`, `users`, `leaderboard` | The database's view, with the dashboard's own scoring |
| `worms status audit [--hours 24] [--kind PREFIX] [--limit 200]` | The event tables ([7](07-security.md#audit-trail)) |
| `worms status rounds`, `gameplans`, `jobs`, `devboxes` | Rounds with their gameplan and attempts; each gameplan as the orchestrator and the node found it (with its current version's SHA-256), and why one is invalid (this checkout's dry run); the round agent's latest host jobs; every team's devbox (its slug `t<id>` and VM names) and SSH keys |
| `worms show <vm>` | One VM: state, resources, disks, networks, management address and login user, OpenTofu address, snapshots, its team, the services that run there (the registry), whether it answers over SSH ([`vms.py`](../src/worms_cli/vms.py)) |
| `worms show <team>` | A team (its name or slug `t<id>`): its devbox's VMs `a`, `b`, `c` and `gw` with vmid, management address and state, and its keys on each (as `worms keys` reads them), with the shared SSH gateway's lines for it |
| `worms show database [<table>] [--schema] [--limit 20]` | The VM, then its tables (rows, size); a table's newest rows, or its columns, indexes and constraints ([`database.py`](../src/worms_cli/database.py)) |
| `worms log [<job>\|<name>\|<vm>] [-f] [-n N] [--since T]` | Every log from one place ([`logs.py`](../src/worms_cli/logs.py)): a host job's log by its number (`worms status jobs`; `/var/log/worms/jobs/<uuid>.log` on the node), a service's by its name in the service registry ([`services.py`](../src/worms_cli/services.py), which `worms restart` and `worms show <vm>` read too: `agent`, `firewall`, `conductor`, `processor`, `api`, `dashboard`, `scoring`, `logging`, `database`, `monitor`, `audit`: every platform VM's audit trail on the syslog VM), or any platform VM's whole journal by its name (`node` too). `worms log latest [-f]`: the newest host job's log without its number (`-f` moves on to the next job, as `tail -F latest.log` on the node). `worms log build [N] [-f]`: the newest (N-th newest) run log on this machine (Conventions). `-f` follows. Alone: the run logs, the names and the newest jobs. Not the team VMs |
| `worms run <target>... [--root] -- <command>` | A command on VMs without a login ([`remote.py`](../src/worms_cli/remote.py)): `node`, a VM (a target, above), or a group: `infrastructure`, `services`, `private-internet`, which hold platform VMs only; a team's VM runs a command only when it is named itself. Never interactive (`BatchMode`, no terminal), the WORMS key in its own agent and never forwarded, the node over the session's SSH (on the node: a local shell). The words after `--` are joined with spaces and run by the VM's shell, as ssh does; `--root` runs them with `sudo -n sh -c`. With several VMs each line gets a `[<vm>]` prefix and a summary follows; what a devbox or sandbox VM prints is escaped. A stopped VM is not started (exit 1 for it). The exit code is the worst of them |
| `worms login <vm>` | A shell over the management SSH, as `debian` (`sudo -i` for root). `worms login --help` lists the VMs there are now, with their team: the platform's, the private internet, `ssh-gateway` (the shared SSH gateway), devbox and sandbox VMs, and `node` from a laptop. A team's VM is also `<team>/<role>` (Targets, above). The agent is never forwarded. `worms login <vm> -- <command>` runs that command there instead, as `worms run <vm> -- <command>` |
| `worms deploy [<component>...] [--packages] [--plan] [--during-round] [-- <ansible args>]` | Configures what exists, as `worms build` configures an existing component, and does what operators did by hand after it. Never OpenTofu: a stack (`platform`, `firewall`) or `devbox` is refused, and so is a component (or a dependency) that is not there, pointing at `worms build`. No internet window unless `--packages`. Refused while a sandbox VM exists, unless `--during-round` (it restarts the conductor, which costs the running team its attempt). When the orchestrator or the round agent is deployed (or pending migrations bring the orchestrator in): the gameplans too (`worms build gameplans`), then the round agent is restarted once no host job runs (as `worms restart agent`), then the conductor. Alone: every component that exists (node plays and VMs), offline. `--plan` shows the plan only (as `worms build`'s, below) |
| `worms reset database` † | Truncates every table except the event tables, the gameplan versions and their history, and Django's own; then runs `init`. No migration |
| `worms reset services [--wait]` † | Rolls the 11 service VMs back to `clean` (refused while a sandbox exists); then has the agent write the devbox map again (`--wait`: until it has) |
| `worms reset devbox <team>... \| --all-teams [--fresh] [--wait]` † | Each team's devbox back to its clean snapshot, through the orchestrator as the dashboard's reset is: a rollback, then the internal CA, the team's keys and new tokens over SSH; its version goes up, its dev score starts over. `--fresh`: its VMs destroyed and built again from nothing, as `worms build devbox --rebuild --team <team>`. `--wait`: until the jobs have ended |
| `worms restart <service>... [--timeout 900] [--during-round]` | Restarts services by their names in the registry ([`services.py`](../src/worms_cli/services.py), the names `worms log` lists), in a safe order: `agent`, `processor`, `api`, `conductor`, `dashboard`, `scoring`, `logging`, `monitor`; then checks they are active. `agent` waits until the database shows no `running` host job (systemd would kill a sandbox build at its stop timeout) and refuses after `--timeout` seconds; with the conductor it always comes first (the conductor checks the gameplans against the node when it starts). `agent`, `processor`, `conductor`, `scoring` and `logging` are refused while a sandbox VM exists, unless `--during-round`. `firewall`, `database` and `audit` are not restarted from here (it says what instead) |

### Teams and keys

| Command | Does |
|---|---|
| `worms keys` (`worms keys status`) | Whether every team's devbox SSH keys are in place: for each team, the SSH gateway and its devbox VMs A, B and C (✓, or how many keys are missing or old there; `off` for a stopped VM, which is not checked), read over the management SSH and compared by fingerprint with the dashboard's; then the last key push job and one verdict. Exit 1 if a team's keys are not in place ([`keys.py`](../src/worms_cli/keys.py)) |
| `worms keys sync` | Copies every team's SSH keys from the database over SSH: its key file on its devbox VMs A, B and C, its lines on the SSH gateway; stopped VMs are skipped. Then `worms keys`. No round agent, no internet |
| `worms keys add <team> <key\|file\|->` | Adds an SSH key to a team's devbox with the dashboard's checks ([`ssh_keys.py`](../projects/dashboard/core/ssh_keys.py)), then copies the keys as `worms keys sync` does |
| `worms keys remove <team> <name\|fingerprint>` † | Removes one, copied the same way |
| `worms admin-reset-password [--username admin]` | A 20-character one-time password, printed once. Creates the admin if missing |
| `worms admin-create-staff EMAIL` | A staff account with a one-time password. The dashboard cannot create staff |
| `worms delete-team NAME` † | A team and everything it owns, by database cascade; its users stay. Its VMs are not touched |

<a id="build"></a>

### Build and machines

| Command | Does |
|---|---|
| `worms build` | Everything, end to end (= `make`): below |
| `worms build <component>... [--rebuild] [--packages] [--plan] [-- <ansible args>]` | Some components, and what they need first (below) |
| `worms build devbox [--rebuild [--team NAME]] [--wait]` | A devbox for every team without one: a `devbox_provision` job for the round agent. With `--rebuild` †: each team's (or `--team`'s) VMs from nothing, through the orchestrator. `--wait`: until those host jobs have ended, each one's end said as it comes (exit 1 if one failed; Ctrl-C stops the waiting, not the jobs) |
| `worms internet enable [--yes]`, `worms internet disable` | Opens or closes the internet window by hand. Enabling is refused during a round (a sandbox VM exists). Every devbox VM that is not stopped (as `Internet.yml` counts them) is shut down first, after asking (`--yes`: without asking), forced after 120 s; it writes them down on the node (`/var/lib/worms/window-stopped-devboxes.json`), and `worms internet disable` starts them again once the window is closed (those still there and stopped, and not held by a round) |
| `worms power start\|stop <target>...` | `qm start`, or `qm shutdown` forced after 120 s, on the node; targets as `worms run` takes them ([`power.py`](../src/worms_cli/power.py)). No team VM starts while the internet window is open; a devbox held by a round is left to its release; sandbox VMs are the round agent's. `worms power start --held`: the devbox VMs a round holds (`/var/lib/worms/held-devboxes.json`), once no sandbox VM exists; the hold stays recorded until the round releases it |
| `worms backup database [-o FILE]` | `pg_dump -Fc` of the platform's database (`postgres_db` in `group_vars/all.yml`) on the database VM, streamed over its management SSH into a new local file, mode 0600, in `~/.worms/backups/` (0700) by default; prints the path. Nothing is written on the VM; a failed dump leaves no file |
| `worms rsync <source>... <target> [-n] [--delete] [--preview] [-- <rsync options>]` | `rsync -avz` between here and a VM, in rsync's own syntax with a VM's login name for the host: `worms rsync team-1-backup/ devbox-t1-a:` up, `worms rsync devbox-t2-a: team-2-backup/` down; a team's VM also as `alpha/a:` (when that team is there and no local file of that name is). As in rsync: a ":" with no "/" before it names a VM (`./a:b` is a local file), a later source `:path` is on the first source's VM, a trailing "/" copies a directory's contents, `<vm>:` alone is the home; the daemon syntax is refused. Over the same SSH as `worms login`; the VM's rsync runs as `<user>@` (sudo) from that user's home: root's on a devbox and on the node, `debian`'s elsewhere. `--preview`: rsync's counts (files, sizes, bytes), nothing sent. With a devbox or sandbox VM (assumed rooted) this end is guarded: downloaded files get no setuid bits, owner or device files, symbolic links stay inert (`--munge-links`, restored on the way back up), and what the VM prints is escaped. rsync has to be on the VM: the devboxes' packages include it, the SSH gateway has none ([`rsync.py`](../src/worms_cli/rsync.py)) |

### Set up

| Command | Does |
|---|---|
| `worms secrets issue <token>` | Creates or **rotates** `proxmox-token`, `orchestrator-token` or `topology-token` with `pveum` on the node, and stores it in the vault. Consumers get the new value at the next `worms build` |
| `worms secrets set <name>` | Stores a token, or `ssh-key` (`worms secrets set ssh-key < key`), by hand |
| `worms secrets` | Creates the vault and every missing generated secret; prints states, never values |
| `worms secrets check` | Asks Proxmox what each token may do: `ok`, `broader than`, `missing from`, `invalid`, `not set` |
| `worms trust-node` | Once, with your own login: root on the node, and VMs older than the key, accept the WORMS key |

### Stop and tear down

| Command | Does |
|---|---|
| `worms emergency-stop` † | `qm stop` on every running VM, database and dashboard included. Destroys nothing |
| `worms destroy` † | Destroys the sandbox, devbox, infrastructure and private-internet stacks (the database's records with them) |
| `worms destroy --all` † | Stops, unlocks and destroys **every VM on the node**, including VMs WORMS did not create; then the firewall and platform stacks |

`make`, `make destroy` and `make destroy_all` call `worms build`, `worms destroy --yes` and `worms destroy --all --yes` ([`Makefile`](../Makefile)).

### Develop

| Command | Does |
|---|---|
| `worms test [-- <pytest args>]` | The offline suite (Testing) |
| `worms test --live [-- <ansible args>]` | The end-to-end simulation on the deployed platform |
| `worms step <name> [-- <ansible args>]` | One pipeline step ([`pipeline.py`](../src/worms_cli/pipeline.py), `STEPS`): `init`, `inventory`, `show-config`, `ping`, `offline`, `snapshot-services`, and the round agent's `round-configure-sandbox`, `devbox-apply`, `devbox-configure`, `devbox-keys`. The last two are refused without the agent's `-e @<varfile>`: the roles' defaults would write every team's key files empty (`worms keys sync` writes the keys; `worms reset devbox <team>` or `worms build devbox` a devbox). `worms step debug HOST`: why Ansible cannot reach a host (address, TCP 22, SSH, Python, sudo, cloud-init) |

## `worms build`

Sources: [`components.py`](../src/worms_cli/components.py) (what a component is and needs), [`pipeline.py`](../src/worms_cli/pipeline.py) (the steps).

**Components.** One per service; `worms build --help` lists them in build order, with what each needs:

| Kind | Components | Built by | "Exists" when |
|---|---|---|---|
| a stack | `platform`, `firewall` | `tofu apply` of its stack | its OpenTofu state has resources |
| a node play | `host-network`, `round-agent` | its playbook on the node | its service is active (`worms-host-firewall`, `worms-round-agent`) |
| a VM | `dns`, `syslog`, `database`, `orchestrator`, `dashboard`, `scoring`, `logging`, `monitor`, `apt-cache`, `devpi`, `docker-registry`, `manpages`, `ntp`, `cve-search`, `wikipedia` | `tofu apply -target=<its module>`, then the configure playbooks with `--limit <its group>` | Proxmox has a VM of that name |
| devboxes | `devbox` | the round agent (a host job) | — |
| gameplans | `gameplans` | `SetupRoundAgent.yml --tags agent-sync` (the node's copy), then `SetupDbAndDashboard.yml --limit orchestrator --tags gameplans` (the orchestrator's; the conductor restarts and checks them). No packages, no internet; refused during a round | — |

Groups: `infrastructure` (the platform's own VMs), `private-internet`, `services` (the VMs a reset returns to their snapshot).

**What a build of some components does** (`worms build dashboard`):
1. **The plan.** Each component requested: *create* (missing), *configure again* (exists), or *recreate* (`--rebuild`). Each missing dependency: *create*. Each line says what it touches (the OpenTofu stack and its `-target`/`-replace`, or the playbook and its group); under the table, what the whole run touches: the configure playbooks and their `--limit`, whether the internet window opens, whether the devboxes are shut down, which service VMs are rolled back and snapshotted again, what restarts. When a component exists already, a note says that `worms deploy` configures it too. `--plan` stops here.
2. **Asks** †, when the plan builds a dependency nobody asked for or recreates something (`--rebuild` of `database` loses its records). `platform` and `firewall` are never rebuilt alone: everything stands on them.
3. **Builds:**
   - A VM that is created or recreated needs packages, so the internet window opens and the devboxes are shut down meanwhile. A VM that exists is configured without packages (`--skip-tags packages`) and without the window, unless `--packages`.
   - Service VMs are rolled back to `clean` first and snapshotted again at the end, with the devboxes off meanwhile (as `worms build` does).
   - Only the orchestrator migrates and the database VM grants: configuring the orchestrator includes the database's plays; while the database lacks a migration of the checkout, building the dashboard or the database brings the orchestrator (and the database) into the plan ([`build.py`](../src/worms_cli/build.py), `pending_migrations`). Not during a round: the conductor restarts.

**`worms build` (everything), in order:**
1. secrets → init → platform → firewall → host network.
2. Devboxes are shut down. The service VMs roll back to `clean`.
3. The **internet window** opens. The infrastructure is applied, the inventory written, the 14 configure playbooks run.
4. The window closes. Offline mode runs. The service VMs are snapshotted as `clean`.
5. The devboxes start again.

A failed step ends the build. Before exiting, it still closes the window, switches DNS and the caches back to offline mode (best effort: `!!! DNS AND THE CACHES MAY STILL BE IN ONLINE MODE` if that fails too, then `worms internet disable`) and starts the devboxes.

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

**Arguments after `--`** go to every playbook: `worms build orchestrator -- -vvv`.

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
| `provision_devboxes` | orchestrator | `worms build devbox` | Devbox rows and indexes (at most 16 teams), then one `devbox_provision` job |
| `init` | core | after migrations (Ansible), `reset_database` | Idempotent: `admin` (no usable password) and the competition `WORMS <year>` |
| `reset_admin_password` | core | `worms admin-reset-password` | One-time password, ends sessions |
| `reset_database --yes [--no-init]` | core | `worms reset database` | Truncate, keep the event tables and the gameplan versions, `init` |
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
| A `worms` run that changed something | Here: `~/.worms/runs/<time>-<verb>.log` (0600). The newest 200 | `worms log build [N]` |
| A host job (devbox build, round prepare, …) | On the node: `/var/log/worms/jobs/<uuid>.log`, `latest.log`. Kept 30 days | `worms log <#>`, `worms log latest -f` |
| Conductor, capture processing | On the orchestrator: `journalctl -u orchestrator-conduct-rounds`, `-u orchestrator-process-attempts` | `worms log conductor`, `worms log processor` |
| Dashboard | `journalctl -u dashboard`, nginx logs | `worms log dashboard` |
| Audit logs of the infrastructure VMs | The syslog VM: `/var/log/remote/<ip>/audit.log` | `worms log audit` |
| Security events | The database | `worms status audit` |
