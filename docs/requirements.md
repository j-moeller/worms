# Requirements → implementation map

This maps the platform's requirements to where they are implemented and how
they are verified, with residual gaps called out. The detailed docs:
`rounds.md`, `devbox.md`, `monitoring.md`, `accounts.md`, `management.md`,
`team_api.md`, `visualization.md`, `testing.md`, and the security review in
`security-assessment.md`.

Legend: ✅ implemented & covered offline · ⚠️ implemented, not yet exercised on
a live Proxmox host (the standing caveat — no node is available here).

## Database as the central dead end

- ✅ Ingress on 5432 only from the Proxmox host (RW), orchestrator, dashboard,
  scoring, logging, monitor — Proxmox security group `database`
  (`opentofu/stacks/firewall`), host nftables (`ingest → database` only), and
  `pg_hba` per-`/32` (`roles/database/setup_db_base`).
- ✅ Per-role least privilege (orchestrator read-mostly, dashboard read + its
  user writes, scoring/logging insert-only, monitor insert-only) —
  `roles/database/setup_db_permissions`, verified by `tests/test_database.py`.
  Full table in `accounts.md`.
- ✅ Dead end between the malicious VMs and the rest: sandbox/devbox reach only
  scoring/logging (which hold insert-only roles); nothing routes them to the
  database. The monitor and scoring/logging are the only bridge, and they can
  only INSERT.

## Dashboard

- ✅ Login; forced password change on first login — `dashboard/views/password.py`,
  `ForcePasswordChangeMiddleware` (`accounts.md`).
- ✅ Team registration window — `dashboard/views/teams.py`, the registration
  state in `dashboard/middleware.py`.
- ✅ Dev/prod toggle — `EnvironmentTransitionMiddleware`; the home view
  dispatches (`dashboard/views/devbox.py`).
- **Dev view**: ✅ devbox score (tokens found at the current version), devbox
  logs, SSH-key management, reset (version bump + rebuild job), and the SSH
  command to the main VM through the gateway (`devbox.md`).
- **Prod view**: ✅ leaderboard (overall points, and per round every team
  ranked by the VMs it captured, with its sandbox's size), zip upload (a
  team's uploads are its own, `Upload.team`; a zip must have `main.py` at its
  top level), round countdown/status (`dashboard/views/leaderboard.py`,
  `uploads.py`, the status presentation in `views/util.py`,
  `tests/test_leaderboard.py`).
- **Round progression**: ✅ a team replays its rounds (Live: its run; Dev: its
  devbox) on the PROGRESSION page — only the VMs it captured, scoring,
  logging and the private internet; never another team's devbox, other
  sandbox VMs, infrastructure, firewall internals or secrets — and downloads
  them (one format: JSON, or a zip with optional log records and traffic;
  unfinished rounds included). The same format feeds the staff replay
  (`orchestrator/rounds/progression.py`, `dashboard/views/progression.py`,
  `docs/progression.md`, `tests/test_progression.py`).
- **Team API**: ✅ teams download their round scoring, logs and progression
  (JSON, zip) with an access token (bearer), with item/byte/time/rate limits
  (`dashboard/api.py`, `docs/team_api.md`, `tests/test_team_api.py`).
- **Staff administration**: ✅ the registration window and the development
  competition, student accounts (single or CSV, one-time passwords shown
  once, reset, deactivation, team), teams and members, round definitions
  (window, seed, sandbox size, game plan, timing; schedule check) and their
  runs/host jobs — database records only, with column-scoped grants; staff
  accounts and all infrastructure stay on the console
  (`dashboard/views/staff*.py`, `docs/accounts.md` §3, `tests/test_staff_admin.py`).
- **Admin**: ✅ leaderboard + infrastructure/sandbox topology (VMs as nodes
  with their status, firewall groups/rules/bridges on hover, the operational
  connections) and a round/team replay of every run — the TOPOLOGY view,
  staff-only (`dashboard/infra_view.py`, `orchestrator/rounds/timeline.py`,
  `templates/topology/`, `docs/visualization.md`), fed by
  `manage.py collect_topology` (read-only Proxmox token). Covered in a real
  browser by `tests/test_topology_page.py`.

## Sandbox and devbox (malicious, isolated, hardened)

- ✅ Assumed rooted; isolated at the network layer and resource-limited —
  per-VM Proxmox firewall (input/output DROP + ipfilter), host nftables, own
  storage and CPU/NIC/disk caps (`docs/rounds.md` "Containment", `docs/devbox.md`).
- ✅ Sandbox VMs reach only: each other (generated networks), scoring, logging,
  private internet — groups `sandbox`/`sandbox_gw`, `sandbox_api`.
- ✅ Devbox: three VMs A–B, B–C per team; reach scoring/logging/private
  internet; never other teams' devboxes or the sandbox — `docs/devbox.md`
  (disjoint networks, default-drop host routing, gateway↔gateway drop on the
  shared services bridge). ⚠️ not run on a live node.
- ✅ Private internet is a dead end: serves sandbox/devbox, **no egress** except
  each PI VM's own package installs through the apt-cache (narrow
  `pi_apt_client`; the previously over-broad `services_client` was removed from
  the PI VMs — see below).

### The `services_client` question

`services_client` granted egress to the **whole** services subnet on every
service port. That was two things at once: the sandbox/devbox **gateways**
legitimately need egress to the services, but the **private-internet VMs** had
it too, contradicting "PI is a dead end". Fixed: the gateways keep a
`services_client` scoped to the **actual service hosts** (not the whole /24,
and gateway↔gateway is dropped at the host anyway); the PI VMs get a narrow
`pi_apt_client` (apt-cache:3142 only). PI is now a true dead end.

## Management networks

- ✅ Separated: sandbox on `vmbr4`, devbox on `vmbr6` — **not** shared with the
  critical infrastructure (database/orchestrator on `vmbr2`; dashboard/scoring/
  logging/… on `vmbr5`). Addressing in `opentofu/shared/shared-variables.tf`.
- ✅ Ingress only SSH from the Proxmox host (`ssh` security group, source = the
  host's address on that bridge); egress forbidden (every VM's output policy is
  DROP and the management NIC carries no outbound rule). Management bridges are
  also port-isolated (hookscript) so VMs there cannot reach each other.

## Minimal privilege, SSH key-only

- ✅ cloud-init: root login via the Proxmox host key only, no passwords, no
  other method — `10-worms-hardening.conf` + `ssh_pwauth: false` in every VM
  template (`opentofu/modules/cloud_init/templates/*`, the sandbox/devbox
  templates). The login user is `debian` (key) + `sudo`.
- ✅ Services run as dedicated non-root users wherever possible (`accounts.md`).

## Monitor

- ✅ One monitor VM records all sandbox traffic to the database via a
  host-side mirror (`worms-mirror`) into a receive-only capture NIC in
  promiscuous mode; Zeek + the shipper write `network_flows`/`network_events`
  (`docs/monitoring.md`). Devbox bridges are not mirrored (unless added to
  `host_mirror_extra_bridges`), so a Dev round's progression has no traffic
  (`docs/progression.md`).

## Residual gaps / to verify on a live host

- ⚠️ Nothing has run on a real Proxmox node (no node here): VM/bridge creation,
  the devbox `-replace` rebuild, the devbox host-job → DB reconciliation, the
  SSH gateway's `permitopen` confinement in practice, and `worms simulate`.
- The port-isolation **hookscript is wired through every VM module but left
  commented** (`hook_script_file_id`): attaching it needs `root@pam`, not the
  provisioning API token, so until it is enabled management-bridge isolation
  rests on the Proxmox firewall alone (`docs/security-assessment.md`, S4).
- A full live round and a full devbox lifecycle are covered only offline
  (fakes + a real PostgreSQL); see `testing.md`.
