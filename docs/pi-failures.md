# Private internet: what is misconfigured

Everything found wrong in the private internet stack (OpenTofu and Ansible), from a
review of the configuration and tests on the running VMs on 2026-11-03.

- **Confirmed** means it was observed on a running VM or follows directly from the
  configuration. Anything not tested is marked as such.
- Findings 1-3 are fixed because they stopped `make`, and finding 8 at your request.
  The rest are documented and unchanged, and some need a decision from you.
- All fixes are uncommitted.

## Summary

| # | Problem | Effect | Status |
|---|---|---|---|
| 1 | `apt-cacher-ng` installed before its cache dir has the right owner | `make` fails | Fixed |
| 2 | `apt-cache` VM keeps a stale package index | `make` fails | Fixed |
| 3 | Offline runs still try to refresh apt from the internet | `make` fails, depending on timing | Fixed |
| 4 | Man pages broken by the systemd hardening | Service broken, `make` still passes | Open, fix tested |
| 5 | `make` does not fill the caches, despite the README | Services empty | Open, needs a decision |
| 6 | The documented way to fill the caches cannot run | No way to fill devpi or the registry | Open, needs a decision |
| 7 | NTP cannot reach its upstream servers | Serves an undisciplined clock | Open |
| 8 | Anonymous writes to devpi and the Docker registry | High security risk | Fixed |
| 9 | Port isolation is not active, though the README says it is | Weaker isolation | Open, known limit |
| 10 | Admin pages and web UI open to clients | Information exposure | Open |
| 11 | Private internet VMs never get package upgrades | 60 packages behind | Open |
| 12 | `a2enmod` task has the wrong guard | Task repeats on every run | Open, minor |
| 13 | Leftover and stale configuration | Misleading | Open, minor |

## Stops `make`

### 1. `apt-cacher-ng` is installed before its cache dir has the right owner

**File:** `ansible/roles/private_internet/apt_cache/tasks/main.yml`

- cloud-init mounts the data disk at `/var/cache/apt-cacher-ng`, owned by root.
- Installing the package starts the service straight away, as user `apt-cacher-ng`.
  It exits with `Cannot create any directory in /var/cache/apt-cacher-ng/`, and that
  makes the package install fail.
- The role's task that fixes the owner came after the install, so it never ran.

**Fix applied:** the role now creates the user and sets the owner before the install.
The package's script accepts an existing user.

**Verified:** the install passes, the service runs with 0 restarts, and the owner is
correct. This VM already had the user, so a brand-new VM, where the role creates the
user first, has not been run yet.

### 2. The `apt-cache` VM keeps a stale package index

**File:** `ansible/roles/private_internet/apt_client/tasks/main.yml`

- `apt_cache` refreshes the index, then `apt_client` rewrites the VM's apt sources.
- Later apt tasks use `cache_valid_time: 3600`, which checks only the age of the
  index, not whether the sources changed. They skip the refresh for an hour.
- The VM then knows no packages, and `SetupAudit.yml` fails with
  `No package matching 'auditd' is available`.
- Other VMs escape this because their first refresh happens after the rewrite.

**Fix applied:** `apt_client` refreshes the index when it has changed the sources.

**Verified:** `SetupAudit.yml` now passes. The refresh itself ran in run 4. It was then
limited to runs where the sources change, and they have not changed since, so that
condition has not been exercised on a fresh VM.

### 3. Offline runs still try to refresh apt from the internet

**Files:** 9 roles: `setup_dns`, `apt_cache`, `devpi`, `docker_registry`, `manpages`,
`ntp`, `wikipedia`, `auditd` and `syslog_server`

These tasks set `update_cache: "{{ internet_enabled | bool }}"` to skip the refresh
offline, together with `cache_valid_time: 3600`. In the `apt` module, `cache_valid_time`
turns the refresh on by itself, so `update_cache: false` had no effect.

Offline, they fail once the index is over an hour old, with `Failed to update apt cache
after 5 retries`. So the offline pass worked only if it ran soon enough after the
online pass. Run 4 failed this way in `SetupDNS.yml`. A test that requires
`cache_valid_time` on these tasks helped the pattern spread.

**Fix applied:** `cache_valid_time` is now conditional on `internet_enabled` as well. New
test: `test_apt_does_not_refresh_its_index_offline`, which failed before the fix.

**Verified:** the offline stage now passes.

## Service broken or empty after a successful `make`

### 4. Man pages are broken by the systemd hardening

**File:** `ansible/roles/private_internet/manpages/templates/hardening.conf.j2`

The front page loads, but every man page returns 503, sometimes only after waiting about
32 seconds. The log shows `Couldn't bind unix domain socket .../socks/cgisock` and
`cgid daemon failed to initialize`. The file's own comment says the hardening is
"UNTESTED on a real VM".

Apache's CGI daemon creates and hands over a socket in a directory owned by
`www-data`, and `CapabilityBoundingSet` allows neither step. Tested with a temporary
drop-in, since removed:

| Added to `CapabilityBoundingSet` | Result for `ls(1)` |
|---|---|
| Nothing (as deployed) | 503 after 32 s |
| `CAP_CHOWN` | Still failing |
| `CAP_DAC_OVERRIDE` | 503 after 32 s |
| `CAP_DAC_OVERRIDE CAP_CHOWN` | 200 in 0.02 s |

**Suggested fix:** add `CAP_DAC_OVERRIDE CAP_CHOWN` to the template. `CAP_DAC_OVERRIDE`
is a broad capability, so the alternative is to serve man pages without CGI.

### 5. `make` does not fill the caches

The README says "the private-internet caches populate in online mode". Nothing in
`make` does this. `populate-caches.sh`, `wikipedia-download` and `cve-search-populate`
are all manual. Measured after the online phase of run 3:

| Service | Contents | State |
|---|---|---|
| apt-cache | 177 MB, 362 files | Only what the private internet VMs installed for themselves |
| devpi | 0 stored files | Running, nothing cached |
| docker-registry | Empty catalog | Running. The config has no `proxy` section, so it fetches nothing by itself |
| cve-search | 0 objects, all sources `last_update: null` | Running with an empty database |
| wikipedia | 0 ZIM files | Not running, nothing on port 8888 |

So, once offline, sandbox VMs should find almost nothing to install. This follows from
the contents above but has not been tested from a sandbox.

Related:

- `wikipedia-download full` needs about 100 GB, but the data disk is 20 GB. Only the
  mini version (about 12 GB) can fit.
- The `cve_search` role says "do not start -- run populate first", but a handler starts
  it anyway, so it serves an empty database.

**Decision needed:** should `make` fill these, or stay manual with correct docs? The
downloads are large: the scripts' own estimates are about 12 GB for Wikipedia and
20-60 minutes for cve-search.

### 6. The documented way to fill the caches cannot run

**Files:** `helpers/offline-mirror/README.md`, `helpers/offline-mirror/populate-caches.sh`

The workflow needs a "helper client" on the services bridge while the internet window
is open. None can exist:

- Only `sandbox-gw` and `devbox-gw` may reach devpi and the registry (`services_client`),
  plus sandbox and devbox VMs (their own groups).
- `Internet.yml` refuses to open the window while any sandbox or devbox VM exists.
- Private internet VMs can reach only `apt-cache:3142` (`pi_apt_client`).
- The host has no IPv4 address on the services bridge (`vmbr23`).

The docs also use `make internet-allow` and `make internet-deny offline`, which no
longer exist. The Makefile has only `all`, `destroy` and `destroy_all`.

**Decision needed:** a possible fix is a populate step that runs on each service VM
against itself, but that is a design choice.

### 7. NTP cannot reach its upstream servers

**Files:** `ansible/roles/private_internet/ntp/templates/chrony.conf.j2`,
`ansible/roles/host/host_network/defaults/main.yml`

Online, chrony is set to use `pool.ntp.org`, but the internet window opens only TCP 80
and 443 and port 53. UDP 123 is never allowed. All 6 upstream servers showed `^?` with
reach 0.

Offline, the `pool` line is removed as intended. Either way, chrony reports stratum 10
from its local reference, so clients get the VM's own clock, which nothing corrects.

**Suggested fix:** allow UDP 123 during the window, or sync from the host. This matters
only if the sandbox clocks have to be accurate rather than just consistent with each
other.

## Security

### 8. Anonymous writes to devpi and the Docker registry (fixed)

**Files:** `ansible/roles/private_internet/devpi/`,
`ansible/roles/private_internet/docker_registry/templates/config.yml.j2`

This was finding S2 (High) in `docs/security-assessment.md`: "a poisoning and a
cross-environment channel" that persists across rounds. Commit `34e41fa` enabled these
services without the lockdown that finding asked for.

**Fix applied:**

- **Registry:** read-only whenever internet access is denied, so it is writable only
  while the caches are being filled.
- **devpi:** runs with `--restrict-modify root`, so only root can create users or indexes
  or upload. Root's password is generated and stays in the vault.

**Verified on the running VMs, in offline mode:**

| Request | Before | Now |
|---|---|---|
| Registry: anonymous blob upload | 202 | 405 |
| Registry: `GET /v2/` | 200 | 200 |
| devpi: log in as `root` with an empty password | Succeeds | 401 |
| devpi: create a user anonymously | Not tested | 403 |
| devpi: read `/+api` and `/root/pypi` | 200 | 200 |

These requests were made locally on each VM, not from a sandbox. Filling the caches later
needs the online phase for the registry, and root's password for any devpi change.

### 9. Port isolation is not active

**File:** `opentofu/modules/private_internet_vm/main.tf`

`hook_script_file_id` is commented out, because attaching it needs `root@pam`. On the
live host, 0 of 7 VMs have a hookscript, and 0 of 7 management ports are isolated.

The README says every VM carries it, while `docs/security-assessment.md` lists S4 as
"fixed". `TODO` has it right. The Proxmox firewall still applies to these VMs, so this
removes one layer rather than all protection.

### 10. Admin pages and web UI open to clients

`docs/security-assessment.md` calls this S13, "only relevant once those PI services are
re-enabled", and they now are. Both answer locally with 200:

- The apt-cacher-ng report page (`/acng-report.html`).
- The cve-search web UI (`LoginRequired: False`, `WebInterface: Full`).

## Maintenance

### 11. Private internet VMs never get package upgrades

**File:** `ansible/roles/system/apt_updates/tasks/main.yml`

The upgrade task skips these VMs, and its comment says they "are updated by their
roles". The roles only use `state: present`, which never upgrades. They have 60
upgradable packages and an older libc (`deb13u3`) than the other VMs (`deb13u4`).

**Suggested fix:** upgrade them through the cache after `apt_client`. Based on the other
VMs, expect this to add 10+ minutes to a fresh build.

### 12. The `a2enmod` task has the wrong guard

**File:** `ansible/roles/private_internet/manpages/tasks/main.yml`

The task is guarded by `creates: .../mods-enabled/cgi.load`. On Apache's event MPM,
`a2enmod cgi` enables `cgid` instead, so that file never appears. Confirmed: the task
reports `changed` on every run, and then Apache restarts.

**Suggested fix:** enable `cgid` and guard on `cgid.load`.

### 13. Leftover and stale configuration

Harmless, but misleading:

- **Stale claims that services are not deployed:** the `hardening.conf.j2` files of
  `manpages` and `ntp`, and `apt_client/defaults/main.yml`.
- **Unattached firewall group:** `private_internet` in `stacks/firewall`, kept "for one
  migration cycle".
- **Unused variable:** `wikipedia_host`. `kiwix-serve` is started without it.
- **Unused package:** `gunicorn` is installed for cve-search, but the unit runs
  `web/index.py`.

## Not tested

- **A brand-new VM:** all of these checks ran on VMs that already existed, so the
  first-run paths of findings 1 and 2 have not been run. A `make destroy_all` followed
  by `make` would cover them.
- **Filling the data:** `cve_search` now has 1024 MB of RAM (your uncommitted change) and
  a 10 GB data disk. At idle it has 477 MB available and no OOM kills, but neither limit
  has been tested with `cve-search-populate` running.
- **`kiwix-serve` under its hardening:** it has never run with a ZIM file. Its own
  comment notes that it may need write access.
- **Traffic from a sandbox:** all service checks ran on the VMs themselves.

## Checked and found correct

- **Ports:** role defaults, the per-service firewall groups and `services_ports` agree.
- **Firewall:** each VM has its own port group, and the rest of its traffic is limited.
- **Data disks:** `apt-cache`, `docker-registry` and `cve-search` have the right owners.
  The `cve_search` disk is mounted as configured.
- **Hardening:** `apt-cacher-ng`, `docker-registry`, `devpi`, `chrony` and `cve-search` all
  run under theirs, with 0 restarts and no OOM kills. Only `manpages` is broken by it.
- **Offline mode:** `apt-cacher-ng` has `Offlinemode: 1`, `devpi` runs with
  `--offline-mode`, chrony has no `pool` line, and apt can resolve a package through
  the offline cache (simulated install).

## Not specific to the private internet

On every VM, `systemctl --failed` lists `apt-daily.timer` and `apt-daily-upgrade.timer`
(`Unit to trigger vanished`). This comes from masking them in `apt_updates`. It is
harmless, but it adds noise to a check you would use to spot real failures.
