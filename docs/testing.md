# Testing

Two layers:

| | `worms test` | `worms simulate` |
|---|---|---|
| Runs | anywhere (laptop, CI, the Proxmox host) | on the Proxmox host, against the deployed platform |
| Needs | Python 3; optionally PostgreSQL server binaries, `tofu` (+ provider), `ip`/`tc`/`tcpdump` and root, `nft`, `systemd-analyze` | a provisioned platform; a deployed sandbox for the traffic part |
| Covers | configuration, code, privileges, mirror logic | the running system end to end: features, configuration, containment |
| Changes things | nothing outside temp dirs and private network namespaces | creates a test team/token, removes it afterwards |

## `worms test`: offline suite

`worms test` creates `.venv-test` from `tests/requirements.txt` on first use
and runs `pytest` (`worms test -k shipper -x` to narrow it down). A test
whose tool is missing is **skipped with the reason** (`-ra` summary), so the
suite runs everywhere and does as much as it can. On the development machine:
138 passed, 2 skipped (its kernel lacks the nftables bridge family and the tc
`gact` action).

| File | What |
|---|---|
| `test_database.py` | A throwaway PostgreSQL with the **real migrations** (as `django_migrator`), the roles from `group_vars/all.yml` and the **rendered `privileges.sql.j2`**. Permission matrix: scoring/logging/monitor can append to exactly their table and read nothing they wrote; no role can create objects; the dashboard/orchestrator can read the monitoring tables; ON CONFLICT DO NOTHING works without SELECT, upserts are refused; grants re-apply idempotently. |
| `test_sandbox_api.py` | The scoring and logging APIs (`app.py`, Flask test client) against that database as their INSERT-only roles: valid submissions are stored; invalid/revoked tokens, oversized/deep/odd payloads, wrong methods are refused before the database; team rate limit; database down. Uses the same test-team SQL as the simulation. |
| `test_zeek_shipper.py` | Parsing of Zeek JSON (realistic conn/dns records, hostile values: bad addresses, huge numbers, NUL bytes, forged timestamps, SQL in fields); size limits; following logs across rotation and restarts; rewind after a failed insert; end to end into the database: no duplicates after a restart (even with lost state), nothing lost in a database outage, throttling without dropping, heartbeat. |
| `test_worms_mirror.py` | The rendered `worms-mirror` in **network namespaces** (a miniature host: sandbox bridge, two VMs, a sandbox-gw, the mirror bridge with the monitor): each frame copied exactly once (tcpdump on the "monitor"), router ports copy only services replies, Proxmox' rate-limit filter still runs, re-plugged ports are repaired, the daemon picks up new ports, the monitor's port is receive-only. Root only; uses unique namespace names, so it is safe on the Proxmox host. |
| `test_ansible.py` | `--syntax-check` of every playbook; rendered templates: host nftables (`nft -c`), shell scripts (`bash -n`, shellcheck), systemd units (`systemd-analyze verify`), Zeek config and unit hardening; inventory groups (monitor in audit/tls/network_monitor groups); provisioning order. |
| `test_tofu.py` | `tofu fmt`; `tofu validate` of every stack (needs the provider, see below); the addressing fixture matches `shared-variables.tf`; cross-layer consistency (every database client allowed by the Proxmox security group, pg_hba and host routing; the monitor's capture NIC unfiltered and address-less). |
| `test_simulation.py` | A rehearsal of `Simulate.yml` with localhost as the Proxmox host and database (the throwaway PostgreSQL): the test team is created and removed, missing platform parts are reported as FAIL (not a crash); `verify.sql` recognises a successful run. |

`tests/fixtures/platform-outputs.json` is the platform stack's
`infrastructure` output, computed by tofu from
`opentofu/shared/shared-variables.tf`. `test_tofu.py` regenerates it and
fails when it is stale. To update it, copy `shared-variables.tf` and the
`infrastructure` output into an empty directory, then run `tofu init && tofu
apply -var proxmox_api_url=x -var proxmox_api_token=x && tofu output -json >
tests/fixtures/platform-outputs.json`.

`tofu validate` needs the `bpg/proxmox` provider. Without registry access,
set `TF_CLI_CONFIG_FILE` to a config with a `filesystem_mirror` that holds
the provider binary.

## `worms simulate`: end-to-end simulation

```
# deploy a sandbox first (ideally >= 2 VMs), then:
worms simulate                                 # = inventory + ansible-playbook Simulate.yml
worms simulate -- -e sim_wait_seconds=600      # wait longer for the records
```

The playbook plays a sandbox VM (`groups.sandbox[0]`, peer
`groups.sandbox[1]`). Each check is recorded as PASS or FAIL. At the end
there is one report, and the run fails if any check failed:

```
PASS  proxmox.local  host: worms-mirror covers every port  [...]
PASS  sandbox-vm01   sandbox -> scoring: flag submission accepted (201, TLS verified)
PASS  database...    scoring: flag stored in flag_capture_attempts (exactly once)
PASS  database...    monitor: UDP flow recorded once, 5 packets / 500 bytes / 640 IP bytes
FAIL  ...
```

What it checks:

1. **Host:** proxmox-firewall, the host firewall, the firewall watchdog,
   worms-mirror and the **round agent** (`worms-round-agent`) are active;
   every sandbox port is mirrored and the monitor's is receive-only
   (`worms-mirror status`); nftables drops what enters the mirror bridge.
2. **Services:** units on scoring, logging, monitor and database (incl.
   auditd/rsyslog); the monitor's capture NIC is promiscuous without address;
   the **conductor daemon** (`orchestrator-conduct-rounds`) is active.
3. **Scoring and logging (sandbox → API → database → scoring):** from the
   sandbox VM, DNS resolves `scoring.worms.internal`; a flag and a log are
   submitted over HTTPS **with certificate verification** through sandbox-gw
   (201); refused requests: invalid token 401, oversized body 413 (nginx),
   GET 403, other path 404. In the database the flag and the log are stored
   **exactly once**, and — because scoring is now capture-based — the flag is
   **scored**: `manage.py process_captures` on the orchestrator turns the
   attempt into a `FlagCapture` of the run's VM (once) and a `captured` event
   on the run's timeline, and the log is attributed to the team's run. The run
   setup for this (a finished `RoundRun` + a VM whose released secret is the
   submitted flag) is created by `files/simulation/setup.sql` and removed by
   `cleanup.sql`.
4. **Monitor (traffic → Zeek → database):** the sandbox VM makes traffic
   with known properties to the peer VM on random ports (so earlier runs never
   match): HTTP on a high port, which must come out as `service = http` (detected
   from the payload) and exactly one flow row; 5 UDP datagrams of 100 bytes,
   which must give exactly one flow with 5 packets / 500 payload bytes / 640 IP
   bytes (no copy is missed or duplicated); a connection to a closed port
   (`REJ`). Also the API connection, recorded with the **VM's own address**
   (copied before SNAT) and `service = ssl`; the DNS query and the HTTP
   request in `network_events`; the shipper and Zeek heartbeats younger than
   3/5 minutes.
5. **Containment:**
   - sandbox VM: cannot reach the database, dashboard, orchestrator, wazuh,
     the monitor, the Proxmox API/host, sandbox-gw's management address,
     non-API ports of the services, the internet;
   - monitor: reaches only the database and wazuh;
   - scoring/logging: reach only the database;
   - the database roles, tested **from their own VMs with their own
     credentials over verified TLS**: monitor INSERT-only, cannot read or
     modify anything; scoring/logging only read token hashes, cannot read or
     modify attempts or other tables;
   - frames the monitor sends on its capture NIC are dropped by the host
     (tc drop counter).
6. **Cleanup:** the test team, token and everything submitted with it are
   deleted (checked). The recorded flows stay: they are real observations,
   on random high ports.

Zeek writes a connection when it ends, and UDP "connections" end after a
minute of silence. So the database checks are polled for up to
`sim_wait_seconds` (default 240).

Without a sandbox, steps 3 and 4 and the sandbox containment are reported as
FAIL ("sandbox deployed"); the other checks still run.

## Not covered (yet)

- The topology collector against a real Proxmox API. Dashboard workflows
  are covered by the isolated rehearsal below and the dashboard-specific tests.
- A full live round driven by the conductor + agent on a real Proxmox host
  (the simulation checks capture scoring with an injected finished run, not a
  sandbox the conductor built; the lifecycle itself is covered offline in
  `test_rounds.py`/`test_round_agent.py`).
- Wazuh: whether the audit logs arrive (only that auditd/rsyslog run).
- Load: flood behaviour of the APIs and the monitor (the limits are
  unit-tested, not stress-tested).
- The private-internet services.

## Round orchestration tests

Two more files, both offline (`worms test`), covering the round orchestration
(`docs/rounds.md`):

| File | What |
|---|---|
| `test_rounds.py` | The game plan generator (deterministic; every secret released once; a delayed secret always follows a stop; `min_alive` respected). The conductor driven end to end against the test database with a **fake Proxmox** (records stop/start) and a **fake agent** (completes host jobs as the real one would): a full run reaches FINISHED with every stop and secret done; a released secret submitted by the right team during the run becomes a capture (once); the wrong team's token does not; a failed prepare fails the run without a teardown; a team with no upload is SKIPPED; the timeline export has the nodes, ordered events, captures and each VM's power state over time; flows are mapped to scoring / the private internet, flag submissions and logs become events attributed to the sending VM; the TOPOLOGY endpoints are staff only. This caught a real conductor bug (a `release_secret` step left in REQUESTED was never revisited). |
| `test_team_api.py` | The team API through the dashboard's real URLs and middleware against PostgreSQL: bearer authentication (missing, malformed, unknown, revoked, token in the URL), team and competition scoping, scoring results, keyset paging (complete across identical timestamps), item and byte limits, bad parameters, the statement timeout, and that no session row is written (`docs/team_api.md`). |
| `test_infra_view.py` | The TOPOLOGY view's infrastructure model on a realistic Proxmox snapshot: roles/status, no management network drawn, database ingress read from its firewall, devbox links never crossing teams, verified service edges (`docs/visualization.md`). |
| `test_topology_page.py` | The round player in Chromium (Node Playwright) with fixture data. TOPOLOGY (staff): layout, hover card, tabs, playback, event badges and dots (click pauses), VM state over time, lazily fetched connections and log records (an XSS payload shown as text). Files: download, offline viewer, lossless round trip, the server's zip, the earlier format, broken, crafted and hostile files. PROGRESSION (a team): no infrastructure or firewall anywhere, VMs faint until captured, a Dev round without traffic, the zip link, a running round refreshed in place. Skipped without Node Playwright. |
| `test_round_agent.py`, `test_orchestrator_api.py`, `test_dashboard_frontend.py` | Devbox SSH keys end to end: the validators reject template/shell/quote characters in a key comment (template-injection guard) and a key blob shared by two teams; the agent writes the key var file tagged `!unsafe` (no `{{ }}` evaluated); a reset enqueues a rebuild carrying the full teams spec and that spec passes the agent validator; add/remove a key and leaving a team enqueue a `devbox_reconfigure` and revoke the member's keys. |
| `test_api_roundtrip.py` | Write, then read back through the team API: a seeded generator (3 seeds) writes rounds, runs, VMs, events, submissions, captures, logs, flows and devboxes for three teams, with deliberate noise (another team's run on the same addresses, records outside a run's window, devbox records inside it, VMs never captured, traffic to infrastructure and unknown addresses, another competition), and keeps a ledger. The expected answer of every endpoint (rounds, scoring and logging fully paged, progression, flows, each log record, the zip, the page's own endpoint) is derived from the ledger alone and compared exactly: same items, values and keys, nothing of anyone else. Mutation-checked: naming an uncaptured VM, keeping traffic to one, serving another team's log record or logs, ignoring the Dev round's start, showing run errors -- each fails it. |
| `test_progression.py` | The round progression (`docs/progression.md`) against PostgreSQL: what a team sees and never sees (uncaptured VMs, infrastructure, other teams, internals, secrets), staff seeing everything, Dev windows, the clock of running and finished rounds, log-record scoping, the zip, the team API's, the PROGRESSION page's and the staff endpoints. |
| `test_round_agent.py` | The agent's parameter validation (rejects path traversal, bad hashes, out-of-range numbers); each handler's command sequence with a **fake executor** (no Proxmox/SSH): prepare builds and reports the sandbox, check flags a down VM, release writes the flag over stdin and returns its hash, deploy fetches + verifies + copies the worm and arms the fuse (and rejects a tampered artifact), teardown destroys. The claim/run/result loop against the test database, including that a job is claimed once even with two workers (FOR UPDATE SKIP LOCKED). |

The conductor and agent are exercised against a real PostgreSQL with the real
migrations and grants, but no sandbox is built on a live Proxmox node (none is
available here), so the actual tofu/qm/ssh calls are only checked at the
"which commands run" level.

## Isolated dashboard rehearsal

Use Python 3.12+, PostgreSQL server binaries (`initdb`, `pg_ctl`, `pg_config`)
and OpenSSL on PATH. The test requirements include Ansible and its PostgreSQL
collection. No Docker, vault, production database or Proxmox access is needed.

```sh
python3 -m venv .venv-test
.venv-test/bin/pip install -r tests/requirements.txt
PATH="$PWD/.venv-test/bin:$PATH" .venv-test/bin/python scripts/dashboard_e2e.py
```

On Homebrew, add `$(brew --prefix postgresql@17)/bin` to PATH if those binaries
are not already available. The script prints the local URL and a generated
admin password, also saved in its private temporary `session.json`. It binds
only to loopback: HTTP on localhost:28180 for browser testing and HTTPS on
localhost:28181 for clients that explicitly trust the printed certificate.
The certificate is never installed into the system trust store. The server
uses production middleware, Secure cookies and the restricted
`django_dashboard` database role; migrations and grants match the deployed
configuration. Open the HTTP URL as `localhost` (browsers treat Secure cookies
on localhost specially). Ctrl-C removes the temporary PostgreSQL cluster.
Media and session metadata remain in the printed work directory. This is a
local test server, not a deployment server.

The repeatable journey starts its own isolated instance on random ports:

```sh
PATH="$PWD/.venv-test/bin:$PATH" .venv-test/bin/python -m pytest -q \
  tests/test_dashboard_e2e.py tests/test_enrollment_concurrency.py
```

For the real browser replay/download check, install Node Playwright and its
Chromium browser (as for `test_topology_page.py`). `NODE_PATH` may point at an
installation outside the repository; `WORMS_SCREENSHOTS` selects where browser
screenshots are saved. The browser test opens the running server, reads the
stored log, downloads the ZIP, checks student/staff access and records
JavaScript and HTTP errors. The HTTPS journey still runs without Playwright.

It covers staff account/CSV creation, student first login and password change,
team join/leave and staff assignments, valid/invalid uploads, round creation,
log/score HTTP ingestion as the INSERT-only service roles, processing as the
orchestrator role, team/staff replay and ZIP downloads, cross-team refusals,
CSRF/origin rejection, deactivation and password reset. Run/VM records are
fixtures; no conductor or node agent runs and no sandbox is built. The
concurrency regressions synchronize real PostgreSQL requests to check duplicate
accounts/team names, the last team place, simultaneous assignments, resizing,
and committing membership changes before SSH keys are re-pushed.

The case-insensitive uniqueness migration preserves existing spellings. Before
applying it to an existing deployment, check for previously allowed collisions:

```sql
SELECT lower(username), count(*) FROM users GROUP BY lower(username) HAVING count(*) > 1;
SELECT competition_id, lower(name), count(*) FROM teams
GROUP BY competition_id, lower(name) HAVING count(*) > 1;
```

Any collisions must be resolved by the operator first. The migration does not
merge or delete accounts or teams automatically.

`test_dashboard_races.py` additionally exercises identical uploads from two
teams, missing/corrupt media recovery, disk-write interruption, simultaneous
SSH-key registration, membership removal during registration, canonical key
identity, and the agent's key-count/encoding limits. Orchestrator API tests
check that queued reconfiguration jobs receive current keys and that changes
during a running job get a follow-up. Deployment must apply the database
permission role too: the orchestrator now needs column-scoped `UPDATE (params)`
on `host_jobs` to refresh an unclaimed job; it still cannot change job state.

Key updates must also follow any queued rebuild/provision that carries an older
key snapshot. The API regressions cover that ordering and concurrent snapshot
creation: all three producers share a transaction lock, and an older queued
key update is not reused across a later full-stack job. Otherwise a rebuild
could reinstall a key that a previous update had just revoked.

## Linux stack and failure recovery

The container includes PostgreSQL, nginx, Gunicorn, Chromium, Firefox, WebKit,
OpenTofu and the Linux network-test tools. Build it with the `tests` directory
as its context; it copies requirements/tools, never the application's secrets.

```sh
docker build -f tests/docker/Dockerfile -t worms-dashboard-e2e:local tests
# Preserve access to history when this checkout is a Git worktree:
git_common=$(git rev-parse --path-format=absolute --git-common-dir)
docker run --rm --privileged --shm-size=1g --memory=3g \
  --mount "type=bind,source=$PWD,target=/repo,readonly" \
  --mount "type=bind,source=$git_common,target=$git_common,readonly" \
  worms-dashboard-e2e:local
```

Select the `colima` Docker context explicitly if Docker Desktop is inactive
(`docker --context colima …`); no global context change is necessary. Privilege
is needed only for the separate host-network tests' private network namespaces.
The dashboard stack tests themselves work without `--privileged` and can be
selected with `python -m pytest -q tests/test_linux_stack.py` after the image
name. Code is mounted read-only and no live infrastructure is contacted.

`scripts/dashboard_linux.py --work-dir /tmp/fresh-stack` starts the stack for
interactive inspection inside that image. It runs the rendered deployment
nginx template, three dashboard Gunicorn workers, a separate orchestrator API,
and the real continuous attempt processor against disposable PostgreSQL with
service grants. Only local paths, ports and test identities are substituted;
there is no conductor or Proxmox agent. Container processes use the invoking
user; this does not reproduce the production systemd sandbox. Stop it with
Ctrl-C to remove the database. For browser access publish only loopback ports,
e.g. `-p 127.0.0.1:28190:28190 -p 127.0.0.1:28191:28191`.

The stack tests cover verified HTTPS/CSRF through nginx, forged headers,
static files, body limits, dashboard-to-orchestrator certificate trust,
three-browser log/replay/download flows, twelve concurrent student first
logins competing for three team places, killing/replacing a worker while
retaining sessions, and stopping/restarting PostgreSQL while submissions are
processed. Browser contexts permit the self-signed fixture server; independent HTTPS
clients verify its certificate and reject an untrusted one.

Concurrent functional tests use a higher **test-only** sign-in rate so they
exercise the application rather than the throttle. A separate check restores
and verifies production's **10 attempts/minute per source IP**, plus the API
limits. Consequently a class behind one NAT address can hit that sign-in limit;
the concurrency result is not a production capacity guarantee for shared IPs.

The populated-upgrade tests preserve password hashes, memberships, uploads and
logs, and prove that a case collision fails atomically without deleting data.
Daemon tests also exercise reconnecting a closed database session. Staff
configuration and planning race tests protect the single development
environment, non-overlapping schedule and parameters of already-planned runs.


## Deployment regressions from the read-only audit

`tests/test_deployment_connectivity.py` runs only in a disposable privileged
Linux container. It executes the Django connectivity tasks against fixture
files, verifies a second run changes nothing, builds a private CA trust store,
and checks the real dashboard and both Proxmox clients over HTTPS. The fixture
hostnames are resolved through a private mount of `/etc/hosts`; wrong names and
untrusted certificates are rejected. No live host, token or API is used.

The Zeek regression reproduces the old rotation failure with separate writable
bind mounts, then checks repeated atomic moves with the rendered unit's shared
writable data directory. This tests the Linux mount behavior that caused EXDEV;
it does not replace a live Zeek capture/rotation rehearsal.

Deployment must run the updated `django_app` and `network_monitor` roles:

- Dashboard installs `requests` and resolves `orchestrator.worms.internal` to
  its management IP in a managed `/etc/hosts` block. Its HTTPS URL uses that
  hostname, which the certificate covers.
- Orchestrator resolves the Proxmox node name locally and receives the public
  cluster CA via the authenticated Ansible connection to `proxmox_host`.
  Both topology collection and power control use the system CA bundle by
  default. Explicit CA environment overrides remain supported. With a custom
  Proxmox certificate, configure `django_proxmox_ca_source` and the API hostname
  for the certificate's issuer and SANs rather than disabling verification.
  Application/env/CA changes restart its management daemons as well as the web
  service, so the conductor picks up the new hostname and trust configuration.
- Zeek has one writable `network_monitor_data_dir` containing both current and
  archived logs, so systemd does not put a mount boundary between them. Both
  directories must also reside on the same underlying filesystem. Restart Zeek
  when applying the unit change and confirm stats/capture-loss timestamps keep
  advancing after rotation.

The audit also found an unconfigured Wikipedia VM and historical `apt-daily`
OOM failures. These are operational follow-ups: download the configured ZIM
with `wikipedia-download` before starting `kiwix-serve`, and validate the
first-boot package policy on a fresh VM. The role deliberately does not start
Wikipedia before its dataset is available. This branch includes Jonas's
explicit cloud-init `package_update: false` / `package_upgrade: false` change;
it does not erase failed-unit history or claim a fresh-VM boot was tested.
