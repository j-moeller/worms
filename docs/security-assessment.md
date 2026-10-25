# Security assessment and hardening

Two threat models, assessed against the platform (OpenTofu + Ansible +
Django/Python on Proxmox):

1. **Malicious software is root on a sandbox or devbox VM** and attacks the
   shared infrastructure it can reach.
2. **An attacker with an ordinary dashboard account** (a team member, no
   secrets) attacks the dashboard.

Both attackers know the whole architecture; neither knows a secret (API tokens,
passwords, keys). This document records the findings and the fixes made in
response. IDs (`S*`, `D*`, `C*`) are referenced from code comments.

## Summary

Network containment is strong in both models: a rooted sandbox/devbox VM
reaches neither the Proxmox host nor the database, dashboard or orchestrator,
and the dashboard has no XSS/CSRF/IDOR/privilege-escalation path that review
found. The weak areas were **availability** (one shared database disk; a few
unauthenticated or unbounded write paths), **defense-in-depth gaps** where a
control was documented but not actually deployed (port isolation; the firewall
watchdog ignored devboxes), and a handful of **integrity** gaps in
scoring/logging attribution and the round lifecycle.

## What was fixed

| ID | Area | Fix | Where |
|---|---|---|---|
| S1/D1 | Availability: shared DB disk | Database disk raised to 100 GB | `opentofu/stacks/infrastructure/persistent/main.tf` |
| D1 | Availability | Anonymous requests no longer create `django_session` rows | `projects/dashboard/dashboard/middleware.py` |
| S4 | Defense in depth | Port-isolation hookscript made **available on every VM module/stack** (still commented — it needs `root@pam`, which the provisioning API token is not); the firewall watchdog now also hard-stops uncontained **devbox** VMs | `opentofu/modules/{devbox_vm,infrastructure_vm,private_internet_vm}`, `opentofu/stacks/{sandbox,devbox}`, `ansible/roles/host/host_network/templates/worms-firewall-watchdog.sh.j2` |
| devbox isolation | SSH gateway | Teams reach **one** entry VM (A) per team; gateway `authorized_keys` is root-owned (a team cannot widen its own confinement); gateway accepts SSH only from the host's SNAT source, so a devbox VM on the shared fabric cannot attack it | `scripts/round_agent/handlers.py`, `ansible/roles/applications/ssh_gateway/*`, `opentofu/stacks/firewall/main.tf`, `ansible/roles/host/host_network/templates/worms.nft.j2`, `opentofu/stacks/devbox/main.tf` |
| sandbox/devbox egress | Firewall | Sandbox/devbox VMs and their gateways may reach **only the service hosts** (scoring, logging, DNS, private internet) on the services bridge, never another gateway's SNAT address | `opentofu/stacks/firewall/main.tf` |
| S5 | Integrity/availability | Scoring/logging attribution is **fail closed** (only the sandbox-gw source maps to the running round; every other unknown source is refused); nginx limits are keyed per source so a devbox cannot spend the round's budget | `ansible/roles/applications/sandbox_api/*` |
| S6 | Devbox isolation | The internet window is refused while **any** sandbox *or* devbox VM exists | `ansible/roles/host/host_network/tasks/internet.yml` |
| S7 | Round integrity | The next run does not build until the previous run's teardown has completed | `projects/dashboard/orchestrator/rounds/conductor.py` |
| S8 | Availability | dnsmasq query log is bounded by logrotate | `ansible/roles/dns/setup_dns/tasks/main.yml` |
| S14 | Confidentiality | The Wazuh install bundle (all generated passwords) is pulled to the controller and shredded from the log-parsing VM | `ansible/roles/audit/wazuh_server/*` |
| D2 | Availability | A devbox reset does not pile up rebuilds (one queued/running per team) | `projects/dashboard/orchestrator/api.py` |
| D6 | Integrity (RCE) | A registered SSH key is no longer evaluated as a Jinja template on the host: strict comment validation on both sides, plus `!unsafe` in the agent's var file | `projects/dashboard/dashboard/views/devbox.py`, `scripts/round_agent/validation.py`, `scripts/round_agent/handlers.py` |
| D7 | Isolation/integrity | A key blob is unique across teams (fail-closed in the agent); adding/removing a key or leaving a team re-pushes keys (`devbox_reconfigure`); reset now carries the teams spec so the rebuild actually installs keys | `projects/dashboard/dashboard/views/{devbox,teams}.py`, `projects/dashboard/orchestrator/{api.py,rounds/devbox.py}`, `scripts/round_agent/*` |

### The two isolation rules (as requested)

- **A round's sandbox VMs** may talk only to other sandbox VMs (the
  experiment) and to the service hosts — scoring, logging, DNS, private
  internet — via sandbox-gw. The per-VM security group's services egress is now
  scoped to those hosts (`sandbox`/`sandbox_gw` groups), the host bridge chain
  drops gateway↔gateway, and sandbox bridges (`vmbr1*`) are never routed to
  devbox bridges (`vmbr9*`).
- **A team's devbox** is isolated to that team: its VMs talk among themselves
  (A–B–C) and, via the team's devbox-gw, only to the service hosts
  (`devbox`/`devbox_gw` groups). The SSH gateway lets a team reach **one** devbox
  VM (A), is not reachable from the sandbox (disjoint bridges, no route), and is
  not attackable by a devbox (root-owned keys + source-pinned SSH + the devbox
  VM's own output-DROP policy).

### Port isolation (deliberately available-but-commented)

The hookscript that sets a VM's management-bridge (and vmbr7) ports to
`isolated` is wired through every VM module and stack but left **commented**,
because attaching a Proxmox hookscript requires the provider to act as
`root@pam`; the provisioning API token cannot. Flip the single
`hook_script_file_id = …` line (and apply with `root@pam` credentials) to turn
it on. The shared SSH gateway is intentionally **not** given the hookscript: its
vmbr7 port must stay non-isolated so it can reach the (isolated) devbox VMs; the
devbox VMs are the ones isolated from each other.

## Findings in full

Severity reflects impact under the stated threat model. "CONFIRMED" = the code
path was read end to end; nothing was run against a live Proxmox host.

### Threat model 1 — root on a sandbox/devbox VM

- **S1 (High, fixed):** the monitor shipper and the always-on devbox logs write
  to one shared database with no retention. Mitigated by the 100 GB disk; a
  retention/partition policy for `network_flows`/`network_events` remains
  advisable (see `docs/monitoring.md`, Limitations).
- **S2 (High, latent):** devpi (empty root password, writable) and the Docker
  registry (no auth) would accept anonymous writes that persist across rounds —
  a poisoning and a cross-environment channel. **Not yet fixed**; these services
  are currently disabled in `opentofu/stacks/private_internet` (only Wikipedia
  is deployed). Lock them to read-only/authenticated before re-enabling.
- **S3 (fixed):** the shared `team` gateway account could rewrite its own
  `authorized_keys`. Now root-owned.
- **S4 (fixed):** port isolation was commented out everywhere and the watchdog
  ignored devboxes. Watchdog now covers the devbox pool; the hookscript is wired
  everywhere (still `root@pam`-gated).
- **S5 (fixed):** attribution failed open; now fail-closed, with per-source
  nginx limits.
- **S6 (fixed):** the internet window could open while devboxes ran.
- **S7 (fixed):** a failed teardown did not block the next run.
- **S8 (fixed):** the dnsmasq query log was unbounded.
- **S9 (not fixed):** KSM memory dedup (a cross-VM side channel) is not
  disabled. Proxmox-host setting; disable KSM on the host if this is in scope.
- **S10 (not fixed):** Zeek keeps raw-packet capabilities with no seccomp
  filter. Blast radius is bounded by `IPAddressDeny` + the INSERT-only role.
- **S11 (not fixed):** syslog to Wazuh is plain TCP by source CIDR; a
  compromised exposed VM could forge/flood entries.
- **S12 (not fixed):** the sandbox groups apply per-VM rather than per-NIC (the
  devbox module already scopes per-NIC); rules are source/dest-scoped regardless.
- **S13 (partly moot):** apt-cacher-ng report page, all-ports-open, cve-search
  web UI — only relevant once those PI services are re-enabled (see S2).
- **S14 (fixed):** the Wazuh install bundle is removed from the VM.

### Threat model 2 — dashboard user

No XSS, CSRF, IDOR or privilege escalation was found; team scoping is enforced
server-side on every view and API endpoint, autoescaping is on, the JS builds
the DOM with `textContent`, and the bearer-token APIs are the only
`csrf_exempt` paths. Remaining items:

- **D1 (fixed):** anonymous requests created a session row each; now they don't.
- **D2 (fixed):** devbox reset had no throttle; now one rebuild per team.
- **D6 (fixed, High — template injection → root on the Proxmox host):** a team's
  registered SSH key is attacker-controlled text that the round agent renders
  through Jinja into the devbox and gateway `authorized_keys` (on the host, as
  root). The comment field was unvalidated (`.{0,200}`), so a key such as
  `ssh-ed25519 AAAA… {{ '{{' }} … {{ '}}' }}` was **evaluated** as a template
  (confirmed on ansible-core 2.18 and 2.19). Fixed two ways: (a) both validators
  — `dashboard/views/devbox.py:_valid_ssh_key` and
  `scripts/round_agent/validation.py:ssh_keys` — now reject any key whose
  comment contains template/shell/quote characters (`{ } % $ "` ' \ ; | < >`);
  (b) the agent writes the keys to the Ansible var file tagged `!unsafe`
  (`handlers._unsafe_yaml`), so key text is never templated even if validation
  is loosened. A team *cannot* forge its gateway `permitopen`/`restrict` by the
  key line itself (the options are emitted before the key; the comment is after
  it, and no newline is allowed), but the Jinja path was the real escape.
- **D7 (fixed — cross-team key / stale access):** a key could be registered by
  more than one team (sshd matches a key to the first `authorized_keys` line, so
  a shared key would let one team inherit another's gateway forward and log in
  to its VMs), adding/removing a key did nothing until an operator
  re-provisioned, a devbox *reset* enqueued a rebuild **without** the `teams`
  list so the agent failed it (keys were never installed and the VMs never
  rebuilt), and leaving a team kept the member's keys. Now: a key blob must be
  unique across teams (rejected on the dashboard, and fail-closed in the agent's
  `devbox_teams`); adding/removing a key enqueues a lightweight
  `devbox_reconfigure` (re-push keys only, no VM rebuild); reset carries the full
  teams spec; leaving a team removes that member's keys and reconfigures.
  Residual: the cross-team uniqueness check is app-level (a simultaneous add by
  two teams could still race past the dashboard, but the agent then fails the
  job closed rather than installing a hijacking key); a DB fingerprint
  constraint would close the race.
- **D3 (partly fixed):** sign-in POSTs are rate-limited per client address
  by nginx (`nginx_login_rate`, 10/min with a burst of 5, dashboard only),
  which matters more now that the staff pages are on the public dashboard. No
  per-account lockout yet (a distributed guesser is only slowed per address).
- **D4 (not fixed):** team-join is open and racy (no consent, no row lock).
- **D5 (not fixed):** the voluntary password change does not require the current
  password (first-login change legitimately does not).
- **D8 (not fixed):** no Content-Security-Policy (defense in depth; no XSS sink
  found).
- **D9 (fixed — production breakage found while adding the staff pages):** the
  dashboard's role had no `INSERT`/`DELETE` on `team_memberships`, so joining
  or leaving a team failed in production, and the first-login password change
  saved the whole user row although the role may update only
  `password`/`must_change_password`, so it failed too. Both are granted/fixed
  and now run under the real grants in `tests/test_staff_admin.py`.
- **D10 (design — staff pages on the public dashboard):** staff manage
  database records there (student accounts, teams, the registration window,
  round definitions). The role stays column-scoped: no `username`,
  `is_staff`, `is_superuser` updates; an `INSERT` of a staff or superuser row
  by `django_dashboard` is refused by a trigger (`privileges.sql.j2`), so a
  compromised dashboard cannot mint staff accounts (`worms admin
  create-staff` on the console does); teams and rounds are deleted only while
  nothing refers to them (no cascade rights). Nothing on these pages builds
  infrastructure: devboxes are provisioned from the console, sandboxes by
  the round agent when a round starts. A stolen staff session can still
  create rounds (bounded parameters, validated again by the agent) and
  accounts.

### Cross-cutting / infrastructure (C)

- **C1 (not fixed):** the Proxmox provider uses `insecure = true`. In laptop
  mode OpenTofu talks to `https://<node>:8006` directly, exposing the root
  provisioning token to an on-path attacker. Fixing it well needs a cert valid
  for the connection endpoint and its CA trusted (the CLI already fetches the
  PVE CA); left as a deliberate, documented decision rather than a half-measure.
- **C2 (not fixed):** VM host keys are not verified (`host_key_checking = False`,
  `StrictHostKeyChecking=no`) because lab VMs are rebuilt with fresh keys.
  Anti-spoofing rests on the per-NIC ipfilter.
- **C3 (accepted):** the migrator credentials live only on the orchestrator.
- **C6 (good):** default users are key-only, root is `prohibit-password`, no
  passwords are set, and the Proxmox tokens are least-privilege.

## Verification

The offline suite passes (`worms test`); new regression tests cover the host
SNAT for the gateway, the per-host firewall egress scoping, the gateway's
single entry VM, fail-closed attribution, the per-source nginx limits, the
teardown-before-next-run gate, the one-rebuild-per-team guard, and the
anonymous-session fix. As with the rest of the platform, nothing here was run
against a live Proxmox host (none available); the OpenTofu changes pass
`tofu validate` and `tofu fmt`.
