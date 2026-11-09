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

One verb per task ([`src/manage.py`](../src/manage.py)). Commands marked † ask for confirmation unless given `--yes`; a command run by the round agent never asks.

### See

| Command | Shows |
|---|---|
| `worms status` | The system, the firewall, the internet window, every VM (kind, state, uptime, memory), the competition |
| `worms status teams`, `users`, `leaderboard` | The database's view, with the dashboard's own scoring |
| `worms status audit [--hours 24] [--kind PREFIX] [--limit 200]` | The event tables ([7](07-security.md#audit-trail)) |
| `worms status rounds`, `gameplans`, `jobs`, `devboxes` | Rounds with their gameplan and attempts; each gameplan as the orchestrator and the node found it, and why one is invalid (this checkout's dry run); the round agent's latest host jobs; every team's devbox and SSH keys |
| `worms show <vm>` | One VM: state, resources, disks, networks, management address and login user, OpenTofu address, snapshots, whether it answers over SSH ([`vms.py`](../src/worms_cli/vms.py)) |
| `worms show database [<table>] [--schema] [--limit 20]` | The VM, then its tables (rows, size); a table's newest rows, or its columns, indexes and constraints ([`database.py`](../src/worms_cli/database.py)) |
| `worms login <vm>` | A shell over the management SSH, as `debian` (`sudo -i` for root). `worms login --help` lists the VMs there are now: the platform's, the private internet, `ssh-gateway`, `ssh-gateway-<team slug>`, devbox and sandbox VMs, and `node` from a laptop. The agent is never forwarded |

### Build

| Command | Does |
|---|---|
| `worms build` | Everything, end to end (= `make`): below |
| `worms build <component>... [--rebuild] [--packages] [--plan] [-- <ansible args>]` | Some components, and what they need first (below) |
| `worms build devbox [--rebuild [--team NAME]]` | A devbox for every team without one: a `devbox_provision` job for the round agent. With `--rebuild` †: each team's (or `--team`'s) VMs from nothing, through the orchestrator |
| `worms internet enable`, `worms internet disable` | Opens or closes the internet window by hand |

### Operate

| Command | Does |
|---|---|
| `worms reset database` † | Truncates every table except the event tables and Django's own, then runs `init`. No migration |
| `worms reset services` † | Rolls the 11 service VMs back to `clean` (refused while a sandbox exists); then has the agent write the devbox map again |
| `worms reset devbox <team>... \| --all-teams` † | Each team's devbox back to its first-boot snapshot, through the orchestrator as the dashboard's reset is: its version goes up, its dev score starts over |
| `worms devbox-add-key <team> <key\|file\|->` | Adds an SSH key to a team's devbox with the dashboard's checks ([`ssh_keys.py`](../projects/dashboard/core/ssh_keys.py)); the orchestrator has the agent push the keys |
| `worms devbox-remove-key <team> <name\|fingerprint>` † | Removes one, pushed the same way |
| `worms devbox-reconfigure` | Pushes the teams' SSH keys and the attribution map again |
| `worms admin-reset-password [--username admin]` | A 20-character one-time password, printed once. Creates the admin if missing |
| `worms admin-create-staff EMAIL` | A staff account with a one-time password. The dashboard cannot create staff |
| `worms delete-team NAME` † | A team and everything it owns, by database cascade; its users stay. Its VMs are not touched |

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
| `worms step <name> [-- <ansible args>]` | One pipeline step ([`pipeline.py`](../src/worms_cli/pipeline.py), `STEPS`): `init`, `inventory`, `show-config`, `ping`, `offline`, `snapshot-services`, and the round agent's `round-configure-sandbox`, `devbox-apply`, `devbox-configure`. `worms step debug HOST`: why Ansible cannot reach a host (address, TCP 22, SSH, Python, sudo, cloud-init) |

## `worms build`

Sources: [`components.py`](../src/worms_cli/components.py) (what a component is and needs), [`pipeline.py`](../src/worms_cli/pipeline.py) (the steps).

**Components.** One per service; `worms build --help` lists them in build order, with what each needs:

| Kind | Components | Built by | "Exists" when |
|---|---|---|---|
| a stack | `platform`, `firewall` | `tofu apply` of its stack | its OpenTofu state has resources |
| a node play | `host-network`, `round-agent` | its playbook on the node | its service is active (`worms-host-firewall`, `worms-round-agent`) |
| a VM | `dns`, `syslog`, `database`, `orchestrator`, `dashboard`, `scoring`, `logging`, `monitor`, `apt-cache`, `devpi`, `docker-registry`, `manpages`, `ntp`, `cve-search`, `wikipedia` | `tofu apply -target=<its module>`, then the configure playbooks with `--limit <its group>` | Proxmox has a VM of that name |
| devboxes | `devbox` | the round agent (a host job) | — |

Groups: `infrastructure` (the platform's own VMs), `private-internet`, `services` (the VMs a reset returns to their snapshot).

**What a build of some components does** (`worms build dashboard`):
1. **The plan.** Each component requested: *create* (missing), *configure again* (exists), or *recreate* (`--rebuild`). Each missing dependency: *create*. `--plan` stops here.
2. **Asks** †, when the plan builds a dependency nobody asked for or recreates something (`--rebuild` of `database` loses its records). `platform` and `firewall` are never rebuilt alone: everything stands on them.
3. **Builds:**
   - A VM that is created or recreated needs packages, so the internet window opens and the devboxes are shut down meanwhile. A VM that exists is configured without packages (`--skip-tags packages`) and without the window, unless `--packages`.
   - Service VMs are rolled back to `clean` first and snapshotted again at the end, with the devboxes off meanwhile (as `worms build` does).
   - Only the orchestrator migrates and the database VM grants: configuring the orchestrator includes the database's plays; a dashboard whose code needs a migration the database lacks brings both into the plan ([`build.py`](../src/worms_cli/build.py), `pending_migrations`). Not during a round: the conductor restarts.

**`worms build` (everything), in order:**
1. secrets → init → platform → firewall → host network.
2. Devboxes are shut down. The service VMs roll back to `clean`.
3. The **internet window** opens. The infrastructure is applied, the inventory written, the 14 configure playbooks run.
4. The window closes. Offline mode runs. The service VMs are snapshotted as `clean`.
5. The devboxes start again.

A failed step ends the build. Before exiting, it still closes the window and starts the devboxes.

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
| `reset_database --yes [--no-init]` | core | `worms reset database` | Truncate, keep event tables, `init` |
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

| What | Where |
|---|---|
| A host job (devbox build, round prepare, …) | On the node: `/var/log/worms/jobs/<uuid>.log`, `latest.log`. Kept 30 days |
| Conductor, capture processing | On the orchestrator: `journalctl -u orchestrator-conduct-rounds`, `-u orchestrator-process-attempts` |
| Dashboard | `journalctl -u dashboard`, nginx logs |
| Audit logs of the infrastructure VMs | The syslog VM: `/var/log/remote/<ip>/audit.log` |
| Security events | `worms status audit` |
