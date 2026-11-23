# Migrating production: the unified CLI, a working apt cache, the first round

The procedure that brings production from its state on the night of 2026-11-19/20 to this
document's commit or later. It runs in two parts: **tonight, offline**, so the teams can work again, and
**tomorrow's egress window**, which refills the caches and gives the devboxes their packages.
Every step was rehearsed on worms-dev with the same code; [`first-round-preparation.md`](first-round-preparation.md)
has the rehearsal's details and timings.

Run everything **on the host, in your checkout** (`<checkout>`), unless a step says otherwise.
Each step says what you should see. If you don't see it, stop there.

## Where production stands

| What | Effect | Fixed by |
|---|---|---|
| **The apt cache has no package index.** Its `InRelease` files exist only under temporary names and none of the `Packages` lists does, so offline it answers `503 Unable to download in offline mode`. Its `clean` snapshot, if it has one, holds the same broken index. | Teams can't use apt. A fresh VM (a sandbox gateway at a round's first attempt, a rebuilt devbox) finds no package (`No package matching 'nftables'`), so the round's first attempt and every retry would fail. | Tonight: the offline repair, then a new snapshot (step 5). Tomorrow: the refill (window, step 1). |
| **Devboxes in mixed states.** Some were rolled back to a bare first-boot snapshot: Debian's sources, `root@localhost`, no CA, no `/etc/worms/flag`. | Teams can't work on them, and dev uploads that run on them fail. | Tonight: a reset of every team's devboxes (step 6). Tomorrow: a rebuild with packages (window, step 3). |
| **The round agent may be stuck on a job** (an apt task that never returns). | It runs one job at a time, so every dev upload, devbox reset and key push queues behind it. | Step 1. |
| The devbox gateways had no `nftables`. | Nothing. | Done by hand. |

## What changes

| Area | Change |
|---|---|
| **The CLI** | Git-style ([3](03-manage-py.md)): look with `status`, `show`, `log`, `check`, `trace`; change with `apply` and a noun's verbs (`worms vm reset alpha`, `worms jobs pause`). A team VM can be named as `<team>/a\|b\|c\|gw` (`alpha/gw`, `t2/a`). The commands before are gone: table below. |
| **`worms status`** | Starts with an *attention* block: internet window open, devboxes held or stopped, missing `clean` snapshots, pending migrations, failed jobs, host services down. A team column; *created* and *updated* columns from the per-VM history. |
| **`worms apply`** | Brings the platform up to the checkout: alone, what changed since what runs; or the components named. No internet window unless `--packages`. Then it syncs the gameplans and restarts the agent (once no job runs), then the conductor: no more manual restarts. Refused during a round. `--plan` shows what each line touches. |
| **`worms run`** | `worms run <vm>... [--root] -- <command>` runs a command on VMs without a login; `worms login <vm> -- <command>` too. Groups (`services`, `private-internet`, `infrastructure`) never include team VMs; a team VM's output is escaped. |
| **Logs** | Every changing command keeps its whole output in `~/.worms/runs/` (`worms log run`); `worms log latest -f` follows the newest host job. |
| **The apt cache** | The fill keeps the old index until the new one is complete, fetches every list a fresh VM needs, and checks offline that the cache serves a fresh VM. `scripts/apt-cache-offline-index.py` rebuilds the index offline from the apt-cache VM's own lists. `worms internet off` switches DNS and the caches back to offline mode. 13 more apt packages, 2 more PyPI packages (step 1 of the window). |
| **Safety** | `worms step devbox-configure`/`devbox-keys` by hand are refused (they erased every team's keys). A build that fails inside the window switches DNS and the caches offline. Tab completion offers only plain names. |
| **Per-VM history** | `/var/lib/worms/vm-history.jsonl` on the host: when each VM was created, configured, snapshotted or rolled back, by which commit and command. `worms show <vm>` lists it. |
| **Database** | Migration `0020` (the round's "shut down every devbox" switch), if production lacks it. |

| Old command | Now |
|---|---|
| worms build &lt;existing component&gt; | `worms apply <component>` |
| worms status keys, worms devbox-sync-keys, worms devbox-add-key, worms devbox-remove-key | `worms key`, `worms key sync`, `worms key add`, `worms key remove` |
| worms build devbox --rebuild --team X | `worms vm rebuild X` |
| `worms login X`, then a command | `worms run X -- <command>` |
| `systemctl restart worms-round-agent`, then the conductor on the orchestrator | `worms service restart agent conductor` (waits for the agent's running job) |
| `pg_dump` on the database VM | `worms db backup` |

## Tonight, offline

### 1. The code, and a stuck agent

Bring the checkout to this document's commit or later, the way you always push code there. The new CLI works at
once; the host's round agent runs the new code only after step 4.

```sh
git log --oneline -1           # this document's commit, or later
git status --short             # nothing modified: the host's workspace is synced from the working tree
worms jobs              # a job `running` for a long time, others `queued` behind it?
```

If one job is stuck, end it, so the queue moves again:

```sh
worms log <its number>                 # the last lines: where it hangs
systemctl restart worms-round-agent    # up to ~90 s: systemd ends the hung job
worms jobs                      # the queued jobs run again, one at a time
```

Use plain `systemctl` here. `worms service restart agent` waits for the running job to finish, up to 15
minutes, and a stuck job never does. The ended job is lost; rerun it later if it was a reset.

### 2. Pre-flight

```sh
pgrep -af 'manage.py|ansible-playbook'   # nothing: a second worms run can close the window under a build
worms status                             # read the attention block; internet access ● denied; no sandbox VM
```

### 3. Back up the database

```sh
worms db backup          # ● <size>: ~/.worms/backups/<host>-mydb-<time>.dump, readable by you only
```

### 4. Deploy the code (offline)

```sh
worms apply --plan dashboard orchestrator round-agent   # database comes along if migration 0020 is pending
worms apply dashboard orchestrator round-agent          # 12 min on dev
```

It ends with `● agent: restarted, active`, `● conductor: restarted, active` and
`● applied: …`. Name the components: `worms apply --all` builds and configures everything, the host's
host network included, and rolls every service VM back to its snapshot.

```sh
worms show gameplans         # round_one: valid on the orchestrator and the host
```

### 5. Repair the apt cache (offline), and snapshot it

```sh
worms run apt-cache --root -- python3 - < scripts/apt-cache-offline-index.py
```

It ends with `OK: the cache gives a fresh VM its whole index, offline.` It was tested on dev, on a
cache broken exactly like production's. The many `W: Tried to start delayed item … but failed`
lines are expected: apt tries the compressed lists, which the cache lacks, before the uncompressed
ones. If it fails, it prints the command that puts the previous index back.

Then the snapshot. A round's first attempt rolls the service VMs back to `clean`; without a new
snapshot it would roll the cache back to the broken index.

```sh
worms show apt-cache           # its vmid, and under "snapshots" whether it has `clean`
```

If it has `clean`, retake it:

```sh
qm shutdown <vmid> --timeout 120 && qm delsnapshot <vmid> clean && qm snapshot <vmid> clean --description "worms: offline index $(date +%F)" && qm start <vmid>
```

Check from a team gateway that apt works again:

```sh
worms run <team>/gw --root -- apt-get update     # Get:/Hit: lines from apt-cache.worms.internal, no Err:
```

### 6. Reset every team's devboxes (offline)

```sh
worms vm reset devboxes     # about 1–3 min per team, one after another
worms key                         # every cell ✓
```

Each team's VMs go back to their snapshot. One that never got its one-time setup gets it now,
with no network: hostname, CA, apt and pip pointed at the private internet, time sync. Then the
keys, the CA and `/etc/worms/flag`. The teams lose what's on their devboxes. Packages (`rsync`,
`pip`) come with the rebuild in tomorrow's window.

Then check every team's devbox as the team uses it:

```sh
worms check          # or: worms check <team>...
```

For each team, A, B and C, it checks:
- **login:** through the SSH gateway, with the team's registered keys;
- **flag:** `/etc/worms/flag` is there;
- **scoring**, **logging:** both answer;
- **apt**, **PyPI:** a fresh index and a package through the caches.

Every cell should be ✓. Tonight, `PyPI` may say `no pip` on a VM whose packages wait for
tomorrow's rebuild; the line under the table says why. The command changes nothing on the VMs.

**The SSH gateway's line** may be yellow, or red. Every team reaches the gateway from one address:
the host masquerades them. OpenSSH's per-source penalties are on by default, so a few failed
logins by anyone shut all teams out for up to 10 minutes. Red means that happened in the last
24 h, which may be the teams' failed SSH logins last night. Whether to turn the penalties off
there (`PerSourcePenalties no` in its sshd) is still to be decided.

## Tomorrow: the egress window

### 1. Refill the caches

The lists now hold 13 more apt packages and 2 more PyPI packages, every name checked against
trixie's index:
- **The Python libraries for `round_one`'s files and logins, as Debian packages:**
  `python3-pykeepass`, `python3-pyotp`, `python3-docx`, `python3-odf`, `python3-pexpect`,
  `python3-py7zr`, `python3-pyzipper`, `python3-asyncssh`. A worm on a sandbox VM can then
  `apt install` them; pip there needs an index URL and `--break-system-packages`.
- **Compiled worms:** `golang-go`, `musl-tools`, `upx-ucl`. Go and static C binaries need nothing on
  the target.
- **KeePass on the shell:** `keepassxc-minimal`, `kpcli`.
- **On PyPI:** `pex`, `shiv`, for a Python worm and its dependencies as one runnable file.

Give the host egress, then:

```sh
worms apply --yes apt-cache devpi --packages
```

Take egress away again afterwards. The build opens and closes the worms window itself. apt-cache
took 7 min on dev; devpi takes longer (its list holds large packages).

**What you should see:**
- `Re-prime the indexes through the cache, every list a fresh VM fetches (online)`: `changed`
- `Check every suite's index arrived under its own name`: `ok`
- after the window, `Ask the cache for a fresh VM's package index, offline`: `ok`
- the service VMs snapshotted again: the round rolls back to the refilled cache

**If it fails:** `Say the indexes were not refreshed` means the egress wasn't there; the previous
index is back. `Say the cache cannot serve a fresh VM` (here, or on `worms internet off`,
`worms step offline`) means the cache has no index to give offline: repair it as in tonight's
step 5, retake its snapshot, then run `worms step offline` so it checks again. Whatever failed, run `worms internet off` before you stop: it closes the window
and switches DNS and the caches back to offline mode.

### 2. Check from a team gateway

```sh
worms run apt-cache -- sh -c 'grep -c . /etc/apt-cache-package-list; cat /etc/seed-version'
worms run <team>/gw --root -- apt-get update --error-on=any
```

The first shows `156` and this checkout's commit: the fill writes them only once every package
and its dependencies were fetched. The second shows no `Err:`. (`apt-cache policy` alone proves
nothing: it names a candidate for every package in trixie's index, cached or not.)

### 3. Rebuild the devboxes with their packages

```sh
worms vm rebuild devboxes   # 12–14 min per team, one after another
worms key
worms check                           # every cell ✓, apt and PyPI too
```

A team's A, B and C are replaced (fresh disks), configured with their packages from the refilled
cache and snapshotted. Later resets are a rollback of seconds. The gateways stay. The teams lose
what's on their devboxes, so warn them first.

## The round

On the dashboard, **Staff → Rounds**:

- **Gameplan:** `round_one` (valid).
- **Minutes per team: at least 24.** `round_one` gives the worm 10 min on A, waits 10 more, and
  churns until 5 min before the end; it's written for 60. Below 24 the round shows `invalid` and
  waits.
- **Start and end:** a team's slot is its minutes + 17¼ min. Add **about an hour for the first
  attempt's sandbox build**: 67 min on dev, for 21 VMs. With 4 teams × 60 min that's about 6½ h,
  plus room for retries.
- **"Shut down every devbox while its attempts run":** on, for the host's memory. It can only be
  set before the round starts.
- **Simulate** the round: `ok`.
- Each team runs its latest **Live** upload from before the start; a team without one is skipped.

**Following it:**

```sh
worms status                 # attention: devboxes held by the round, any failed job
worms round
worms log latest -f          # the newest host job: the first prepare builds the sandbox (about 1 h)
worms log conductor -f
worms vm kill         # hard-stop every VM, destroy nothing: the only hard stop
```

## Before the gameplan is assigned (2026-11-23)

Production runs `f11b1a5` (2026-11-20). Everything since then comes with this update: `Site.yml`
with role stamps, the round's preflight, check and watch (migrations `0021`-`0023`), the sandbox
gateway's memory fix. Checked against the code by hand and by three independent reviews; the
round itself was rehearsed on worms-dev (round 13). Plan about 1 h before the round's start.

**0. Before the update** (production's CLI is still `f11b1a5`: no `jobs` or `round` yet):

```sh
git log --oneline -1                     # f11b1a5: note it, the way back
pgrep -af 'manage.py|ansible-playbook'   # nothing
# f11b1a5's CLI (indented here: the docs' test checks today's CLI):
 worms status jobs                       # nothing running or queued
 worms status rounds                     # the round: no gameplan yet; Start (UTC, compare `date -u`) at least ~1 h ahead
```

The round's window: end − start ≥ the first sandbox build (~1 h) + teams × (minutes per team + 17¼ min)
+ room for retries. Move start or end on Staff → Rounds now if needed (the start is the teams' upload
deadline: tell them). Tell the teams that their devboxes are off and the dashboard restarts for ~40 min.

Only if a sandbox is left over (`worms status`: a sandbox VM), with the wrapper's old name:
`/usr/local/sbin/worms-node-run opentofu/stacks/sandbox 'tofu destroy -auto-approve -parallelism=1'`.

**1. The code** (no egress: a bundle, made on worms-dev with `git bundle create /root/worms-update.bundle main`,
copied to production outside the checkout):

```sh
git bundle verify /root/worms-update.bundle
git pull --ff-only /root/worms-update.bundle main
git log --oneline -1                     # the bundle's head (c8457d4 or later)
git status --short                       # nothing
worms status                             # new CLI: attention 'migrations: the database lacks 3'; internet ● denied; no sandbox VM
worms jobs                               # nothing running or queued
worms db backup                          # ● ... ~/.worms/backups/worms-mydb-<time>.dump
```

**2. Deploy (offline).** apt-cache first, by name: its seed list grew since `f11b1a5`, and `worms apply`
without names refuses a component whose package names changed (they need the internet window). By
name it is configured offline; the new seed packages (devbox tooling, not needed by `round_one`) wait
for the next egress window (`worms apply apt-cache --packages`).

```sh
worms apply --plan apt-cache             # apt-cache: configure again; internet window: closed
worms apply apt-cache                    # rolled back to clean, configured, snapshotted again
worms apply --plan                       # no packages error now; 'internet window: closed'; OpenTofu: in place only,
                                         # 0 replaced, 0 destroyed; every VM configured again (Site.yml is new here)
worms apply                              # y; a dump first; devboxes off while the service VMs are rolled back,
                                         # configured (every role: no stamps yet) and snapshotted again;
                                         # migrations 0021-0023 and the grants; ~25-40 min. Ends with
                                         # '● agent: restarted, active', '● conductor: restarted, active', '● applied: ...'
```

If the agent was not restarted (a host job ran over 900 s): once `worms jobs` shows nothing running,
`worms service restart agent conductor`. If `worms apply` fails: `worms log run 1 <vm>`, fix, run the same
command again (finished roles are skipped). Do not assign the gameplan to a half-deployed platform.

**3. Verify:**

```sh
worms apply --plan                       # nothing to apply: the platform runs this checkout
worms status                             # no 'migrations', 'host services', 'internet window', 'devbox VMs' rows;
                                         # 'snapshots' is a warning (the services are then not reset at the round)
worms jobs                               # devbox_reconfigure done; nothing running or queued
worms round                              # the round, no gameplan, not ON HOLD
worms show table rounds --limit 3        # devboxes_off true, devbox_hold empty
worms show gameplans                     # round_one.py valid on the host and the orchestrator: 3858cca4c905
cat /opt/worms/.worms-commit             # the bundle's head (the agent's code, the framework, the sandbox stack)
grep -n 'floating = 512' /opt/worms/opentofu/stacks/sandbox/main.tf   # the sandbox gateway's memory fix
systemctl show -p ExecStart worms-round-agent | grep -c worms-host-run   # 1 (the wrapper's new name)
worms run apt-cache --root -- "find /var/cache/apt-cacher-ng -name 'inetutils-telnetd_*.deb' -o -name 'inetutils-inetd_*.deb' -o -name 'nftables_*.deb'"
                                         # three .debs (round_one's telnet hosts, the sandbox gateway)
worms run dns --root -- 'date; ls -l /var/log/audit/audit.log; df -h /'   # audit.log within a minute of date; / not full
free -m; awk '/^(size|c_min) / {print $1, int($3 / 1048576), "MB"}' /proc/spl/kstat/zfs/arcstats
                                         # available + (size - c_min) + ~0.5 GB per devbox VM >= 12 616 MB
pvesm status --storage local-zfs-sandbox # Available (KiB) >= 110100480 (105 GiB)
worms check                              # every team's A, B, C: every cell ✓ (minutes; it changes nothing)
```

**4. The dashboard, in this order:** Staff → Notifications: "when the round begins, every devbox is shut
down until it is over". Staff → Simulations: `round_one.py` at the round's minutes per team → ok. Then
Staff → Rounds → the round, one save: gameplan `round_one.py · 3858cca4c905`, minutes per team ≥ 24,
"Shut down every devbox" left on. Then `worms log conductor -f`: at the start, "round … begins", the
devboxes shut down, the preflight, the prepare (the first build: about 1 h).

**From then until the round is over:** no `worms apply` of any kind, no `worms service restart`, no
`worms vm reset` or `worms vm rebuild`, no `worms jobs pause`, no `worms round start` (it moves the upload
deadline), no `worms internet on`. If a round goes ON HOLD, `worms log conductor` says why;
`worms round resume <r>` once fixed.

**The way back** (last resort, before the round): `git checkout f11b1a5`, then that CLI's
deploy command, `deploy dashboard orchestrator round-agent` (the agent, then the conductor restart); migrations
`0021`-`0023` stay (their defaults suit the old code); `worms db restore` only if a migration damaged data.
The old sandbox stack brings the gateway's balloon back (worms-dev, round 13).

## Rollback

- **The code:** check out `9d1382a` (the last commit before the CLI merge), then
  `worms apply --yes dashboard round-agent` and restart the agent, then the conductor. The migrations
  stay; nothing of the old code depends on their absence.
- **The database:** step 3's dump, restored on the database VM with `pg_restore`. It loses
  everything since the dump.
- **The apt cache:** the offline repair keeps the previous index as `dists.before-offline-index`
  in each backend directory of `/var/cache/apt-cacher-ng`.

## Don'ts

- **No deploy during a round.** It restarts the conductor, and that costs the running team its
  attempt. `worms apply` refuses unless `--during-round`.
- **No `worms apply --all` tonight.** It reconfigures the host network and rolls every service VM
  back. Name the components.
- **No `worms destroy`.** It takes the database VM and its records too.
- **No `worms apply apt-cache --packages` without egress.** It puts the old index back and achieves
  nothing.
- **If dpkg asks about `/etc/nftables.conf` on a gateway, answer `N`.** The gateway's rules are in
  that file.
