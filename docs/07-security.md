# 7. Security

> Neither sandbox nor devbox must have internet access. ([`docs/IMPORTANT`](IMPORTANT))

## Threat models

The attackers know the whole architecture and none of the secrets.

| # | Attacker | Has | Must not reach |
|---|---|---|---|
| T1 | A worm or a team member as **root on a sandbox or devbox VM** | Root on the VM. On a devbox: all three VMs of its team, its uploads, its registered keys | the internet; other teams; the infrastructure; the host; another round's results |
| T2 | A **dashboard user** (team member) | A student account, team API tokens | other teams' data; staff functions; the server |
| T3 | A **compromised dashboard VM** (internet-facing) | The dashboard's DB role, the orchestrator API token, the uploads | the host; Proxmox; gameplan content; changing results |

**Trusted:**
- the host, the controller (vault, CA, state), the orchestrator, the database, the round agent;
- gameplan authors: gameplan code runs as root on the host;
- staff, who are less trusted than the admin;
- the LLM endpoint's operators.

## Capabilities

### Proxmox

Tokens are defined in [`tokens.py`](../src/worms_cli/tokens.py). Each is privilege-separated, and `worms check tokens` verifies its rights.

| Token | Role, path | Privileges | Used by |
|---|---|---|---|
| `worms-provision@pve!worms` | `WormsProvision` on `/` | Datastore.*, Pool.Allocate/Audit, SDN.Use, Sys.Audit/Modify, VM.Allocate/Audit/Config.*/PowerMgmt. No user, ACL, console or node power rights | OpenTofu (controller), the round agent (host) |
| `orchestrator@pam!orchestrator-prov` | `WormsSandboxPower` on `/pool/sandbox` | VM.Audit, VM.PowerMgmt | the conductor: sandbox VM power ([`power.py`](../projects/dashboard/orchestrator/rounds/power.py) also checks VM name and run) |
| `topology@pve!readonly` | `WormsTopology` on `/` | Pool.Audit, SDN.Audit, Sys.Audit, VM.Audit | `collect_topology`, `worms status` |

Root SSH to the host goes beyond the tokens:
- OpenTofu uploads snippets as root over SSH with the WORMS key.
- The round agent runs as root.

The agent is bounded by its fixed, validated job menu and a database role that can only read and update `host_jobs` ([5](05-live-environment.md#architecture)).

### Database

PostgreSQL accepts TLS only (`hostssl`, `scram-sha-256`), and each role connects only from its own VM's /32. Grants: [`setup_db_permissions`](../ansible/roles/database/setup_db_permissions/defaults/main.yml), applied in one transaction (revoke all, then grant exactly).

| Role | From | May |
|---|---|---|
| `admin` (superuser) | the host | everything: the `worms` CLI |
| `django_migrator` | orchestrator | owns the tables, runs migrations, `init` |
| `django_orchestrator` | orchestrator | Read: the 25 tables its API, conductor, attempt processor and topology collector use (rounds, teams, uploads, runs, VMs, jobs, attempts, captures, logs, devbox tables, gameplans, topology). Never accounts, sessions, tokens, memberships, notifications, the monitor's tables, nor a gameplan version's `content`. Write: runs, VMs, topology, devbox tables, `gameplans`, `gameplan_simulations`, captures, logs, `run_events`; INSERT `host_jobs`; INSERT only, named columns, into `gameplan_versions` and `gameplan_changes`; named columns of `rounds`/`teams` |
| `django_dashboard` | dashboard | Read: the 31 tables its pages use, every column of `gameplan_versions` but `content`, and only the `id` of `password_resets` (what an INSERT returns). Not `network_events`, `gameplan_simulations`, Django's auth/admin tables (no `/admin/`). Insert: uploads, tokens, SSH keys, memberships, users, teams, rounds, password resets. Update: named columns only. A trigger refuses staff or superuser rows. Never `username`, `is_staff`, `is_superuser` |
| `dbuser_scoring`, `dbuser_logging` | scoring, logging | INSERT into their attempt table only (3–4 columns) |
| `dbuser_network_monitor` | monitor | INSERT into `network_flows`, `network_events` |
| `round_agent` | host | SELECT `host_jobs`; UPDATE its state, timestamps, result, error |
| `dbuser_events_dashboard`, `dbuser_events_node` | dashboard, host | INSERT named columns of their event tables; read nothing |

The 9 event tables are append-only for every role.

### Operating system

- **SSH into VMs.** Key-only, from the host, on the management NIC (cloud-init: `PermitRootLogin prohibit-password`, no passwords; `HardenSSH` pins `ListenAddress`).
- **Gameplan exception.** On sandbox VMs a gameplan may add logins for its own users: SSH passwords (a `Match User` block, [`sandbox_vm`](../ansible/roles/applications/sandbox_vm/tasks/main.yml)), SSH key plus TOTP code (`Match Group worms-totp`), and telnet. The host's own login (`debian`, key-only) is never in either block.
- **Services** run as dedicated users with systemd hardening:
  - common: `NoNewPrivileges`, `ProtectSystem=strict`, `MemoryDenyWriteExecute`;
  - `sandbox-api` and the Zeek shipper also get an empty capability set and `IPAddressAllow=<db>/32`.
  - Zeek keeps only `CAP_NET_RAW`/`CAP_NET_ADMIN`.
- **Root:** auditd and rsyslog, nginx masters, the monitor's NIC one-shot, and on the host the agent, mirror, host firewall and watchdog.
- **Untrusted by design:**
  - sandbox VMs: the worm runs as `debian` with sudo;
  - devbox VMs: teams are root;
  - both kinds of gateway: assumed compromised, and bound by the host's and the VMs' firewalls.

## Account management

| Account | Privilege | Created by |
|---|---|---|
| `admin` | dashboard superuser and staff | `manage.py init` (no usable password); `worms user password` issues a one-time password |
| staff | the staff pages; manage students | `worms user add EMAIL`. The dashboard cannot create staff (DB trigger) |
| students | their own team | **Staff → Users**, one at a time or by CSV. One-time password, changed at first sign-in |
| team access tokens | read their team's results via the API | team members on the profile, after registration closes |

Account rules ([`core/accounts.py`](../projects/dashboard/core/accounts.py), [`middleware.py`](../projects/dashboard/dashboard/middleware.py)):
- **Passwords.**
  - One-time passwords are 14 characters (admin: 20) and shown once.
  - `must_change_password` holds the user on `/change-password/`.
  - Django's validators apply (minimum 8 characters, not common, not numeric).
  - A change ends the user's other sessions.
- **Resets.** Staff reset students; only the superuser resets staff. Every reset writes `password_resets` and a password event.
- **Removal.** Accounts are deactivated, never deleted. Teams are deleted only when empty, or with `worms team delete`.
- **View as.**
  - The superuser only, by POST.
  - Session-bound and checked on every request; read-only.
  - Ends when either account changes. Logged in `impersonation_events`.

## Secrets

**The vault.** `ansible/group_vars/vault.yml` on the controller is AES-256 ansible-vault, mode 0600, with key `ansible/.vault_pass`. Both are git-ignored ([`secrets.py`](../src/worms_cli/secrets.py)).

| Entered | Generated when missing, never rotated |
|---|---|
| the three Proxmox tokens (`worms secret issue` rotates them) | 10 PostgreSQL passwords; the CA passphrase; 2 Django `SECRET_KEY`s; the orchestrator API token; devpi's root password; the WORMS SSH key (ed25519; not regenerated while VMs exist) |

`worms secret` shows states, never values. One-time passwords are printed once and stored only as hashes.

**Where secrets end up**

| Where | What |
|---|---|
| Every VM | The WORMS public key (cloud-init). No private key, token or password is placed on a team VM |
| Host | `/etc/worms/ssh/worms` (the WORMS key) and `/etc/worms/round-agent.env` (agent DB role, provisioning token), both 0600 |
| Orchestrator | `/etc/orchestrator/django.env` (0600): its own DB password (never the migrator's, the tables' owner: Ansible passes that to migrate, init and create_users only), the power and read-only tokens, the API token, Proxmox's CA |
| Dashboard | `/etc/dashboard/django.env` (0600): its DB passwords and the orchestrator API token |
| Scoring, logging, monitor | Their INSERT-only DB password, in 0600 env files |
| Controller | The internal CA (`ansible/pki/worms/`, RSA 4096, key encrypted with the vault passphrase) |

- **TLS.** The CA signs the nginx and PostgreSQL certificates, and every DB client uses `verify-full`. The dashboard's public certificate comes from the same CA, so browsers warn.
- **Per run.** A gameplan's SSH keys, passwords and TOTP seeds are generated on the host for each run and kept in `/var/lib/worms/runs/<run>.json` (0600) until teardown. They never reach the database. The documents, archives and vaults that hold them are made on the host too. `vm.read_file` returns only a file's size and SHA-256.
- **Flags.** Generated on the host; only their SHA-256 leaves it.
- **Gameplan content** stays on the orchestrator and the host:
  - The dashboard sees file names, validity and a run's topology.
  - And each gameplan's `description`: text its author wrote for teams, published on purpose. The orchestrator records it with the version; the round page shows it to every signed-in user, and validation refuses a run's secrets in it ([5](05-live-environment.md#__init__-the-sandbox)).
  - The database holds every version's bytes (`gameplan_versions.content`, [5](05-live-environment.md#versions)). Only the orchestrator, which has the files anyway, and the admin may read them. A role that may read every table gets every column but `content` (setup_db_permissions, `postgresql_restricted_columns`); every other role gets nothing. A compromised dashboard learns each version's hash, size and description, never its code.
  - The record cannot be rewritten: the database checks each version's hash against its bytes, grants no one UPDATE or DELETE on the versions or their history, and refuses both to the owner with a trigger. The host and the orchestrator never run code from the database, so a database write cannot reach the host.
  - And the simulations staff ask for ([5](05-live-environment.md#simulation)): what a run's timeline would show (VMs, networks, addresses, firewall rules; states, power, flags, waits, files by number). A compromised dashboard can ask for one of every gameplan, so it learns the sandbox's shape and the attempt's timing. Never paths, contents, setup commands, packages, log lines or problems.
  - A simulation runs gameplan code on the orchestrator in a child process: 20 s, 10 s of CPU, 1 GiB, 16 MiB of files; stdout carries only its record. One is made at a time (an advisory lock; others get 503), so the API token keeps at most one CPU busy.
  - Errors of the jobs that load a gameplan name only the job ([`handlers.py`](../scripts/round_agent/handlers.py), `_quiet`).

## Firewalls

| Layer | Policy | Where |
|---|---|---|
| Proxmox cluster/node firewall | input DROP. The host accepts its own subnet plus the operator rules for 2222 and 8006, which are added by hand | [`stacks/firewall`](../opentofu/stacks/firewall/main.tf) |
| Per-VM firewall | Every VM: DROP in and out, `ipfilter` and `macfilter` (each NIC pinned to its address). Security groups per role; replaced with the VM | the VM modules, [`stacks/firewall`](../opentofu/stacks/firewall/main.tf) |
| Host nftables | Zone firewall: forwards only the flows below. Bridge filters on services, sandbox and devbox bridges: allowed addresses and ports only; on the services bridge every packet has a sandbox or devbox gateway at one end (gateway↔gateway and service VM↔service VM dropped). Management bridges (vmbr2, vmbr4, vmbr5, vmbr6): VM↔VM dropped, a VM reaches the host only. Per-VM rates (services SYN 20/s; any SYN 500/s; UDP 5000 pps) | [`worms.nft.j2`](../ansible/roles/host/host_network/templates/worms.nft.j2) |
| Gateways | sandbox-gw and devbox-gw forward only team networks → service ports and → LLM:443, SNATed to their own address. No default route. **No forwarding between sandbox networks or between a devbox's two links** | [`sandbox-gw.tftpl`](../opentofu/stacks/sandbox/templates/sandbox-gw.tftpl), [`devbox-gw.tftpl`](../opentofu/stacks/devbox/templates/devbox-gw.tftpl) |
| Gameplan rules | Per sandbox VM; peers limited to sandbox CIDRs | [`stacks/sandbox`](../opentofu/stacks/sandbox/main.tf), [5](05-live-environment.md#writing-a-gameplan) |

### Target architecture

| VM | Ingress | Egress |
|---|---|---|
| Dashboard | 443 (public, host DNAT) | database:5432, orchestrator:443 |
| Orchestrator | 443 from the dashboard | database:5432; **exception:** the Proxmox API (host:8006: sandbox power, topology; two narrow tokens) |
| Scoring, logging | 443 from the sandbox and devbox gateways | database:5432 |
| Monitor | the mirror bridge (a copy of all sandbox traffic) | database:5432 |
| Database | 5432 from the dashboard, orchestrator, scoring, logging, monitor; **exception:** the host (round agent, `worms`) | none |
| Sandbox VMs | – | scoring, logging, their sandbox networks (the gameplan's rules), the private internet; **exception:** the LLM endpoint |
| Devbox VMs | 22 from the SSH gateway | scoring, logging, their team's A–B–C links, the private internet; **exception:** the LLM endpoint |
| Private internet (DNS included) | its own service port, from the sandbox and devbox gateways | none |
| SSH gateway | 22 (public, host DNAT) | devbox VMs:22 |
| Every VM | SSH from the host, on its own management NIC (management bridges: VM↔VM dropped) | – |
| Every VM but sandbox and devbox | – | the internet (80, 443, 53) while the window is open |
| Every infrastructure VM | – | **exception:** its audit log to the syslog VM (TCP 514, rate-limited) |

The exceptions are deliberate (2026-11-20): the audit trail, the conductor's sandbox power, the round agent's job queue and the teams' LLM access. Database roles: least privilege ([Database](#database)); scoring, logging and the monitor are INSERT-only on their own tables.

### Trust zones

| Zone | VMs | Reaches | Reached by |
|---|---|---|---|
| Trusted | the host, the orchestrator, the dashboard (internet-facing: T3) | the database; dashboard → orchestrator; orchestrator → Proxmox API | the internet (dashboard:443 only) |
| Choke point | the database | nothing | the trusted zone and the semi-trusted one, each role from its own VM, with its own grants |
| Semi-trusted | scoring, logging, the monitor | the database (INSERT-only roles) | the sandbox and devbox gateways (scoring, logging); the sandbox mirror (monitor) |
| For the untrusted | DNS, the private internet (apt-cache, devpi, docker-registry, manpages, ntp, cve-search, wikipedia) | nothing (their packages: the Debian mirrors, in the internet window) | the sandbox and devbox gateways only |
| Untrusted | sandbox and devbox VMs, their gateways | through their gateway: DNS, scoring, logging, the private internet; the LLM endpoint | the host (SSH); devbox VMs: the SSH gateway |

The database is the one place where the trusted zone meets the rest. Nothing on the trusted side reaches scoring, logging, DNS or the private internet, and nothing but the sandbox and devbox gateways reaches DNS or the private internet: their per-VM rules accept the gateways' addresses only (sandbox-gw `10.2.0.2`, the devbox gateways `10.2.0.32/27`), and the host's bridge filter drops service VM↔service VM on the services bridge a second time. On the management bridges every VM reaches the host only. Besides the database, one other flow crosses zones: every infrastructure VM's audit log to the syslog VM (TCP 514, rate-limited per sender).

**Allowed flows** (everything else is dropped):
- internet → dashboard:443; internet → SSH gateway:22 (dev: 2222). Both by DNAT; the client address is kept.
- dashboard → database:5432 and orchestrator:443. Orchestrator → database and Proxmox API:8006.
- scoring, logging, monitor → database:5432. Every infrastructure VM → syslog:514.
- sandbox/devbox VMs → their gateway → DNS, scoring, logging and private internet, on service ports only (TCP 53, 80, 443, 3141, 3142, 5000, 8888; UDP 53, 123). DNS and the private internet accept nothing else; no service VM reaches another.
- sandbox VMs ↔ sandbox VMs: only on a shared network and as the gameplan's rules allow. Devbox VMs ↔ their team's VMs on the team's links.
- sandbox/devbox VMs → `teachllm.mlsec.tu-berlin.de:443` (141.23.117.34): **the one outside exception**. Every layer allows only this address and port, and DNS answers only this outside name. Whatever that server does with requests is part of the containment.
- host → every VM:22 on its management NIC.

**The internet window** ([3](03-manage-py.md#worms-build)) opens 80, 443 and 53 to the internet for the infrastructure and private-internet zones only.
- It is refused while a sandbox VM exists or a devbox VM runs.
- The stacks and the agent refuse team VMs while it is open, and the watchdog stops any that run.
- Its state is runtime-only: a reboot closes it.

## Devbox and sandbox containment

| Measure | State |
|---|---|
| Network | the firewall layers above. Team VMs' bridges get no host address except management and SSH fabric |
| CPU | `x86-64-v3`: no VMX, no `/dev/kvm`. The devbox module refuses `host`/`max`; the agent refuses a worm unless `qm config` says exactly `cpu: x86-64-v3`. Nesting is off on the host (after a reboot) |
| Resources | 1 core at weight 50 (sandbox: as the gameplan says, ≤ 4). 512 MB (sandbox 256–8192 MB). NIC 10 MB/s; disk 200 IOPS / 20 MB/s. Own storage `local-zfs-sandbox` |
| Devices | No guest agent, display or USB. `qm stop` works without the guest |
| Services | Scoring and logging accept `POST /submit` only, with size and rate limits and bans. Their roles are INSERT-only. Attribution is by source address and fails closed ([8](08-user-guide.md#scoring-and-logging)) |
| Caches | Offline: apt-cacher-ng `Offlinemode`, devpi `--offline-mode`, registry read-only, DNS without recursion |
| Clean state | Each attempt starts from a snapshot taken before any team code. Each devbox reset rolls back to a snapshot taken before any team key |
| Monitoring | The host mirrors sandbox traffic to Zeek (devbox traffic is not recorded). The watchdog checks firewalls every 15 s |
| Attribution of captures | Per-attempt random flags. A sandbox flag counts only for the team whose run holds that VM, inside the run's window. Devbox tokens count only on the devbox of their own team |

## Dashboard

- **Authentication.**
  - Django sessions in the database, with `Secure`, `HttpOnly` and `SameSite=Lax` cookies.
  - `/login/` is rate-limited by nginx (10/min per address). Failed sign-ins go to `login_events`.
- **Authorization.**
  - Every team view is scoped server-side to `request.team`, and the API to the token's team.
  - Staff pages need `is_staff`. Staff manage students; only the superuser manages staff.
- **Headers** (in production): HSTS; `X-Frame-Options: DENY`; `nosniff`; Secure CSRF cookies. On every dashboard response (also in development), a Content-Security-Policy ([`config/settings/dashboard.py`](../projects/dashboard/config/settings/dashboard.py)): `default-src 'none'`; scripts, styles, images and fetches from its own origin only, nothing inline or evaluated; forms post there only; no frames, plugins or `<base>`. The browser tests serve their pages with it and fail on any violation. CSRF protection is on everywhere except the GET-only token API. Templates autoescape, and the JavaScript builds the DOM with `textContent`.
- **Uploads.**
  - At most 100 MiB, 30 per hour, and 2 GiB per team.
  - Must be a zip with a top-level `main.sh`.
  - Stored content-addressed under `MEDIA_ROOT`, the service's only writable path. The host re-checks the hash before using an upload.
- **API tokens.** 256-bit random values; only the SHA-256 is stored; revocable. GET only, no session, `statement_timeout` 3 s.
- **SSH keys.** Strictly validated (D6: a key comment once reached a Jinja template as root). They reach Ansible tagged `!unsafe`.
- **Debug.** `DEBUG` is off on both systems (`PRODUCTION=1`). The debug pages exist only with DEBUG on.
- **What a team sees of others.** The public leaderboard (team names, points, captures per round) and, during registration, team names with member counts. Nothing else.

## Audit trail

| Record | Written by | Read with |
|---|---|---|
| Event tables (append-only): `login_events`, `password_events`, `impersonation_events`, `account_events`, `team_events`, `credential_events`, `competition_events`, `ssh_gateway_logins`, `platform_events` | INSERT-only roles of the dashboard and the host; `written_by` and `created_at` set by the database | `worms show audit` |
| `password_resets` | dashboard, in the reset's transaction | SQL |
| auditd on every infrastructure VM | forwarded to the syslog VM | `/var/log/remote/<ip>/audit.log` |
| Sandbox network traffic | Zeek → `network_flows`, `network_events` | progression pages, SQL |
| Host jobs, run events | agent, conductor | staff pages, job logs |

## Findings

IDs are cited in code comments. S = team VMs, D = dashboard, C = cross-cutting. Entries marked "live" were checked on worms-dev on 2026-11-06; the rest come from reading the code on 2026-11-08.

### Open

| ID | Severity | Finding |
|---|---|---|
| S4 | High | **No port isolation** on vmbr7 (the devbox SSH fabric): the hookscript is attached to no VM. Teams' devbox VMs share layer 2 there; only the per-VM firewall separates them. The other management bridges (vmbr2, vmbr4, vmbr5, vmbr6) are isolated by the host's bridge filter since 2026-11-20 (VM↔VM dropped); vmbr7 cannot be, as the SSH gateway reaches the devbox VMs there |
| S18 | High | **Conntrack exhaustion.** One table for all VMs (262 144 entries, 5 days for established TCP; the role's sysctls are overridden). A team could fill it and stop every new connection on the host. Fix: per-source `ct count` limits |
| S19 | Medium–High | **ICMP errors pass every per-VM rule** (Proxmox accepts them before `ipfilter`/`macfilter`). Any source address and MAC: a channel between teams, and MAC poisoning of the SSH gateway. Fix: with S4 |
| S9 | Medium | KSM merges pages across VMs (live): a side channel |
| — | Medium | SMT on (L1TF/MDS between hyperthreads); nesting off only after a reboot |
| S13 | Medium | apt-cacher-ng's report page and cve-search's UI need no login. cve-search runs `cpu: host` with `/dev/kvm` and team VMs reach its :5000 |
| S17 | Medium | A new team VM runs before its firewall options apply (the host's rules still apply) |
| S31 | Medium | Under memory pressure Proxmox's autoballoon can shrink platform VMs to their floors, below what monitor (Zeek), devpi or cve-search use; scoring and logging go to 256 MB. It happened on worms-dev on 2026-11-23 while a sandbox was built (the host above 80 %): it took sandbox-gw to 256 MB, the guest thrashed at its first apt update and every build hung until its timeout (the gateway has no balloon since). The preflight warns when the VMs that run could outgrow the host; the platform VMs' floors are unchanged |
| S20 | Medium | Team disks share the ZFS pool with the database disk, with no reservation |
| S21 | Low–Medium | The SSH gateway's shared login slots (`MaxStartups`) can be exhausted from the internet. The host's rate rule limits only logging |
| S22 | Medium | The host's SSH and 8006 accept any source unless the operator rules have `--source` |
| S23 | Low | IPv6 rests on one layer: VMs may send router advertisements (`radv: 1`), and the host accepts them on untrusted bridges |
| S24 | Medium | The service VMs roll back only when a round's sandbox is built. A compromise of one by a team's worm persists into later attempts and devboxes |
| S25 | Low | Gateways and the SSH gateway run CPU type `qemu64`/default, which no check covers |
| S26 | Low | Teams are told to use `ssh -A`: their agent is forwarded into VMs that are rooted by design |
| D3 | Partly | Sign-in is rate-limited per address only; no per-account lockout |
| D4 | Partly | Joining a team needs no consent of its members |
| D5 | Open | Changing one's password does not ask for the current one |
| D10 | Design | Staff pages are on the public dashboard. The DB role cannot mint staff, but a stolen staff session can create rounds and accounts |
| D13 | Low | Team API tokens never expire and survive their creator leaving the team |
| C1 | Accepted | The Proxmox provider uses `insecure = true` |
| C2 | Open | VM host keys are not checked (`StrictHostKeyChecking no`) |
| C3 | Partly | **The WORMS key's ssh-agent outlives a CLI run that is killed** (live, 2026-11-22). A run ended by a signal (its terminal closed, SIGTERM) never reaches its `atexit`, so its agent stays with the WORMS key loaded. `worms login` and `worms log <VM>` left one at every run, since they `exec`ed ssh: fixed (below). worms-dev had 53 such agents, up to 18 days old; they are not cleaned up by the fix. worms-gui stops its own agent on SIGHUP and SIGTERM too |
| S11 | Partly | Audit forwarding is plain TCP, sorted by source address |
| S1 | Open | One shared database disk (100 GB): no database backup, no retention for `network_flows` |

### Fixed

- **S** (team VMs):
  - S2: anonymous writes to devpi and the registry.
  - S3: gateway keys could be rewritten.
  - S5: attribution failed open.
  - S6: the internet window could open with team VMs running.
  - S7: a run could start before the last teardown.
  - S8: unbounded DNS query log. Reopened and fixed again on 2026-11-18: only the `queries` category was bounded. The failed lookups (`query-errors`) and the rest of named's log still went to syslog, which rotates weekly with no size limit. The devboxes' NTP pool lookups, about 30 000 failures an hour, filled the DNS VM's 4.8 GB disk in a week. Every BIND category now goes to a bounded file under `/var/log/named/`.
  - S15: a replaced VM lost its firewall.
  - D8 (2026-11-22): no Content-Security-Policy. Now a strict one; it found the topology page setting `style` attributes (now through the CSSOM).
  - D12 (2026-11-20): `/admin/login/` signed staff in without nginx's rate limit and without `login_events`. Django's admin is no longer served.
  - S16: a team login got a shell on the SSH gateway.
  - F14/G8: gateways without nftables.
  - G5: Zeek stopped at log rotation.
  - S27 (2026-11-08): a failing gameplan job could copy gameplan content into the database.
  - S33 (2026-11-22): dns recorded no audit event from 2026-11-14 to 2026-11-22. Its disk had filled with DNS query logs (S8), and Debian's auditd.conf suspends logging then and never resumes by itself, while `auditctl -s` still says enabled, lost 0. The role sets every disk action to SYSLOG now, and resumes a suspended auditd. That week of dns's events is lost.
  - S29 (2026-11-22): a sandbox VM that died during a run (an OOM kill, a crash) went unnoticed and the attempt counted; every QEMU had `oom_score_adj` 0, so an OOM kill took the biggest VM (the database, cve-search). Now the conductor watches every sandbox VM and the gateway through the Proxmox API and asks the host why one stopped: only the guest's own power-off is the worm's doing, anything else fails the attempt. Team VMs' QEMUs get `oom_score_adj` 1000, and a preflight refuses a sandbox whose ceilings the host cannot hold ([5](05-live-environment.md#an-attempt)).
  - S30 (2026-11-22): the firewall watchdog passed without checking anything when it could not list the VMs, had no timeout, and stopped every team VM during sandbox builds before proxmox-firewall had compiled their rules (2026-11-12, -13). Now it fails loudly, ends after 60 s, and checks a VM whose firewall configuration just changed on its next run.
  - S32 (2026-11-22): a QEMU killed by a signal or aborting (no `systemd-coredump` on the host, no kernel crash line) read as the guest's own power-off, and the attempt counted. The kernel unregisters such a QEMU's taps itself (`tap<id>i0 (unregistering)`); a QEMU that exits, after the guest's power-off as after a host shutdown, closes them first. Tried on worms-dev with `kill -KILL`, an ACPI power-off and `qm shutdown`: crash, guest, host.
  - The sandbox gateway tracked every sandbox-to-services connection with the kernel's default for 512 MB (about 4000 entries); now 262 144.
  - S28 (2026-11-18): no VM's clock was synced. timesyncd asked Debian's fallback pool, which the private resolver refuses, and UDP 123 is not forwarded, so the private NTP server never synced either. After two weeks the platform VMs ran 39 s ahead of the host, whose clock stamps a worm's start. Now:
    - every platform VM's chrony reads the host's clock through KVM's PTP clock (`ptp_kvm`), a hypercall every KVM guest has. No VM gets a network path to the host for it;
    - the NTP VM does the same and serves the services network;
    - team VMs and gateways ask only the NTP VM, with no fallback pool.
    - Still unsynced: the devbox SSH gateway. It has no package source for chrony, and it serves only SSH forwarding.
  - Disk filling by logs (with S8): rsyslog's files on every platform VM rotate past 100 MB, checked hourly.
  - Journal noise (2026-11-18): no VM has a serial port, but the image's kernel names `console=ttyS0`, so systemd's getty there failed and restarted every 10 s on every VM (some 26,000 journal lines a day; counters past 114,000). Masked where ttyS0 does not answer ([`serial_getty.yml`](../ansible/roles/system/log_limits/tasks/serial_getty.yml)): platform VMs by role `log_limits` (`Site.yml`), team VMs and gateways by their roles.
- **D** (dashboard):
  - D1: a session row per anonymous request.
  - D2: devbox rebuilds piled up.
  - D6: SSH key comment template injection, as root.
  - D7: keys shared across teams or kept by leavers.
  - D9: joins failed under the real grants.
  - D11: uploads without quota.
  - D14 (2026-11-08): a team could fetch a log of an attempt that had not finished, by its uuid; the progression also named the attempt's entry VM.
- **C** (cross-cutting):
  - C3, in part (2026-11-22): `worms login` and `worms log <VM>` replaced themselves with ssh (`exec`), so the CLI's `atexit` never stopped its ssh-agent, and one agent with the WORMS key stayed behind per login (worms-gui's `l` and `L` made that frequent). ssh now runs as the CLI's child (`vms.attached`), and the agent ends with the CLI.
