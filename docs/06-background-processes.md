# 6. Infrastructure background processes

This page lists every long-running service, timer and loop, by host. Unit files are rendered by Ansible from the role named in the table. Hardening is in [7 Security](07-security.md).

## Proxmox node

| Unit | Kind, schedule | User | Does | Source |
|---|---|---|---|---|
| `worms-host-firewall` | oneshot at boot, before `pve-guests` | root | Loads `/etc/worms/worms.nft` (zone firewall, DNAT, bridge filters); `ExecStop` deletes it | [`host_network`](../ansible/roles/host/host_network/templates/) |
| `worms-firewall-watchdog.timer` | every 15 s (from 1 min after boot) | root | Hard-stops **every** running sandbox and devbox VM if any of these holds: the internet window is open; `proxmox-firewall` is down; a host nft table is missing; a running team VM's tap has no rules. Logs `auth.crit` and a `watchdog_stopped` event | `worms-firewall-watchdog.sh.j2` |
| `worms-mirror` | daemon, `Restart=always`; reacts to `ip monitor link`, rechecks every 10 s | root | Mirrors every sandbox bridge port (`vmbr1*`) into `vmbr3` for the monitor with `tc mirred`; vmbr3 itself drops everything | `worms-mirror.sh.j2` |
| `worms-round-agent` | daemon, `Restart=always` | root | The round agent ([5](05-live-environment.md#architecture)): `LISTEN host_jobs` plus a 5 s poll; one job at a time; heartbeat every 30 s. Runs via `worms-node-run`, with the WORMS key in a private ssh-agent | [`round_agent`](../ansible/roles/host/round_agent/), [`agent.py`](../scripts/round_agent/agent.py) |
| `worms-audit-gateway.timer` | every 60 s | root | `worms-audit collect-gateway`: reads new SSH-gateway logins and matches each to the client address the node logged (`worms-ssh-gw:`). Writes them to `ssh_gateway_logins` | [`events.py`](../scripts/round_agent/events.py) |
| `proxmox-firewall`, `pve-*` | Proxmox | root | Per-VM firewalls (nftables backend) | Proxmox |

**Job logs**
- Location: `/var/log/worms/jobs/<uuid>.log`, with `latest.log` pointing at the newest. The directory is 0700.
- Retention: 30 days.
- Only argv is logged; secrets go over stdin.

**Run state**
- `/var/lib/worms/runs/<run>.json` holds one run's secrets, mode 0600.
- `/var/lib/worms/devbox-teams` is the attribution map, restored after a rollback.

## Orchestrator VM

Every unit below runs as `django:www-data`, with `/etc/orchestrator/django.env` ([`group_vars/orchestrator.yml`](../ansible/group_vars/orchestrator.yml), [`django_app` templates](../ansible/roles/applications/django_app/templates/)).

| Unit | Kind, schedule | Does |
|---|---|---|
| `orchestrator` | gunicorn behind nginx :443 | The REST API for the dashboard: devbox resets, gameplan simulations (each in a child process, [5](05-live-environment.md#simulation)) |
| `orchestrator-conduct-rounds` | daemon, `Restart=always` (5 s) | Plans due rounds and runs one attempt per tick; idles 5 s. SIGTERM fails a running attempt, which is then retried ([`conduct_rounds.py`](../projects/dashboard/orchestrator/management/commands/conduct_rounds.py)) |
| `orchestrator-process-attempts` | daemon, `Restart=always`; idles 2 s | The single writer of captures and logs, for rounds and devboxes. Also applies finished devbox jobs and advances dev runs |
| `orchestrator-collect-topology.timer` | every 2 min, `Persistent` | Snapshots VMs, bridges and firewall rules from Proxmox with the read-only token |

`SetupDbAndDashboard.yml` stops the daemons while it migrates, so **a deploy during a round costs the running team an attempt**.

## Dashboard VM

| Unit | Does |
|---|---|
| `dashboard` | gunicorn: 2 workers on a unix socket, user `django`, writes only to `/opt/dashboard/media` |
| `nginx` | nginx.org's stable package, from its own repository (keys shipped in [`nginx_tls`](../ansible/roles/web/nginx_tls/), pinned over Debian's), configured as Debian's was: `sites-enabled/`, user `www-data`. TLS, HSTS. Rate limits: `/login/` 10/min per address; `/api/` 5/s per address and 3/s per token. Upload bodies up to 101 MB |
| `fail2ban` | Installed, with no jail configured |

The dashboard runs no background work of its own: it has no timers and no Django signals.

## Scoring and logging VMs

| Unit | Does |
|---|---|
| `sandbox-api` | Flask under gunicorn (1 worker, 8 threads), user `sandbox-api`. May reach only the database (`IPAddressAllow`). Reads `/etc/sandbox-api/current-team` and `devbox-teams` on each request (cached by mtime) |
| `nginx` | nginx.org's, as on the dashboard. `POST /submit` only; body caps; 50 r/s per source |

## Monitor VM

| Unit | Kind | Does |
|---|---|---|
| `worms-capture-iface` | oneshot, root | Puts the capture NIC in promiscuous mode, with no address, ARP or IPv6 |
| `zeek` | daemon, user `zeek` (`CAP_NET_RAW`, `CAP_NET_ADMIN`) | Standalone on vmbr3. JSON logs rotate hourly; stats every minute; capture loss every 5 min |
| `zeek-shipper` | daemon, user `zeek-shipper` | `conn.log` → `network_flows`; dns/http/ssl/ssh/… → `network_events`. Capped at 2000 flows/s and 1000 events/s. Heartbeat every 60 s |
| `zeek-archive-cleanup.timer` | hourly | Deletes archived logs older than 48 h |

Devbox traffic is not mirrored, so it is not recorded.

## Syslog VM and audit

| Where | Unit | Does |
|---|---|---|
| syslog | `rsyslog` (imtcp :514) | Writes each sender's lines to `/var/log/remote/<sender ip>/audit.log`. The directory comes from the TCP peer, not from the message. Rotated daily, kept 30 days |
| syslog | `worms-reply-route` | oneshot: a policy route so replies leave by the siem bridge |
| every infrastructure and private-internet VM | `auditd`, `rsyslog` | auditd rules (exec, identity, config, persistence, modules) are forwarded over plain TCP, with a disk queue of up to 1 GB ([`audit/*`](../ansible/roles/audit/)) |
| every platform VM (not the team VMs or their gateways) | `chrony` | The host's clock through KVM's PTP clock (`refclock PHC /dev/ptp0`, module `ptp_kvm`): no network source and no route to the host. Steps whenever 1 s off, e.g. after a snapshot rollback ([`timesync`](../ansible/roles/system/timesync/)) |
| every platform VM | `logrotate.timer` | Hourly, not daily: rsyslog's files rotate past 100 MB whatever the week ([`log_limits`](../ansible/roles/system/log_limits/)) |

## DNS and private internet

| VM | Units | Notes |
|---|---|---|
| dns | `named` | Authoritative for `worms.internal`. Recursion only while the internet window is open. Every log is a file under `/var/log/named/` capped at 4 × 20 MB: `query.log` (queries and failed lookups), `named.log` (the rest). Nothing goes to syslog |
| apt-cache | `apt-cacher-ng` (:3142) | `Offlinemode: 1` while offline. After each successful seed: what it fetched in `/etc/apt-cache-package-list`, the commit in `/etc/seed-version` (`-dirty`: the role had uncommitted changes) |
| pypi | `devpi` (:3141) | `--offline-mode` while offline; only root may upload. After each successful seed: what it fetched in `/etc/pypi-package-list`, the commit in `/etc/seed-version` |
| docker-registry | `docker-registry` (:5000) | Read-only while offline; nothing seeds it |
| manpages | `apache2` (:80) | man2html |
| ntp | `chrony` (UDP 123) | Follows the host's clock like every platform VM (below) and serves it to the services network; `local stratum 10` if that clock cannot be read. Never asks the internet |
| cve-search | `mongod`, `redis-server`, `cve-search` (:5000) | `cve-search` is enabled but not started. Fill its data by hand with `/usr/local/bin/cve-search-populate` |
| wikipedia | `kiwix-serve` (:8888) | Skipped until a ZIM file exists. Download it by hand with `/usr/local/bin/wikipedia-download` |

`UpdateVMs.yml` masks `apt-daily*` and `apt-listchanges` and purges `unattended-upgrades` on every VM, so packages change only during a build.

## Gateways and team VMs

| Where | Unit | Does |
|---|---|---|
| sandbox-gw, devbox-gw | `nftables` | Forward rules, NAT (cloud-init writes them, role `untrusted_gateway` enables them) |
| sandbox-gw, devbox-gw | `worms-llm-route` | oneshot: a /32 route to the LLM endpoint via the node |
| devbox-ssh-gateway | `sshd` | Port forwarding to the teams' A VMs |
| sandbox and devbox VMs, both gateways | `systemd-timesyncd` | Asks only the private NTP server, by address (`ntp.worms.internal`); no fallback pool ([`ntp_client`](../ansible/roles/private_internet/ntp_client/)) |
| sandbox and devbox VMs | `worms-start.timer` / `.service` (transient) | The armed worm: `bash main.sh` as `debian`. On a devbox, `RuntimeMaxSec=3600` |

## Backups

| What | State |
|---|---|
| vzdump of the persistent VMs (`worms-stateful`, 02:30 daily, keep 7 daily / 4 weekly) | Created only if `backup_storage` is set ([`backup.tf`](../opentofu/stacks/infrastructure/persistent/backup.tf)); off by default |
| Database dumps | None |
| Controller state: vault, `.vault_pass`, `ansible/pki/worms/`, `terraform.tfstate` | By hand, off the node ([2](02-installation.md#4-build)) |
