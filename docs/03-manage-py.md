# 3. manage.py: the `worms` CLI and Django's management commands

There are two `manage.py` files:

| File | What it is | Where it runs |
|---|---|---|
| [`src/manage.py`](../src/manage.py) | The `worms` CLI: builds, deploys and operates the platform | the controller |
| [`projects/dashboard/manage.py`](../projects/dashboard/manage.py) | Django's: its commands run as services on the orchestrator, or behind `worms` subcommands | the orchestrator VM |

## The `worms` CLI

```sh
worms() { /root/app/.venv-test/bin/python /root/app/src/manage.py "$@"; }
worms --help              # the commands, grouped by purpose
worms build --list        # the build's components
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

Commands marked † ask for confirmation unless given `--yes`.

### Set up

| Command | Does |
|---|---|
| `worms secrets issue <token>` | Creates or **rotates** `proxmox-token`, `orchestrator-token` or `topology-token` with `pveum` on the node, and stores it in the vault. Consumers get the new value at the next `worms build provision` |
| `worms secrets set <name>` | Stores a token, or `ssh-key` (`worms secrets set ssh-key < key`), by hand |
| `worms secrets` | Creates the vault and every missing generated secret; prints states, never values |
| `worms secrets check` | Asks Proxmox what each token may do: `ok`, `broader than`, `missing from`, `invalid`, `not set` |
| `worms node trust` | Once, with your own login: root on the node, and VMs older than the key, accept the WORMS key |
| `worms build [<component>] [-- <ansible args>]` | Everything (= `make`), or one component (below) |

### Operate

| Command | Does |
|---|---|
| `worms devbox provision` | Gives every team a devbox. Enqueues a `devbox_provision` job for the round agent |
| `worms devbox reconfigure` | Pushes the teams' SSH keys and the attribution map again |
| `worms admin reset-password [--username admin]` | A 20-character one-time password, printed once. Creates the admin if missing |
| `worms admin create-staff EMAIL` | A staff account with a one-time password. The dashboard cannot create staff |
| `worms teams delete NAME` † | A team and everything it owns, by database cascade; its users stay. Its VMs are not touched |

### Look

| Command | Shows |
|---|---|
| `worms status` | System, firewall, bridges, VMs by pool, internet window, competition, rounds, runs, queued host jobs |
| `worms teams`, `worms users`, `worms leaderboard` | The database's view, with the dashboard's own scoring |
| `worms audit [--hours 24] [--kind PREFIX] [--limit 200]` | The event tables ([7](07-security.md#audit-trail)) |
| `worms debug HOST` | Why Ansible cannot reach a host: address, TCP 22, SSH, Python, sudo, cloud-init |

### Stop and tear down

All of these ask for confirmation (†).

| Command | Does |
|---|---|
| `worms emergency-stop` | `qm stop` on every running VM, database and dashboard included. Destroys nothing |
| `worms reset-database` | Truncates every table except the event tables and Django's own, then runs `init`. No migration |
| `worms destroy` | Destroys the sandbox, devbox, infrastructure and private-internet stacks |
| `worms destroy-all` | Also the firewall and platform stacks |
| `worms terminate` | Stops, unlocks and destroys **every VM on the node**, including VMs WORMS did not create |

`make`, `make destroy` and `make destroy_all` call `worms build`, `worms destroy --yes` and `worms destroy-all --yes` ([`Makefile`](../Makefile)).

To tear down only the devboxes, run on the node: `/usr/local/sbin/worms-node-run opentofu/stacks/devbox 'tofu destroy -auto-approve -parallelism=1'`.

## `worms build`

Source: [`pipeline.py`](../src/worms_cli/pipeline.py).

| Component | Does |
|---|---|
| `all` (default) | The steps below |
| `secrets`, `init`, `platform`, `firewall`, `host-network` | The first steps of `all`, one at a time |
| `infrastructure` | Applies `private_internet`, `infrastructure/persistent`, `infrastructure/ephemeral`, then the inventory |
| `inventory`, `show-config` | Regenerates the Ansible inventory / prints the hosts Ansible will contact |
| `configure` | The 14 playbooks, **without** the internet window, rollback or snapshot |
| `provision` | `configure` wrapped like `all`: rollback, window, offline, snapshot. Use it after a rotation or a code change |
| `dashboard` | Redeploys the dashboard VM only (`SetupDbAndDashboard.yml --limit dashboard --skip-tags packages`). Devboxes keep running. Not for a new table, role or grant |
| `offline` | `SetupDNS` and `SetupPrivateInternet` in offline mode |
| `internet-allow`, `internet-deny` | Opens or closes the internet window by hand |
| `round-reset-services` | Rolls the 11 service VMs back to `clean` (refused while a sandbox exists) |
| `rebuild-services` | Replaces the service VMs, configures and snapshots them again |
| `snapshot-services` | Snapshots the service VMs (refused while any team VM exists) |
| `ping`, `simulate` | `ansible -m ping` on every VM; the end-to-end check (Testing) |
| `round-configure-sandbox`, `devbox`, `devbox-apply`, `devbox-configure` | Called by the round agent |

**`all`, in order:**
1. secrets → init → platform → firewall → host network.
2. Devboxes are shut down. The service VMs roll back to `clean`.
3. The **internet window** opens. The infrastructure is applied, the inventory written, configure runs.
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

**Arguments after `--`** go to every playbook: `worms build provision -- --limit orchestrator`.

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
| `provision_devboxes` | orchestrator | `worms devbox provision` | Devbox rows and indexes (at most 16 teams), then one `devbox_provision` job |
| `init` | core | after migrations (Ansible), `reset_database` | Idempotent: `admin` (no usable password) and the competition `WORMS <year>` |
| `reset_admin_password` | core | `worms admin reset-password` | One-time password, ends sessions |
| `reset_database --yes [--no-init]` | core | `worms reset-database` | Truncate, keep event tables, `init` |
| `create_users CSV [--staff]` | core | by hand | Accounts from a CSV; prints `email,password` |

Ansible runs `migrate --database=migrator` (on the orchestrator only, with its services stopped) and `collectstatic` (on both VMs).

## Testing

| What | How |
|---|---|
| Offline suite | `worms test` or `worms test -- -k <pattern>`. Creates `.venv-test` if missing. A missing tool (tofu, ansible, PostgreSQL, root) skips its tests with the reason |
| Database tests | Need PostgreSQL server binaries (`initdb`). Without a server package, unpack `postgresql-17` with `dpkg -x` and put its `bin` on `PATH` ([`tests/pg.py`](../tests/pg.py)) |
| `worms simulate` | On a deployed node ([`Simulate.yml`](../ansible/Simulate.yml)): a temporary team and token, scoring, logging, monitor, DNS, isolation checks. PASS/FAIL per check |
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
| Security events | `worms audit` |
