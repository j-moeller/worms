# End-to-end test run, 2026-11-03

A live run of the administrator and user journey on the dev node (`worms-dev`), to assess how
far development has come. It follows `docs/e2e_test_protocol.md`. It ran from the evening of
2026-11-03 to 03:55 on 2026-11-04.

## Summary

**The whole journey now works on this system, except what teams can install. It did not work before this run:** no team could
have finished it, at several separate points. This was the first time the devbox path ran on a real Proxmox host. The unit tests
stub out or never execute these parts, so they could not catch what broke here.

| Stage | What you asked for | Result |
|---|---|---|
| 1 | Administrator creates 9 users in 3 teams | **PASS** |
| 2 | Devboxes for the 3 teams | **PASS, on the 8th attempt** |
| 3 | Users sign in with one-time passwords, add SSH keys, create tokens | **PASS** after fixes |
| 4 | Log in to the devbox with the key | **PASS** for all 9 users, from the node. Port 2222 is not published, so it does not yet work from outside |
| 5 | Reach scoring and logging | **PASS**, verified TLS, 3/3 on the dashboard |
| 5 | `apt install` through the private internet | **PARTLY:** works for packages in the cache, fails for the rest |
| 5 | `pip install` through the private internet | **FAIL by content:** pip reaches devpi, but devpi holds no packages |
| 5 | No path to the real internet or other teams | **PASS**, 22 of 22 forbidden destinations closed |
| 6 | A user resets the devbox (added during the run) | **PASS, on the 3rd try** |

**The four critical findings, all fixed:**

- **F11:** no user could add or remove an SSH key (HTTP 500).
- **F14:** no devbox VM could reach anything, not even DNS, because the gateways never got `nftables`.
- **F24:** no user could reset a devbox.
- **F26:** replacing a VM could leave it running with its firewall off, and OpenTofu could not see it. Through my own step in this run, the SSH gateway ran unprotected for about 2 h 10 min.

### Findings

| # | Severity | Finding | Status |
|---|---|---|---|
| F11 | Critical | Adding or removing a key crashes the dashboard | Fixed, confirmed live |
| F14 | Critical | Gateways have no `nftables`, so devbox VMs reach nothing | Fixed, confirmed live for devboxes. `sandbox-gw` has the same fix, not run |
| F24 | Critical | A reset can never work: invalid `-replace` address | Fixed, confirmed live |
| F26 | Critical | A replaced VM can lose its firewall, silently | Fixed, confirmed live. Nothing watches infrastructure VMs for this |
| F3 | High | A build's own VMs trigger the watchdog | Fixed |
| F4 | High | Untrusted VMs run without a firewall during a build | **Open** |
| F7 | High | The agent cannot run any Ansible | Fixed, confirmed live |
| F8 | High | The SSH gateway locks out Ansible | Fixed, confirmed live |
| F10 | High | The build does not wait for a new VM to boot | Fixed, confirmed live |
| F12 | High | The dashboard never shows how to connect | Fixed, confirmed live. Port 2222 is your decision |
| F13 | High | Most users' keys are silently dropped | Fixed, confirmed live |
| F15 | High | apt tries the real internet, and the build hangs | Fixed, confirmed live |
| F16 | High | The cache cannot supply what untrusted VMs need | Fixed in the repo, **seed step not yet run** |
| F19 | High | A team login gets a shell on the SSH gateway | Fixed, confirmed live |
| F20 | High | A lost handler leaves the CA untrusted | Fixed, confirmed live |
| F21 | High | The build reports success with nothing installed | Fixed, confirmed live |
| F6 | Medium | With devboxes up, there is no supported way to deploy, or to remove only the devboxes | **Open** |
| F2 | Medium | An interrupted build leaves things behind | **Open** |
| F5 | Medium | `zfs create` times out under load | **Open** |
| F9 | Medium | The agent discards the output that explains a failure | **Open** |
| F22 | Medium | Per-team service names do not resolve | Fixed, confirmed live |
| F25 | Medium | A failed reset looks like a success, and costs the team its score | **Open** |
| F1 | Low | A user for a full team still gets an account | **Open** |
| F17 | Low | Devbox VMs are all called `localhost` | Fixed, confirmed live |
| F18 | Low | No VM addresses on the page; Copy covers the command | **Open** |
| F23 | Low | The API's documented limits differ from what happens | **Open** |

17 fixed, 9 open. What needs you is in `docs/check-with-user.md`.

### Not tested

- **A real round or a sandbox.** So the sandbox's copies of these fixes, including its internet containment, have not run live. Only devboxes were checked. Given how many devbox faults only showed up live, expect more there.
- **A build on a freshly filled cache.** This needs a devbox teardown, which the safety check refused, so it is your decision. Instead I gave the gateways `nftables` and the VMs `unzip` by hand, and put `unzip` in the cache by hand.
- **The public port forward on 2222, and the dashboard's name from outside the node.**
- **The team API on real round data and its rate limit, uploading a worm, and leaving a team.**

### What on the running system did not come from a build

All of it matches the repo (hashes compared at 03:55), so a future build produces the same, apart from the seed step above.

| Where | What |
|---|---|
| Dashboard VM | `orchestrator_client.py`, `views/devbox.py` copied by hand |
| Orchestrator VM | `orchestrator/api.py` copied by hand, `DEVBOX_SSH_GATEWAY_HOST` added to its env file |
| 3 devbox gateways | `nftables` installed from packages the node downloaded |
| 6 devbox VMs (bravo, charlie) | `unzip` installed the same way. Alpha's 3 were rebuilt, and got it from the cache |
| Apt cache VM | `unzip` placed in the cache by hand |
| SSH gateway | Firewall options restored through the Proxmox API |
| Database | Job `1fec992b` marked failed by hand |

### State left behind

28 VMs running, internet denied, no job running, 0 watchdog stops since the fix. Teams `alpha` (devbox version 5), `bravo` and `charlie`, 9 active test users, 3 deactivated ones. The
admin password was changed by the test. Nothing is committed.

**Offline test suite at the end:** 638 passed, 4 failed. The same 4 failed before this run: `test_playbook_syntax[inventory.generated.yml]`, `test_fixture_matches_shared_variables`,
`test_monitor_port_is_receive_only` and `test_two_teams_run_one_after_another` (`docs/progress-setup-infrastructure.md`).

**No secret reached this repository:** every saved password and token was compared against 484 files with 0 matches, and I viewed the screenshots that could show one. Anna's first one-time password did appear in the session's output (Stage 1).

## How this was tested

- **Who:** Claude Code, acting as the administrator and then as the users.
- **Browser:** real Chromium (Playwright) on the node, against the live dashboard. The hostname
  `worms-dev.mlsec.tu-berlin.de` was mapped to the dashboard VM (`10.10.0.18`), because a browser on
  the node cannot use the port forward, which only matches traffic arriving on `vmbr0`. The
  certificate warning was accepted, as a user would have to.
- **`worms`:** `.venv-test/bin/python src/manage.py`. Commands that use the database need Django, which
  is only in `.venv-test`, and system `python3` cannot run them.
- **Passwords:** kept in a private scratch file, and left out of this document and the screenshots.
- **Results:** PASS means it matched what the docs promise. FAIL means it did not. NOTE means it
  worked, but something is worth a look.

## Stage 0: prior state

| Check | Command | Expected | Actual | Result |
|---|---|---|---|---|
| Platform up | `worms status` | Firewall on, internet denied, 0 teams, 0 rounds | Firewall enabled, 15 VMs running, internet denied, `WORMS 2026`, 0 teams, 0 rounds | PASS |
| Only `admin` exists | `worms users` | `admin` only | `admin`, never logged in | PASS |
| No teams | `worms teams` | Empty | "No teams" | PASS |
| Tokens valid | `worms secrets check` | All 3 ok | All 3 ok, exact rights | PASS |
| Host services | `systemctl is-active proxmox-firewall worms-host-firewall worms-mirror worms-round-agent` | All active | All active | PASS |
| Offline test suite | `pytest`, with PostgreSQL and Playwright | Passes | 601 passed, 4 failed, 0 skipped. The 4 are explained in `docs/progress-setup-infrastructure.md` | NOTE |

## Stage 1: the administrator creates users and teams

**Prior state:** Stage 0.

### 1.1 Admin signs in

| Step | Action | Expected | Actual | Result |
|---|---|---|---|---|
| 1 | `worms admin reset-password --yes` | Prints a one-time password once | 20 characters | PASS |
| 2 | Sign in with a wrong password | Refused | 401, "Invalid username or password." | PASS |
| 3 | Sign in with the one-time password | Forced to `/change-password/` | Landed on `/change-password/` | PASS |
| 4 | Open `/staff/users/` before changing it | Still forced to change it | Sent back to `/change-password/` | PASS |
| 5 | Set a new password | Accepted | "Password saved.", landed on `/profile/` | PASS |
| 6 | Open Competition, Users, Teams, Rounds, Topology | All open | All 200, 0 JavaScript errors | PASS |

![Forced password change](screenshots/01-admin-forced-password-change.png)

### 1.2 Development competition and teams

| Step | Action | Expected | Actual | Result |
|---|---|---|---|---|
| 1 | Staff → Competition → **Create it** | Dev competition exists | "Development competition 'WORMS 2026 · dev'" | PASS |
| 2 | Staff → Teams → create `alpha`, `bravo`, `charlie`, at most 3 members | Teams exist | All created, each with the hint to run `worms devbox provision` | PASS |

![Dev competition created](screenshots/03-dev-competition-created.png)

### 1.3 Nine users

| Step | Action | Expected | Actual | Result |
|---|---|---|---|---|
| 1 | Staff → Users → form: `anna.alpha@example.org` in `alpha` | Account plus a one-time password, shown once | Created | PASS |
| 2 | Reload that page | Password gone, `no-store` | Gone; `Cache-Control: max-age=0, no-cache, no-store, must-revalidate, private` | PASS |
| 3 | Staff → Users → CSV import of the other 8 (`Vorname,Nachname,E-Mail,Team`) | 8 accounts, passwords shown once | 8 created, no problems reported | PASS |
| 4 | `worms teams` | 3 teams, 3/3 each | `alpha`, `bravo`, `charlie`: 3/3 | PASS |
| 5 | `worms users` | 9 users with team, one-time password set | All 9, "never logged in", "one-time pw" | PASS |
| 6 | Database: `teams` | Tied to both competitions | All 3: `WORMS 2026` and `WORMS 2026 · dev`, `devbox_version` 1 | PASS |

**Edge cases:**

| Action | Expected | Actual | Result |
|---|---|---|---|
| Create `Anna.Alpha@EXAMPLE.org` (same email, other case) | Refused, nothing created | "This email already exists", no password shown | PASS |
| Create a fourth member of `alpha` | Refused, team stays at 3 | Team stays at 3/3, **but the account is still created, without a team** | NOTE |
| CSV: a full team, an unknown team, a bad email, a duplicate | Each reported, nothing wrong created | All 4 reported. **The rows naming a full or unknown team still create accounts without a team** | NOTE |
| Staff role on the user form | No such control | None | PASS |

**Finding F1 (low):** a user created for a full or unknown team still gets an account, with no team.
The admin is told, so this may be intended, but it leaves accounts to clean up. I deactivated the 3
this test created (`full.team`, `no.team`, `fourth.member`) under Staff → Users. There is no delete
button there.

**Test slip:** Anna's one-time password appeared in this session's output because of a bad masking
pattern. It is a test account, and the password stops working at her first login.

## Stage 2: the administrator builds the devboxes

**Prior state:** 3 teams, 15 VMs, 0 devbox VMs, `worms-round-agent` active, internet denied. This is the
first live run of this path, and the first time the reduced-rights provisioning token creates anything.

### Attempt 1, aborted on request

| Time | Action | Actual |
|---|---|---|
| 23:48:17 | `worms devbox provision` | "3 team(s): t4@0, t5@1, t6@2", job enqueued, agent started it |
| 23:49:01 | | VM 115 `devbox-ssh-gateway` being created, bridges `vmbr9000`-`vmbr9005` and `vmbr7` up |
| 23:51:22 | You asked to abort: `systemctl stop worms-round-agent` | Agent and `tofu apply` stopped; job failed with `exited -15` |

The failure came from the abort, not from a bug. It did leave a half-created VM 115 that OpenTofu's
state did not know about, which I removed with `qm destroy 115`.

**Finding F2 (medium):** interrupting a devbox build can leave a VM outside OpenTofu's state, and a retry
would not clean it up. It happened here on purpose, but a crash or reboot could do the same.

### Attempt 2

| Time | Action | Actual |
|---|---|---|
| 23:53 | Checked VM 115 was stopped and not in OpenTofu's state, then `qm destroy 115 --purge 1` | Removed, back to 15 VMs |
| 23:54 | `systemctl start worms-round-agent`, then `worms devbox provision` | "3 team(s): t4@0, t5@1, t6@2", job enqueued |
| 23:54-00:26 | Watched `qm list` and the agent's journal | One VM about every 1-4 minutes: all 28 existed after 32 minutes, no permission errors |
| 00:27-00:28 | | The firewall watchdog hard-stopped 9 running devbox VMs (3 times, 3 VMs each) |
| 00:28 | | Job failed: `creating custom disk ... zfs create -V 3145728k rpool/vm-127-disk-0 failed: got timeout` |

**Result: FAIL.** The new provisioning token did create all 13 new VMs, 6 bridges and the firewall rules,
which it had never done before, so it does have enough rights for this. The build failed for two other
reasons, both found only by this live run.

**Finding F3 (high): a build stops its own VMs.** Nothing ordered a VM's pool membership after its
firewall, so a VM could join the `devbox` pool before it had rules. The watchdog then saw a running
untrusted VM without rules and stopped all running VMs in the pool, as designed:

```
00:27:45 no firewall rules for sandbox/devbox VM 116; stopping sandbox/devbox VMs: 116 117 118
00:28:03 no firewall rules for sandbox/devbox VM 119; stopping sandbox/devbox VMs: 119 121 122
00:28:19 no firewall rules for sandbox/devbox VM 124; stopping sandbox/devbox VMs: 123 124 125
```

The sandbox stack had the same structure, so a real round's build could have hit this too. **Fixed:**
pool membership now depends on the firewall resources, in `modules/devbox_vm` and `stacks/sandbox`, with a
new test. The watchdog is unchanged.

**Finding F4 (high, open): untrusted VMs run without a firewall during a build.** VMs start as soon as they
are created, but their firewall options come later. Here VM 116 started at 23:59:56 and first
had its rules checked at 00:27:45, 28 minutes later. Outside the pool, the watchdog does not look at them. The
host's own nftables rules still apply, but the per-VM firewall does not. Fixing this needs VMs to be
created stopped and started after their firewall, which affects both stacks. Not fixed.

**Finding F5 (medium, open): disk creation fails under load.** Proxmox's `zfs create` hit its time limit on 2 of
12 VMs (`devbox-t5-b`, `devbox-t6-b`), with disk pressure up to 91%. The `qcow2 ... using 'raw'` line in the error
is only a warning, and the timeout on the next line is the cause. OpenTofu marks those VMs tainted, and a retry
replaces them. Nothing retries automatically.

**Finding F6 (note): `make` no longer works once devbox VMs exist.** Opening the internet window is refused
while any untrusted VM exists, by design. So after this stage, `make` and `worms build provision` fail at
`Internet.yml`, until the devboxes are destroyed. The fix above reached the agent's workspace with
`ansible-playbook SetupRoundAgent.yml --tags agent-sync` instead.

### Attempt 3

| Time | Action | Actual |
|---|---|---|
| 00:33 | `ansible-playbook SetupRoundAgent.yml --tags agent-sync`, then `worms devbox provision` | Job enqueued |
| 00:34-00:39 | | The 2 tainted VMs replaced, back to 28 VMs, all 13 new VMs running |
| | Watchdog during this attempt | **No stops**, which confirms the F3 fix |
| 00:44 | | Job failed: `The vault password file /opt/worms/ansible/.vault_pass was not found` |

**Result: FAIL**, at the next step.

**Finding F7 (high): the agent could not run any Ansible.** Its workspace has no vault, by design, but the synced
`ansible.cfg` names the vault's key, and a missing key is fatal for every playbook. So `devbox-configure` could
never have worked live. **Fixed:** the sync step removes that line from the workspace's copy, with a new test. The
controller's `ansible.cfg` is unchanged.

### Attempt 4

| Time | Action | Actual |
|---|---|---|
| 00:44 | `ansible-playbook SetupRoundAgent.yml --tags agent-sync`, then `worms devbox provision` | Job enqueued |
| 00:50 | | `tofu apply` clean, `SetupSSHGateway.yml` started |
| 00:50 | | Job failed: `ansible-playbook exited 4`, `debian@10.5.0.40: Permission denied (publickey)` |

**Result: FAIL**, at the next step. From the node, `ansible "devbox:devbox_gw" -m ping` succeeded for the 12
other new VMs, so this was the SSH gateway alone.

**Finding F8 (high): the SSH gateway locks out Ansible.** The gateway's cloud-init sets `AllowUsers team`, so
sshd refuses `debian`, the account Ansible configures it with. `SetupSSHGateway.yml` could never have
run against a new gateway. **Fixed:** `AllowUsers team debian@<node's management address>`, in
`stacks/devbox/templates/ssh-gateway.tftpl`, with a new test. `debian` is accepted only from the node.
Cloud-init runs only on first boot, so the existing gateway VM had to be replaced.

**Finding F9 (medium, open): the agent throws away what explains a failure.** `scripts/round_agent/executor.py`
discards Ansible's stdout and keeps 500 characters of stderr. Ansible reports task failures on stdout, so
a failed host job usually says only `exited 2` or `exited 4`. For F8, I found the cause by running the same
connection by hand.

### Attempt 5

**Prior state:** 28 VMs, F3, F7 and F8 fixed in the workspace.

| Time | Action | Actual |
|---|---|---|
| 00:53 | `ansible-playbook SetupRoundAgent.yml --tags agent-sync` | Fix present in `/opt/worms` |
| 00:54 | `worms-node-run opentofu/stacks/devbox 'tofu taint proxmox_virtual_environment_vm.ssh_gateway'`, so only the gateway is replaced | "marked as tainted" |
| 00:54 | `worms devbox provision` | Job `1a0c6f01` enqueued and started |
| 00:58 | | VM 115 replaced and booted at 00:58:16 |
| 00:59 | | Job failed: `SetupSSHGateway.yml` exit 4 |
| 01:00:47 | `ssh debian@10.5.0.40` by hand | `Connection refused` |
| 01:01:44 | Same, with `sshd -T` and `systemd-analyze` | Login works. `allowusers team`, `allowusers debian@10.5.0.1`, so **the F8 fix works**. Boot took 3 min 5 s |

**Result: FAIL**, at a new step.

**Finding F10 (high): the build does not wait for a new VM to boot.** Ansible connected 45 s after the VM
started, but sshd only came up after about 3 minutes, under this host's disk load. The playbook does have a
wait for cloud-init, but gathering facts comes first and needs SSH, so the play fails as unreachable. The
earlier attempts did not hit this because their gateway had been up for a long time. `SetupDevbox.yml` and
`SetupSandbox.yml` have the same structure, so a real round could fail the same way. **Fixed:** each of the
three playbooks now starts with a play that waits up to 10 minutes for SSH without gathering facts, with a new
test.

### Attempt 6

| Time | Action | Actual |
|---|---|---|
| 01:04 | `ansible-playbook SetupRoundAgent.yml --tags agent-sync` | Fix present in `/opt/worms` |
| 01:05 | `worms devbox provision` | Job `ce0087b8` enqueued and started |
| 01:06 | | **`SetupSSHGateway.yml` passed**, `SetupDevbox.yml` started |
| 01:06-01:34 | Watched `ps` | `SetupDevbox.yml` sat on one task for 28 minutes |
| 01:26 | `ps` on `devbox-t4-a` | `apt`'s `https` method running for 19 minutes |
| 01:28 | Bounded checks on `devbox-t4-a` | `getent hosts` timed out for every name, including `scoring.worms.internal`. `ss`: `SYN-SENT` to `10.2.0.11:53` |
| 01:30 | Same on its gateway `devbox-t4-gw` | The gateway itself reaches DNS and the cache, but `nft list ruleset` is empty |
| 01:34 | `systemctl stop worms-round-agent` | Job failed with `exited -15` |

**Result: FAIL.** This is the "steps sometimes hang" symptom from the start of this work, seen live. Three findings
came out of it.

**Finding F14 (critical): no devbox VM could reach anything.** The gateway's cloud-init log says why:

```
Failed to enable unit: Unit nftables.service does not exist
/var/lib/cloud/instance/scripts/runcmd: 5: nft: not found
```

The cloud image has no `nftables`, cloud-init never touches apt by design, and no playbook installs it. So the
gateway has no NAT: VMs have no DNS, no scoring, no logging and no private internet. Cloud-init's status still says
`done`, so nothing reports it. `sandbox-gw` has the same template and no install step either, so **a real round's
worm should fail the same way**. That follows from the code; I did not run a round. **Fixed in the repo:** a new role, `untrusted_gateway`,
installs and starts it and fails if the rules are not active, for both devbox and sandbox gateways, before their VMs are configured.

**Finding F15 (high): apt was never pointed at the private internet.** You flagged this during the run. A devbox VM
kept the image's `https://deb.debian.org` sources, so `apt` tried the real internet, which those VMs must not reach, and blocked for
as long as the job allowed. `failed_when: false` on that task tolerates failure but does not limit the time.
**Fixed in the repo:**

- Every untrusted VM (new inventory group `untrusted`) now gets `apt_client`, pointing at `apt-cache`.
- pip on devbox VMs now points at devpi.
- `python3-pip` is now installed on devbox VMs.

**Finding F16 (high): the cache cannot supply what these VMs need.** `apt-cacher-ng` runs with `Offlinemode: 1` and
held 153 packages, none of them `nftables` or `cowsay`. It serves only what some VM fetched while the internet was
open. **Fixed in the repo:** a seed list (`apt_cache_seed_packages`) is fetched with all dependencies during the internet window. The
dependency resolution was checked on the real cache VM (74 packages), but **the download itself has not run**, because the
window cannot open now. `apt install` of anything outside the seed list will still fail, which needs your
decision (see the summary).

**Checked and not a problem:** with the gateway's rules missing, I expected it to route straight between
segments A-B and B-C, which the design forbids. It does not: from A, `traceroute` to C (`10.128.1.13`) stops after the
gateway and port 22 gets no answer, so another layer stops it.

**Also found:** the hostname of every devbox VM is `localhost`, while `docs/e2e_test_protocol.md` expects
`devbox-<team>-a` (F17, low). **Fixed** in the `devbox` role, without replacing VMs.

### Decision point: tearing down the devboxes was refused

Filling the cache needs the internet window, which is refused while devbox VMs exist (F6). So the clean route was to
destroy the 13 devbox VMs, open the window, and rebuild. There is no `worms` command for destroying only the devboxes, so I
tried `worms-node-run opentofu/stacks/devbox 'tofu destroy -auto-approve'`. **The safety check refused it**
("Protected-Scope IaC Apply"). I did not try to get the same result another way. It is listed for your decision.

**What I did instead:** the node downloaded the 3 packages the gateways lack (`nftables`, `libnftables1`,
`libjansson4`, Debian 13 like the VMs) and installed them with `dpkg --force-confold -i` over SSH. This is a one-time
step for these VMs, not part of the fix. The rules themselves were then turned on by the new role, in the build below.
**Consequence: a devbox build on a fresh cache, with the seed list, has not been tested.**

### Attempt 7

**Prior state:** 28 VMs, `nftables` installed but not started on the 3 gateways, 9 keys registered, the fixes above
synced to `/opt/worms` and copied to the VMs with matching hashes.

| Time | Action | Actual |
|---|---|---|
| 01:36 | Marked the orphaned `devbox_reconfigure` job failed, directly in the database | The agent claimed it while stopping. There is no command for cancelling a job |
| 01:42 | `ansible-playbook SetupRoundAgent.yml --tags agent-sync`, then `systemctl restart worms-round-agent` | Synced, restarted |
| 01:47 | `worms devbox provision` | Job `7dce9974` enqueued and started, carrying all 9 keys |
| 01:49 | | `tofu apply` and `SetupSSHGateway.yml` passed. `SetupDevbox.yml` rewrote the gateways' apt sources to the cache |
| 01:51 | `fuser -k /var/lib/apt/lists/lock` on the 9 VMs | Cleared a leftover `apt` from the job I stopped at 01:34, which still held the lock. Ansible had not reached those VMs yet |
| 01:51 | | Job failed at the gateways: `Failed to update cache` |
| 01:52 | `apt-get update` by hand on the gateway | `trixie-backports ... 503 Unable to download in offline mode` |
| 01:53 | Same, with a source list without backports | 0 errors. `nftables`, `python3-pip`, `acl` and `cowsay` all known to apt |

**Result: FAIL.** The cache holds the index of every suite except `trixie-backports`. Plain `apt-get` treats that as a
warning, and Ansible's `apt` module treats it as fatal. **Fixed:** untrusted VMs no longer list backports. Apt installs nothing from
there unless told to. The infrastructure VMs still list it, and do not refresh while the internet is closed.

**Added to F2:** stopping a job does not stop what it started on the VMs. The leftover `apt` would have failed the next
run on its lock.

### Attempt 8

| Time | Action | Actual |
|---|---|---|
| 01:54 | `ansible-playbook SetupRoundAgent.yml --tags agent-sync`, then `worms devbox provision` | Job `7a96982a` enqueued and started |
| 02:02 | | **Job done**, after 7 min 52 s |

**Result: PASS.** Checked afterwards, rather than trusting the job's state:

| Check | Expected | Actual | Result |
|---|---|---|---|
| `qm list` | 28 VMs running | 28 running, 13 of them devbox | PASS |
| `worms status` | 12/12 devbox VMs, internet denied | `devbox VMs 12/12 running`, `internet access denied`, `teams 3` | PASS |
| Watchdog during attempts 7 and 8 | No stops | 0 stops | PASS |
| Gateways: `systemctl is-active nftables`, `nft list ruleset` | Active, NAT present, on all 3 | Active, enabled, `masquerade` present, on all 3 | PASS |
| Gateways: apt sources | The cache, no backports | `http://10.2.0.20:3142/...`, `trixie trixie-updates`, `trixie-security` | PASS |
| Database: `devboxes` | A record per team with the gateway's public name | `alpha`, `bravo`, `charlie`, indexes 0-2, `worms-dev.mlsec.tu-berlin.de:2222`, user `team` | PASS |
| Database: `devbox_vms` | 3 VMs per team | 9, fabric addresses `10.7.{0,1,2}.{11,12,13}` | PASS |
| SSH gateway: `authorized_keys` | All 9 keys, each limited to its team's A, root-owned | All 9, `permitopen` to the right A, `root:root 644` | PASS |

This also confirms the fixes for F3, F7, F8, F12 and F13 on the live system. F10's fix ran here too, but no VM was new in this attempt.

**Observations during the build:**

| What | Measured | Why it matters |
|---|---|---|
| Disk pressure on the host | Up to 91% "some", 70% "full" | One SATA SSD carries everything |
| `worms status` | 51 s (a few seconds when idle) | Slow for an administrator during a build |
| `worms users` | Did not finish within 40 s | Same |
| Dashboard login page | 0.2-0.8 s | Usable during a build |
| Dashboard sign-in (`curl`) | 7.5 s, then success | Slow, but works |

**Test limitation:** a browser on the node can fail with `ERR_NETWORK_CHANGED` while new VM network interfaces
appear, which happened once at 23:48. A user's own browser does not see the node's interfaces, so this does not
affect users. Two later 150 s browser timeouts were not this: they were a bug in my script, which tried to
click a button inside a collapsed section.

**Test slip:** my first sign-in script overwrote the saved new passwords of `aylin` and `carl` when it
retried after a slow response. The database shows both changed their password. Recovering them is
now part of the test, in Stage 3.

## Stage 3: the users sign in, then deploy SSH keys and create access tokens

**Prior state:** 9 accounts with one-time passwords, 3 teams, no keys and no access tokens. Sign-ins ran while
the devbox build was loading the host, so the times below are worst case.

### 3.1 First sign-in, all nine users

| Step | Action | Expected | Actual | Result |
|---|---|---|---|---|
| 1 | Sign in with the one-time password, all 9 | Forced to `/change-password/` | All 9 landed there, 5-10 s per sign-in under load | PASS |
| 2 | Set a new password, all 9 | Accepted, landed on `/profile/` | All 9, 4.5-7.7 s per change, 0 JavaScript errors | PASS |
| 3 | Check the team on the profile | The user's own team | Correct for all 9 | PASS |
| 4 | Anna signs in with her old one-time password | Refused | 401, "Invalid username or password." | PASS |
| 5 | Anna signs in with her new password | `/profile/` | `/profile/` | PASS |
| 6 | Anna opens `/staff/users/` | Refused | Redirected to `/` | PASS |

![Forced password change as a user](screenshots/06-user-forced-password-change.png)

![Profile with access tokens and API instructions](screenshots/07-user-profile.png)

### 3.2 The administrator replaces a lost password

My test slip above lost the passwords of `aylin` and `carl`, which made this a real recovery.

| Step | Action | Expected | Actual | Result |
|---|---|---|---|---|
| 1 | Staff → Users → the user → **New one-time password** | A new password, shown once | Shown once. The button is inside a collapsed section | PASS |
| 2 | Sign in with it | Forced to `/change-password/` | Yes, for both | PASS |

![New one-time password, with the password masked in the screenshot](screenshots/08-staff-new-one-time-password-redacted.png)

### 3.3 SSH keys

**Prior state:** all 9 users signed in, 0 keys, build attempt 6 running.

**First try, FAIL.** Anna's six bad keys were refused correctly (see below). Her real key then returned
**HTTP 500**:

```
POST /devbox/ssh-keys/add/ 500
  File "/opt/dashboard/src/dashboard/views/devbox.py", line 145, in _reconfigure_devbox
  File "/opt/dashboard/src/dashboard/orchestrator_client.py", line 29, in post
ModuleNotFoundError: No module named 'requests'
```

The key had already been saved, so the user saw an error page, while the database held a key that nothing would
install.

**Finding F11 (critical): no user could add or remove an SSH key.** The dashboard's only client for the
orchestrator imported `requests`, which only the orchestrator VM has. So adding a key, removing a key,
leaving a team and resetting a devbox all crashed. The dashboard's page tests replace this client with a stub, so they could
not see it. A second bug waited behind it: the client connected by IP and ignored `ORCHESTRATOR_TLS_NAME`, so
installing `requests` alone would have failed certificate verification.

**Fixed:** the client now uses only the standard library and verifies the certificate for
`orchestrator.worms.internal`. A new test file runs it against a real local HTTPS server, including a wrong name,
an unknown CA and a server that is down, and checks that each VM gets the packages its code imports.

**How I deployed it:** copied the one file to the dashboard VM and restarted the service, then compared hashes. The
documented command, `worms build provision -- --limit dashboard`, cannot run while devboxes exist (F6). **This
makes F6 worse than a note: with devboxes up, there is no supported way to ship a dashboard fix.**

**Second try, with the fix:**

| Step | Action | Expected | Actual | Result |
|---|---|---|---|---|
| 1 | Sign in, click the Dev toggle, open `/` | Dev view with the SSH keys panel | Shown. The toggle stays on the current page | PASS |
| 2 | Add `hello world` | Refused, nothing stored | "not a single-line OpenSSH public key", count unchanged | PASS |
| 3 | Add a valid key with the comment `{{7*7}}` | Refused | Refused, count unchanged | PASS |
| 4 | Add a valid key with a comment of two words | Refused | Refused | PASS |
| 5 | Add a key with bad base64 | Refused | Refused | PASS |
| 6 | Add a key of type `ssh-dss` | Refused | Refused | PASS |
| 7 | Paste `-----BEGIN OPENSSH PRIVATE KEY-----` | Refused | Refused | PASS |
| 8 | Ben (`bravo`) adds Anna's key | Refused, other team | "already registered by another team", count 0 | PASS |
| 9 | Anna adds her key again | "already has" | "Your team already has that key." | PASS |
| 10 | The other 8 users each add their own key | Listed, install announced | All listed, each team 3 keys, "It is being installed on your devbox and the SSH gateway now." 0 JavaScript errors | PASS |
| 11 | Compare the keys in the database with the queued job | 9 keys in the job | **9 registered, the job holds 2** (`t4:2 t5:0 t6:0`) | **FAIL** |

![Devbox page during the build](screenshots/09-user-dev-view-during-build.png)

![A key with a template in its comment is refused](screenshots/10-ssh-key-refused.png)

![Key added](screenshots/11-ssh-key-added.png)

![Another team's key is refused](screenshots/12-ssh-key-of-another-team-refused.png)

**Finding F13 (high): most users' keys were silently dropped.** A `devbox_reconfigure` job carries the keys as they were
when it was created, and the orchestrator skipped creating another while one was queued or running. So the first key
created the job, and later ones were told "It is being installed" while no job held them. This is what happens whenever
a class of users adds keys around the same time. **Fixed:** a job is skipped only if a queued one already carries
exactly these keys, and the agent skips a job when a newer one is queued, so a burst still runs once. Also fixed: adding
a key your team already has now pushes again. Before, the "retry" that the failure message recommends did nothing.

**Finding F12 (high): the dashboard never shows how to connect.** Nothing sets `DEVBOX_SSH_GATEWAY_HOST`, on the VM or in
the repo, and the page shows the `ssh -J` command only when it is set. A team is told "Not ready yet: the devbox is still being
built", permanently. **Fixed:** the orchestrator now gets the node's public name. The name map moved from
`group_vars/dashboard.yml` to `group_vars/all.yml` so both VMs can read it. On this run I added the line to the VM's env
file by hand, since the supported route needs the internet window.

**Not fixed, your decision:** port 2222 is not published (`host_publish_ssh_gateway: false`), so the command the
dashboard prints will not work from outside. I did not open a public port without being asked.

### 3.4 Access tokens

**Prior state:** no tokens.

| Step | Action | Expected | Actual | Result |
|---|---|---|---|---|
| 1 | Anna: **Create a token** with a blank name | Refused | "Give the token a name." | PASS |
| 2 | All 9 users create a token | Shown once | Shown, 43 characters | PASS |
| 3 | Anna reloads the page | Token gone | Gone | PASS |
| 4 | Anna creates `to-revoke` and revokes it | Row marked revoked | Row shows REVOKED | PASS |
| 5 | Database: `access_tokens` | Only hashes | 10 rows, right team on each, SHA-256 only, `revoked` set on one | PASS |

![New token, with the value masked in the screenshot](screenshots/13-new-access-token-masked.png)

![One token revoked](screenshots/14-access-tokens-one-revoked.png)

**Team API**, called with `curl`, token sent on stdin:

| Request | Expected | Actual | Result |
|---|---|---|---|
| `GET /api/v1/rounds/` with no token | 401 | 401 `unauthorized` | PASS |
| With a made-up token | 401 | 401 "Unknown or revoked access token." | PASS |
| With the revoked token | 401 | 401, same message | PASS |
| With Anna's, Ben's and Cleo's tokens | 200, own team only | 200, `alpha`, `bravo`, `charlie` respectively, `rounds: []` | PASS |
| Anna's token, round `999999` | 404 | 404 "No such round for your team." | PASS |
| `http://` on the dashboard VM | No answer | Connection fails | PASS |

**Not tested:** reading another team's real round through the API, because no round has run, and the API's rate limit.

**NOTE (wording):** the page says "Tokens your worm uses to submit flags and logs", and further down says flags and logs
need "No token or team id". The second is what the code does.

## Stage 4: the users log in to their devboxes over SSH

**Prior state:** Stage 2 passed, each team has 3 keys registered and installed.

**What this covers:** I ran SSH from the node to the gateway's fabric address (`10.7.0.2`), with each user's own key and
nothing else available. Port 2222 is not published, so **the public port forward is the one hop not tested**. The gateway
sees the node's fabric address either way, which is what it would see through the forward.

### 4.1 What the dashboard tells a user

| Check | Expected | Actual | Result |
|---|---|---|---|
| Connect panel | The `ssh -J` command | `ssh -J team@worms-dev.mlsec.tu-berlin.de:2222 root@10.7.0.11` | PASS |
| Score | 0 of 3 | `0/3` | PASS |
| VM table | 3 VMs, with addresses | 3 VMs, **addresses column shows "—"** | NOTE |

![Dev view once the devbox is ready](screenshots/15-dev-view-devbox-ready.png)

**Finding F18 (low):** the page says "Reach B and C from there", but the database has no addresses for the VMs
(`devbox_vms.addresses` is `[]`), so it shows none. A user has to find them from inside A. Also, the Copy button covers the end of the command.

### 4.2 Logins

Command, with the gateway's fabric address in place of the public name:

```sh
ssh -F <only this user's key> -J team@10.7.0.2 root@10.7.0.11 'hostname; id -un; test -s /etc/worms/flag'
```

| User | Target | Expected | Actual | Result |
|---|---|---|---|---|
| anna, arne, aylin | `10.7.0.11` | `devbox-t4-a`, root, flag present | As expected, 0-2 s | PASS |
| ben, bea, bo | `10.7.1.11` | `devbox-t5-a`, root, flag present | As expected, 0-2 s | PASS |
| carl, cem, cleo | `10.7.2.11` | `devbox-t6-a`, root, flag present | As expected, 0-1 s | PASS |

This confirms the hostname fix (F17).

### 4.3 Confinement

| Action | Expected | Actual | Result |
|---|---|---|---|
| Anna, through the gateway, to her team's B and C | Refused, A only | `stdio forwarding failed` | PASS |
| Anna to bravo's A; Ben to alpha's A | Refused | `stdio forwarding failed` | PASS |
| Anna: forward to scoring, the dashboard or the node (`-W`) | Refused | `stdio forwarding failed` | PASS |
| Anna as `debian` or `root` on the gateway | Refused | `Permission denied (publickey)` | PASS |
| A key nobody registered; a password | Refused | `Permission denied (publickey)` | PASS |
| Ben straight to alpha's A, without the gateway | Refused | Connection timed out | PASS |
| Pivot: A to B to C, using Anna's forwarded agent | Works | Landed on `devbox-t4-b`, then `devbox-t4-c`, flag present | PASS |
| A straight to C, skipping B | No path | No answer | PASS |
| `authorized_keys` on the gateway | Root-owned | `root:root`, home and `.ssh` too | PASS |
| **Anna: run a command on the gateway** | **Refused, "jump-only"** | **`uid=1001(team)`: it ran** | **FAIL** |
| **Anna: `scp` and `sftp` on the gateway** | **Refused** | **Both worked. She read every team's `authorized_keys`** | **FAIL** |

**Finding F19 (high): a team login gets a shell on the trusted gateway.** `docs/devbox.md` and
`docs/e2e_test_protocol.md` call it jump-only, but `restrict` does not stop commands. I measured what that gives a
team, read-only, as Anna:

| From the gateway, as `team` | Result |
|---|---|
| Tools available | `curl`, `wget`, `nc`, `python3`, `perl`, `ssh` |
| Write to `/tmp` | Yes |
| Other teams' A and B, port 22 | **Open**, including VMs her key's `permitopen` forbids |
| Syslog, `10.13.0.30:514` | **Open**, so a team could send forged audit records |
| Node, database, orchestrator, dashboard, scoring, real internet | Closed |

So it does not reach the node or the database, but it does break the separation between teams at the network level, and it exposes the audit log. A
login to another team's VM still needs that team's key. **Fixed:** `command="/usr/sbin/nologin"` on every key and a
`nologin` shell on the account. A jump opens no session, so it is unaffected.

### 4.4 Key changes, on the live system

**How this was pushed:** Anna re-added her key in the browser, which now pushes the keys again. Job `83a3e41d` carried all
9 keys. While it ran, Bea and Cleo each added a second key: the exact case F13 broke.

| Step | Expected | Actual | Result |
|---|---|---|---|
| Keys added while a job runs | New jobs, none of the keys lost | `23b902e4` (`t5:4`) and `8c4f1518` (`t5:4 t6:4`) queued | PASS |
| The agent works through the queue | The older queued job skipped | `23b902e4 skipped: a newer one is queued`, `8c4f1518` ran | PASS |
| Jump to their own A with the new keys | Works | `devbox-t5-a`, `devbox-t6-a` | PASS |
| Bea's new key to charlie's A | Refused | `stdio forwarding failed` | PASS |
| Jump after the F19 fix: Anna, Ben, Cleo | Works | `devbox-t4-a`, `devbox-t5-a`, `devbox-t6-a` | PASS |
| Command on the gateway after the fix | Refused | "This account is currently not available." | PASS |
| `scp` and `sftp` after the fix | Refused | Both fail | PASS |
| Forward to bravo's A or syslog after the fix | Refused | `stdio forwarding failed` | PASS |
| Gateway account and keys | `nologin`, on every key | Shell `/usr/sbin/nologin`, 11 of 11 keys | PASS |

So **F13 and F19 are confirmed fixed on the live system**.

## Stage 5: from inside a devbox

**Prior state:** Anna logged in on `devbox-t4-a` as root, dev score 0, internet denied. The script ran in her SSH session
and printed statuses only, never the flag.

### 5.1 First run

| Area | Expected | Actual | Result |
|---|---|---|---|
| DNS, all service names | Resolve | All 9 resolve | PASS |
| DNS, the real internet | Does not resolve | `deb.debian.org` does not resolve | PASS |
| DNS, `t4.scoring.worms.internal` | Resolves, per `docs/devbox.md` | **Does not resolve** | **FAIL** |
| Scoring and logging POST, verified TLS | 201 | **`curl` exit 60, `tls_verify=20`** | **FAIL** |
| `apt-get update` | Works, through the cache | Works | PASS |
| `apt-get install acl` (already cached) | Installs | Installed | PASS |
| `apt-get install cowsay` (not cached) | Fails, nothing else available | `rc=100`, `503` | PASS, see F16 |
| `pip3 --version` | Installed | **Not found** | **FAIL** |
| Devpi, root index | 200 | 200 | PASS |
| Devpi, package `six` | Unknown | 404: it holds no packages, see `check-with-user.md` | NOTE |
| Registry, `/v2/` on port 5000 | 200 | 200 | PASS |
| Man pages, `http` | Reachable | 200 | PASS |
| cve-search, port 5000 | Reachable | 200 | PASS |
| Wikipedia, `http` and port 8888 | Reachable | **No answer**: it is not running, see `pi-failures.md` | NOTE |
| NTP | Answers | Answers, stratum 10 (its own clock, see `pi-failures.md`) | PASS |
| Devpi upload | Refused | 404 | PASS |
| Registry upload | Refused | 405 | PASS |

**Containment, all 22 must be closed:**

| Target | Result |
|---|---|
| Real internet (2 addresses) | Closed |
| Node: management, fabric, WAN | Closed |
| Dashboard, database, orchestrator, monitor, syslog | Closed |
| SSH gateway: fabric, management | Closed |
| Other teams' VMs: segment, fabric and management addresses, and a gateway's service address | Closed |
| A sandbox address, scoring's port 22 | Closed |
| Own VM C, skipping B | Closed |

**PASS: 22 of 22 closed.**

**Finding F20 (high): TLS to scoring and logging fails on the devbox.** The CA file is on the VM, but the trust store was
never updated (its date is still the image's). With the CA named by hand, `curl` verifies it. The cause is a lost handler: the
copy happened in the attempt that hung (F15), the update is an Ansible handler that runs at the end of a play, and that play never
ended. Later runs found the file unchanged and never ran it. Any interrupted first run on any VM can hit this. **Fixed:** `ca_distribute` checks
that the trust store holds the CA, and runs the update straight away.

**Finding F21 (high): the build reports success with nothing installed.** A devbox VM had neither `python3-pip` nor
`unzip`, which the worm start depends on. `unzip` is not in the cache, one uncached package aborts the whole `apt-get install`, and
`failed_when: false` hid that. The job said `done`. Proof: fetching all three gives `503` for `unzip`, and fetching without
it succeeds. **Fixed:** the failure is now visible, and recommended packages are off (83 packages become 3). `unzip` is in the seed list. For
this run the node copied it to the 9 VMs by hand, as for `nftables`, because the cache cannot be filled now.

**Finding F22 (medium): per-team service names do not resolve.** `docs/devbox.md` still described dnsmasq, but the DNS
server is BIND, with no wildcard. **Fixed:** wildcard records. Applied with `SetupDNS.yml`, and `t4.scoring.worms.internal` now resolves to `10.2.0.14`.

**Test slip:** my diagnostic ran `apt-get install python3-pip` on VM A under `timeout 100`, and it was killed part-way
through, leaving `dpkg` broken. I repaired it with `dpkg --configure -a` and a reinstall of `liblocale-gettext-perl`; `dpkg --audit` is
now empty. This came from my test, not the product. It also briefly led me to blame recommended packages, which the logs on an untouched VM ruled out.

### 5.2 After the fixes

**Prior state:** job `dcbb3f87` (triggered by Bea and Cleo removing their second keys) finished, with the fixes for F20 and F21 applied.

| Check | Expected | Actual | Result |
|---|---|---|---|
| Bea's and Cleo's removed keys | Refused | `Permission denied (publickey)` | PASS |
| CA in the trust store | Yes | 151 certificates, `worms-internal-ca.pem` linked | PASS |
| Scoring POST from A, verified TLS | 201 | 201, `tls_verify=0` | PASS |
| Logging POST from A, verified TLS | 201 | 201, `tls_verify=0` | PASS |
| Same from B (one hop) and C (two hops, through B) | 201 | 201 for all 4 | PASS |
| `t4.scoring.worms.internal`, verified TLS | 201 | 201, `tls_verify=0` | PASS |
| Malformed body | 400 | 400 | PASS |
| `GET /submit` | 405, per `docs/e2e_test_protocol.md` | **403** | NOTE |
| Valid JSON of 6 KiB to logging | 413, per `docs/worm-contract.md` (4 KiB) | **201** | NOTE |
| 60 POSTs from one VM | 429 after a limit | 13 × 201, then 47 × 429 | PASS |
| `apt-get update`, through the cache | Works | Works | PASS |
| `apt-get install acl` | Installs | Installed | PASS |
| `apt-get install cowsay` | Fails, not cached | `rc=100` | NOTE, your decision |
| `pip3 --version`, `pip.conf` | Present, points at devpi | `pip 25.1.1`, `pypi.worms.internal:3141` | PASS |
| `pip3 install six` | Fails, devpi is empty | `No matching distribution found` | NOTE, your decision |
| Containment, all 22 again | Closed | 22 of 22 closed | PASS |

**So apt and pip do go to the private internet, not the real one.** apt can install what the cache holds. pip reaches devpi, but devpi holds nothing. Both limits were already on your list (`docs/check-with-user.md`, "Filling the private internet caches").

**F23 (low), the documented limits differ from what happens:** a `GET` gets 403 rather than 405, and logging accepted 6 KiB when the
contract says 4 KiB. I did not look further: nothing breaks, but either the docs or the limits should change.

### 5.3 The feedback on the dashboard

| Check | Expected | Actual | Result |
|---|---|---|---|
| Anna's dev view | 3/3, each VM found | `3/3`, all 3 `FOUND` | PASS |
| Anna's logs | Her entries, each with its JSON | 4 entries, newest first | PASS |
| Bea's dev view (other team) | 0/3, no alpha logs | `0/3`, 0 logs | PASS |
| Bravo's flag sent from alpha's A | No credit to anyone | Accepted with 201, alpha stays at 3 captures, bravo at 0 | PASS |
| Database: `flag_capture_attempts` | All marked devbox | 20 attempts, all `devbox=true`, 2 teams | PASS |
| Database: `devbox_captures` | Alpha's 3 VMs, version 1 | Exactly those | PASS |
| Database: round tables | Nothing from a devbox | `flag_captures` 0, `logging` 0 | PASS |

![Score 3 of 3, with logs](screenshots/16-dev-view-score-and-logs.png)

![Another team sees nothing of it](screenshots/17-dev-view-other-team.png)

## Stage 6: a user resets the devbox

You asked for this during the run.

**Prior state:** alpha at version 1 with 3/3 and 4 logs, bravo at version 1. VM IDs, boot times, and hashes of flags and host keys saved for alpha's and bravo's VMs.

**Getting there:** a reset's rebuild installs `unzip` from the cache on new VMs, the cache did not hold it, and the fix for F21 makes that fatal. So I put that one
package in the cache by hand (`secdeb/pool/updates/main/u/unzip/`, with its `.head` file, after apt-cacher-ng's import left it unfiled). The node downloaded it from Debian, and apt still
checks its hash against the signed index. A devbox then fetched it through the cache. This stands in for the seed step, which cannot run now.

### First try, FAIL

| Time | Action | Expected | Actual | Result |
|---|---|---|---|---|
| 02:48:58 | Anna: **Reset devbox**, then **Yes, rebuild my devbox** | "being rebuilt", VMs replaced | Message shown, version 2, score 0/3. **Job failed in 0.26 s, no VM touched** | **FAIL** |
| 02:49:05 | Arne does the same | "already in progress" | Job had already failed, so **another reset: version 3, also failed in 0.23 s** | **FAIL** |

```
Error: Invalid force-replace address "module.devbox_vm[\\\"devbox-t4-a\\\"].proxmox_virtual_environment_vm.vm"
This character is not used within the language.
```

**Finding F24 (critical): no user could ever reset a devbox.** The handler escaped its quotes for a shell, but no shell
runs the command, so OpenTofu got the backslashes and rejected the address. The tests only checked that a job's parameters were valid, and never gave these arguments to OpenTofu. **Fixed,** with a test that runs the real
`tofu plan` on both the old and new forms.

**Finding F25 (medium, open): a failed reset looks like a successful one, and costs the team its score.** The version goes up and the score goes to 0
before the job runs, so here alpha lost its 3/3 while nothing was rebuilt, and old flags still worked on the old VMs. The page
keeps saying "being rebuilt", with no sign of failure. Only an administrator reading `host_jobs` can tell. Not changed.

### Second try

| Time | Action | Expected | Actual | Result |
|---|---|---|---|---|
| 02:51:31 | Anna resets | "being rebuilt", score 0 | Message shown, version 4, score 0/3, job `a061abde` running | PASS |
| 02:51:44 | Arne resets while it runs | "already in progress", no second job | That message, version stays 4, no new job | PASS |
| 02:51:51 | Bea's view | Unchanged | Version 1, her team's own log still there | PASS |
| 02:51:53 | Cleo logs in to charlie's A during the rebuild | Works | `devbox-t6-a` | PASS |

![Reset confirmation](screenshots/18-reset-confirmation.png)

![After the reset](screenshots/19-after-reset.png)

| Time | Event | Result |
|---|---|---|
| 03:01:56 | Watchdog: `no firewall rules for sandbox/devbox VM 121; stopping sandbox/devbox VMs: 116 ... 127` | **All 12 devbox VMs of all 3 teams stopped** |
| 03:03 | Stopped the job, which was waiting on stopped VMs | Failed after 11 min 55 s |

**Result: FAIL.** The watchdog did exactly what it is for. The problem it caught is worse than the outage.

**Finding F26 (critical): replacing a VM can leave it running with no firewall, silently.** VM 121's firewall file held rules but
no `[OPTIONS]` block: no `enable`, no `DROP` policy, no IP filter, and 0 compiled rules. Proxmox deletes a VM's firewall settings
with the VM. The replacement happened to get the same ID, the options resource had nothing to change, and so they
were never written back. VMs that got a different ID did get them. A `tofu plan` afterwards lists only VM starts, so **OpenTofu cannot see this or
repair it**.

**This had already happened, because of my own step.** I replaced the SSH gateway at 00:54 with `tofu taint` (Attempt 5), and it got its ID back.
**VM 115, the trusted gateway, then ran without a firewall from its boot at 00:58 until 03:07, about 2 h 10 min.** It is not in a watched pool, so nothing noticed. I found it only
because of VM 121:

| VM | `enable` | `policy_in` | Compiled rules |
|---|---|---|---|
| 115 `devbox-ssh-gateway` | **none** | **none** | **0** |
| 121 `devbox-t4-c` | **none** | **none** | **0** |
| The other 11 | 1 | DROP | 32 or 42 |

**Correction to F19.** I measured what a team shell could reach during those 2 hours. Measured again with the firewall on:

| From the gateway | Firewall off (F19 table) | Firewall on |
|---|---|---|
| Other teams' VMs, port 22 | Open | Open |
| **Syslog, port 514** | **Open** | **Closed** |
| Node, database, orchestrator, dashboard, scoring, real internet | Closed | Closed |

So the syslog exposure came from this bug, not from the gateway's design. The shell on the gateway was still real, and is fixed.

**Correction to F3.** I wrote that attempts 3 and 4 confirmed that fix. Its ordering is right, but those attempts did not exercise replacing a VM
that keeps its ID.

**What I did:**

1. At 03:07, restored VM 115's options through the Proxmox API, with the values `stacks/devbox/main.tf` declares. Its rules compiled 3 s later, and jumps still work.
2. Restarted the 9 stopped VMs of the gateways and of bravo and charlie, after checking each has compiled rules. Left alpha's three stopped, since the next reset replaces them.
3. **Fixed in the repo:** `replace_triggered_by` on the firewall options of every VM type (devbox, sandbox, infrastructure, private internet, monitor, SSH gateway), and on the
   memberships of the watched pools. A replaced VM now leaves the pool first, and rejoins after its firewall. Nothing changes until a VM is next replaced.

### Third try

**Prior state:** version 4, alpha's VMs stopped, VM 121 with no firewall options, fix synced to `/opt/worms`.

| Time | Action | Expected | Actual | Result |
|---|---|---|---|---|
| 03:14:40 | Anna resets | "being rebuilt", score 0 | Message shown, version 5, job `86cfd56f` running | PASS |
| 03:14:47 | Arne resets while it runs | "already in progress", no second job | That message, version stays 5 | PASS |
| 03:30:55 | | Job done | Done after 16 min, **0 watchdog stops** | PASS |

**Checked afterwards.** All three VMs kept their IDs this time, which is exactly the case that failed before:

| VM | VM ID | New boot | New flag | New host key | Expected |
|---|---|---|---|---|---|
| `devbox-t4-a` | 122 → 122 | Yes | Yes | Yes | Replaced |
| `devbox-t4-b` | 119 → 119 | Yes | Yes | Yes | Replaced |
| `devbox-t4-c` | 121 → 121 | Yes | Yes | Yes | Replaced |
| `devbox-t5-a`, `-b`, `-c` | Unchanged | No | No | No | Untouched |

| Check | Expected | Actual | Result |
|---|---|---|---|
| All 13 VMs: firewall | Enabled, `DROP`, compiled rules | All 13: `enable=1`, `policy_in=DROP`, 22-42 rules | PASS |
| Pool `devbox` | 12 members | 12 | PASS |
| Anna logs in to the new A | Works | `devbox-t4-a`, root, flag present, `dpkg --audit` empty | PASS |
| New VM: apt, CA, pip, `unzip`, DNS | All in place | Apt on the cache, CA linked, pip 25.1.1, `unzip` present, DNS resolves | PASS |
| New flags from A, B and C | 201 | 201, `tls_verify=0`, 6 of 6 | PASS |
| Dashboard | 3/3 at version 5, only the new logs | `3/3`, version 5, 3 logs | PASS |
| Earlier records | Kept | Captures: 3 at version 1, 3 at version 5. Logs: 4 and 3 | PASS |
| Ben and Cleo log in | Works | `devbox-t5-a`, `devbox-t6-a` | PASS |

![Score 3 of 3 again after the reset](screenshots/20-after-reset-score-again.png)

**Result: PASS. A user can reset their devbox, and it now does what the page promises.** New VMs went through the whole path here: boot wait,
gateway, apt through the cache, CA, pip, flags. So this also confirms the fixes for F10, F15, F20, F21 and F26 on new VMs.

**NOTE:** a reset took 16 minutes on this disk. It also changes the VMs' host keys, so a user with a `known_hosts` entry gets SSH's "host identification
has changed" warning, which the page does not mention.
