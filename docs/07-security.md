# 7. Security

> Neither sandbox nor devbox must have internet access. ([`docs/IMPORTANT`](IMPORTANT))

## Threat models

The attackers know the whole architecture and none of the secrets.

| # | Attacker | Has | Must not reach |
|---|---|---|---|
| T1 | A worm or a team member as **root on a sandbox or devbox VM** | Root on the VM. On a devbox: all three VMs of its team, its uploads, its registered keys | the internet; other teams; the infrastructure; the node; another round's results |
| T2 | A **dashboard user** (team member) | A student account, team API tokens | other teams' data; staff functions; the server |
| T3 | A **compromised dashboard VM** (internet-facing) | The dashboard's DB role, the orchestrator API token, the uploads | the node; Proxmox; gameplan content; changing results |

**Trusted:**
- the node, the controller (vault, CA, state), the orchestrator, the database, the round agent;
- gameplan authors: gameplan code runs as root on the node;
- staff, who are less trusted than the admin;
- the LLM endpoint's operators.

## Capabilities

### Proxmox

Tokens are defined in [`tokens.py`](../src/worms_cli/tokens.py). Each is privilege-separated, and `worms secrets check` verifies its rights.

| Token | Role, path | Privileges | Used by |
|---|---|---|---|
| `worms-provision@pve!worms` | `WormsProvision` on `/` | Datastore.*, Pool.Allocate/Audit, SDN.Use, Sys.Audit/Modify, VM.Allocate/Audit/Config.*/PowerMgmt. No user, ACL, console or node power rights | OpenTofu (controller), the round agent (node) |
| `orchestrator@pam!orchestrator-prov` | `WormsSandboxPower` on `/pool/sandbox` | VM.Audit, VM.PowerMgmt | the conductor: sandbox VM power ([`power.py`](../projects/dashboard/orchestrator/rounds/power.py) also checks VM name and run) |
| `topology@pve!readonly` | `WormsTopology` on `/` | Pool.Audit, SDN.Audit, Sys.Audit, VM.Audit | `collect_topology`, `worms status` |

Root SSH to the node goes beyond the tokens:
- OpenTofu uploads snippets as root over SSH with the WORMS key.
- The round agent runs as root.

The agent is bounded by its fixed, validated job menu and a database role that can only read and update `host_jobs` ([5](05-live-environment.md#architecture)).

### Database

PostgreSQL accepts TLS only (`hostssl`, `scram-sha-256`), and each role connects only from its own VM's /32. Grants: [`setup_db_permissions`](../ansible/roles/database/setup_db_permissions/defaults/main.yml), applied in one transaction (revoke all, then grant exactly).

| Role | From | May |
|---|---|---|
| `admin` (superuser) | the node | everything: the `worms` CLI |
| `django_migrator` | orchestrator | owns the tables, runs migrations, `init` |
| `django_orchestrator` | orchestrator | Read: every table except the event tables. Write: runs, VMs, topology, devbox tables, `gameplans`, captures, logs, `run_events`; INSERT `host_jobs`; named columns of `rounds`/`teams` |
| `django_dashboard` | dashboard | Read: every table except the event tables. Insert: uploads, tokens, SSH keys, memberships, users, teams, rounds, password resets. Update: named columns only. A trigger refuses staff or superuser rows. Never `username`, `is_staff`, `is_superuser` |
| `dbuser_scoring`, `dbuser_logging` | scoring, logging | INSERT into their attempt table only (3–4 columns) |
| `dbuser_network_monitor` | monitor | INSERT into `network_flows`, `network_events` |
| `round_agent` | node | SELECT `host_jobs`; UPDATE its state, timestamps, result, error |
| `dbuser_events_dashboard`, `dbuser_events_node` | dashboard, node | INSERT named columns of their event tables; read nothing |

The 9 event tables are append-only for every role.

### Operating system

- **SSH into VMs.** Key-only, from the node, on the management NIC (cloud-init: `PermitRootLogin prohibit-password`, no passwords; `HardenSSH` pins `ListenAddress`).
- **Gameplan exception.** A gameplan may add password logins for its own users on sandbox VMs, via a `Match User` block ([`sandbox_vm`](../ansible/roles/applications/sandbox_vm/tasks/main.yml)).
- **Services** run as dedicated users with systemd hardening:
  - common: `NoNewPrivileges`, `ProtectSystem=strict`, `MemoryDenyWriteExecute`;
  - `sandbox-api` and the Zeek shipper also get an empty capability set and `IPAddressAllow=<db>/32`.
  - Zeek keeps only `CAP_NET_RAW`/`CAP_NET_ADMIN`.
- **Root:** auditd and rsyslog, nginx masters, the monitor's NIC one-shot, and on the node the agent, mirror, host firewall and watchdog.
- **Untrusted by design:**
  - sandbox VMs: the worm runs as `debian` with sudo;
  - devbox VMs: teams are root;
  - both kinds of gateway: assumed compromised, and bound by the node's and the VMs' firewalls.

## Account management

| Account | Privilege | Created by |
|---|---|---|
| `admin` | dashboard superuser and staff | `manage.py init` (no usable password); `worms admin reset-password` issues a one-time password |
| staff | the staff pages; manage students | `worms admin create-staff EMAIL`. The dashboard cannot create staff (DB trigger) |
| students | their own team | **Staff → Users**, one at a time or by CSV. One-time password, changed at first sign-in |
| team access tokens | read their team's results via the API | team members on the profile, after registration closes |

Account rules ([`core/accounts.py`](../projects/dashboard/core/accounts.py), [`middleware.py`](../projects/dashboard/dashboard/middleware.py)):
- **Passwords.**
  - One-time passwords are 14 characters (admin: 20) and shown once.
  - `must_change_password` holds the user on `/change-password/`.
  - Django's validators apply (minimum 8 characters, not common, not numeric).
  - A change ends the user's other sessions.
- **Resets.** Staff reset students; only the superuser resets staff. Every reset writes `password_resets` and a password event.
- **Removal.** Accounts are deactivated, never deleted. Teams are deleted only when empty, or with `worms teams delete`.
- **View as.**
  - The superuser only, by POST.
  - Session-bound and checked on every request; read-only.
  - Ends when either account changes. Logged in `impersonation_events`.

## Secrets

**The vault.** `ansible/group_vars/vault.yml` on the controller is AES-256 ansible-vault, mode 0600, with key `ansible/.vault_pass`. Both are git-ignored ([`secrets.py`](../src/worms_cli/secrets.py)).

| Entered | Generated when missing, never rotated |
|---|---|
| the three Proxmox tokens (`worms secrets issue` rotates them) | 10 PostgreSQL passwords; the CA passphrase; 2 Django `SECRET_KEY`s; the orchestrator API token; devpi's root password; the WORMS SSH key (ed25519; not regenerated while VMs exist) |

`worms secrets` shows states, never values. One-time passwords are printed once and stored only as hashes.

**Where secrets end up**

| Where | What |
|---|---|
| Every VM | The WORMS public key (cloud-init). No private key, token or password is placed on a team VM |
| Node | `/etc/worms/ssh/worms` (the WORMS key) and `/etc/worms/round-agent.env` (agent DB role, provisioning token), both 0600 |
| Orchestrator | `/etc/orchestrator/django.env` (0600): its own and the migrator's DB passwords, the power and read-only tokens, the API token, Proxmox's CA |
| Dashboard | `/etc/dashboard/django.env` (0600): its DB passwords and the orchestrator API token |
| Scoring, logging, monitor | Their INSERT-only DB password, in 0600 env files |
| Controller | The internal CA (`ansible/pki/worms/`, RSA 4096, key encrypted with the vault passphrase) |

- **TLS.** The CA signs the nginx and PostgreSQL certificates, and every DB client uses `verify-full`. The dashboard's public certificate comes from the same CA, so browsers warn.
- **Per run.** A gameplan's SSH keys and passwords are generated on the node for each run and kept in `/var/lib/worms/runs/<run>.json` (0600) until teardown. They never reach the database.
- **Flags.** Generated on the node; only their SHA-256 leaves it.
- **Gameplan content** stays on the orchestrator and the node:
  - The dashboard sees file names, validity and a run's topology.
  - Errors of the jobs that load a gameplan name only the job ([`handlers.py`](../scripts/round_agent/handlers.py), `_quiet`).

## Firewalls

| Layer | Policy | Where |
|---|---|---|
| Proxmox cluster/node firewall | input DROP. The node accepts its own subnet plus the operator rules for 2222 and 8006, which are added by hand | [`stacks/firewall`](../opentofu/stacks/firewall/main.tf) |
| Per-VM firewall | Every VM: DROP in and out, `ipfilter` and `macfilter` (each NIC pinned to its address). Security groups per role; replaced with the VM | the VM modules, [`stacks/firewall`](../opentofu/stacks/firewall/main.tf) |
| Node nftables | Zone firewall: forwards only the flows below. Bridge filters on services, sandbox and devbox bridges: allowed addresses and ports only, gateway↔gateway dropped. Per-VM rates (services SYN 20/s; any SYN 500/s; UDP 5000 pps) | [`worms.nft.j2`](../ansible/roles/host/host_network/templates/worms.nft.j2) |
| Gateways | sandbox-gw and devbox-gw forward only team networks → service ports and → LLM:443, SNATed to their own address. No default route. **No forwarding between sandbox networks or between a devbox's two links** | [`sandbox-gw.tftpl`](../opentofu/stacks/sandbox/templates/sandbox-gw.tftpl), [`devbox-gw.tftpl`](../opentofu/stacks/devbox/templates/devbox-gw.tftpl) |
| Gameplan rules | Per sandbox VM; peers limited to sandbox CIDRs | [`stacks/sandbox`](../opentofu/stacks/sandbox/main.tf), [5](05-live-environment.md#writing-a-gameplan) |

**Allowed flows** (everything else is dropped):
- internet → dashboard:443; internet → SSH gateway:22 (dev: 2222). Both by DNAT; the client address is kept.
- dashboard → database:5432 and orchestrator:443. Orchestrator → database and Proxmox API:8006.
- scoring, logging, monitor → database:5432. Every infrastructure VM → syslog:514.
- sandbox/devbox VMs → their gateway → DNS, scoring, logging and private internet, on service ports only (TCP 53, 80, 443, 3141, 3142, 5000, 8888; UDP 53, 123).
- sandbox VMs ↔ sandbox VMs: only on a shared network and as the gameplan's rules allow. Devbox VMs ↔ their team's VMs on the team's links.
- sandbox/devbox VMs → `teachllm.mlsec.tu-berlin.de:443` (141.23.117.34): **the one outside exception**. Every layer allows only this address and port, and DNS answers only this outside name. Whatever that server does with requests is part of the containment.
- node → every VM:22 on its management NIC.

**The internet window** ([3](03-manage-py.md#worms-build)) opens 80, 443 and 53 to the internet for the infrastructure and private-internet zones only.
- It is refused while a sandbox VM exists or a devbox VM runs.
- The stacks and the agent refuse team VMs while it is open, and the watchdog stops any that run.
- Its state is runtime-only: a reboot closes it.

## Devbox and sandbox containment

| Measure | State |
|---|---|
| Network | the firewall layers above. Team VMs' bridges get no node address except management and SSH fabric |
| CPU | `x86-64-v3`: no VMX, no `/dev/kvm`. The devbox module refuses `host`/`max`; the agent refuses a worm unless `qm config` says exactly `cpu: x86-64-v3`. Nesting is off on the node (after a reboot) |
| Resources | 1 core at weight 50 (sandbox: as the gameplan says, ≤ 4). 512 MB (sandbox 256–8192 MB). NIC 10 MB/s; disk 200 IOPS / 20 MB/s. Own storage `local-zfs-sandbox` |
| Devices | No guest agent, display or USB. `qm stop` works without the guest |
| Services | Scoring and logging accept `POST /submit` only, with size and rate limits and bans. Their roles are INSERT-only. Attribution is by source address and fails closed ([8](08-user-guide.md#scoring-and-logging)) |
| Caches | Offline: apt-cacher-ng `Offlinemode`, devpi `--offline-mode`, registry read-only, DNS without recursion |
| Clean state | Each attempt starts from a snapshot taken before any team code. Each devbox reset rolls back to a snapshot taken before any team key |
| Monitoring | The node mirrors sandbox traffic to Zeek (devbox traffic is not recorded). The watchdog checks firewalls every 15 s |
| Attribution of captures | Per-attempt random flags. A sandbox flag counts only for the team whose run holds that VM, inside the run's window. Devbox tokens count only on the devbox of their own team |

## Dashboard

- **Authentication.**
  - Django sessions in the database, with `Secure`, `HttpOnly` and `SameSite=Lax` cookies.
  - `/login/` is rate-limited by nginx (10/min per address). Failed sign-ins go to `login_events`.
- **Authorization.**
  - Every team view is scoped server-side to `request.team`, and the API to the token's team.
  - Staff pages need `is_staff`. Staff manage students; only the superuser manages staff.
- **Headers** (in production): HSTS; `X-Frame-Options: DENY`; `nosniff`; Secure CSRF cookies. CSRF protection is on everywhere except the GET-only token API. Templates autoescape, and the JavaScript builds the DOM with `textContent`.
- **Uploads.**
  - At most 100 MiB, 30 per hour, and 2 GiB per team.
  - Must be a zip with a top-level `main.sh`.
  - Stored content-addressed under `MEDIA_ROOT`, the service's only writable path. The node re-checks the hash before using an upload.
- **API tokens.** 256-bit random values; only the SHA-256 is stored; revocable. GET only, no session, `statement_timeout` 3 s.
- **SSH keys.** Strictly validated (D6: a key comment once reached a Jinja template as root). They reach Ansible tagged `!unsafe`.
- **Debug.** `DEBUG` is off on both systems (`PRODUCTION=1`). The debug pages exist only with DEBUG on.
- **What a team sees of others.** The public leaderboard (team names, points, captures per round) and, during registration, team names with member counts. Nothing else.

## Audit trail

| Record | Written by | Read with |
|---|---|---|
| Event tables (append-only): `login_events`, `password_events`, `impersonation_events`, `account_events`, `team_events`, `credential_events`, `competition_events`, `ssh_gateway_logins`, `platform_events` | INSERT-only roles of the dashboard and the node; `written_by` and `created_at` set by the database | `worms audit` |
| `password_resets` | dashboard, in the reset's transaction | SQL |
| auditd on every infrastructure VM | forwarded to the syslog VM | `/var/log/remote/<ip>/audit.log` |
| Sandbox network traffic | Zeek → `network_flows`, `network_events` | progression pages, SQL |
| Host jobs, run events | agent, conductor | staff pages, job logs |

## Findings

IDs are cited in code comments. S = team VMs, D = dashboard, C = cross-cutting. Entries marked "live" were checked on worms-dev on 2026-11-06; the rest come from reading the code on 2026-11-08.

### Open

| ID | Severity | Finding |
|---|---|---|
| S4 | High | **No port isolation** on vmbr4/vmbr6/vmbr7: the hookscript is attached to no VM. Teams share layer 2 there; only the per-VM firewall separates them |
| S18 | High | **Conntrack exhaustion.** One table for all VMs (262 144 entries, 5 days for established TCP; the role's sysctls are overridden). A team could fill it and stop every new connection on the node. Fix: per-source `ct count` limits |
| S19 | Medium–High | **ICMP errors pass every per-VM rule** (Proxmox accepts them before `ipfilter`/`macfilter`). Any source address and MAC: a channel between teams, and MAC poisoning of the SSH gateway. Fix: with S4 |
| S9 | Medium | KSM merges pages across VMs (live): a side channel |
| — | Medium | SMT on (L1TF/MDS between hyperthreads); nesting off only after a reboot |
| S13 | Medium | apt-cacher-ng's report page and cve-search's UI need no login. cve-search runs `cpu: host` with `/dev/kvm` and team VMs reach its :5000 |
| S17 | Medium | A new team VM runs before its firewall options apply (the node's rules still apply) |
| S20 | Medium | Team disks share the ZFS pool with the database disk, with no reservation |
| S21 | Low–Medium | The SSH gateway's shared login slots (`MaxStartups`) can be exhausted from the internet. The node's rate rule limits only logging |
| S22 | Medium | The node's SSH and 8006 accept any source unless the operator rules have `--source` |
| S23 | Low | IPv6 rests on one layer: VMs may send router advertisements (`radv: 1`), and the node accepts them on untrusted bridges |
| S24 | Medium | The service VMs roll back only when a round's sandbox is built. A compromise of one by a team's worm persists into later attempts and devboxes |
| S25 | Low | Gateways and the SSH gateway run CPU type `qemu64`/default, which no check covers |
| S26 | Low | Teams are told to use `ssh -A`: their agent is forwarded into VMs that are rooted by design |
| D3 | Partly | Sign-in is rate-limited per address only; no per-account lockout |
| D4 | Partly | Joining a team needs no consent of its members |
| D5 | Open | Changing one's password does not ask for the current one |
| D8 | Open | No Content-Security-Policy (no XSS sink found) |
| D10 | Design | Staff pages are on the public dashboard. The DB role cannot mint staff, but a stolen staff session can create rounds and accounts |
| D12 | Medium | `/admin/login/` signs staff in without nginx's rate limit and without `login_events` |
| D13 | Low | Team API tokens never expire and survive their creator leaving the team |
| C1 | Accepted | The Proxmox provider uses `insecure = true` |
| C2 | Open | VM host keys are not checked (`StrictHostKeyChecking no`) |
| S11 | Partly | Audit forwarding is plain TCP, sorted by source address |
| S1 | Open | One shared database disk (100 GB): no database backup, no retention for `network_flows` |

### Fixed

- **S** (team VMs):
  - S2: anonymous writes to devpi and the registry.
  - S3: gateway keys could be rewritten.
  - S5: attribution failed open.
  - S6: the internet window could open with team VMs running.
  - S7: a run could start before the last teardown.
  - S8: unbounded DNS query log.
  - S15: a replaced VM lost its firewall.
  - S16: a team login got a shell on the SSH gateway.
  - F14/G8: gateways without nftables.
  - G5: Zeek stopped at log rotation.
  - S27 (2026-11-08): a failing gameplan job could copy gameplan content into the database.
- **D** (dashboard):
  - D1: a session row per anonymous request.
  - D2: devbox rebuilds piled up.
  - D6: SSH key comment template injection, as root.
  - D7: keys shared across teams or kept by leavers.
  - D9: joins failed under the real grants.
  - D11: uploads without quota.
  - D14 (2026-11-08): a team could fetch a log of an attempt that had not finished, by its uuid; the progression also named the attempt's entry VM.
