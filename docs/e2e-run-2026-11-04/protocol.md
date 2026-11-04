# End-to-end test run, 2026-11-04: from a clean base state

A repeat of the administrator and user journey on the dev node (`worms-dev`), starting from a clean base state. It follows
the format of `docs/e2e-run-2026-11-03/protocol.md`.

## Summary

**The journey you described now works from start to finish on a clean system, with nothing installed by hand.** A build needs retrying, though, because the disk times out, and getting here took three more fixes.
The automated suite is written and its helpers work on this system, but it has not yet run from start to finish, which needs one more teardown from you.

| Stage | What you asked for | Result |
|---|---|---|
| 0 | A clean base state | **PASS** after 2 tries at `make`, and it was less clean than I first reported (G7) |
| 1 | Administrator creates users and teams | **PASS**, first try |
| 2 | Administrator creates devbox VMs | **PASS on the 7th attempt**: 4 disk timeouts (F5), and 2 bugs, now fixed (G7, G8) |
| 3 | Users sign in and add SSH keys | **PASS**, all 9 |
| 4 | Users reach their devbox through the SSH gateway | **PASS**, all 9, from the node. Port 2222 is still not published, so it does not yet work from outside |
| 5 | Create a file, `apt`, `pip`, `curl` to scoring and logging | **PASS** on all 9 VMs, with plain commands and nothing special |
| 6 | The dashboard shows the traces | **PASS**: scoring and logging on the team's page, and in the database as an administrator |
| 7 | A user resets the devbox | **PASS**, first try, 19.5 minutes |
| 8 | SSH again, and all changes are gone | **PASS**: the file, `jq` and `tabulate` gone on all 3 VMs, other teams untouched, and the new VMs work |

### Findings

| # | Severity | Finding | Status |
|---|---|---|---|
| F5 | **High**, raised | `zfs create` times out under disk load. It hit 4 of 7 builds, and the disk looks like the main reason steps hang | **Open**, try a TRIM (`docs/check-with-user.md`, 3d) |
| G5 | High | Zeek stops recording all traffic at its first log rotation, while showing `active`. Sandbox rounds would lose it too | Fixed, confirmed live |
| G1 | High | A plain `pip install` fails before it contacts devpi. The previous protocol hid this by using a flag it did not mention | Fixed, confirmed live |
| G2 | High | devpi is empty | Fixed, confirmed live, with a minimal seed |
| G6 | High | devpi could not be filled at all on this VM | Fixed, confirmed live |
| G7 | High | A teardown can leave a volume that blocks later builds. **The command I gave you caused one**. Round teardowns had the same flaw | Fixed. The fix has not been through a teardown yet |
| G8 | High | No freshly built gateway could finish configuring, so my F14 fix did not work on a clean build. A sandbox's would have stopped at the same place | Fixed, confirmed live for devboxes. Sandbox not run |
| G3 | Medium | Nothing in the apt seed could show that `apt install` works | Fixed, confirmed live, with `jq` |

**Also confirmed by this run, from the previous one:** F14, F15, F16 (the seed step finally ran), F17, F24, F26, and devbox traffic not being recorded (by design).

**Still open from the previous run:** F1, F2, F4, F6, F9, F18, F23 and F25. None got in the way of this journey except F9: the agent discards Ansible's output, so finding G8 meant running the playbook by hand.

### Not tested

- **A real round or a sandbox**, so the sandbox's copies of the G7 and G8 fixes have not run.
- **The automated suite from start to finish.** Its helpers were tried on this system, and it refuses correctly when the system is not in base state, but creating accounts and pressing Reset through it need a clean start.
- **A teardown with the G7 fix.**
- **The public port forward on 2222, and the dashboard's name from outside the node.**
- **A reset of more than one team, or two at once.** Only `alpha` was reset.
- **Access tokens.** They were not part of this run. The previous run tested them.

### What was done by hand

Everything else went through `make`, `worms` and the dashboard.

| What | Why |
|---|---|
| You ran the teardown | The safety check refused it for me |
| Deleted my hand-placed `unzip` from the apt cache | So the seed had to supply it |
| Removed the volume `vm-118-cloudinit` | G7. No VM used it |
| `ansible-playbook SetupRoundAgent.yml --tags agent-sync`, restarted the agent when idle | To deliver the G8 fix without `make`, which refuses while devboxes exist (F6) |

### Offline test suite

655 passed, 5 failed, 15 skipped (the live journey, without `--live`). 4 of the failures are the same as before this run: `test_playbook_syntax[inventory.generated.yml]`, `test_fixture_matches_shared_variables`,
`test_monitor_port_is_receive_only` and `test_two_teams_run_one_after_another`. The 5th, `test_mirror_copies_each_frame_once`, saw no captured frames once during the full run. It then passed 5 of 5 times, alone and with its file, and I changed nothing it covers, so this looks like timing under load. I did not prove that.

**No secret reached this repository as text:** the 20 passwords of this run were compared against 29,677 files, with 0 matches. I viewed the screenshots that could show one, after painting out 04 and 05 (Stage 1).

### State left behind

28 VMs running, internet denied, no job running, 0 watchdog stops since 12:00. Teams `alpha` (devbox version 2), `bravo` and `charlie` (version 1), with 9 users. The admin password was changed by the test. Nothing is committed.

## What this run tests

1. **Administrator:** creates users, teams and devbox VMs.
2. **User:** signs in, adds SSH keys and reaches the devbox through the SSH gateway.
3. **On the devbox:** creates a file, runs `apt`, runs `pip`, and calls logging and scoring with `curl`.
4. **Dashboard:** the team's page shows the `curl` calls to scoring and logging. Other traces are checked as an administrator, in the database.
5. **Reset:** the user resets the devbox and signs in over SSH again. The file, the `apt` package and the `pip` package must be gone.

## Stage 0: reaching the base state

**Base state means:** the 15 infrastructure VMs as `make` leaves them, no devbox VMs, and a database holding only `admin` and the default
competition.

**Prior state, 10:20:** 28 VMs running, 13 of them devbox VMs from the previous run. Teams `alpha`, `bravo` and `charlie`, 9 active test
users and 3 deactivated ones. Internet denied, no jobs running, 0 watchdog stops since 03:10.

### Steps

| Time | Step | Why | Result |
|---|---|---|---|
| 11:13 | You destroyed the devbox stack (`worms-node-run opentofu/stacks/devbox 'tofu destroy -auto-approve'`) | The internet window stays closed while devbox VMs exist | 15 VMs left, none named `devbox`, devbox state empty. `vmbr7` stays, as it belongs to the platform stack. 0 watchdog stops |
| 11:15 | `worms reset-database --yes` | Removes the previous run's users and teams | 31 tables emptied. `worms users` shows only `admin`, `worms teams` shows none |
| 11:15 | Deleted the hand-placed `unzip` from the apt cache | The seed step must supply it by itself | 0 `unzip`, 0 `nftables`, 0 `jq` left in the cache. devpi holds 0 files |
| 11:15-11:32 | `make`, attempt 1 | Ships all fixes through the supported path, and fills the apt cache and devpi | **FAIL** at the new devpi seed step, see G6. The internet window closed again by itself. The apt seed passed |
| 11:35-11:58 | `make`, attempt 2, with the fix for G6 | | **PASS**, exit 0, "setup complete (internet access denied)". OpenTofu: no changes on any stack |
| 11:58 | Checked the base state | | **PASS**, table below |

### The base state

| Check | Expected | Actual | Result |
|---|---|---|---|
| `qm list` | 15 VMs, all running, none named `devbox` | 15, 15, 0 | PASS |
| `worms status` | Firewall on, internet denied, no sandbox or devbox VMs, 0 teams, 0 rounds | As expected | PASS |
| `worms users`, `worms teams` | Only `admin`, no teams | As expected | PASS |
| `worms secrets check` | All 3 tokens valid | Valid | PASS |
| Host services | All active | `proxmox-firewall`, `worms-host-firewall`, `worms-mirror`, `worms-round-agent` active | PASS |
| Files by hand, compared with the repo | `orchestrator_client.py`, `views/devbox.py`, `orchestrator/api.py` match | All 3 hashes match, so `make` now ships what was copied by hand last time | PASS |
| Apt cache | `jq`, `nftables`, `unzip` supplied by the seed | Each present, after being absent at 11:15 | PASS |
| devpi | `tabulate` held, `--offline-mode` on | Page `200`, wheel stored, mirror lists only `tabulate`, offline flag present | PASS |
| devpi: a project it does not hold | Refused | `six`: `502`. Refused, but not a `404`; its effect on `pip` is checked in Stage 5 | NOTE |
| Zeek, for G5 | One writable mount | `/var/lib/zeek` only, and the first rotation, below, passed | PASS |

**G5 confirmed fixed by the 12:00 rotation:** `/var/lib/zeek/archive` now holds rotated logs (`stats.2026-11-04-10-29-29.log` and others), 0 `failed to rename` errors, and the live logs were written again after 12:00.

**Finding G6 (high, fixed): the documented way to fill devpi could never work on this VM.** By default devpi first downloads the name of every project on PyPI. Here that does not finish within devpi's 5-second
request timeout, so it answers "does not exist" for every project: `serving stale projects for 'pypi', getting data timed out after 5 seconds`, then `The project tabulate does not exist`.
This is also probably part of why devpi was empty after every earlier `make`. **Fixed:** the role sets `mirror_no_project_list` on `root/pypi` before seeding, with a new test. devpi then asks PyPI about each project directly. This should also avoid
the 1.8 GB indexing peak noted in `devpi.service.j2`, since only fetched projects are indexed. I did not measure that.

### The teardown was refused

| Time | Action | Result |
|---|---|---|
| 10:21 | `worms-node-run opentofu/stacks/devbox 'tofu destroy -auto-approve'` | **Refused** by the safety check: "Blind Apply" |
| 10:22 | `tofu plan -destroy -out=...`, then read the plan | Allowed. 108 deletions and nothing else: VMs 115-127, 6 bridges, and their firewall rules, IP sets, pool memberships and files. No infrastructure VM |
| 10:23 | `tofu apply` of that reviewed plan | **Refused** by the safety check: "Auto-Mode Bypass" |

I stopped there and did not try another way to the same result, such as `qm destroy` or an agent job. After the refusals, all 28 VMs were
still running and OpenTofu's devbox state was unchanged. I deleted the saved plan, and you ran the command yourself.

## Stage 1: the administrator creates users and teams

**Prior state:** the base state above.

| Time | Step | Action | Expected | Actual | Result |
|---|---|---|---|---|---|
| 11:59 | 1 | `worms admin reset-password --yes` | A one-time password, printed once | 20 characters | PASS |
| 11:59 | 2 | Sign in with a wrong password | Refused | 401, "Invalid username or password." | PASS |
| 11:59 | 3 | Sign in with the one-time password | Forced to `/change-password/` | Landed there | PASS |
| 11:59 | 4 | Open `/staff/users/` before changing it | Still forced to change it | Sent back to `/change-password/` | PASS |
| 11:59 | 5 | Set a new password | Accepted | "Password saved.", landed on `/profile/` | PASS |
| 11:59 | 6 | Open Competition, Users, Teams, Rounds, Topology | All open | All 200, 0 JavaScript errors | PASS |
| 11:59 | 7 | Staff → Competition → **Create it** | Dev competition exists | "Development competition 'WORMS 2026 · dev'" | PASS |
| 11:59 | 8 | Staff → Teams → `alpha`, `bravo`, `charlie`, at most 3 members | 3 teams | All created, each with the hint to run `worms devbox provision` | PASS |
| 12:00 | 9 | Staff → Users → form: `anna.alpha@example.org` in `alpha` | An account and a one-time password | Created | PASS |
| 12:00 | 10 | Staff → Users → CSV import of the other 8 | 8 accounts, no problems | 8 created, none reported | PASS |
| 12:01 | 11 | `worms teams`, `worms users` | 3 teams at 3/3, 9 users, "never logged in", "one-time pw" | As expected | PASS |

![Forced password change](screenshots/01-admin-forced-password-change.png)
![Dev competition created](screenshots/02-dev-competition-created.png)
![Teams created](screenshots/03-teams-created.png)
![CSV import, passwords painted out](screenshots/05-csv-import-passwords-hidden.png)

**Test slip, mine:** my screenshot mask covered the password table but not the "As CSV" block below it, so screenshots 04 and 05 held the 9 one-time passwords. I saw this when checking 05, which also put 8 of
them in this session's output. They were painted out at once, the file was never committed, and the suite will mask both blocks. These are test accounts on this dev system, and each password stops working at that user's first login in Stage 3.

## Stage 2: the administrator builds the devboxes

**Prior state:** 3 teams, 9 users, 15 VMs, no devbox VMs, internet denied. This is the first build on a freshly filled cache, the case the previous run could not test.

### Attempt 1

| Time | Action | Actual |
|---|---|---|
| 12:00 | `worms devbox provision` | "3 team(s): t1@0, t2@1, t3@2", job `73502eeb` enqueued, started by the agent |
| 12:01-12:26 | Watched `qm list` | One new VM every 1-4 minutes, 12 VMs after 25 minutes. Watchdog: 0 stops |
| 12:26 | | Job **failed**: `command 'zfs create -V 3145728k rpool/vm-122-disk-1' failed: got timeout` |

**Result: FAIL.** This is F5 from the previous run, still open: Proxmox's `zfs create` hit its time limit under disk load, on 1 of 13 VMs. The retry below is the documented fix.

### Attempt 2

| Time | Action | Actual |
|---|---|---|
| 12:28 | `worms devbox provision` | Job `35b9ac4d`. The VMs attempt 1 left tainted or unfinished (119, 122, 127) were removed |
| 12:31 | | Job **failed**: `Error: VM create`, on `qmcreate:118` |
| 12:32 | Read the Proxmox task log | `unable to create VM 118 - zfs error: cannot create 'rpool/vm-118-cloudinit': dataset already exists` |

**Result: FAIL**, for a new reason, and retrying again would not have helped.

**Finding G7 (high, fixed in the repo): a teardown can leave a volume that blocks later builds, and the command I gave you caused it.**
The volume was created at 00:01 last night, for the previous run's VM 118, and it outlived your teardown. `tofu destroy` runs in parallel unless told otherwise, unlike every `apply` here,
which uses `-parallelism=1`. So 13 `qmdestroy` tasks ran within 45 seconds, and Proxmox's storage lock timed out on 8 of them, with only a warning:

```
WARN: Could not remove disk 'local-zfs-sandbox:vm-118-cloudinit', check manually: can't lock file '/var/lock/pve-manager/pve-storage-local-zfs-sandbox' - got timeout
```

Each finished as `WARNINGS`, so `tofu destroy` reported success. Proxmox then hands out the free ID 118 again, and every attempt to create a VM with it fails.
**My base-state check above missed this:** it looked at VMs and not at their volumes, so the base state was not as clean as it reported.

**What I did:**

- Checked that no VM 118 exists and that it was the only such volume, then removed it with `pvesm free local-zfs-sandbox:vm-118-cloudinit`.
- `Pipeline._destroy` now uses `-parallelism=1`, with a new test.
- The round agent's sandbox teardown had the same flaw, and sandboxes have more VMs, so **every round's teardown could have hit it**. It now uses `-parallelism=1` too, with a test. This has not run live, and neither of these fixes has been through a teardown yet.
- The suite now checks for such volumes in its base state.

### Attempts 3 and 4

| Time | Action | Actual |
|---|---|---|
| 12:34 | `worms devbox provision`, attempt 3 | VM 118 now builds, so G7 was the cause of attempt 2 |
| 12:42 | | Job **failed**: F5 again, `zfs create -V 3145728k rpool/vm-127-disk-0 failed: got timeout`, on the last VM |
| 12:44 | `worms devbox provision`, attempt 4 | All 13 VMs running at 12:48, firewalls on all, 0 watchdog stops. `SetupSSHGateway.yml` passed |
| 12:50 | | Job **failed**: `SetupDevbox.yml` exit 2 |
| 12:51 | Ran the playbook by hand, because the agent discards its output (F9) | `cloud-init status --wait`: `rc=1`, `status: error` on all 3 gateways |
| 12:52 | `cloud-init status --long` on a gateway | `('scripts-user', RuntimeError('Runparts: 1 failures (runcmd) in 1 attempted commands'))` |

**F5 is worse than the previous run suggested:** it hit 2 of the 4 attempts so far, and a retry is the only answer.

**Finding G8 (high, fixed): no freshly built gateway could ever finish configuring, so my F14 fix did not work on a clean build.** The gateway's cloud-init script runs `nft`, which the image does not have. That leaves cloud-init in `error`,
`SetupDevbox.yml` treats that as fatal, and the play stops before the role I added last run to install `nftables`. The previous run could not see this, because I had installed `nftables` on those gateways by hand, which is exactly the gap this clean run was meant to close.
`sandbox-gw` has the same two lines and the same playbook check, so **a real round's sandbox would have stopped at the same place.** That follows from the code; no sandbox has been built.

**Fixed:** I removed the two `nft` lines from `devbox-gw.tftpl` and `sandbox-gw.tftpl`, with a new test. A real cloud-init failure still stops the play. The `tofu plan` replaces exactly the 3 gateways and nothing else. I synced the agent and restarted it while idle, which also loads the teardown fix from G7.

### Attempts 5 to 7

| Time | Action | Actual |
|---|---|---|
| 12:56 | `worms devbox provision`, attempt 5 | The 3 gateways were replaced. New gateways show `status: done`, so the G8 fix works |
| 13:09 | | Job **failed**: F5, on gateway `t2` |
| 13:10 | Attempt 6 | Job **failed** after 90 seconds: F5, on the same gateway, replacing only that one VM |
| 13:14 | Attempt 7 | `tofu apply` passed at 13:18 and `SetupSSHGateway.yml` at 13:20 |
| 13:20-13:29 | | `SetupDevbox.yml` ran for 9 minutes, without hanging |
| 13:29 | | Job **done** |

**Result: PASS on the 7th attempt.** Of the 6 failures, 4 were F5 (attempts 1, 3, 5 and 6), 1 was G7 (attempt 2) and 1 was G8 (attempt 4).

| Check on the result | Expected | Actual | Result |
|---|---|---|---|
| `qm list` | 13 devbox VMs running | 13 of 13 | PASS |
| Firewall options on each devbox VM | All on | All on | PASS |
| Firewall watchdog, since 12:00 | No VM stopped | 0 stops, through all 7 attempts | PASS |
| 3 gateways | `nftables` from the seeded cache, NAT rules active, cloud-init `done` | All 3 | PASS |
| Devbox VMs, sampled 4 of 9 | Named after themselves, `python3-pip` and `unzip`, token present, apt at `http://10.2.0.20:3142`, `break-system-packages` | All | PASS |
| Database | 3 VMs per team | 3, 3, 3 | PASS |

**A devbox build on a fresh cache, which the previous run could not test, now works with nothing installed by hand.**

**Finding F5, raised from medium to high: the `zfs create` timeout decides whether a build succeeds.** It hit 4 of 7 attempts, once when only one VM was being replaced. Measured during the run:

| Measure | Value |
|---|---|
| Proxmox's limit on `zfs create` outside a worker (`ZFSPoolPlugin.pm`) | 10 s |
| `zfs create` of a small volume, quiet moment | 0.4 to 2.2 s |
| ZFS transaction groups, last 99 at 13:13 | Median 2.9 s, 20 over 5 s, slowest **108.7 s** |
| During attempt 7 | 13-21 MiB took 3-7 s to write. Slowest group 110.6 s |
| Read latency, `zpool iostat -l` | 67 ms, which is very slow for an SSD |
| The disk | One SATA SSD, Samsung SM841N, 19 TB written. SMART shows no errors and 86% wear level remaining |
| `zpool status -t` | `(untrimmed)`, `autotrim` off |

**Likely cause, not proven:** the SSD has never been told which blocks are free, which can make old SATA SSDs very slow to write. This is probably also a large part of the "steps hang" you first asked about. I did not run `zpool trim`: it is your disk, and some older Samsung SATA drives have a history of TRIM bugs, so it is your decision (`docs/check-with-user.md`).

## Stage 3: users sign in and add their SSH keys

**Prior state:** 13 devbox VMs running, 9 accounts with one-time passwords, no keys.

| Time | Action | Expected | Actual | Result |
|---|---|---|---|---|
| 13:31-13:35 | Each of the 9 users signs in with the one-time password | Forced to `/change-password/` | All 9, status 200 | PASS |
| 13:31-13:35 | Each sets a new password | Lands on the profile | All 9 on `/profile/` | PASS |
| 13:31-13:35 | Each opens the dev view and adds an SSH key | Saved, and the page says it is being pushed | All 9: "SSH key added. Your devbox and the SSH gateway are being updated now." 1, 2 and 3 keys per team as they arrive. 0 JavaScript errors | PASS |
| 13:35-13:39 | Watched the push jobs | All done | 9 `devbox_reconfigure` jobs, all done. Jobs behind a running one mostly finished at once: 3 took about 3 minutes each | PASS |
| 13:37 | Each user's old one-time password again | Refused | 9 of 9 refused, 401. So the passwords exposed by my slip in Stage 1 no longer work | PASS |
| 13:40 | `anna`'s dev view | Version 1, 0/3 tokens, the SSH command, 3 keys | `VERSION 1`, 0/3, `ssh -J team@worms-dev.mlsec.tu-berlin.de:2222 root@10.7.0.11`, `3 REGISTERED`, 0 JavaScript errors | PASS |

![Forced password change](screenshots/07-user-forced-password-change.png)
![SSH key added](screenshots/09-ssh-key-added.png)
![Dev view with the SSH command](screenshots/10-dev-view-ready.png)

## Stage 4: SSH through the gateway

**Prior state:** all keys pushed. **From the node:** port 2222 is not published, so I used the command the dashboard shows, with the gateway's address `10.7.0.2` in place of the public name. This covers everything but the port forward (`docs/check-with-user.md`, 3b).

| Time | Action | Expected | Actual | Result |
|---|---|---|---|---|
| 13:40 | `ssh -J team@10.7.0.2 root@10.7.0.11 'hostname; id -un'`, as each of `anna`, `arne`, `aylin` | `devbox-t1-a`, `root` | All 3 | PASS |
| 13:40 | The same to `10.7.1.11`, as `ben`, `bea`, `bo` | `devbox-t2-a`, `root` | All 3 | PASS |
| 13:40 | The same to `10.7.2.11`, as `carl`, `cem`, `cleo` | `devbox-t3-a`, `root` | All 3 | PASS |
| 13:41 | `anna` to `bravo`'s A | Refused | `stdio forwarding failed`, exit 255 | PASS |
| 13:41 | `ben` to `alpha`'s A | Refused | `stdio forwarding failed`, exit 255 | PASS |
| 13:41 | `anna` to her own team's B, directly | Refused: only A is an entry point | `stdio forwarding failed`, exit 255 | PASS |
| 13:41 | `anna`: a command on the gateway | Refused | `This account is currently not available.`, exit 1 | PASS |
| 13:41 | `anna`: forward to syslog through the gateway | Refused | `stdio forwarding failed`, exit 255 | PASS |
| 13:41 | A key nobody registered | Refused | `Permission denied (publickey)`, exit 255 | PASS |

## Stage 5: on the devbox

**Prior state, 13:41:** on all 9 VMs, no marker file, no `jq`, no `tabulate`. As `anna`, `ben` and `carl`, in their own SSH sessions through the gateway. A directly, B through A, and C through B, with their forwarded agent.

Each row covers **all 9 VMs**, with identical results.

| Action | Expected | Actual | Result |
|---|---|---|---|
| `echo run-<team> > /root/e2e-marker.txt` | File created | Created | PASS |
| apt sources | The private internet's cache | `http://10.2.0.20:3142/deb.debian.org/debian` | PASS |
| `apt-get update` | Exit 0 | Exit 0 | PASS |
| `apt-get install jq` | Absent, then installed | Absent, exit 0, `jq-1.7` | PASS |
| `pip3 install tabulate`, with no special flags | Absent, then installed | `ModuleNotFoundError`, exit 0, then `0.10.0` | PASS |
| `curl` POST to `https://scoring.worms.internal/submit` with the VM's token | 201, verified TLS | `http=201 tls_verify=0` | PASS |
| `curl` POST to `https://logging.worms.internal/submit` with a marker | 201, verified TLS | `http=201 tls_verify=0` | PASS |
| `curl https://deb.debian.org/`, `https://pypi.org/simple/`, `http://1.1.1.1/` | Closed | Closed: no name, no name, timeout | PASS |

**This is the first run where a plain `pip install` works**, and where `apt` and `pip` install from the private internet on a freshly built VM.

**What the private internet does not hold**, on `alpha`'s A:

| Action | Expected | Actual | Result |
|---|---|---|---|
| `apt-get install cowsay` | Fails | Exit 100 after 10 s: `Unable to fetch some archives` | PASS. What it should hold is your decision (`docs/check-with-user.md`, 3c) |
| `pip3 install six` | Fails | Exit 1 after 9 s: `No matching distribution found`. So devpi's `502` from Stage 0 does no harm | PASS, same decision (`docs/check-with-user.md`, 5) |

## Stage 6: the traces

**Prior state, 13:51:** each team's 3 VMs have submitted their tokens and a log entry.

### On the team's page

| Action | Expected | Actual | Result |
|---|---|---|---|
| `anna`, `ben`, `carl` each open their dev view | Version 1, 3/3 tokens, 3 log entries | All 3 teams, 0 JavaScript errors | PASS |
| `alpha`'s page, entries opened | Each VM marked found. Log entries with the VM's name and `run-alpha` | As expected, see below | PASS |
| `bravo`'s page, entries opened | Its own entries, none of `alpha`'s | `run-bravo` only, see below | PASS |

![alpha's page: 3 of 3 tokens and 3 log entries](screenshots/11-dashboard-alpha-traces.png)
![bravo's page: only its own entries](screenshots/12-dashboard-bravo-traces.png)

### In the database, as an administrator

| Table | Expected | Actual | Result |
|---|---|---|---|
| `devbox_captures` | 3 per team, at version 1 | `alpha`, `bravo`, `charlie`: 3 each, at the times of the `curl` calls | PASS |
| `devbox_logs` | 3 per team, at version 1 | 3 each | PASS |
| `flag_capture_attempts`, `logging_attempts` | 9 each, all processed, marked devbox | 9 and 9, all processed | PASS |
| `devbox_ssh_keys` | 3 per team | 3 each | PASS |
| `host_jobs` | The build attempts and key pushes | 7 `devbox_provision`, 9 `devbox_reconfigure`. The 6 failed builds keep their errors | PASS |
| `network_flows` on devbox addresses | None, by design | 0 on `10.128.0.0/19`, `10.7.0.0/16`, `10.6.0.0/16`, `10.2.0.32/27`. The table's 2 rows are IPv6 router solicitations from before Stage 5 | PASS |
| `network_events`: Zeek's heartbeat | Current | `stats` at 13:51, with 2 rotations passed and 0 rename errors, 8 archived logs | PASS, **G5 confirmed fixed** |
| Mirror filters on devbox ports | None, by design | 0 | PASS |

**Traces outside the database:** the apt cache logged `jq` 3 times for each team's address (`10.2.0.32`, `.33`, `.34`). devpi's journal has 18 lines naming `tabulate`. A file creation leaves no trace outside the VM, as expected.

## Stage 7: a user resets the devbox

**Prior state, 13:53:** as in Stage 6, and on all 9 VMs the file, `jq` and `tabulate`. Tokens, boot times and VM IDs saved for comparing. `arne` presses the button, though `anna` did the work, because it is the team's devbox.

| Time | Action | Expected | Actual | Result |
|---|---|---|---|---|
| 13:54 | `arne`: **Reset devbox**, then **Yes, rebuild my devbox** | Accepted, version 2, score 0/3, no log entries | 200, "Reset requested: your devbox VMs are being rebuilt and the score starts over." `VERSION 2`, 0/3, 0 entries, 0 JavaScript errors | PASS |
| 13:54 | Database | `alpha` at version 2, the others at 1 | 2, 1, 1 | PASS |
| 13:54-14:14 | Watched the job, `qm list` and the watchdog | Done. Only `alpha`'s 3 VMs replaced | `devbox_rebuild` done in 19.5 minutes, on the first try. VMs replaced one at a time. 0 watchdog stops | PASS |

![Reset confirmation](screenshots/13-reset-confirmation.png)
![Straight after pressing it](screenshots/13-reset-after.png)

## Stage 8: after the reset

**Prior state, 14:15:** the reset job has finished.

### Everything changed is gone, on the team that reset, and only there

| VM | VM ID | File | `jq` | `tabulate` | New token | New boot | Result |
|---|---|---|---|---|---|---|---|
| `devbox-t1-a` (`alpha`) | 122, same | yes → **no** | yes → **no** | yes → **no** | Yes | Yes | PASS |
| `devbox-t1-b` (`alpha`) | 120, same | yes → **no** | yes → **no** | yes → **no** | Yes | Yes | PASS |
| `devbox-t1-c` (`alpha`) | 124, same | yes → **no** | yes → **no** | yes → **no** | Yes | Yes | PASS |
| `devbox-t2-a`, `-b`, `-c` (`bravo`) | same | yes, kept | yes, kept | yes, kept | No | No | PASS |
| `devbox-t3-a`, `-b`, `-c` (`charlie`) | same | yes, kept | yes, kept | yes, kept | No | No | PASS |

| Time | Action | Expected | Actual | Result |
|---|---|---|---|---|
| 14:15 | `anna`, `arne`, `aylin`: SSH through the gateway, with the same keys | All sign in, keys not added again | All 3 reach `devbox-t1-a` | PASS |
| 14:15 | Firewall options on all 13 devbox VMs | All on, including the replaced VMs (F26) | All on | PASS |
| 14:15 | Firewall watchdog, since 12:00 | No VM stopped | 0 | PASS |
| 14:16 | `alpha`'s page | Version 2, 0/3, no entries, the SSH command | `VERSION 2`, 0/3, 0 entries, the command | PASS |
| 14:16 | `bravo`'s page | Unchanged | `VERSION 1`, 3/3, 3 entries | PASS |
| 14:16 | Database: `alpha`'s records | Kept at version 1 | 3 captures and 3 logs at version 1 | PASS |

![alpha after the reset](screenshots/14-after-reset-alpha.png)
![bravo, untouched](screenshots/15-after-reset-bravo-untouched.png)

### The new VMs work

| Time | Action, on all 3 of `alpha`'s new VMs | Expected | Actual | Result |
|---|---|---|---|---|
| 14:16-14:19 | The same as Stage 5 | All work again | File, `apt install jq`, `pip3 install tabulate`, 201 from scoring and logging over verified TLS, real internet closed | PASS |
| 14:19 | `alpha`'s page | 3/3 at version 2 | `VERSION 2`, 3/3, 3 entries, `run-alpha-v2` shown | PASS |
| 14:19 | Database | Both versions kept | 3 captures and 3 logs at each of versions 1 and 2 | PASS |

![alpha scored again after the reset](screenshots/16-scored-again-after-reset.png)

## Found while preparing

These come from reading the code and from one failed `pip3 install` on a devbox from the previous run.

**Correction to the previous protocol:** its pip test ran `pip3 install --break-system-packages`, and the protocol did not say so. It reported that pip reaches
devpi, which held only with that flag.

| # | Severity | Finding | State |
|---|---|---|---|
| G1 | High | A plain `pip install` on a devbox fails with `externally-managed-environment` before it contacts devpi, and `python3-venv` is not installed as a fallback | Fixed: `break-system-packages = true` in `/etc/pip.conf`. Confirmed in Stage 5 |
| G2 | High | devpi is empty, so no `pip install` can succeed | Fixed: seed list `devpi_seed_packages`, fetched while the internet is open. Confirmed in Stage 5 |
| G3 | Medium | Nothing in the apt seed list is missing from a devbox except `nftables`, so no sensible package can show that `apt install` works and that a reset removes it | Fixed: `jq` added to the seed list. Confirmed in Stages 5 and 8 |
| G4 | Not a finding | The devbox page shows only tokens found (`curl` to scoring) and log entries (`curl` to logging). You confirmed this is correct, so the test checks those two on the page and the rest in the database | Settled |

G2 and G3 are the smallest seeds that make the test meaningful. They do not decide what the private internet should hold, which stays
your decision (`docs/check-with-user.md`, 3c and 5).

## What the database records, as an administrator

You asked whether the other traces, such as network traffic, reach the database. I checked this on 2026-11-04 at about 10:50, read-only through
the CLI's `admin` connection. The data comes from the previous run, whose VMs were still up. Stage 6 repeats it on the clean system, with the same result.

| Action on a devbox | In the database? | Where |
|---|---|---|
| `curl` to scoring | **Yes** | `flag_capture_attempts` (23 rows, only a hash of the token) and `devbox_captures` (6 rows, with team and devbox version) |
| `curl` to logging | **Yes** | `logging_attempts` (8 rows) and `devbox_logs` (8 rows, with team and devbox version) |
| Adding an SSH key | **Yes** | `devbox_ssh_keys` (9 rows) |
| Building, reconfiguring, resetting | **Yes** | `host_jobs` (17 rows), and `devbox_version` on `teams` |
| SSH login | **No** | |
| Creating a file | **No** | |
| `apt` and `pip` | **No** | |
| Any other network traffic | **No** | `network_flows` has no devbox rows |

### Network traffic: not recorded, by design

`docs/requirements.md` (Monitor) says devbox bridges are not mirrored. The live system matches:

| Check | Expected | Actual | Result |
|---|---|---|---|
| Rows in `network_flows` touching `10.128.0.0/19`, `10.7.0.0/16`, `10.6.0.0/16` or `10.2.0.32/27` | 0 | 0 | PASS |
| Rows to apt-cache, devpi, scoring, logging or DNS | 0 | 0 | PASS |
| All of `network_flows` | | 1 row: an IPv6 router solicitation at 18:50 on 2026-11-03, before any devbox existed | NOTE |
| Mirror filters on the 44 devbox ports (`tc filter show ... ingress`) | 0 | 0 | PASS |
| `worms-mirror status` | No devbox port | Only the monitor's own port | PASS |

**Fresh traffic as a control (10:49):** from `devbox-t4-a`, 5 DNS lookups and HTTP requests to devpi, apt-cache and scoring. All three answered.

| Measure | Before | Sent | About 3 minutes later |
|---|---|---|---|
| Packets received by the monitor's capture NIC | 39 | 39 | 39 |
| Rows in `network_flows` | 1 | | 1 |
| Zeek rows in `network_events` | 59 | | 59 |

The capture NIC's counter is the conclusive one, because it does not depend on Zeek: the frames never reach the monitor. Its total of 39 packets also shows that none
of the previous run's traffic reached it.

### Finding G5 (high, open): Zeek stops recording at its first log rotation

This was not what I was looking for, and it has nothing to do with devboxes, but it does affect rounds.

| Check | Actual |
|---|---|
| `systemctl is-active zeek zeek-shipper` | Both `active` |
| Last `stats` heartbeat in `network_events` | 18:59 on 2026-11-03, none in the 16 hours since. It should arrive every few minutes |
| Zeek's journal at 19:00 (shown here in CET; it logs 18:00 UTC) | `conn/Log::WRITER_ASCII: failed to rename conn.log to /var/lib/zeek/archive/conn....log: Invalid cross-device link`, then `terminating thread`. The same for `stats`, `notice`, `capture_loss` and `packet_filter`, and for `reporter` an hour later |
| `/var/lib/zeek/archive` | Empty. No rotation has ever succeeded |
| `/proc/<zeek>/mountinfo` | `/var/lib/zeek/logs` and `/var/lib/zeek/archive` are separate mounts |

**Cause:** `ReadWritePaths={{ network_monitor_log_dir }} {{ network_monitor_archive_dir }}` in `zeek.service.j2`. systemd gives each path its own bind mount,
so a `rename()` between them fails even though both are on the same disk, and Zeek ends that log's writer.

**Effect:** Zeek keeps running, but `conn.log` is no longer written, so `network_flows` gets nothing after the first full hour since Zeek started. The shipper keeps
sending its own heartbeats showing `flows: 0`, and nothing alerts. **So a sandbox round's traffic would stop being recorded in the same way.** That follows from the
above; no round has run. `worms simulate` would catch it through its heartbeat check, but needs a sandbox deployed.

**Fixed in the repo at your request:** `ReadWritePaths` now names the shared parent directory, with a new test. I reproduced the failure and the fix under systemd's sandbox on the node: with two paths `rename()` fails with `Errno 18`, with the parent it works. **Confirmed live** by the 12:00 rotation after `make` (Stage 0).

### Traces outside the database

| Where | What it holds | Limit |
|---|---|---|
| apt-cache: `/var/log/apt-cacher-ng/apt-cacher.log` | Every package request with the client address. Devbox teams appear as their gateway's address: `10.2.0.32`, `.33`, `.34` for `alpha`, `bravo`, `charlie` | By team, not by user or VM |
| devpi: journal | Each request's path, such as `GET /root/pypi/+simple/six/` | The lines I read name no client |
| SSH gateway: journal | Each jump, with the key's fingerprint | Only on that VM, so a teardown loses it |
| Devbox VM: its own logs, such as `/var/log/apt/history.log` | The VM's own `apt` history and SSH logins | A reset removes them |
| Syslog VM: `/var/log/remote` | Infrastructure VMs only | **No devbox VM and no SSH gateway logs here** |

A created file leaves no trace outside the VM.
