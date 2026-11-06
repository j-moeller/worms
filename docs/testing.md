# Testing

| | `worms test` | `worms simulate` | `worms test -- tests/live --live` |
|---|---|---|---|
| Runs | anywhere | on the node, against the deployed platform | on the node, against the deployed platform |
| Needs | Python 3; optionally PostgreSQL server binaries, `tofu` with the provider, root with `ip`/`tc`/`tcpdump`/`nft`, Node with Playwright | a provisioned platform; a sandbox for the traffic part | the platform in its base state, Node with Playwright |
| Covers | configuration, code, privileges, the mirror | the running system: services, scoring path, monitor, containment | the devbox journey of an admin and a team, nothing faked |
| Changes | nothing outside temp dirs and network namespaces | a test team and token, removed afterwards | users, teams and devbox VMs, left in place |

## `worms test`: the offline suite

Creates `.venv-test` from `tests/requirements.txt` on first use and runs `pytest`
(`worms test -- -k shipper -x`). A test whose tool is missing is skipped with the reason.

**Last full run, 2026-11-05, on this node without PostgreSQL:** 522 passed, 458 skipped, 3
failed. The skips are the database tests: this node has no PostgreSQL binaries, so the
conductor, team API, staff and progression tests ran only on a SQLite harness. Earlier runs with
PostgreSQL (2026-11-04): 655 passed. Known failures, none a product bug:
`test_playbook_syntax[inventory.generated.yml]` (treats a generated file as a playbook),
`test_fixture_matches_shared_variables` (this node's local name `worms-dev`),
and `test_monitor_port_is_receive_only` (fails on clean `HEAD` too; the live check passes).
`test_two_teams_run_one_after_another` (its fake agent reused flag hashes) is fixed by the merge
of `end-to-end-tests`, which passed 807 tests with PostgreSQL in its container.

| Area | Files |
|---|---|
| Database grants with the real migrations and `privileges.sql.j2` | `test_database.py` |
| Scoring and logging APIs as their INSERT-only roles | `test_sandbox_api.py` |
| Zeek shipper: parsing hostile values, rotation, outages, no duplicates | `test_zeek_shipper.py` |
| `worms-mirror` in network namespaces: every frame copied once, receive-only | `test_worms_mirror.py` (root) |
| Playbook syntax, rendered nftables/scripts/units, the apt seed, the watchdog | `test_ansible.py` |
| `tofu fmt`/`validate`, the addressing fixture, cross-layer consistency, no `/dev/kvm` CPU on team VMs | `test_tofu.py` |
| The conductor end to end with a fake Proxmox and agent; game plan; timeline | `test_rounds.py` |
| The round agent's validation and every handler's commands, with a fake executor; snapshots | `test_round_agent.py` |
| The CLI, the build's order, the vault, where steps run | `test_manage_cli.py`, `test_pipeline.py`, `test_secrets.py`, `test_laptop.py` |
| Team API, progression, write-then-read round trip with noise | `test_team_api.py`, `test_progression.py`, `test_api_roundtrip.py` |
| Devbox: data model, uploads, keys, the page | `test_devbox.py`, `test_devbox_runs.py`, `test_devbox_page.py` |
| Accounts, staff pages, view-as, password resets, registration | `test_accounts.py`, `test_staff_admin.py`, `test_view_as.py`, `test_password_resets.py`, `test_team_registration.py` |
| Pages in Chromium: topology player, progression, hostile files | `test_topology_page.py` |
| The docs name only commands the CLI accepts | `test_docs.py` |

`tests/fixtures/platform-outputs.json` is the platform stack's output computed from
`shared-variables.tf`; `test_tofu.py` fails when it is stale. `tofu validate` needs the
`bpg/proxmox` provider (or a `filesystem_mirror` in `TF_CLI_CONFIG_FILE`). Browser tests need Node
and Playwright outside the repo (`npm install --no-save playwright && npx playwright install
chromium` in the parent directory); on a server, install only the libraries Chromium reports
missing.

## The live devbox journey

`tests/live/test_devbox_journey.py` does on the real system what `docs/manual-devbox-run.md`
describes by hand, in about an hour and a half. Without `--live` it is skipped.

```sh
export NVM_DIR="$HOME/.nvm"; . "$NVM_DIR/nvm.sh"
.venv-test/bin/pytest tests/live --live -s --live-out=/var/tmp/worms-live-run
```

Stages: the base state (no devbox VMs or leftover volumes, only `admin`, internet denied); the
admin signs in and creates 3 teams and 9 users; `worms devbox provision`; each user adds a key and
reaches their own A through the gateway (other teams' VMs, a gateway shell and an unregistered key
refused); on every VM a file, `apt`, `pip`, `curl` to scoring and logging, the internet closed; the
traces on the page and in the database; a reset; afterwards the file and packages gone, a new
boot and token, earlier records kept, other teams untouched. Each run writes `report.md` and
screenshots, never a password.

It does not return to the base state. To run it again:

```sh
/usr/local/sbin/worms-node-run opentofu/stacks/devbox 'tofu destroy -auto-approve -parallelism=1'
.venv-test/bin/pytest tests/live --live -s --live-wipe-database=<this node's name>
```

`--live-wipe-database` deletes every user, team and result, and needs the node's name typed out.
**It has never run from start to finish** (the first run was stopped at step 05 on request).

## `worms simulate`

```sh
worms simulate                                     # = inventory + ansible-playbook Simulate.yml
worms build simulate -- -e sim_wait_seconds=600    # wait longer for the records
```

It plays a sandbox VM and reports PASS/FAIL per check: the node's services (firewall, watchdog,
`worms-mirror`, round agent); the service units and the conductor; a flag and a log submitted
over verified HTTPS and stored exactly once, the flag scored through an injected finished run;
known traffic recorded by the monitor with exact sizes; containment (the sandbox VM cannot reach
the database, dashboard, orchestrator, syslog, monitor, node, internet; each database role does
only its own INSERT, tested from its VM). **Without a sandbox**, the sandbox parts report FAIL
("sandbox deployed"), and no sandbox has existed yet.

## Not covered

- A real round driven by the conductor and agent, and any snapshot or rollback on a real VM.
- Whether the audit logs arrive at the syslog VM (only that the daemons run).
- Load: flooding the APIs, the monitor or the dashboard's uploads.
- The private-internet services from a sandbox.

## Rehearsals without the node (from `end-to-end-tests`, Fabian)

These need no Proxmox, vault or production data. They passed 807 tests in their container on
2026-11-05, before the merge into `main`; they have not run on this node, which lacks
PostgreSQL and Docker.

**The isolated dashboard rehearsal** starts the dashboard against a throwaway PostgreSQL with the
production middleware, secure cookies, the restricted `django_dashboard` role and the real grants,
on loopback only (HTTP on localhost:28180 for a browser, HTTPS on :28181). It prints the URL and a
generated admin password; Ctrl-C removes the database. Needs Python 3.12+, PostgreSQL server
binaries and OpenSSL on `PATH`:

```sh
PATH="$PWD/.venv-test/bin:$PATH" .venv-test/bin/python scripts/dashboard_e2e.py
PATH="$PWD/.venv-test/bin:$PATH" .venv-test/bin/python -m pytest -q tests/test_dashboard_e2e.py tests/test_enrollment_concurrency.py
```

They cover staff and CSV accounts, first sign-in and password change, joining, leaving and staff
assignments, uploads, rounds, scoring and logging as the INSERT-only roles, processing as the
orchestrator role, replays and zips, cross-team refusals, CSRF and origin checks, deactivation
and resets; with Node Playwright also the browser replay and download.

**Races** (`test_dashboard_races.py`, `test_enrollment_concurrency.py`, `test_round_planning_races.py`,
`test_staff_configuration_races.py`, `test_orchestrator_api.py`): real concurrent PostgreSQL
requests for duplicate accounts and team names (now case-insensitive), the last place in a team,
simultaneous assignments, identical uploads from two teams, missing or corrupt media, simultaneous
key registration, round planning against staff edits, and devbox jobs: every producer of a job
carrying the teams' keys (reset, reconfigure, provision) takes one lock, and a queued key update
is never reused across a later rebuild or provision, which would reinstall a revoked key.

**The Linux stack** (`tests/docker/Dockerfile`, `test_linux_stack.py`, `scripts/dashboard_linux.py`):
the rendered nginx, three gunicorn workers, the orchestrator API and the attempt processor against
a disposable PostgreSQL in a container; three browsers, twelve students signing in at once for three
team places, a worker killed and replaced, PostgreSQL stopped and restarted under load. The
concurrent tests use a higher test-only sign-in rate; production keeps 10 a minute per address.

```sh
docker build -f tests/docker/Dockerfile -t worms-dashboard-e2e:local tests
docker run --rm --privileged --shm-size=1g --memory=3g --mount "type=bind,source=$PWD,target=/repo,readonly" worms-dashboard-e2e:local
```

**Deployment regressions** (`test_deployment_connectivity.py`, privileged container only): the
Django role's connectivity tasks against fixtures (service names in a managed `/etc/hosts` block,
the Proxmox CA into the system trust store, idempotent), and Zeek's log rotation across one
writable data directory.

**Migrations 0005 and 0006** make account and team names case-insensitive and SSH key fingerprints
canonical. They refuse, and change nothing, when existing data collides. Before upgrading a
populated database, look for collisions:

```sql
SELECT lower(username), count(*) FROM users GROUP BY lower(username) HAVING count(*) > 1;
SELECT competition_id, lower(name), count(*) FROM teams GROUP BY competition_id, lower(name) HAVING count(*) > 1;
```
