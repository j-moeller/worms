# Status: what works, what is missing, what needs deciding

As of 2026-11-06, on `worms-dev`. This replaces `requirements.md`, `check-with-user.md`,
`todos/dev-rounds.md`, `TODO` and the status part of the progress log (now in `docs/archive/`).

**The goal for 2026-11-10, the first product:** users sign in, reach their devboxes, develop
and upload their worms, and get a feeling for the environment. Rounds are not part of it.

## 1. The first product, step by step

| Step | State | Notes |
|---|---|---|
| Staff create teams and students (CSV), hand out one-time passwords | **Works live** | Passwords are shown once on the page; nothing emails them |
| A student signs in and sets a password | **Works live** | Sign-in is rate-limited per address; no per-account lockout |
| Adds an SSH key on the profile | **Works live** | Installed by a job in about 3 minutes |
| Reaches the devbox: `ssh -A -J team@<name>:2222 root@<A>`, then B and C | **Works from the node.** **Not tried from outside** | Port 2222 and the dashboard's name must be tried from a laptop |
| Works on the VMs: files, `apt`, `pip`, scoring, logging, the LLM endpoint | **Works live** | `apt` only has the ~150 cached packages plus the seed; `pip` a minimal seed |
| Uploads a worm in the Dev view: the devbox resets and `main.sh` runs on A | **Worked live once** (8 min 36 s) | Uploads from several teams queue: one job at a time |
| Sees the dev score, logs and the replay | **Works live** | The replay has no network traffic: devbox traffic is not recorded |
| Resets the devbox | **Worked live, by replacing the VMs** (8–20 min) | The faster rollback is **not deployed and never ran** |
| Reads the rules: what to upload, what is reachable | **Only in this repository** | `docs/worm-contract.md`; the dashboard shows only the `curl` commands for scoring and logging |

## 2. Before users arrive

In order. Items 1–4 are blocking.

1. **Decide where it runs.** `worms-dev` works today. `worms-pve` needs a DNS record for
   `worms.mlsec.tu-berlin.de`, a rename of the node (the OpenTofu state and VMs are tied to the
   name; `make destroy_all` before and `make` after, never tried), a first build from nothing
   (never tried either), and hardware checks: AVX2 (team VMs do not start without it), nesting
   `N`, `SMT vulnerable`, enough RAM for the number of teams (§5).
2. **Deploy the current code and test the snapshots** (they have never run). `worms build`
   (stops the devboxes for its length), `worms build rebuild-services` (20–60 min, the service
   VMs have run without a snapshot), `time worms build round-reset-services`, `worms simulate`,
   then reset one devbox twice: the first reset replaces and snapshots its VMs, the second
   should roll them back. Check `/etc/sandbox-api/devbox-teams` on scoring and logging afterwards.
3. **Fix the devbox security items marked "before users" in §4.**
4. **From a laptop:** `curl -vk https://worms-dev.mlsec.tu-berlin.de/` (expect 200) and
   `ssh -p 2222 team@worms-dev.mlsec.tu-berlin.de` (expect `Permission denied (publickey)`).
   A timeout means the network in front of the node blocks the port.
5. **Clean up the test data:** the 9 test accounts, teams `alpha`, `bravo`, `charlie`, their
   devboxes and `WORMS 2026 · dev` (`docs/management.md`, "Clean the database"). Then set the
   admin password: `worms admin reset-password` (nobody knows the current one).
6. **The header says `ROUND IN PROGRESS (DEV)`** with no round, for every team: a competition
   without registration dates counts as a dev round (`dashboard/middleware.py`,
   `get_competition_state`). Setting a registration window on `/staff/` hides it; the code
   should not need that.
7. **Give teams the rules:** hand out `docs/worm-contract.md`, or put it on a dashboard page.
8. **Back up the database.** Nothing does today. A nightly `pg_dump` to the node is the minimum.
9. **Retire the old Proxmox token** `terraform-prov@pve` (it still has `Administrator`) and
   delete the old credential copies: `find opentofu -name 'credentials.auto.tfvars.disabled'
   -not -path '*/.terraform/*' -delete; rm .env`.

## 3. Decisions

| # | Question | Options, and a recommendation |
|---|---|---|
| D-1 | Where does the first product run? | `worms-dev` now and move later (works today), or `worms-pve` now (untested rename and fresh build). **Recommended: `worms-dev`** |
| D-2 | **How does a worm get from one VM to the next?** The core of the game is not designed | Sandbox VMs run only SSH with the operator's key, so a worm can capture only its start VM. On a devbox, B and C accept the team's own keys, so a worm carrying a registered private key spreads there, but that path does not exist in a round. Seeded vulnerable services, weak credentials, or a planted key per round are the candidates. Until decided, teams develop against a devbox that does not behave like a round |
| D-3 | Remove dev rounds? | The dev view no longer uses them; staff pages, API and middleware still carry them. **Recommended: remove**, together with fixing the header (§2.6) |
| D-4 | May a manual devbox reset delay a round? | Leave it (up to 8 min per round job), wait for the whole round (82 min per team), or wait only near timed jobs. Dev uploads already wait |
| D-5 | Profile lists the whole team's SSH keys, or only your own? | Whole team (a teammate can revoke a lost key; today) or own only |
| D-6 | What the apt cache and devpi hold | A longer seed list, a large Debian mirror, or keep it minimal and tell teams to vendor |
| D-7 | How the private-internet caches get filled | `worms build` fills them (slow builds) or a separate command (the documented one cannot run: `helpers/offline-mirror/README.md`) |
| D-8 | Man pages (503 under the systemd hardening) | Add `CAP_DAC_OVERRIDE`+`CAP_CHOWN` (tested, broad) or serve them without CGI |
| D-9 | Public certificate for the dashboard | Through TU Berlin (renewed by hand), ACME (awkward with the internet closed), or keep the internal CA (every browser warns) |
| D-10 | Record devbox traffic? | Default no. `host_mirror_extra_bridges` exists, untested for devboxes, and would double gateway records |
| D-11 | auditd on the SSH gateway | **Recommended yes**: every login recorded centrally, kept through a teardown. On devbox VMs it could only log locally |
| D-12 | Teams and students from the console | A small `worms` command; today only the dashboard can |
| D-13 | Check SSH host keys when deploying | Off since 2026-10-14. Recommended on before going live, with each build recording new keys |
| D-14 | Nesting off on the node | Written, active after a node restart or reloading `kvm_intel` with all VMs stopped |
| D-15 | Hyperthreading off | Closes L1TF/MDS between threads; 8 threads become 4 cores on this node. Check `worms-pve` first |
| D-16 | KSM off | It is on under memory pressure (2.2 GB shared on 2026-11-06), a cross-VM side channel. Off costs that memory |
| D-17 | `cve-search` to `x86-64-v3` | It still has `cpu: host` and `/dev/kvm`, and team VMs reach its port 5000 |
| D-18 | Trim the disk monthly | `zfs set org.debian:periodic-trim=enable rpool`. **Recommended yes**: the one-off TRIM cut the slowest writes from 1.5 s to 11 ms |
| D-19 | The public repository | `github` (`j-moeller/worms`) is public, findings included. Move some to `https` only? |
| D-20 | Upload limits | 100 MiB per zip, no per-team quota or rate, on a dashboard disk with 2.4 GB free |

## 4. Security: a rogue program on a devbox

Checked live on 2026-11-06, claim by claim (`docs/security-assessment.md`). **What holds:** the
per-VM firewall on every devbox VM and gateway, the node unreachable from them, no route to the
sandbox or between gateways, no internet except `teachllm:443`, DNS without recursion, the caches
offline, the SSH gateway's confinement, no `/dev/kvm`, the CPU/RAM/NIC/disk caps, no secrets on
the VMs. **What the docs claimed and is not so:**

| # | Claim | Reality | Before users? |
|---|---|---|---|
| S4 | vmbr6 and vmbr7 are port-isolated (README, `devbox.md`) | **Not implemented**: all 22 ports `isolated off`, no hookscript. Every team's VMs share layer 2 on both bridges; only the per-VM firewall separates them | **Yes.** `bridge link set dev <tap> isolated on` for team VMs' ports, from the node's tooling (the hookscript needs `root@pam`) |
| S18 | Conntrack capacity and timeouts are tuned | **Ineffective**: 262 144 entries, 5 days for established TCP. One team can fill the node's table from inside its own devbox (500 TCP connections and 5000 UDP packets a second per address are allowed) and every new connection on the node fails, the dashboard included. Not tested | **Yes.** Per-source connection-count limits for team addresses |
| S19 | `ipfilter`/`macfilter` pin every frame | ICMP error packets pass before any per-VM rule, with any source address and MAC: a channel between teams, and MAC poisoning that can cut every team off the SSH gateway. From the ruleset, not tested | **Yes**, with S4: a bridge rule that drops frames whose source MAC is not the port's own |
| S9 | KSM is off (`resources.md`) | `ksmtuned` is active; 2.2 GB merged across VMs: a side channel between guests | Decide (D-16) |
| S20 | Team VMs have separate storage | Same ZFS pool as the database's disk, no reservation; bounded per VM by thick disks | Before many rounds |
| — | The watchdog covers team VMs | Not the SSH gateway, and it does not check the firewall's policy | Soon |
| — | Resets roll back to a clean snapshot | Not deployed, never run (§2.2) | Yes, or keep resets as replacements |

**Also open:** S17 (a new team VM runs without its firewall while being built, up to 28
minutes), nesting on with `cve-search` on `cpu: host` (D-14, D-17), hyperthreading (D-15), the
SSH gateway's shared login slots (S21), IPv6 on the node's untrusted bridges (S23), and the LLM
endpoint as a channel out: whatever `teachllm` does with requests is part of the containment.

## 5. Capacity

- **RAM:** 32 GB here; 24 GB in use with 3 teams (13 devbox VMs). A team costs about 2 GB, a
  default round about 4 GB, so this node has room for about 3 more teams (`docs/resources.md`).
  The platform caps at 32 teams. Nothing checks free RAM before a build.
- **Time:** a devbox build took 17 minutes for 3 teams; how it scales with more teams is
  unmeasured. The disk timed out (`zfs create`, F5) in 4 of 7 builds before the TRIM and in none
  of the 2 since.
- **One job at a time:** the round agent runs every build, key push, reset and dev upload in
  sequence. With many teams uploading, each waits for all before it.
- **Builds stop every devbox** for 20 minutes or more; plan deploys outside working hours, or
  use the Django-only redeploy (`docs/management.md`).

## 6. Missing features

**Game and rounds**
- A way between sandbox VMs (D-2): seeded vulnerabilities, a richer topology (L2/L3, sites).
- A round on a real node: no sandbox has ever been built, rolled back or torn down.
- Pause or abort a round; a command to cancel a host job; a second agent worker.
- A GPU or inference service for jailbreaking scenarios (planned in the old `TODO`).

**Dashboard**
- A rules page for teams (§2.7); a design pass.
- A failed devbox reset looks like a success: the score is reset, the VMs are not, and the page
  keeps saying "being rebuilt" (F25).
- A user created for a full or unknown team gets an account without a team, and there is no
  delete (F1).
- A page for the password-reset log; email for one-time passwords.
- Account security: per-account sign-in lockout (D3), consent to join a team (D4), the current
  password on a voluntary change (D5), a Content-Security-Policy (D8).

**Operations**
- Database backups (§2.8) and retention for `network_flows`.
- Health monitoring of the services (the "heartbeat" in the old `TODO`).
- An interrupted build leaves VMs outside OpenTofu's state or an `apt` lock (F2).
- `Update all packages` prints nothing for about 16 minutes; `no_log` hides migration errors.
- The conntrack settings the role writes do not take effect (Proxmox resets them).
- Nothing checks that infrastructure VMs have their firewall on (F26); the watchdog covers only
  sandbox and devbox VMs.

**Private internet** (`docs/archive/pi-failures.md`)
- devpi holds a minimal seed, the registry is empty, cve-search has no data, Wikipedia is not
  running (its 20 GB disk holds only the mini dump), man pages answer 503 (D-6 to D-8).
- NTP has no upstream (UDP 123 is never allowed), so clients get an uncorrected clock.
- The private-internet VMs never get package upgrades (60 behind).
- The apt-cacher-ng report page and the cve-search UI need no login (S13).

## 7. Not yet proven live

- Any snapshot or rollback; the agent's devbox-map restore.
- A sandbox: `bash main.sh` in a round, the LLM endpoint, no `/dev/kvm` and the 512 MB floor
  there, the sandbox gateway's fixes G7/G8, `worms simulate`'s traffic part.
- The power token starting and stopping a sandbox VM.
- The live suite from start to finish (stopped at step 05 on request).
- The team API's rate limits under real use; a reset of two teams at once.
- `Permission denied (publickey)` from A to B, seen once on alpha and not resolved; it worked on
  charlie with the key in the agent.

## 8. Tests

The offline suite: 522 passed, 458 skipped (no PostgreSQL on this node), 3 known failures
(`docs/testing.md`). Temporary PostgreSQL servers from interrupted earlier runs still run under
`/tmp/worms-pg-*` (7 directories on 2026-11-06), about 25 MB each; remove them when no tests run:

```sh
ps -eo pid,ppid,comm,args | awk '$2==1 && $3=="postgres" && /\/tmp\/worms-pg-/ {print $1}' | xargs -r kill; rm -rf /tmp/worms-pg-*
```

A long run on 2026-11-05 also had setup errors in `test_uploads.py`, `test_round_agent.py`,
`test_team_registration.py` and `test_devbox.py` that passed when run alone; their cause was not
kept. Keep the full output of the next run.

**On hold, as asked:** parallelizing Ansible, and a full rebuild (`make destroy_all`, then
`make`), which is still the only test of the first-run paths on brand-new VMs.

## 9. Local settings, not committed

`opentofu/shared/shared-variables.tf` (node name `worms-dev`), `opentofu/stacks/private_internet/main.tf`
(`cve_search` RAM 1024 MB, untested with its data load) and `docs/IMPORTANT` differ from the
repository on purpose. Both settings reached `main` once (`52fc7a1`) and were reverted
(`e6e5b33`); removing them from history would need a force-push.
