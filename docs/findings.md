# Findings, state and changes: network monitor, monitoring database, testing

Work on branch `claude/jolly-ride-5az76k` (based on `feature/auditd-wazuh`).
Details: [monitoring.md](monitoring.md), [testing.md](testing.md).

## What already existed (assessment)

| Building block | State found | Now |
|---|---|---|
| `opentofu/modules/network_monitor_vm` | Not deployed anywhere. No pool, no `ipfilter`/`macfilter`, the `security_groups` variable unused, the SSH rule commented out, so no way in. Only a management and a mirror NIC: **no path to the database**. The mirror NIC had the Proxmox firewall on with input policy DROP, which would drop every mirrored frame (they are addressed to other VMs). | Reworked like `infrastructure_vm` and deployed in the ephemeral stack: data NIC on ingest, management on mgmt-exposed, capture NIC on the mirror bridge without firewall and address. |
| Addresses (`shared-variables.tf`) | `monitor` already had `10.12.0.100` (ingest) and `10.5.0.100` (mgmt-exposed). | Used as is. |
| Mirror bridge `vmbr3` | Created by the platform stack, host has no address on it; host nftables drops it at the inet level. | Plus: bridge-level drop of everything entering it (receive-only). |
| Proxmox firewall | The `database` group already accepted `monitor`; `db_client` ("and later the monitor") reusable. | Used as is. |
| Host nftables | Already routed `monitor → database:5432`. | Used as is. |
| Database role | `dbuser_network_monitor` existed (pg_hba from `10.12.0.100/32`, vault password) with **no table privileges** (`[]`) and no tables. | Tables `network_flows`/`network_events`, INSERT-only grants, connection limit. |
| Mirroring (`helpers/monitor_network.sh`) | Manual. Mirrored the **bridge device**, which misses every frame forwarded between VMs (only broadcasts and host traffic pass the bridge device's qdisc), and mirrored **into `vmbr4`, which is now the sandbox management bridge**. | Replaced by the `worms-mirror` service on the host (per-port, automatic). The helper now exits with a warning. |
| cloud-init `vm-network-monitor` | Listed `zeek` (not in Debian 13: the whole package step would fail) and `tshark` twice. | Zeek comes from the Zeek project's OBS repository via Ansible. |

## Bugs found (and fixed)

1. **Scoring and logging could not write to the database.** The sandbox
   APIs insert only `(team_token_hash, vm_flag_hash | log_data)`, as their
   INSERT-only roles allow. But `processed`, `created_at` and the logging
   attempt's `uuid` only had **Python-side** Django defaults, no database
   defaults, so every INSERT failed with a NOT NULL violation (the API answers
   503). This matches the unchecked "Writing to DB" items in `TODO`. Found by
   `tests/test_database.py`. Fix: database defaults for `processed`/`created_at`
   (migration `0004`); the logging API generates the `uuid` and may insert
   that one column. `UUID4()` as a database default is not available on
   SQLite, which the DEBUG setup uses.
2. **`makemigrations` under the orchestrator settings planned to drop most
   tables.** `core/models/__init__.py` imported only two models; the rest were
   registered only through the dashboard's URLconf. Fix: it imports every
   model module.
3. **worms-mirror itself** (during development, caught by its test): the
   iproute2 keyword is `mcast_snooping`, not `multicast_snooping`.

## Open issues (not changed: outside this task)

- **The port-isolation hookscript is not attached to any VM.**
  `hook_script_file_id` is commented out ("TODO: HOOK") in
  `infrastructure_vm`, `private_internet_vm`, the sandbox VMs and sandbox-gw,
  so the README's "management bridges are port-isolated" does not hold. The
  isolation on the management bridges currently rests on the Proxmox firewall
  alone (output policy DROP, `ssh` group inbound only), and
  `worms simulate` checks that from a sandbox VM. Possibly because Proxmox lets only
  `root@pam` set hookscripts, not API tokens.
- The sandbox VMs do not trust the internal CA (README "Round start":
  "distribute the self-signed CA" is not implemented for the sandbox group).
  (Resolved: the sandbox guests now trust the internal CA; SetupSandbox.yml,
  run by the agent's prepare job.)
- `opentofu/stacks/sandbox/generated_vms.tf` is an old, fully commented-out
  topology using `10.5.x.0/24`, which overlaps mgmt-exposed. Harmless while
  commented; `generated_vms_01.tf` is not `tofu fmt`-clean (cosmetic).
  (Note: the round agent now overwrites `generated_vms.tf` and removes other
  `generated_vms*.tf` at prepare time, so these committed samples are replaced
  at runtime.)
- Processing of attempts: **resolved.** The scoring/logging servers record the
  running team's uuid directly (no access token; the agent writes
  `/etc/sandbox-api/current-team` at deploy), and a continuous daemon
  (`manage.py process_attempts`) turns attempts into `flag_captures` / `logging`
  rows and `captured` timeline events. See docs/rounds.md.
- Infrastructure VMs have no time synchronisation configured. Flow
  timestamps come from the monitor's clock, submission timestamps from the
  database's.
- Retention of `network_flows` (see monitoring.md, Limitations).

## Changes (by commit)

1. *Deploy the network monitor VM and mirror the sandbox traffic to it:*
   OpenTofu module + ephemeral stack, cloud-init output, `host_vars` in the
   inventory generator, `worms-mirror` + nftables, inventory groups, test
   fixtures, mirror tests.
2. *Monitoring schema, INSERT-only grants, zeek-shipper; fix attempt
   inserts:* Django models/migrations `0003`/`0004`, grants, `zeek_shipper.py`,
   the scoring/logging fix, database/API/shipper tests.
3. *Ansible role network_monitor:* Zeek + capture NIC + shipper + archive
   cleanup, `SetupNetworkMonitor.yml` in `worms build provision`, Ansible tests.
4. *Test suite and end-to-end simulation:* `Simulate.yml` + helper scripts,
   `worms test`, `worms simulate`, tofu/simulation tests.
5. *Documentation:* this file, monitoring.md, testing.md, README, TODO.

## How it was verified, and what was not

- Offline suite: 138 passed, 2 skipped. The skips are kernel features
  missing in the development container (nftables bridge family for `nft -c`,
  tc `gact` for the drop test).
- `tofu validate` of all six stacks, with OpenTofu 1.13.2 and bpg/proxmox
  0.118.0 built from source.
- The `worms-mirror` logic ran against real kernel bridges, veths and tc in
  network namespaces, with exactly-once delivery checked by tcpdump.
- **Not run on a Proxmox host** (none available): VM creation, the Proxmox
  side of the firewall, Zeek itself (its OBS repository was not reachable
  from the development environment), the systemd hardening in practice.
  First steps on the real host: `make` (or `worms build infrastructure`, `worms build host-network`,
  `worms build provision`), deploy a sandbox, `worms simulate`.

## Infrastructure visualization (dashboard topology view)

Rewritten from scratch: a fixed layout (Proxmox host / orchestrator /
dashboard, database, scoring / logging, devboxes / sandbox / private
internet), VMs with their Proxmox status, only operational connections
(database ingress read from its firewall; sandbox/devbox → services verified
against the firewall), hover cards with bridges, groups and rules, and a
round/team player that replays a run (dots for connections, icons for events,
click to pause and inspect, a seekable timeline). Details:
`docs/visualization.md`. Covered by `tests/test_infra_view.py`,
`tests/test_rounds.py` and, in a real browser, `tests/test_topology_page.py`.

Found while building it (devbox addressing, `scripts/generate_devbox.py`):

- team 0's devbox gateway was given `10.6.0.1/16` on the devbox maintenance
  bridge — the **Proxmox host's own address** there: an address conflict, and
  an untrusted gateway configured with the host's address. Gateways now use
  `10.6.i.2`; a test asserts no devbox address is a host / SSH-gateway address.
- the per-team SNAT source was taken from the `/27`'s *usable hosts*, skipping
  `.32`: team *i* got `10.2.0.(33+i)` while `provision_devboxes` records
  `10.2.0.(32+i)` (the documented rule), and teams 30/31 crashed. It is now
  `10.2.0.(32+i)` for every index.

## Early fail when a VM fails to set up

The deploy now stops as soon as any VM fails, instead of finishing the other
hosts first:

- **OpenTofu**: a failed `tofu apply` already exits non-zero; `make` stops
  before Ansible runs (the `infrastructure` target's EXIT trap only regenerates
  the inventory, it preserves the failure code). A VM that OpenTofu creates but
  that fails to boot / cloud-init is caught in the next step.
- **Ansible**: every setup play (`WaitForVMs` and the `configure` sequence, plus
  `SetupSandbox`) now carries `any_errors_fatal: true` — the repo's existing
  pattern (SetupDbAndDashboard), extended to all of them. `WaitForVMs` is the
  early gate: it waits for SSH + `cloud-init status --wait` on every
  infrastructure VM, so an unreachable VM or a failed cloud-init aborts the
  whole run before any configuration starts; a task failing on any host later
  aborts immediately rather than after the other hosts finish.

## Round orchestration (second task)

Added the round orchestration (`docs/rounds.md`): the conductor on the
orchestrator, the round agent on the Proxmox host, the data model, the game
plan, capture processing and the timeline export.

Bugs found and fixed:

- **`orchestrator/management/commands/deploy_worms.py` was dead code**: it
  imported `ProductionVirtualMachine` (does not exist), queried a
  non-existent `virtual_machine__round` relation, and ended in `assert False`
  with the real work behind `if False:`. Removed; the conductor replaces it.
- **The release-secret state bug** (caught by `test_rounds.py`): a game-plan
  step that requested a host job and moved to REQUESTED was never revisited,
  because the step loop only looked at PENDING steps, so a delayed secret was
  never applied. `_run_due_steps` now drains REQUESTED steps each tick.

Open issues (noted, not changed here):

- **`MEDIA_ROOT` is not configured.** `dashboard/views/uploads.py` saves
  artifacts through `default_storage` with no `MEDIA_ROOT`, so they land
  relative to the process's working directory. The round agent reads them
  from `round_agent_dashboard_media_root` (`/opt/dashboard/media`); the
  dashboard must be made to store them there (set `MEDIA_ROOT`) before deploy
  can fetch a worm. Until then the `deploy` job's artifact fetch fails.
- **The sandbox guests are not configured** beyond cloud-init: no CA
  distribution, and the worm start assumes the image has `python3`/`unzip`.
- The conductor runs **one run at a time** (one sandbox per host). Parallel
  sandboxes would need per-run bridge/pool namespacing.
- Still **not run on a real Proxmox host** (see the monitoring section): the
  tofu/qm/ssh paths of the agent are only validated offline.

## Accounts, management CLI and devbox (this work)

Three features added on top of the above; full design in `docs/accounts.md`,
`docs/management.md` and `docs/devbox.md`.

- **Accounts.** `manage.py init` (idempotent) creates the `admin` account
  (is_staff + superuser; password from the vault key
  `vault_django_admin_password`, delivered to the orchestrator and applied
  after the migrations) and the default competition `WORMS <year>`.
  `manage.py create_users` imports students from a CSV with one-time passwords;
  `must_change_password` (migration 0008/0010) + `ForcePasswordChangeMiddleware`
  force a change on first login. The public dashboard role is granted only
  `UPDATE(password, must_change_password)` on `users`.
- **Management CLI** (`src/manage.py`, `src/worms_cli`): status/teams/users/
  leaderboard/build, reusing the dashboard models and scoring (one source of
  truth). Building wraps the Makefile locally or over SSH.
- **Devbox**: team-personalised always-on sandbox clones, dev dashboard view,
  version-based reset, source-address scoring attribution, per-team SSH keys
  via a locked-down gateway, and strict devbox⇔sandbox isolation.

Bug found and fixed along the way: `orchestrator/rounds/captures.py` queried a
`UUIDField` with the attacker-controlled `team_uuid` directly, which raises on
a malformed value and could stall the attempt processor; it now drops
non-UUID values before the query (both the round and the devbox paths).

## Remaining security challenges (devbox and general)

Priority items a reviewer should weigh before a live deployment:

1. **devbox⇔sandbox on the shared services bridge.** Isolation rests on three
   independent layers (disjoint networks/bridges + default-drop host routing;
   a host nftables drop of gateway↔gateway on the services bridge; each
   gateway's Proxmox firewall accepting no inbound service connections). This
   is defense in depth, but it is the one place the two environments share a
   medium — worth a live packet-level check (extend `worms simulate`).
2. **SSH gateway confinement.** The `restrict,port-forwarding,permitopen=...`
   per-key options are the real boundary; verify on a live host that a team key
   can ONLY reach its own VMs and cannot open a shell, a second forward, or
   another team's VM. The gateway is trusted infrastructure and must stay
   patched; a gateway compromise would expose the devbox fabric.
3. **Reset races.** A devbox attempt in flight when a team resets is tagged
   with whichever version is current at processing time (best effort). Not a
   containment issue, but note it if dev scores must be exact across a reset.
4. **The port-isolation hookscript is still not attached** (pre-existing; see
   above). vmbr6/vmbr7 isolation and the mgmt-bridge isolation depend on it;
   until it is wired, that isolation rests on the Proxmox firewall alone.
5. **Root on the untrusted VMs.** Devbox and sandbox VMs are root by design;
   the gateway→VM and host→VM SSH is to `root`. Containment is the network and
   firewall, not in-guest privilege — as intended, but it means a guest escape
   to the hypervisor would be catastrophic (same risk the sandbox already has).
6. **Not run on a real Proxmox host.** As with the sandbox and rounds, the
   OpenTofu/qm/ssh/Ansible devbox paths are validated offline only
   (`tofu validate`, rendered nftables, the agent's command sequences and
   parameter validation). First live steps: `worms build devbox`, create a
   team, connect through the gateway, submit a token, reset.
7. **General, pre-existing**: remove remaining root logins, attach the
   hookscript, set `MEDIA_ROOT`, and run `worms simulate` on the real host.

## Team API (bearer token)

Teams can download their round scoring and logs as JSON (`docs/team_api.md`).
Found while building it: the dashboard's `EnvironmentTransitionMiddleware`
writes to the session on **every** request, so any anonymous request creates
a `django_session` row — a database-write amplifier for anyone on the
internet. The API paths now bypass the session middlewares (a test fails if a
call writes a session row); the rest of the site is unchanged. The dashboard's
nginx had no rate limiting at all; `/api/` now has per-address and per-token
limits (the HTML pages are still unlimited — see TODO).

## Environment audit (CLI, OpenTofu, Ansible)

Writing down every environment variable the tools read (`docs/accounts.md`
§4) turned up:

- **The round agent could not run OpenTofu.** It applies the sandbox and
  devbox stacks from a systemd service, whose env file had neither
  `TF_VAR_proxmox_api_url` nor `TF_VAR_proxmox_api_token` (both required) nor
  an ssh-agent (the provider uploads snippets over SSH with `agent = true`):
  every round's prepare and every devbox build would have failed at
  `tofu apply`. Fixed: the provisioning token is kept in the vault
  (`vault_tofu_proxmox_api_token`, from `TF_VAR_proxmox_api_token`), the env
  file carries the `TF_VAR_*`, and `ssh-agent` wraps the service with root's
  key (systemd kills anything `ExecStartPre` forks, so it cannot start there).
- **The orchestrator's power token had no rights.** `SetupRoundAgent.yml`
  granted the sandbox-pool role to the token only; a privilege-separated
  token gets the intersection of its own and its user's ACL. Now both are
  granted.
- **The quick start was incomplete**: it set only `PROXMOX_API_TOKEN` (the
  orchestrator's token), while every OpenTofu stack requires
  `TF_VAR_proxmox_api_url` / `TF_VAR_proxmox_api_token` (a different, root
  token) and an ssh-agent. `docs/accounts.md` §1.1 conflated the two tokens.
  Both fixed; §1.1 has the `pveum` commands for all three tokens.
- **CLI defaults**: `worms status` asked for `WORMS_SSH` even on the node
  (where the build steps run locally without it) — it now reads the internet
  flag directly there; the database CA defaulted to the system bundle, which
  cannot verify the internal CA's certificate — it now defaults to the
  checkout's `ansible/pki/worms/ca.crt`; a port in `WORMS_PROXMOX_URL` was
  dropped — it is now used.
- **Open**: the orchestrator verifies the Proxmox API certificate, but PVE's
  default certificate is signed by PVE's own CA and does not name
  `10.11.0.1`, the address the orchestrator uses. Issue the node's API
  certificate for that address (e.g. from the internal CA the VMs trust), or
  set `PROXMOX_CA_BUNDLE` / `PROXMOX_VERIFY_SSL` via `django_extra_env`.

## Dashboard login: "CSRF verification failed" behind a forwarded port

Reproduced over the real stack (nginx with the rendered `worms-tls` site,
gunicorn with the dashboard's production settings, PostgreSQL, Chromium):
reached through a forwarded port (an SSH tunnel, `https://<name>:8443`), the
login POST was refused with "CSRF verification failed". nginx forwarded
`Host $host`, which drops the port, while Django compares the browser's
`Origin` (`https://<name>:8443`) with `https://<Host>`. nginx now forwards
`$http_host`; the name is still validated by `ALLOWED_HOSTS`.

Right behind it: every page after a successful login answered 500, because
`Competition.is_registration_period()` compared `None` dates (the default
competition `manage.py init` creates has none) and the navigation calls it on
every page; the competition-state middleware had the same problem with a
round without dates. Both now treat missing dates as "no registration window"
/ "always active". The login also returns to the page that asked for it
(`?next=`, same site only) instead of always the profile.

`dashboard_public_hosts` (`group_vars/dashboard.yml`) adds the node's public
name to `ALLOWED_HOSTS` when the dashboard is published on the WAN;
`DJANGO_CSRF_TRUSTED_ORIGINS` covers a proxy in front that rewrites the Host.
Tests: `tests/test_dashboard_login.py`, `tests/test_ansible.py`.

Found with it: the CLI's `config/settings/manage.py` let a `DB_HOST` /
`DB_PORT` / `DB_NAME` / `DB_SSLMODE` / `DB_SSLROOTCERT` already in the
environment win over `WORMS_DB_*` (the opposite of its own comment), so a
shell with a service's `DB_*` variables (e.g. after sourcing an env file)
sent `worms` to another database. `WORMS_DB_*` now always win
(`tests/test_manage_cli.py`).
