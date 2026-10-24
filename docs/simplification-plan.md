# Simplification plan

Status: **proposal — nothing here is executed yet.** Goal: fewer moving parts
for the operator and less code, **without** giving up the security
properties the platform exists for (untrusted sandbox/devbox VMs contained,
least privilege for everything internet- or sandbox-facing).

Guiding rule: **one owner per concern.**

| Concern | Owner after this plan |
|---|---|
| VMs, bridges, firewall objects | OpenTofu (one VM module) |
| first boot (hostname, SSH key, SSH hardening) | cloud-init (one template) |
| software and configuration on VMs | Ansible (one site playbook) |
| secrets | one root-only directory on the node |
| anything that touches Proxmox at runtime | the host's round agent (root, no API token) |
| communication between components | the database (no side channels) |

---

## 1. Where we are

Non-test code and configuration, ≈ **32 000 lines** (tests ≈ 5 300, docs ≈ 2 900):

| Area | Lines | Notes |
|---|---:|---|
| OpenTofu | 5 100 | 7 stacks, 7 modules; 16 cloud-init templates (870 lines); 6 near-identical VM definitions |
| Ansible | 7 700 | 25 roles, 20 playbooks (18 of them 8–94-line wrappers), vault plumbing |
| Django | 15 200 | 3 apps, 4 settings modules, 14 migrations (1 400 lines), `init_debug` alone 1 700 |
| scripts | 3 100 | generators, round agent, `plot_network.py` (unused) |
| CLI (`src/`) | 1 400 | pipeline, status, teams |

Credentials an operator has to know about today:

| Kind | Count | Where |
|---|---:|---|
| Proxmox API tokens | **3** | provisioning (`TF_VAR_proxmox_api_token`, + vault copy), orchestrator power (`PROXMOX_API_TOKEN` → vault), read-only topology (`PROXMOX_READONLY_API_TOKEN` → vault) |
| generated secrets | 16 | encrypted Ansible vault + `.vault_pass` next to it |
| DB roles | 8 | admin, migrator, dashboard, orchestrator, scoring, logging, monitor, round agent |
| places config is read from | 4 | shell env (`TF_VAR_*`, `PROXMOX_*`), vault, `~/.worms/config`, per-service env files |
| other | — | root SSH key + an ssh-agent for OpenTofu, Django admin password (vault → env → post-migrate hook), orchestrator API bearer token |

Fixed VMs: **16** (database, dashboard, orchestrator, scoring, logging,
monitor, wazuh, dns, 7 private-internet, SSH gateway), plus per run the
sandbox and per team four devbox VMs.

---

## 2. Proposals

Each item: what changes, what it removes, the security effect, and how the
**live deployment** gets there (there is one — the dashboard is running).
Line counts are estimates.

### A. Proxmox credentials: 3 → 1 (OpenTofu)

**Today.** Besides OpenTofu's provisioning token, the orchestrator VM holds
two Proxmox API tokens: a pool-scoped power token (start/stop sandbox VMs in
the game plan) and a read-only token (`collect_topology`). Each needs a
Proxmox user, a token, ACLs (`pveum` tasks in Ansible), settings, env
variables, a firewall path orchestrator → host:8006, and TLS verification of
the Proxmox API certificate, which currently does not work (open item in
`findings.md`).

**Proposal.** The round agent on the node already executes privileged work
from the `host_jobs` queue (LISTEN/NOTIFY, ~instant). Let it also:

- **power**: the conductor enqueues `vm_power` jobs (start/stop, by VMID)
  instead of calling the API; the agent runs `qm start|stop` after checking
  the VM is in the `sandbox` pool — the same scope the token had;
- **topology**: the agent writes the snapshot (`pvesh get … --output-format
  json`, the same data `proxmox_inventory.py` collects) into
  `topology_snapshots` on a timer;
- **`worms status`** reads infrastructure state from that snapshot in the
  database instead of the Proxmox API.

**Removes.** Two tokens and their users/ACLs, `orchestrator/rounds/power.py`,
proxmoxer on the orchestrator, `worms_cli/proxmox.py` and all
`WORMS_PROXMOX_*`, `PROXMOX_*` / `PROXMOX_READONLY_*` settings and env,
the orchestrator → :8006 firewall rule, the Proxmox-TLS open item.
≈ −350 lines.

**Security.** Better: the orchestrator VM no longer holds *any* Proxmox
credential. The agent's job menu stays fixed and validated; its DB role
gains `INSERT/UPDATE` on `topology_snapshots` only.

**Live migration.** Deploy the agent and conductor together; then delete the
two tokens/users in Proxmox (`pveum user delete orchestrator@pam`,
`topology@pve`).

### B. One secrets directory instead of vault + env + config files (Ansible)

**Today.** `generate_secrets.py` writes an ansible-vault file encrypted with a
key file stored *next to it* (protection at rest is nominal); external tokens
come in through three environment variables at first run, OpenTofu needs
`TF_VAR_*` and an ssh-agent in the operator's shell, the CLI needs
`~/.worms/config`, the round agent gets copies through its env file.

**Proposal.** One directory on the node, `/etc/worms/secrets/` (root, 0700,
one file per secret, 0600):

- **generated secrets**: Ansible's built-in `password` lookup creates each
  file on first use and reuses it afterwards (`{{ lookup('password',
  secrets_dir ~ '/postgres_admin chars=ascii_letters,digits length=32') }}`)
  — idempotent, never rotates, no generator script;
- **the one external secret** (the provisioning token) is a file the operator
  writes once: `/etc/worms/secrets/proxmox-token`;
- the **CLI** runs `tofu` with `TF_VAR_proxmox_api_token` read from that file
  and inside `ssh-agent` with root's key (as the round agent does), and
  reads the DB admin password from the same directory on the node —
  `~/.worms/config` is then only needed on a laptop;
- the **round agent** calls `worms build …` for its tofu work, so tofu runs
  through one code path, and its env file loses the `TF_VAR_*` / ssh-agent
  special cases.

**Removes.** `generate_secrets.py` and its tests, `.vault_pass`, the vault
file, `-e @group_vars/vault.yml`, the `secrets` build step, every `vault_*`
indirection, the operator's shell setup (`TF_VAR_*`, `PROXMOX_*`,
`ssh-agent`), most of `docs/accounts.md` §4. ≈ −350 lines, and the operator's
setup shrinks to "write one token file".

**Security.** Equivalent: today the vault key sits beside the vault on the
same root-owned node; after, the secrets are root-only files on that node.
Secrets still reach VMs only through Ansible into 0600 env files.

**Live migration.** One-time export: decrypt the existing vault and write
each key to its file **with the current value** (DB passwords and the CA
passphrase must not change).

### C. Cloud-init: one template

**Today.** 16 templates (one unused: `vm-printer`) plus 15 almost identical
`proxmox_virtual_environment_file` resources; every template repeats the
same SSH key / hardening / hostname block (20 copies of the SSH hardening
across all cloud-init templates) and adds a package list — packages
the Ansible roles mostly install again anyway.

**Proposal.** One template (hostname, the SSH key, the SSH hardening, Python
for Ansible) rendered with `for_each` over the VM names. Packages are
installed by the Ansible role that needs them (most already do; add the few
that only cloud-init installs, e.g. `fail2ban`). The sandbox/devbox
templates share the same header via one partial.

**Removes.** ≈ −700 lines; one place to change SSH hardening.

**Security.** Unchanged (same hardening, one copy instead of 20).

**Live migration.** Snippet changes must not recreate VMs: verify with
`tofu plan` that existing VMs only get an in-place `cicustom` update (or keep
the old snippet file names via `moved` blocks).

### D. OpenTofu: one VM module; drop one-off state migrations

**Today.** Six definitions of "a VM with NICs, per-NIC ipfilter ipsets,
firewall options, security groups, pool membership":
`infrastructure_vm`, `private_internet_vm`, `network_monitor_vm`,
`devbox_vm`, and inline copies for the sandbox VMs/gateway and the SSH
gateway (≈ 1 100 lines). Plus `platform/migrations.tf` and the pipeline's
`firewall_migrate` (one-time moves for deployments older than the firewall
stack; they also break `tofu validate` on 1.8).

**Proposal.** One `vm` module: `nics = [{bridge, cidr, gateway, firewall,
groups}]`, optional data disk, optional address-less capture NIC, cloud-init
snippet, pool, tags. Every stack uses it. Delete the migration blocks once
the live state has been through them.

**Removes.** ≈ −900 lines (module) and ≈ −130 (migrations); a fix (e.g.
attaching the port-isolation hookscript, still an open hardening item) is
made once instead of six times.

**Optional, later:** merge the 7 stacks into 4 (`base` = platform+firewall,
`services` = infrastructure persistent+ephemeral+private internet, `sandbox`,
`devbox`) — ≈ −200 lines of provider/remote-state boilerplate, but needs
`tofu state mv` on the live state; the ephemeral rebuild becomes
`-replace` of those module instances.

**Live migration.** `moved {}` blocks from the old resource addresses to the
module's; the plan must show **no destroy**.

### E. Ansible: one site playbook

**Today.** 20 playbooks; 18 are thin wrappers (8–94 lines) repeating the
same "wait for cloud-init" pre-task in 11 places; the CLI keeps their order in
`CONFIGURE_PLAYBOOKS`. Two near-identical nginx roles (`nginx_tls`,
`nginx_tls_django`).

**Proposal.** One `site.yml` with one play per host group, each tagged by
component (`--tags dns`, `--tags database`, …); the cloud-init wait once.
`worms build <component>` maps to tags. One nginx role (TLS + optional
gunicorn proxy + optional API rate limits). `Simulate.yml` and `Debug.yml`
stay separate.

**Removes.** ≈ −400 lines, the playbook-order list in the CLI.

**Live migration.** None (same roles, same hosts).

### F. Django: fewer side channels, credentials and migrations

1. **Drop the orchestrator's REST API.** Its only caller is the dashboard's
   devbox reset; the VM power endpoints have none. Instead the dashboard sets
   `teams.devbox_reset_requested_at` (one column of `UPDATE`) and the
   orchestrator's loop acts on it — the database is already the bus for
   everything else. Removes `orchestrator/api.py`, `orchestrator_client.py`,
   the bearer token, nginx + TLS + gunicorn on the orchestrator VM (it keeps
   only its daemons) and the dashboard → orchestrator:443 firewall path.
   ≈ −250 lines. *Security: better — the orchestrator VM accepts no inbound
   connection at all.*
2. **Admin via the CLI, like students.** `worms admin create` prints a
   one-time password with `must_change_password` (the student mechanism).
   Removes the vault key, `DJANGO_ADMIN_PASSWORD`, the post-migrate hook in
   the `django_app` role and `init`'s password handling. ≈ −60 lines.
3. **Squash the migrations** (14 files, 1 400 lines → one ≈ 400). Using
   `replaces = [...]` keeps the live database valid without a reset.
4. **Remove legacy models** `WormDeploymentPlan` / `WormDeployment`
   (superseded by `RoundRun`; the uploads page reads `RoundRun.upload`
   instead).
5. **Settings**: the 4 modules (`base`, `dashboard`, `orchestrator`,
   `manage`) become `base` + one per process, with the CLI using the
   orchestrator's — small, but one less variant to reason about.

*Optional, bigger:* unify devbox and round results
(`DevboxVM`/`DevboxCapture`/`DevboxLog` vs `VirtualMachine`/`FlagCapture`/
`Logging`, with a `devbox` flag) — removes the parallel processing paths in
`captures.py`, but touches the most tested code; only if wanted.

### G. Dead code (no behaviour change)

| Item | Lines | Evidence |
|---|---:|---|
| `ansible/roles/database/setup_db_dashboard` | 247 | no playbook uses it |
| `scripts/plot_network.py` | 696 | referenced nowhere |
| `core/management/commands/dump_database.py` | 77 | referenced nowhere |
| `cloud_init/templates/vm-printer.tftpl` | 83 | no resource uses it |
| per-stack `Makefile`s (7) | 35 | the CLI runs `tofu` directly |
| `process_captures` command | 26 | same as `process_attempts --once` |
| orchestrator VM-power API endpoints | ~60 | no caller (see F.1) |
| `init_debug` + `create_teams` | 1 850 | demo-data generator for DEBUG mode; referenced nowhere — **your call** |

≈ −1 300 lines for certain, −3 150 with `init_debug`.

### H. Product decisions that would remove whole components

Not technical clean-ups — they change what the platform *is*, so they need
your decision:

| Option | Saves | Cost |
|---|---|---|
| **Drop Wazuh + audit shipping** (not in the requirements so far) | 1 VM (2–8 GB RAM), the SIEM bridge, 2 roles, a playbook, firewall groups and host rules — ≈ −400 lines | no central audit log of the infrastructure VMs |
| **Private internet: 7 VMs → 2** (one VM for the light caches + DNS: apt, devpi, registry, manpages, ntp; heavy cve-search + wikipedia on the second) | 5 VMs, their addresses, snippets, firewall entries — ≈ −150 lines | caches share a VM (all are trusted, dead-end services; no security boundary between them) |
| **Scoring + logging on one VM** (same Flask app already, two endpoints) | 1 VM, one set of addresses/rules | your visualization spec draws them as two VMs; per-server attribution stays by endpoint |

---

## 3. Deliberately unchanged

These are complex because the security model requires it:

- two firewall layers: Proxmox security groups on every VM tap (per-VM
  policy, ipfilter) **and** host nftables (routing/NAT between bridges);
- `pg_hba` per `/32` and one least-privilege DB role per writer;
- dashboard (internet-facing) and orchestrator (privileged) on separate VMs —
  merging them, or moving the orchestrator onto the node, would put
  migrator credentials or attacker-supplied data next to things that must not
  have them;
- the monitor's mirror capture, the devbox SSH gateway, the round agent's
  pull-based job queue.

---

## 4. Roadmap

Each phase is independently shippable, keeps the test suite green, and is
followed by `worms simulate` on the live system.

| Phase | Content | Est. lines | Operator-facing effect |
|---|---|---:|---|
| 1 | **G** dead code | −1 300 (−3 150) | none |
| 2 | **A** host agent does power + topology; **F.1** drop the orchestrator API | −600 | Proxmox tokens 3 → 1; orchestrator VM has no inbound port and no Proxmox credential |
| 3 | **B** secrets directory; **F.2** admin via CLI | −400 | setup = one token file; no vault, no shell variables |
| 4 | **C** one cloud-init template; **D** one VM module, drop migrations | −1 700 | none (verified empty/in-place plans) |
| 5 | **E** site playbook; **F.3–5** migrations, legacy models, settings | −1 500 | `worms build <component>` unchanged |
| 6 | **H** decisions, optional stack merge, optional result-model unification | −200 … −800 | fewer VMs |

Total: **≈ −5 500 to −8 000 lines (≈ 20–25 %)**. Proxmox tokens 3 → 1,
places configuration is read from 4 → 1, generated secrets handled by code
instead of a generator script, fixed VMs 16 → 9–16 depending on section H.

---

## 5. Decisions needed before executing

1. **Live deployment:** migrate every step in place (as planned above), or
   is it disposable (`worms destroy-all` + rebuild) — which would make phases
   4–5 simpler?
2. **Wazuh / audit shipping:** keep or drop?
3. **Private internet:** keep 7 VMs or consolidate to 2?
4. **Scoring + logging:** keep two VMs or one?
5. **`init_debug`** (1 700-line demo-data generator): still used for local
   development?
6. **Host agent for VM power and topology** (section A): OK that root on the
   node does these, so the orchestrator VM holds no Proxmox credential?
