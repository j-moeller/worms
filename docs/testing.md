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
`test_monitor_port_is_receive_only` (fails on clean `HEAD` too; the live check passes), and
`test_two_teams_run_one_after_another` (its fake agent reuses flag hashes).

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
