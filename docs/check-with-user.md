# Steps that need you

What is waiting on you after the infrastructure setup and the end-to-end run, as of 2026-11-04.
Background is in `docs/e2e-run-2026-11-04/protocol.md`, `docs/e2e-run-2026-11-03/protocol.md`, `docs/progress-setup-infrastructure.md` and
`docs/pi-failures.md`.

## Do these

### 1. Retire the old provisioning token

- [ ] Tell me to do it.

The end-to-end run answered the question that the earlier throwaway test was for: the new token
(`worms-provision@pve!worms`) created 13 VMs, 6 bridges and the firewall rules with no permission errors
(`docs/e2e-run-2026-11-03/protocol.md`, Stage 2). The throwaway test is no longer needed.

**What happens next:** I remove the old `terraform-prov@pve` token and its `Administrator` grant, and
disable that user.

**Until then:** the old token and grant stay as a fallback.

### 2. Delete the old credential copies

- [ ] Run this from the repo root:

```sh
find opentofu -name 'credentials.auto.tfvars.disabled' -not -path '*/.terraform/*' -delete
rm .env
```

The safety check blocked me from deleting these, so I renamed the tfvars files instead. The
vault now holds the tokens that are in use. `.env` also holds the password of
`terraform-prov@pve`, which nothing here uses. If you might want that password later, save it
first.

### 3. Test the dashboard and the SSH gateway from outside by name

- [ ] Run these from your laptop. Expect a `200` from the first, and `Permission denied (publickey)` from the second if you use no registered key:

```sh
curl -vk https://worms-dev.mlsec.tu-berlin.de/
ssh -p 2222 team@worms-dev.mlsec.tu-berlin.de
```

You confirmed access by IP earlier. The IP now gets `400`, because the name is the only one
accepted. The host cannot test either of these itself: the port forward only matches outside traffic. If the `ssh` times out, the network in front of the node may be blocking port 2222.

### 3b. Try the snapshot reset on real VMs

- [ ] Run this, or allow it and tell me to:

The safety check blocked me from restarting the service VMs and from writing to them over SSH, so
`worms build round-reset-services` has only run in offline tests. The real VMs have no snapshot yet. It
needs no devbox or sandbox VM at the snapshot, so this removes the devboxes, which take about 17 minutes
to build again:

```sh
/usr/local/sbin/worms-node-run opentofu/stacks/devbox 'tofu destroy -auto-approve -parallelism=1'
worms build snapshot-services         # restarts the 11 service VMs
time worms build round-reset-services # restarts them again
worms build ping
worms simulate
worms devbox provision                # the devboxes again
```

**What to look at:** how long the reset really takes (my estimate is about 2 minutes), and whether `worms
simulate` passes as it did before. Also run a reset once with devboxes up, and check that the map in
`/etc/sandbox-api/devbox-teams` on scoring and logging comes back.

**This node's snapshot is only good for this test,** because team VMs have reached these services. For a
snapshot to trust, use `worms build rebuild-services` instead.

## Decide these

### 3c. What the apt cache should hold

- [ ] Choose one.

With the internet closed, a team can install only what the cache already holds: about 150 packages that the VMs themselves
needed, plus the seed list (`apt_cache_seed_packages`). `apt install cowsay` fails.

| Option | Trade-off |
|---|---|
| A longer seed list you choose | Small and predictable, but teams get only what you foresaw |
| Mirror a large part of Debian | Teams can install nearly anything, but it takes a lot of disk and download time |
| Keep it minimal | Nothing to do, but tell teams to vendor what they need (`docs/worm-contract.md` already suggests this) |

### 3d. Keep the disk trimmed

- [ ] Say yes or no. I recommend yes.

```sh
zfs set org.debian:periodic-trim=enable rpool
```

The monthly TRIM job installed with ZFS only trims NVMe pools unless told otherwise, so this SATA disk had never been trimmed. This setting turns the job on for it. It runs on the first Sunday of the month, at 00:24, so avoid builds around then.

The one-off `zpool trim rpool` ran on 2026-11-04 and took 3 minutes. On an otherwise quiet system (no devbox VMs), measuring twice before and twice after:

| | Before | After |
|---|---|---|
| Synchronous 16 KiB write, median | 3.6 and 3.9 ms | 2.0 and 2.0 ms |
| Same, slowest 1 in 10 | 363 and 424 ms | 5.7 and 5.6 ms |
| Same, slowest | 1575 and 1243 ms | 10 and 11 ms |
| `zfs create`, slowest of 20 (Proxmox allows 10 s) | 4.6 and 0.6 s | 0.09 and 0.11 s |
| ZFS write cycle, slowest listed | 18 s | 0.11 s |

This is not yet proof for F5, because these samples are small and no build was running. The next build is the real test: 4 of the last 7 failed on the time limit.
The drive looks healthy: no reallocated sectors, no logged errors, and it does not use queued TRIM, the type behind the known Samsung bugs. A scrub straight after the TRIM repaired nothing and found 0 errors, so ZFS's checksums show no data was harmed.

### 3e. Record devbox traffic?

- [ ] Say if you want this. The default is to leave it as it is.

Devbox traffic is not recorded, by design (`docs/requirements.md`, Monitor), so `network_flows` has no devbox rows. The setting `host_mirror_extra_bridges` exists but is not tested for
devboxes. Its rule for gateways names only `sandbox-gw`, so devbox gateways would probably produce duplicate records until that is changed too.

### 3f. Should root create teams and users, and should auditd cover devboxes?

- [ ] Say yes or no to each. Writing `docs/manual-devbox-run.md` showed that neither exists today.

**Teams and users from the console:** `worms` can create only the admin and staff. No command creates a team, and `manage.py create_users` ignores the team column, so both have to go through the dashboard. Adding this would be a small new `worms` command.

**auditd on devboxes:** devbox VMs, their gateways and the SSH gateway have no auditd, so nothing done on a devbox appears in it. A devbox cannot reach the syslog VM, by design. So auditd on a devbox VM could only log locally, and a reset would erase it. The SSH gateway is trusted, so it could join the central log, which would record each login centrally and keep those records through a teardown. I recommend that part.

### 3g. Check SSH host keys when deploying?

- [ ] **Decide:** keep it off, or turn it on?

SSH from the node and Ansible do not check host keys: `StrictHostKeyChecking no` in `/root/.ssh/config`, `host_key_checking = False` in `ansible/ansible.cfg`. Both date from 2026-10-14. So a deploy would not notice a VM being impersonated, and SSH connects anyway, even with `REMOTE HOST IDENTIFICATION HAS CHANGED`.

On 2026-11-05 that warning appeared for the database, dashboard and orchestrator VMs. The likely cause is old `known_hosts` entries from earlier builds. On all three, the MAC address answering on the expected bridge is the one Proxmox assigned, and the traffic stays on this node. This is not cryptographic proof, because the VMs have no guest agent to read the key through.

The risk is low while the traffic stays on local bridges, and rebuilds change the keys on purpose. Recommended before going live: turn checking on, with each build recording the new VMs' keys.

### 3h. Turn nesting off on the node

- [ ] **Decide:** when to restart, because it stops every VM.

Team VMs no longer get `/dev/kvm` (`docs/security-assessment.md`). This is the second lock: with nesting off, no VM can get it, whatever its config says. It also covers `cve-search`, which is still on `cpu: host`.

`/etc/modprobe.d/worms-no-nesting.conf` is written, but `/sys/module/kvm_intel/parameters/nested` stays `Y` until the module reloads. That takes either a node restart, or stopping all VMs and reloading `kvm_intel`, which does not restart the node itself.

### 3i. Turn hyperthreading off?

- [ ] **Decide:** off now, or keep it until `worms-pve`?

The node reports `SMT vulnerable` for L1TF and MDS. A malicious guest may be able to read what its sibling thread has in the cache, including the node or another VM. L1TF Reloaded was demonstrated against KVM, and no CPU type changes it (`docs/cpu-types.md`, "What no CPU type changes"). Turning it off takes effect at once and restarts nothing, but this node goes from 8 threads to 4 cores.

### 3j. Move `cve-search` to `x86-64-v3`?

- [ ] **Decide:** yes, after a test that MongoDB 8 runs on it, or leave it?

It is on `cpu: host` for AVX, and `x86-64-v3` has AVX too. It has `/dev/kvm`, and its port 5000 is open from team VMs (`docs/cpu-types.md`).

### 4. Public certificate for the dashboard

- [ ] Choose one.

The certificate has the right name, but browsers warn because it comes from the internal CA.

| Option | Trade-off |
|---|---|
| Certificate issued through TU Berlin | No browser warning. Probably renewed by hand |
| Automatic public certificate (ACME) | No browser warning and renews itself, but it is awkward with the VMs' internet closed |
| Keep the internal CA | Nothing to do, but every browser warns unless it trusts the CA |

### 5. Filling the private internet caches

- [ ] Choose one.

After `make`, devpi has 0 files, the registry is empty, cve-search has 0 records and Wikipedia is
not running. The documented way to fill them cannot run as written (`docs/pi-failures.md`,
findings 5 and 6).

| Option | Trade-off |
|---|---|
| `make` fills them | Works out of the box, but builds take much longer and download a lot |
| A separate command, with corrected docs | Builds stay fast, but the services are empty until someone runs it |

### 6. Man pages fix

- [ ] Choose one.

Every man page returns `503`, because the systemd hardening blocks Apache's CGI daemon
(`docs/pi-failures.md`, finding 4).

| Option | Trade-off |
|---|---|
| Add `CAP_DAC_OVERRIDE` and `CAP_CHOWN` | Tested and works, but `CAP_DAC_OVERRIDE` is a broad capability on a service sandboxes can reach |
| Serve man pages without CGI | Keeps the tight hardening, but takes more work |

### 7. Smaller fixes

- [ ] Tell me which of these you want. All are documented but not changed.

| Item | Effect now | Reference |
|---|---|---|
| NTP cannot reach its upstream servers | Clients get the VM's own clock, which nothing corrects | `pi-failures.md`, finding 7 |
| Private internet VMs never get upgrades | 60 packages behind | `pi-failures.md`, finding 11 |
| Port isolation is not active | One defense layer is missing; it needs `root@pam` to attach | `pi-failures.md`, finding 9 |
| Report page and cve-search UI have no login | Information exposure on the services bridge | `pi-failures.md`, finding 10 |
| `Update all packages` prints nothing for about 16 minutes | A fresh build still looks stuck there | `progress-setup-infrastructure.md` |
| `no_log` hides migration errors | `make` prints only "censored" when one fails | `progress-setup-infrastructure.md` |
| A failed devbox reset looks like a success | The team loses its score, the VMs stay as they were, and the page keeps saying "being rebuilt" | E2E protocol, F25 |
| Untrusted VMs run without a firewall while being built | Up to 28 minutes measured. The host's own rules still apply | E2E protocol, F4 |
| Nothing checks that infrastructure VMs have a firewall | The watchdog watches only sandbox and devbox VMs. This is how the SSH gateway ran unprotected for 2 hours | E2E protocol, F26 |
| With devboxes up, there is no `worms` command to deploy | `make` and `worms build provision` refuse, and no command removes only the devboxes. For Django code alone, `docs/management.md` now gives an Ansible command that needs no internet window; anything else still has to be copied by hand | E2E protocol, F6 |
| The agent throws away Ansible's output | A failed job usually says only `exited 2`. Each failure in this run had to be reproduced by hand. Keeping the output needs care, as it may hold secrets | E2E protocol, F9 |
| `agent-sync` does not restart the agent | New playbooks take effect, but new agent code does not until `systemctl restart worms-round-agent`. Restarting during a job would kill it | E2E protocol, Stage 2 |
| An interrupted build leaves things behind | A VM outside OpenTofu's state, or an `apt` on a VM holding its lock. There is also no command to cancel a job | E2E protocol, F2 |
| The API's documented limits differ | `GET` gets 403 rather than 405, and logging accepts more than 4 KiB | E2E protocol, F23 |
| A user for a full or unknown team still gets an account | It has no team, and there is no delete button | E2E protocol, F1 |
| Live header says "ROUND IN PROGRESS (DEV)" with 0 rounds | `WORMS 2026` has no registration dates, which the code treats as a dev round. Setting a registration window in the staff pages clears it; the logic could also be fixed | `dashboard/middleware.py` |

### 8. Commits

Work is committed on `main` and pushed to `github` and `https`, as you asked on 2026-11-04.
`github` (`j-moeller/worms`) is **public**, so the protocols and open findings can be read by anyone. Tell me if some of it should go to `https` only.

These stay uncommitted on purpose: the node name default (`worms-dev` here, `worms-pve` in the repo), `cve_search`'s RAM (1024 MB here, 4096 MB in the repo), and your `docs/IMPORTANT`.
Both settings reached `main` once by my mistake, in `52fc7a1`, and `e6e5b33` put the repo's values back. They are still in the history, and neither is a secret. Removing them from the history would take a force-push, which I will only do if you ask.

## On hold, until you say otherwise

- **Parallelizing Ansible:** you asked me not to yet.
- **A full rebuild** (`make destroy_all`, then `make`): you asked to avoid it. It is still the only
  way to test the first-run paths on brand-new VMs, so consider one before going live.

## Before going live on `worms-pve`

- [ ] Create a DNS record for `worms.mlsec.tu-berlin.de`. It does not resolve yet.
- [ ] Check that `worms-pve` has AVX2. Sandbox and devbox VMs use `x86-64-v3` and do not start without it.
- [ ] Check that `/sys/module/kvm_intel/parameters/nested` (or `kvm_amd`) says `N` on `worms-pve`.
- [ ] Check `grep . /sys/devices/system/cpu/vulnerabilities/*` on `worms-pve`. If it says `SMT vulnerable`, decide about turning hyperthreading off (`docs/security-assessment.md`, "Hyperthreading").
- [ ] Make sure `proxmox_instance` is `worms-pve`. A node not listed in `dashboard_public_names`
      (`group_vars/all.yml`) fails the run.
- [ ] Plan the rename. OpenTofu's state and the VMs are tied to the node name, so `make destroy_all`
      should probably run before the rename and `make` after it. This order is not tested.
- [ ] Confirm the `cve_search` RAM setting. Your uncommitted 1024 MB has not been tested with
      `cve-search-populate` running.
- [ ] Set your own admin password. The suite wiped the database and set a random one that it does not keep, so nobody knows the current one. The passwords in the earlier test's `creds.json` no longer work. The CLI needs the packages in `.venv-test`:

```sh
.venv-test/bin/python src/manage.py admin reset-password
```

- [ ] Remove the test's data: the 9 test accounts, the teams `alpha`, `bravo` and `charlie`, their devboxes, and the
      `WORMS 2026 · dev` competition. The commands are in `docs/testing.md`, "The live devbox journey".
