# Progress: setting up the ground state

Goal: OpenTofu + Ansible run end to end (`make`), and find out why steps hang.
Times are host time (CET); the VMs log in UTC (CET - 1h).

## Status

**Last updated:** 2026-11-05, after replacing the rebuild between rounds with a snapshot reset.

**Re-run from a clean base state, 2026-11-04:** passed from start to finish, with a retried build (`docs/e2e-run-2026-11-04/protocol.md`). The automated suite is in `tests/live`.

**The suite's first run, 16:37-16:59, stopped at the user's request, so it has still not run from start to finish.** Steps 00 to 04 passed: base state, admin sign-in, competition and teams, users, and the devbox build. **The build finished on its first attempt, in 17 minutes**, the first build since the TRIM; 4 of the 7 before it hit the disk timeout. One build is encouraging, but it does not prove F5 is gone. It stopped during step 05, with `alpha`'s 3 keys added and none for `bravo` or `charlie`. SSH, file, apt, pip, traces and reset did not run.

**Snapshot reset, 2026-11-05:** `worms build round-reset-services` now rolls the 11 service VMs back to a snapshot instead of rebuilding them, so devboxes can stay (`docs/management.md`, "Between rounds"). **Tested offline only: the safety check blocked the live test** (`docs/check-with-user.md`, 3b), so the real VMs have no snapshot and a reset refuses, with a message. On a throwaway VM with a cloud-init disk: snapshot 3.1 s, rollback 3.5 s, and deleting it with its snapshot left no volume behind. The boots of the 11 VMs took 17 to 73 seconds, which gives an estimate of about 2 minutes. The old rebuild asked for the internet window only after destroying the VMs, so with a devbox up it left the services gone; `rebuild-services` now refuses first.

**View as, 2026-11-05:** the admin can view the dashboard as another user, read only, from `/staff/users/` (`docs/accounts.md`, "View as"). Staff and students cannot.

**Progression VMs and uploads, 2026-11-05:** a team's progression now shows every VM of its own devbox once it is set up (dev), and the VMs it captured plus the entry VM from the worm's start on (live). No round has run since, so the live rule has only been checked with test data. Separately, a check found that an upload does not run on the devbox, and that one made in the dev view would run in the next live round (`docs/todos/dev-rounds.md`).

**Devbox uploads, 2026-11-05:** an upload in the dev view now resets the team's VMs and runs on VM A (`docs/devbox.md`, "Uploads"), and never enters a round. Run live on charlie's devbox: 8 minutes 36 seconds from upload to worm start, flag scored, log received. That run reset charlie's VMs while a root session from the SSH gateway sat idle on A: the check for open sessions and the upload were in one command, so the upload went ahead. Files changed on those VMs since boot were copied off first. Nothing differed from what a rebuild writes, apart from an empty-effect repair folder. The test upload stays in charlie's dev history, by `carl.charlie@example.org`.

**LLM endpoint, 2026-11-05:** `teachllm` is reachable from the sandbox and devbox VMs, as the one exception to "no internet". All 9 devbox VMs connect with TLS verified, and attempts to get anywhere else from a VM or a gateway, or to bypass the gateway, are blocked (`docs/security-assessment.md`, "LLM endpoint"). The three devbox gateways were rebuilt for it; the VMs were not. Not yet seen on a real sandbox.

**CPU types, 2026-11-05:** `docs/cpu-types.md` lists where the CPU type is set, every value, and what each one opens. New from that work: this node's CPU leaks between hyperthreads whatever the CPU type, and L1TF Reloaded has been demonstrated against KVM, so turning hyperthreading off is now a decision (`docs/check-with-user.md`, 3i). `cve-search` still has `/dev/kvm`, and team VMs can reach its port 5000 (3j).

**No `/dev/kvm` on team VMs, 2026-11-05:** `cpu: host` gave root on the devbox VMs `/dev/kvm`, which must not happen because they are assumed hostile and rooted. All 9 now run `x86-64-v3`, and sandbox VMs will too. Measured as root on each VM: `kvm_intel` refuses to load and `/dev/kvm` is gone. The agent refuses any other CPU before a round or a dev upload. Nesting off on the node is written but needs a restart (`docs/security-assessment.md`; `docs/check-with-user.md`, 3h).

**VM settings, 2026-11-05:** the operator set the devbox VMs to `balloon: 512` by hand, and the devbox stack keeps it. Sandbox VMs now have the same memory floor. Gateways are unchanged.

**`teachllm` in the private DNS, 2026-11-05:** the name now resolves on the VMs, so `curl https://teachllm.mlsec.tu-berlin.de/` works from all 9 devbox VMs with TLS verified. Every other outside name is still refused, and `scoring.worms.internal` still resolves. Not yet seen from a real sandbox.

**Password resets, 2026-11-05:** staff keep the one-time password button, and each reset now adds a row to the new `password_resets` table in the same transaction. The dashboard's role may add and read rows but not change or remove them (`docs/accounts.md`, "Password resets are recorded"). This is the first migration after `0001_initial`, and it needs the grants applied too (`docs/management.md`).

Checked on the live database as the dashboard's role, in a transaction that was rolled back, with a throwaway account: the row is written, and update, delete and truncate are refused. No real reset was made, so the table is still empty. During this check a slip of mine briefly started a second gunicorn on the dashboard VM. It failed to boot and exited within a second. The real service listens on a Unix socket, so it got no traffic and did not restart. Its empty `/root/.gunicorn` is removed.

**The devbox page, 2026-11-04 night:** the page showed `—` for every VM address (open since F18): `devbox_vms.addresses` was never filled. Every devbox job now reports the addresses and the orchestrator stores them, so a key change fills them in without rebuilding anything or changing a token. The login command now has `-A`, because without it the hop from A to B cannot log in: the VMs hold no private key. Panel **Connect** gives one command per hop, and tells users to check `ssh-add -L`. Panel **VMs** is now a picture rather than a table: the gateway, then A, B and C, joined interface to interface, with captured VMs marked. The team's SSH keys, the score and Reset moved from this page to the user's profile, the results API to Progression, and the submission commands to Upload. The profile's Team panel shows tokens found in the dev view, and captures, place and points overall and per round in the live view. The dev view's Progression page needs no round: it replays the devbox so far, up to now, and a reset starts it over. New API: `/api/v1/devbox/progression/` and `.zip`. A worm now runs as `bash main.sh`, not `python3 main.py`; that was one line in the round agent plus the upload check, and nothing in OpenTofu, Ansible or `manage.py`. Untrusted VMs fetch from `apt-cache.worms.internal`; private-internet VMs keep the address, because their firewall allows no DNS. Copy no longer covers the command, and Reset has room above it.

**Job logs and port 2222, 2026-11-04 evening:** each host job now writes its full output to `/var/log/worms/jobs/`, and the SSH gateway is published on port 2222 with `host_publish_ssh_gateway`, the same switch as the dashboard's `:443`. No new OpenTofu rule was needed: `devbox_ssh_gateway` already accepts port 22, from the host's address only. Both are deployed. Applying the port added only the 3 intended rules: all 28 VMs kept running, with 0 watchdog stops, and only ports 443 and 2222 are forwarded. The node cannot test the port from inside, so that test is the user's (`docs/check-with-user.md`, 3). The log has been tried on the real path, but no real job has written to it yet. The second build since the TRIM also succeeded on its first attempt, in 17 minutes.

**Found while applying:** the role sets `nf_conntrack_max` to 1048576 and `nf_conntrack_tcp_timeout_established` to 3600, but the live values stay at Proxmox's own defaults, 262144 and 432000, so the role's values do not hold. The likely cause is Proxmox's firewall setting them itself. It is not urgent, and nothing was changed.

**By hand:** `docs/manual-devbox-run.md` describes the whole journey as commands and form inputs, for the user to run. It was written without running anything.

To stop it I stopped the round agent for a few seconds, which ended job `ff353e5a` as failed (exit -15). So the SSH gateway may not hold all of `alpha`'s keys. The agent is running again, idle, with nothing queued. A new run starts with a teardown.

**Why steps hang, the newest part:** the disk. A ZFS write cycle took up to 110 seconds, and the disk had never been trimmed. A TRIM at 16:28 cut the slowest synchronous write on a quiet system from 1.6 s to 11 ms (`docs/check-with-user.md`, 3d). The next build is the real test.

**Teardown at 16:16-16:21:** exit 0, 108 resources. The 13 devbox VMs are gone, the other 15 are unchanged and running, and no volume was left behind. It was the first real teardown with `-parallelism=1`, the fix for G7.

| | |
|---|---|
| Ground state | **Reached on the existing VMs.** The last 4 full `make` runs exited 0. Not yet run from scratch, as you asked |
| VMs | 28 of 28 running: 15 infrastructure, none rebuilt, and the 13 devbox VMs the stopped suite run built |
| Internet window | Closed, tested with real connections from every VM |
| Dashboard | HTTPS only, as `worms-dev.mlsec.tu-berlin.de`. You confirmed outside access by IP before the rename; the name is not yet tested from outside |
| Tokens | All 3 issued and verified with `worms secrets check` |
| Provisioning token | **Tested live:** it built the devbox stack with no permission errors. The old token stays until you retire it (`docs/check-with-user.md`) |
| Commits | On `main`, pushed to `github` and `https` |

Your local changes (node name `worms-dev`, `cve_search` RAM 1024 MB) are still in use and
stay uncommitted. They reached `main` once by my mistake (`docs/check-with-user.md`, 8).

## Why steps hang

There were two different causes, and neither was the disk failing or a deadlock in apt.

1. **A real hang: migration `0006` blocked itself** (finding 1). This hit every fresh
   database, such as after `make destroy_all`, which is why it looked random. Before
   `lock_timeout` was added it hung forever. Fixed.
2. **A false alarm: `Update all packages` is slow and silent** (finding 2). It takes about
   16 minutes and prints nothing. Your Ctrl-C came 1-1.5 minutes before it would have
   finished. **Not fixed:** a fresh build will still look stuck here. A heartbeat
   (`async` + `poll`) would solve this if you want it.
3. **A real hang, found in the end-to-end run: apt on devbox VMs tried the real internet.** With
   no route out and no DNS, `SetupDevbox.yml` sat on one task for 28 minutes, until the job
   limit. Fixed (protocol, F14 and F15).

## `make` runs

| Run | Ended | Result |
|---|---|---|
| Yours | 17:12 | Interrupted with Ctrl-C in `Update all packages` |
| 1 | 17:47 | Exit 2: `Run Django migrations` failed |
| 2 | 18:11 | Exit 2: migrations fixed, then `Install apt-cacher-ng` failed |
| 3 | 18:48 | Exit 2: fresh database passed, then `auditd` not found on `apt-cache` |
| 4 | 19:14 | Exit 2: online phase passed, then the offline `SetupDNS.yml` failed |
| 5 | 19:44 | **Exit 0** |
| 6 | 20:14 | **Exit 0**, with the Proxmox TLS and `zeek-shipper` fixes |
| 7 | 20:52 | **Exit 0**, with the dashboard published |
| 8 | 22:21 | **Exit 0**, with the public names, HTTPS only, read-only caches and new tokens |
| 9 | 23:18 | Stopped on purpose in `ConfigureNginx`, to deploy only the dashboard. The window closed normally |
| Dashboard only | 23:23 | **Exit 0**, dark theme only, in 4 minutes |

A full run takes about 20 minutes when everything is already installed. A build from
nothing took about an hour by my estimate, based on the timings of your first run and
of run 3.

## Checks on the running system

| Check | Result |
|---|---|
| `worms simulate` (after run 8) | 69 pass, 1 fail. The fail is "sandbox deployed": none exists yet |
| Containment | All pass, including the database roles and capture NIC |
| Offline tests, with PostgreSQL | 566 passed, 4 failed, 33 skipped (browser tests) |
| Ansible reaches all VMs | 15 of 15 |
| Internet from every VM | Closed on ports 443 and 53 |
| New schema against the old migrations | Identical: 35 tables, 255 columns, 130 constraints, 166 indexes |
| Topology collector | Works: 15 guests, 0 errors |
| `zeek-shipper` | Recovered, 0 insert failures since the fix |
| Dashboard over TLS | 200 for its one name; 400 for the old name, the IP and unknown names |
| Dashboard certificate | Names only `worms-dev.mlsec.tu-berlin.de` |
| Port 80 | Not forwarded, and nothing listens on it on the dashboard or orchestrator |
| Sign-in through the public address | CSRF passes (a wrong password gets 401, not 403) |

The simulation does not cover sandbox traffic, because no sandbox exists.

The 4 test failures, none caused by these changes:

| Test | Cause |
|---|---|
| `test_fixture_matches_shared_variables` | Your dev node name. The fixture keeps the production name on purpose |
| `test_playbook_syntax[inventory.generated.yml]` | Treats a generated file as a playbook |
| `test_monitor_port_is_receive_only` | Also fails on clean `HEAD` (`rx_packets` is not 0). The live check passes |
| `test_two_teams_run_one_after_another` | The test's fake agent reuses flag hashes. The real agent's are random |

PostgreSQL was unpacked into a temporary directory for these tests. Nothing was
installed on the host.

## Open items

### Needs you

1. **Approve one throwaway test of the provisioning token.** Its rights were checked against
   Proxmox's permission rules and it refreshes all stacks, but it has not created anything
   yet. A standalone `tofu apply` was blocked for lack of your approval. It would create
   and then destroy one test VM, one bridge (`vmbr8999`, which reloads the node's network
   config twice), one pool, one firewall group and two files. Until then the old
   `terraform-prov@pve` token and its `Administrator` grant stay. If a round or rebuild
   fails on permissions before this is tested, that role is the first place to look.
2. **Delete the old credential copies.** Deleting was blocked, and they are yours anyway:
   ```sh
   find opentofu -name 'credentials.auto.tfvars.disabled' -not -path '*/.terraform/*' -delete
   rm .env
   ```
   `.env` also holds that user's password, which nothing here uses. Its token stops working
   once the old token is removed.
3. **Test the dashboard from outside by name:** `curl -vk https://worms-dev.mlsec.tu-berlin.de/`.
   The IP now gets `400`.
4. **Other decisions in `docs/pi-failures.md`:** whether `make` should fill the caches, and
   the man pages fix.

### Not done

- **A build from scratch** (`make destroy_all`, then `make`) has not been run with these
  fixes. It is the only way to exercise the first-run paths on brand-new VMs.
- **Dashboard certificate:** it now has the right name, but browsers will still warn,
  because it comes from the internal CA. Trusted access needs a certificate from a public
  CA. Automatic renewal (ACME) would have to work with the VMs' internet closed.
- **DNS for live:** `worms.mlsec.tu-berlin.de` does not resolve yet.
- **The cause of the parallel slowdown** (finding 8) is unknown.
- **Parallelizing Ansible:** on hold, as you asked.
- **`no_log` hides migration errors:** `make` prints only "censored" when one fails.
- **`worms status` needs extra packages:** Django and proxmoxer are only in `.venv-test`.
  Use `.venv-test/bin/python src/manage.py status`.
- **`worms reset-database` help text is misleading:** it says "re-migrate", but it only
  empties the tables and keeps the migration history.

## Moving to production (`worms-pve`)

The name is read from one place, `proxmox_instance`, and flows into `proxmox.node`.

- **Do not commit the dev value** in `opentofu/shared/shared-variables.tf`. The repo's
  default should stay `worms-pve`. `TF_VAR_proxmox_instance=worms-dev` is the documented
  way to override it on this node.
- **Dashboard name** comes from `dashboard_public_names` in `group_vars/all.yml`,
  keyed on the node: `worms-pve` serves `worms.mlsec.tu-berlin.de`. A node not listed there
  fails the run. That name needs a DNS record before going live.
- **Proxmox TLS** works for either name: each node's certificate names that node and
  verifies against the same CA, and the CA is copied at deploy time.
- **`/etc/pve/nodes/worms-pve`** is a leftover of the rename. I left it in place. After
  renaming back, `worms-dev` will be the leftover. It previously made `worms status`
  fail, and the CLI now reads the name from the OpenTofu state instead.
- **Rebuild order (not tested):** OpenTofu's state and the VMs are tied to the node name,
  so `make destroy_all` should probably run before the rename, and `make` after it.
- **`tests/fixtures/platform-outputs.json`** matches again once the dev value is gone.

## Findings

### 1. Migration `0006` blocked itself (fixed, then replaced)

`AddField` locked `devbox_ssh_keys` in the migrator's open transaction, then `backfill` read
that table through the default connection. Each waited for the other, and PostgreSQL
cannot detect that as a deadlock because one side is waiting in the client. Commit
`52852d8` blamed the orchestrator's daemons, but a fresh database has none.

The tests did not catch it, because their PostgreSQL tests skip on this host. New test:
`test_data_migrations_use_the_migrating_connection`, which needs no PostgreSQL.

### 2. `Update all packages` looks hung (explained, not changed)

It started at 16:56:39, and you interrupted it at 17:11:49. `apt` finished on all 8 VMs at
17:12-17:13, since Ctrl-C does not stop `apt` inside the VMs. So after a Ctrl-C, an
immediate re-run can wait on the dpkg lock for up to 15 minutes.

### 3. Migrations replaced with one `0001_initial.py` (done)

Your suggestion, since there is no live database to upgrade. The models matched the old
chain exactly. `mydb` was dropped first: it held the default `admin` and `WORMS 2026` plus
generated records, with 0 teams, 0 uploads and 0 round runs. Tests for upgrading old
databases were removed.

### 4. The private internet stack (see `docs/pi-failures.md`)

13 problems. Three stopped `make` and are fixed; the rest are documented and unchanged.

### 5. The orchestrator could not reach Proxmox (fixed)

This was a known open item in `docs/accounts.md`. It did not trust PVE's CA, and PVE's
certificate does not name `10.11.0.1`, so the topology collector failed every time, and VM
power control would have too. `make` never reported it.

The orchestrator now gets a copy of PVE's CA and connects by the node's name, mapped to
that address in `/etc/hosts`. TLS verification stays on and the hypervisor is unchanged.
`power.py` previously ignored `PROXMOX_CA_BUNDLE`; both tokens use it now.

### 6. `zeek-shipper` could wedge permanently (fixed)

With no log files yet (normal when idle), any failed insert crashed `rewind()` before the
connection was closed. The aborted transaction then failed every retry until a restart.
Dropping the database triggered it, but any database hiccup would have too. Two new
tests cover it.

### 7. Dashboard published over HTTPS under one name (done)

- `host_publish_dashboard: true` forwards only TCP 443 arriving on `vmbr0`. You confirmed
  that from outside.
- `worms-dev` serves `worms-dev.mlsec.tu-berlin.de`, and `worms-pve` will serve
  `worms.mlsec.tu-berlin.de`. That name is on the certificate and is the only one accepted.
- `dashboard.worms.internal` was unused: it was never in DNS and nothing connects to it. It
  is gone from the certificate and the accepted names. It remains only as Ansible's label
  for the host, which is never resolved.
- Port 80 is closed: not forwarded, and nginx no longer listens on it.
- Client addresses stay visible, so per-address rate limits still work.
- Anyone who can reach the host's address can reach the dashboard. The admin login stays
  disabled until `worms admin reset-password` is run.

### 8. Package operations slow down sharply in parallel (cause not found)

An identical kernel unpack took 14 s on 1 VM and 72-91 s with 8 at once, so 8 VMs get
only about 1.5x the throughput. It is not dpkg's fsyncs: `--force-unsafe-io` made no
difference. The host has one SATA SSD (ZFS, about 360 fsync/s when quiet), and the pool has
never been trimmed (`autotrim=off`). Whether that matters is untested.

### 9. Smaller fixes

- `apt-listchanges.timer` is now masked. It downloaded changelogs on every VM at once
  and kept trying with the internet closed.
- `worms status` failed by asking for the stale node `worms-pve`. It now uses the OpenTofu
  state.
- `Simulate.yml` expected scoring and logging to read `access_tokens`, though the APIs no
  longer use it. It now checks that this is denied.

### 10. Tokens: one definition, one store, least privilege (done, one test pending)

| Token | Rights | Scope | Used by | Delivered to |
|---|---|---|---|---|
| `worms-provision@pve!worms` | `WormsProvision`, 20 privileges | `/` | OpenTofu, round agent | Round agent's `0600` env file |
| `orchestrator@pam!orchestrator-prov` | `WormsSandboxPower`: `VM.Audit`, `VM.PowerMgmt` | `/pool/sandbox` | Orchestrator (VM power) | Orchestrator's `0600` env file |
| `topology@pve!readonly` | `WormsTopology`: 4 read-only privileges | `/` | Orchestrator (topology), `worms status` | Orchestrator's `0600` env file |
| Orchestrator API token | One bearer token, for the devbox reset | Dashboard to orchestrator | Dashboard | Both VMs' `0600` env files |

- **Where values live:** only the vault. `worms secrets issue <name>` creates or rotates a token
  and stores it, `worms secrets check` verifies them, and `worms secrets set` stores one by
  hand. The definitions are in `src/worms_cli/tokens.py`.
- **What was wrong:**
  - The provisioning token's user had `Administrator` on `/`.
  - `credentials.auto.tfvars` silently overrode the vault, because OpenTofu prefers it to
    `TF_VAR_*`. `worms` now refuses to run if such a file exists.
  - The power token in the vault was wrong, so rounds could not have controlled VMs.
  - The docs described a `root@pam` token that did not exist, and a non-root token can do
    the job.
- **Verified:** the power token gets `403` for a VM outside the sandbox pool. The topology token
  sees the same as root in all 5 views. The provisioning token has no `Permissions.Modify`,
  `User.Modify`, `Sys.PowerMgmt` or `Sys.Console`.
- **Not tested:** the power token actually starting and stopping a sandbox VM, because none
  exists. The provisioning token creating anything (see Open items).
- **Known limit:** OpenTofu also logs in to the node as root over SSH, and the round agent runs as
  root on the node, so the token's limits do not bound what those can do there.

### 11. devpi and the registry are read-only (done)

Details and results are in `docs/pi-failures.md`, finding 8.

### 12. Dark theme only (done)

The light theme, the footer picker, its cookie, the `/theme/` route and the
`DASHBOARD_THEME` setting are gone. There is one stylesheet, `css/theme.css`. The topology page
followed the visitor's OS colour scheme on its own, and that is gone too, so a light-mode OS
changes nothing.

Tested in Chromium on the live site, with the OS set to light and to dark: the same dark
background, no picker, 0 JavaScript errors and 0 failed requests. The 33 browser tests pass.

### 13. Redeploying only the dashboard

`worms build provision -- --limit dashboard` takes about 4 minutes instead of 20. It used to skip
opening and closing the internet window when given a limit, because `Internet.yml` runs on the
node; that is fixed, with a test. `collectstatic` does not remove deleted files, so a removed
static file stays on an existing VM until it is deleted by hand.

The orchestrator VM still has the older copy of the dashboard code until the next full `make`. It
does not serve those pages.

### 14. Browser tests can run here

You installed Node with nvm (v26.13.0). Playwright is in `/root/node_modules`, outside the repo,
where the tests find it. Chromium is in `~/.cache/ms-playwright` (282 MB). Playwright's own
`install-deps` would add `xvfb`, an X server and fonts, so I installed only what Chromium needs: 9
small library packages. `apt` removed nothing and touched no Proxmox package.

The host has no emoji font, so 🔥 and ⭐ show as boxes in screenshots taken here. Normal browsers
are not affected.

## Ruled out

- **SSH channel held open by a background process:** the listchanges process logs to
  journald, not to Ansible's pipe.
- **`apt-listchanges` hook blocking dpkg:** 0-1 s wait.
- **dpkg fsync load:** see finding 8.
- **Disk or memory faults:** no OOM kills in the VMs, no hung-task or I/O errors on the
  host, 0 reallocated sectors.

## Traces of the investigation

- A timing test wrote to `/var/tmp/dpkg-ab` on 8 VMs. Removed.
- Registry upload tests and temporary Apache drop-ins on the private internet VMs. Removed.
- A few failed sign-ins for a nonexistent user on the dashboard, to test CSRF.
- `/etc/pve/priv/token.cfg` and the old credential files were read to compare tokens with the
  vault. No values were printed or stored.
- A truncated fragment of a devpi login token appeared in this session's output during the
  first audit. It came from the old empty-password root login and is not usable as printed.
