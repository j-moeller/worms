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
| S14 | Confidentiality | Moot: Wazuh (and its install bundle with all generated passwords) was replaced by a plain rsyslog server without credentials | `ansible/roles/audit/syslog_server/*` |
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

## LLM endpoint

`teachllm.mlsec.tu-berlin.de` (`141.23.117.34`, TCP 443) is **the only exception** to "neither sandbox nor devbox may have internet access". It is a server on the local network, which still needs egress from this system. It is assumed robust against attacks, since any code on these VMs can reach it directly. Nothing else may be added this way.

The address is `llm_access_ip` in `ansible/group_vars/all.yml`. `worms` passes it to the firewall stack, whose output gives it to the sandbox and devbox stacks, so one value drives every layer. Without `worms`, the stacks fall back to `127.0.0.1`, which gives no access.

Each layer is independent and allows only this address and port:

| Layer | What it allows | Where |
|---|---|---|
| The VM's Proxmox firewall | Out to it | `llm-access`, on sandbox and devbox VMs |
| Bridge table on the node | Both directions, under the VM's usual SYN limit | `worms.nft.j2`, chains `sandbox` and `devbox` |
| The gateway's Proxmox firewall | In from its VMs; out on the management NIC only | `sandbox_gw`, `devbox_gw`, `llm-egress` |
| The gateway's nftables | Forward and SNAT to its management address, and a `/32` route via the node. It still has no default route | `sandbox-gw.tftpl`, `devbox-gw.tftpl` |
| The node | Forward from the gateways' management addresses only, then masquerade. The internet window's gate does not apply | `worms.nft.j2`, chains `forward` and `postrouting` |

`teachllm` sees the node's address for every team. `worms status` still says `internet access denied`, because that reports the internet window, which is closed.

The private DNS answers the name, from a zone of its own with one record (`llm_access_name`, role `setup_dns`). The resolver still has no recursion and no forwarders, so every other outside name is refused and DNS gives no way to ask or tell the outside world anything.

Checked on 2026-11-05 on the devbox, acting as root on `devbox-t1-a` and on `devbox-t1-gw`:

| From | To | Result |
|---|---|---|
| All 9 devbox VMs | `teachllm`, TCP 443, TLS verified | Connects. `/` answers 403, which is the server's own answer |
| VM | `teachllm` on ports 80 and 22 | Blocked |
| VM | `141.23.117.35`, `.33`, the node's `.61`, `1.1.1.1`, all TCP 443; `8.8.8.8:53` | Blocked |
| VM, route added through its maintenance NIC | `teachllm:443`, `1.1.1.1:443` | Blocked: the node only allows the gateways' addresses |
| VM, route added through its SSH-fabric NIC | `teachllm:443` | Blocked |
| VM, using its gateway's address | `teachllm:443` | Blocked: Proxmox pins each NIC to its own address |
| Gateway | `teachllm:443` | Connects: this is its path |
| Gateway | `teachllm:80`; `1.1.1.1:443` and `.35:443`, route added via the node; the node's SSH | Blocked |

**Not seen yet:** a real sandbox. No round has run since this change, so the sandbox gateway's rules are only checked by tests (`test_every_layer_on_the_way_to_the_llm_endpoint_allows_only_it`).

## No `/dev/kvm` on team VMs

Every CPU value and what it opens: `docs/cpu-types.md`.

Sandbox and devbox VMs are assumed hostile and rooted, so they must never get `/dev/kvm`. With it, root on the VM can make the node's kernel run its nested-virtualisation code, a large and complex part of KVM with a history of serious bugs. CVE-2021-29657, on AMD, was turned into a full escape from guest to host. A bug like that here would give a team root on the node, which holds every secret and could forge any result.

**How it happened:** the devbox VMs were set to `cpu: host` by hand on 2026-11-05, and that passes the node's nesting on to them. The same day the setting was written into the devbox stack, without anyone checking what it exposed. It was live on all 9 devbox VMs for about 9 hours, with no round running.

**The fix:** sandbox and devbox VMs use `x86-64-v3`, which offers no hardware virtualisation. It has AVX2 and AES, and it is the same CPU on any node.

| Layer | What it does | Where | State |
|---|---|---|---|
| CPU type | Both stacks set `x86-64-v3` | `stacks/sandbox/main.tf`, `stacks/devbox/main.tf` | Live on all 9 devbox VMs. A sandbox gets it when built |
| The stack refuses | `host` and `max` fail validation on a devbox VM | `modules/devbox_vm/variables.tf` | Done |
| Tests | No team VM may ask for `host`, `max`, `+vmx` or `+svm`, and both stacks must match the agent's value | `tests/test_tofu.py` | Done |
| The agent refuses | A round's check job fails, and a dev upload is not placed, unless the node's own config says exactly `cpu: x86-64-v3`. It does not ask the VM, whose answer proves nothing | `round_agent/handlers.py`, `TEAM_VM_CPU` | Done |
| Nesting off on the node | No VM can get it, whatever its config says | `/etc/modprobe.d/worms-no-nesting.conf` | **Written, not active.** It needs a restart (`docs/check-with-user.md`, 3h) |

Measured on 2026-11-05, as root on the VM. The right column holds for all 9 devbox VMs, the left for the one VM probed in full before its restart (`devbox-t1-b`). A sweep afterwards found no `/dev/kvm` on the 9 VMs or their 3 gateways:

| | `cpu: host` | `cpu: x86-64-v3` |
|---|---|---|
| Raw `CPUID`, VMX bit (not `/proc/cpuinfo`, which the guest kernel filters) | 1 | 0 |
| `modprobe kvm_intel` | Loads | `Operation not supported` |
| `/dev/kvm` | Exists | Absent |
| Own device node `10:232`, made with `mknod` and opened | Opens | `ENODEV` |
| AVX2, AES | Yes | Yes |

**Not measured:**
- **A real sandbox.** None exists. The plan shows `x86-64-v3` on every sandbox VM, and the check job stops a round that has anything else.
- **`vmxon` itself.** A guest whose CPU does not list VMX should not be able to turn it on, because KVM refuses `CR4.VMXE` for it. Testing that needs a kernel module on the VM. Nesting off on the node removes the question.

**What it never changed:** the network. A nested guest's traffic leaves through the VM's own NIC, so every firewall layer applied throughout.

**Still on `cpu: host`:** `cve-search`, because MongoDB needs AVX. It runs no team code, but team VMs can reach it over the network, and it still has `/dev/kvm` until nesting is off on the node.

### Hyperthreading (open)

The node reports `SMT vulnerable` for `l1tf` and `mds` (i7-4900MQ, hyperthreading on). Root on a VM may be able to read data from whatever runs on the same core's other thread, including another VM or the node. This holds for every CPU type, so no choice above changes it. The fix is to turn hyperthreading off, which takes this node from 8 threads to 4 cores. What counts is the hardware of `worms-pve`, which has not been checked.

## Findings in full

Severity reflects impact under the stated threat model. "CONFIRMED" = the code
path was read end to end; nothing was run against a live Proxmox host.

### Threat model 1 — root on a sandbox/devbox VM

- **S1 (High, fixed):** the monitor shipper and the always-on devbox logs write
  to one shared database with no retention. Mitigated by the 100 GB disk; a
  retention/partition policy for `network_flows`/`network_events` remains
  advisable (see `docs/monitoring.md`, Limitations).
- **S2 (fixed):** devpi (empty root password, writable) and the Docker registry (no
  auth) accepted anonymous writes that persist across rounds: a poisoning and a
  cross-environment channel. devpi now runs with `--restrict-modify root`, and root's
  password is generated and stays in the vault. The registry is read-only whenever
  internet access is denied, so it is writable only while the caches are filled.
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
- **S11 (partly fixed):** syslog to the syslog VM is plain TCP by source CIDR; a
  compromised exposed VM can write forged lines into its own file (not another
  VM's: files are keyed by the connection's address). Flooding is bounded: the
  host allows each sending VM at most 4 connections and 1 MB/s to the syslog VM
  (`host_syslog_limits`, `worms.nft.j2`), and the logs live on their own data
  disk, so filling it cannot take down the syslog VM's system.
- **S12 (not fixed):** the sandbox groups apply per-VM rather than per-NIC (the
  devbox module already scopes per-NIC); rules are source/dest-scoped regardless.
- **S13 (not fixed):** the apt-cacher-ng report page and the cve-search web UI answer
  without a login on the services bridge. These PI services are enabled.
- **S14 (moot):** Wazuh, and with it the install bundle, was removed.
- **S15 (fixed, found live):** replacing a VM could leave it running with its
  firewall off. Proxmox deletes a VM's firewall settings with the VM, and if the
  replacement got the same ID, OpenTofu saw nothing to write back and no drift
  afterwards. The trusted SSH gateway ran this way for over 2 hours in the
  end-to-end run. The watchdog caught a devbox VM in this state, but it does not
  watch the gateway or the other infrastructure VMs. Now `replace_triggered_by`
  rewrites the options of every VM type. **Still open:** nothing checks that those
  VMs have a firewall (`docs/e2e-run-2026-11-03/protocol.md`, F26).
- **S16 (fixed, found live):** a team login could run commands on the SSH gateway,
  which reaches every team's fabric address. `restrict` does not stop commands. Now
  `command=` on each key and a `nologin` shell (same protocol, F19).
- **S17 (open, found live):** an untrusted VM runs without its firewall while it is
  being built, for up to 28 minutes in that run, and outside the pool the watchdog
  does not look at it. The host's own rules still apply. Creating VMs stopped and
  starting them after their firewall would close this (same protocol, F4).

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
- **D4 (partly fixed):** joining locks the user and the team rows, so concurrent
  joins can neither overfill a team nor put one person into two teams, and only
  teams of the current competition can be joined (`tests/test_team_registration.py`).
  Still open: joining needs no consent of the team's members.
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
