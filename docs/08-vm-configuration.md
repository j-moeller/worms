# 8. What Ansible runs on each VM

Per VM type: which command configures it, and the roles that run there, in their order. A role is one intent (`nginx_tls`, `django_app`, ...); [`Site.yml`](../ansible/Site.yml) says which VM gets which, in four phases ([3](03-manage-py.md#site)):

| Phase | Where | |
|---|---|---|
| 1 | the controller | the CA |
| 2 | `dns` | DNS, before every other VM needs it |
| 3 | every platform VM, all at once (`strategy: free`) | each VM's own roles, in this document's order |
| 4 | the orchestrator, the dashboard, the database, in step | the migrations, then the grants (never skipped: the database says what it lacks) |

In phases 2 and 3 a role whose files, variables and sources did not change since it last ran on a VM is skipped there ([stamps](03-manage-py.md#site)); `--full` runs it anyway. Each VM's whole log of a run: `worms log run [N] <vm>`. The numbers in brackets are the role's tasks.

## The commands

| Command | Runs |
|---|---|
| `worms apply <component>...`, `worms apply` (what changed) | `Site.yml --limit <its groups>,localhost --skip-tags packages`; with the orchestrator also the database's group (its grants). With `--packages`, or a VM that is new: without `--skip-tags`, inside `Internet.yml -e internet=allowed` … `internet=denied`. Service VMs first: `WaitForVMs.yml --limit` after their rollback to `clean`, again after their new snapshot |
| `worms apply --all` | `SetupHostNetwork.yml`; then in the window: `Site.yml` (every VM, packages), `SetupRoundAgent.yml`; then the offline mode (below) |
| `worms apply round-agent`, `worms apply host-network` | `SetupRoundAgent.yml`, `SetupHostNetwork.yml` on the node |
| `worms apply gameplans` (and after the orchestrator or the round agent) | `SetupRoundAgent.yml --tags agent-sync`, then `Site.yml --limit orchestrator --tags gameplans` |
| `worms internet on`, `worms internet off` | `Internet.yml -e internet=allowed|denied` on the node; `off` then the offline mode: `Site.yml --limit dns,<the private internet> --skip-tags packages` |
| `worms vm reset services` | `WaitForVMs.yml --limit <them>` after the rollback; no configuration |
| `worms vm rebuild <vm>` | OpenTofu `-replace`, then as `worms apply <component>` (in the window: a new VM) |
| `worms step <name>` | one step: `offline`, `inventory`, `ping`, … (`worms step --help`) |
| The round agent's jobs | on the node, in its workspace `/opt/worms`, with the node's Ansible (ansible-core 2.19): below, per team VM |

## The controller

- **Phase 1:** `ca_create` (10): the internal CA in `ansible/pki/worms/` (its key encrypted with the vault's passphrase). Once it is there, one check and nothing else.

## The Proxmox node

- `SetupHostNetwork.yml` → `host_network` (24): nftables (the host firewall, NAT, the published ports: dashboard 443, the SSH gateway), sysctls, nested virtualisation off, the firewall watchdog, the traffic mirror for the monitor.
- `Internet.yml`: the internet window's state (the flag `/run/worms/internet-allowed`, a runtime nft set, the `provisioning` security group).
- `SetupRoundAgent.yml` → `round_agent` (12): the agent's Python libraries, its workspace (a copy of the checkout, the CA certificate, the platform and firewall states, the node's stacks initialised), the WORMS key, its environment, the service; the node's events account, `worms-audit`, the SSH gateway login collector, the least-privilege Proxmox roles. `--tags agent-sync`: the workspace alone.

## dns

- **Phase 2:** `setup_dns` (14): BIND switched to the current internet mode first (it is this VM's own resolver), BIND installed, systemd-resolved off, `resolv.conf` to localhost, the query log bounded, the `worms.internal` zone from the inventory, forwarders only while the window is open.
- **Phase 3:** `apt_updates` (3): Debian's automatic apt runs masked, `unattended-upgrades` removed, every package upgraded while the window is open. `timesync` (6): chrony follows the node's clock (KVM PTP), no network source. `sshd_listen` (3): sshd on the management address only. `auditd` (4): auditd, its rules, the audit log forwarded to syslog. `log_limits` (6): rsyslog's files rotate past their size, hourly; the getty of the missing serial port masked.

## syslog

- **Phase 3:** `apt_updates` (3), `timesync` (6), `sshd_listen` (3); `syslog_server` (7): rsyslog receives every platform VM's audit log, one file per sender (`/var/log/remote/<ip>/audit.log`), rotated; the reply route; `auditd` (4), `log_limits` (6).

## database

- **Phase 3:** `apt_updates` (3), `timesync` (6), `sshd_listen` (3); `ca_distribute` (4): the internal CA in the trust store. `setup_db_base` (20): PostgreSQL, its TLS certificate (signed on the controller, again only when it no longer fits), `pg_hba`, the database, the roles and their passwords (the vault), PUBLIC's privileges revoked. `auditd` (4), `log_limits` (6).
- **Phase 4:** `setup_db_permissions` (5): every role's table and column privileges, revoked and granted exactly ([7](07-security.md)); it needs the tables the orchestrator's migrations made.

## orchestrator

- **Phase 3:** `apt_updates` (3), `timesync` (6), `sshd_listen` (3), `ca_distribute` (4); `nginx_tls` (18): nginx from nginx.org, its TLS certificate (signed on the controller only when missing, expiring or no longer matching). `nginx_tls_django` (3): the site, HTTPS only. `django_app`, its setup (25): packages, the Proxmox CA (from the node), the user, the code (rsync, owner and modes set by it), the gameplans and their framework (root's, readable by the app), the virtualenv, the environment file, static files; a change only notes the restart that is due. `auditd` (4), `log_limits` (6).
- **Phase 4:** `django_app`, its start (12): `migrate --check`; only if the database lacks migrations: the services stopped, `migrate`; the post-migration commands (`init`); the units and timers enabled and started; the restart noted in phase 3.

## dashboard

- As the orchestrator, phases 3 and 4, but `django_app` without migrations (no migrator credentials) and without gameplans (any there are removed).

## scoring, logging

- **Phase 3:** `apt_updates` (3), `timesync` (6), `sshd_listen` (3), `ca_distribute` (4), `nginx_tls` (18); `sandbox_api` (17): the sandbox-facing API (flags on scoring, worm log records on logging) behind nginx, its environment, the current-team and devbox-teams files; `auditd` (4), `log_limits` (6).

## monitor

- **Phase 3:** `apt_updates` (3), `timesync` (6), `sshd_listen` (3), `ca_distribute` (4); `network_monitor` (22): the capture interface (the node's mirror), Zeek, its log rotation, the shipper into the database (it retries until the grants are there); `auditd` (4), `log_limits` (6).

## apt-cache

- **Phase 3:** `apt_updates` (3), `timesync` (6), `sshd_listen` (3); `apt_client` (6): this VM's own apt at the Debian mirrors. `apt_cache` (13): apt-cacher-ng and the clients' sources (`/etc/apt-cache-client`). In the window: `apt_cache`, the fill (11): every listed package fetched through the cache, the client index kept. Offline: `apt_cache`, the check (2): the cache gives a fresh VM its whole package index. `auditd` (4), `log_limits` (6).

## devpi

- **Phase 3:** `apt_updates` (3), `timesync` (6), `sshd_listen` (3), `apt_client` (6); `devpi` (18): devpi-server on its data disk, the `root/pypi` index, the seed list fetched in the window, offline mode after; `auditd` (4), `log_limits` (6).

## docker-registry, manpages, ntp, cve-search, wikipedia

- **Phase 3:** `apt_updates` (3), `timesync` (6), `sshd_listen` (3), `apt_client` (6); then their service: `docker_registry` (6, read-only once the window closes), `manpages` (8: apache2 and man2html), `ntp` (4: chrony serves the node's time to the services network), `cve_search` (20: cve-search and its MongoDB), `wikipedia` (6: kiwix-serve); `auditd` (4), `log_limits` (6).

## Devbox VMs A, B, C (a team's)

The round agent's jobs; never `Site.yml`.

- **A new devbox** (`worms apply devbox`, job `devbox_provision`): `worms step devbox-apply` (the devbox stack), a first-boot snapshot, then `worms step devbox-configure -- -e @<varfile>` (the agent's variables): `SetupSSHGateway.yml`, then `SetupDevbox.yml`: wait for SSH and cloud-init; the team's keys; `ca_distribute` (4), `apt_client` (6: the private apt cache, its index accepted however old), `ntp_client` (3: the private NTP server), `devbox` (9: its name, pip at devpi, the packages the cache holds (a package it lacks is named, not installed), the clock, the team's key file).
- **A reset** (job `devbox_rebuild`): no Ansible; the three VMs back to their snapshot, then the CA, the team's keys and new tokens over SSH. A snapshot from before the devbox's setup existed gets it once, without network: `devbox-configure --limit <its VMs>`.
- **A rebuild** (`worms vm rebuild <team>`, the same job, fresh): OpenTofu `-replace` of the three VMs, then `devbox-configure --limit <its VMs>`.
- **A key change** (job `devbox_reconfigure`): `worms step devbox-keys`: `DevboxKeys.yml`, the team key files alone.

## A team's devbox gateway

- In `SetupDevbox.yml`: `apt_client` (6), `ntp_client` (3), `untrusted_gateway` (5): nftables (its NAT and filter: only the services, the private internet, the LLM endpoint), the getty masked.

## The SSH gateway (shared)

- `SetupSSHGateway.yml` (in `devbox-configure`) and `DevboxKeys.yml` (a key change): `ssh_gateway` (5): the team user, its home root's, one confined `authorized_keys` line per team key; the hardened sshd drop-in comes from cloud-init ([9](09-ssh-gateway.md)).

## Sandbox VMs and their gateway (a round's)

- The round's `prepare` job: the sandbox stack from the gameplan, then `worms step round-configure-sandbox -- -e @<varfile>` (the round's variables): `SetupSandbox.yml`: the gateway: `apt_client` (6), `ntp_client` (3), `untrusted_gateway` (5); each VM: `ca_distribute` (4), `apt_client` (6), `ntp_client` (3), `sandbox_vm` (16: the gameplan's part for that VM: packages, users, sudo rules, files, setup commands). Snapshotted, and rolled back between attempts ([10](10-orchestrator.md)).
