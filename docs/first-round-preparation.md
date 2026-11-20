# Preparing production for the first round

The commands that bring production from today's state (2026-11-19) to a platform that can run the
first scored round on 2026-11-20. Run them in order, the evening before. Each step says what you
should see. If you don't see it, stop at that step: everything before it can stay as it is.

Only step 4 needs egress. Every other step runs with the node offline.

**Superseded for the procedure** by [`migrate-on-production.md`](migrate-on-production.md), which uses
the new commands and covers tonight's offline repair. This page keeps the dev rehearsal's results.

**Rehearsal on worms-dev, 2026-11-19 evening,** with the same code. Dev's cache was broken the
same way as production's.
- Step 4: passed (22:31–22:38). The fill re-fetched the whole index, and with the window closed
  the cache served a fresh VM's whole index.
- Step 3: passed (22:40–22:48), offline. The plan named `core.0020_round_devboxes_off`; afterwards
  every gameplan was valid on both sides and `tofu-init` initialised the sandbox stack from the mirror.
- Step 5: passed (22:52–23:29, three teams). The fresh devbox VMs refreshed apt from the
  repaired offline cache and installed their packages; every team's keys ✓; on a devbox its own
  name, DNS, scoring over HTTPS and `rsync` installed.
- Step 6 and a test round with `round_one` (devboxes off): in progress. This file is updated as each passes.

## What is broken, and what fixes it

| What | What it would do to the round | Fixed by |
|---|---|---|
| **The apt cache has no package index.** It holds each suite's `InRelease` only under a temporary name (`InRelease.1795048367`) and none of the `Packages` lists. Offline it answers `503 Unable to download in offline mode`. | A round's first attempt builds a fresh sandbox gateway, which installs `nftables` from the cache. It would find no package (`No package matching 'nftables'`), and that attempt and every retry would fail. | Step 4: one fill with egress, by the corrected fill (below). |
| **Devboxes in mixed states.** Some were rolled back to a bare first-boot snapshot: Debian's own sources, `root@localhost`, no internal CA. | Nothing; teams just can't test their uploads. | Step 5: every team's devbox rebuilt. |
| **Code and database maybe behind.** Migration `0020` (the round's "shut down every devbox" switch) and the node's offline `tofu init` (`scripts/tofu-init`). | Without `0020` the round form and `worms status` fail. Without the offline init the sandbox can't be built. | Step 3. |
| The devbox gateways had no `nftables`. | Nothing. | Done by hand on 2026-11-19. |

**The corrected fill** (`ansible/roles/private_internet/apt_cache/tasks/seed.yml`). Two bugs left
the cache without its index:
- It deleted the index before fetching a new one. When the fetch didn't complete, nothing was left.
- It re-fetched with the apt-cache VM's own `apt update`. That VM already had current lists, so apt
  fetched only `InRelease` and never the `Packages` lists that a fresh VM needs.

Now it moves the old index aside and fetches into an empty lists directory, so every list a fresh
VM asks for goes through the cache. It fails on any list that doesn't arrive, and then puts the
old index back. After the window closes, a check asks the cache, offline, for a fresh VM's whole
index (`apt_cache/tasks/check.yml`). That is the same request tomorrow's sandbox gateway makes.

## Where to run what

- **On the node, in your production checkout:** every `worms` and `git` line. `<checkout>` below.
- **Inside a VM:** indented lines after `worms login <vm>`. Leave with `exit`.

## 0. Pre-flight

```sh
cd <checkout>
pgrep -af 'manage.py|ansible-playbook'   # nothing: a second worms run can close the window under a build
worms show jobs                        # nothing running or queued
worms status                             # internet access ● denied, no sandbox VM in the list
```

`worms status` may stop with `column rounds.devboxes_off does not exist`. That's the missing
migration; step 3 adds it. Its VM list comes first, so read that part.

## 1. Back up the database

Nothing else does.

```sh
worms login database
  sudo -u postgres pg_dump -Fc -f /var/lib/postgresql/mydb-before-first-round.dump mydb
  ls -lh /var/lib/postgresql/mydb-before-first-round.dump
  exit
```

## 2. The code

Bring the checkout to this document's commit or later, the way you always push code there.

```sh
git log --oneline -1      # this document's commit, or later
git status --short        # nothing modified: the node's workspace is synced from the working tree
```

## 3. Deploy the code and the migration (offline)

```sh
worms apply --plan dashboard round-agent   # read it: database and orchestrator come along if migrations are pending
worms apply --yes dashboard round-agent   # 8 min on dev
worms show jobs                          # nothing running, then:
systemctl restart worms-round-agent
worms login orchestrator
  sudo systemctl restart orchestrator-conduct-rounds
  exit
```

Restart the agent before the conductor: the conductor checks the gameplans when it starts, and
the agent must have the new code by then. The agent restarts itself between jobs on new code
too, so the manual restart is only a backstop.

**Check:**

```sh
worms status              # no traceback; the competition section shows
worms show gameplans    # round_one: valid on the orchestrator and the node
cd /opt/worms && sh scripts/tofu-init opentofu/stacks/sandbox; cd -
                          # "initialised from /var/lib/worms/tofu-providers (no internet needed)"
```

The last line is the sandbox build's first step, done offline in advance. If it says "from the
registry (needs the internet)" instead, run it once more during step 4's window: then the node
keeps that provider for good.

## 4. Repair the apt cache (the one step with egress)

1. Give the node egress, as for your last fill. The build opens and closes the worms window itself.
2. Run:
   ```sh
   worms apply --yes apt-cache --packages
   ```
3. Take egress away again.

The build shuts down the devboxes, rolls the service VMs back to their snapshot, opens the window,
fills the cache, closes the window, switches the caches offline, checks the cache and snapshots the
service VMs. The round's first attempt rolls the apt cache back to that snapshot, so the repaired
index must be in it. On dev the build took 7 minutes.

**What you should see:**
- `TASK [apt_cache : Re-prime the indexes through the cache, every list a fresh VM fetches (online)]`: `changed`
- `TASK [apt_cache : Check every suite's index arrived under its own name]`: `ok`
- after the window closes, `TASK [apt_cache : Ask the cache for a fresh VM's package index, offline]`:
  `ok`, and no `Say the cache cannot serve a fresh VM`
- the build ends without an error

It may end with `warning: no snapshot of …: up before this build without one`. That's harmless for
the round: without a `clean` snapshot on every service VM, the round's first attempt leaves them
all as they are (its job log says `not reset: no clean snapshot`), and the cache stays repaired.

**If it fails:**
- `Say the indexes were not refreshed`: the node had no egress, or lost it. The old index is back
  in place, as it was before. Give egress and run step 4 again.
- `Say the cache cannot serve a fresh VM`: the fill ran but the offline cache still lacks lists.
  The message names them. Send it to me; don't start the round.
- `!!! INTERNET ACCESS IS STILL ALLOWED`: run `worms close window`.
- **Whatever failed, and you stop there:** run `worms close window`. A build that fails inside
  the window closes it and switches DNS and the caches back to offline mode itself (online, they
  answer every request they can't fetch with an error). `disable` does both again and checks the
  cache; it is needed for sure when the build says `!!! DNS AND THE CACHES MAY STILL BE IN ONLINE
  MODE`.

**Check by hand.** Do this even if the build passed: it's the request the round will make.

```sh
worms login devbox-t1-gw
  sudo apt-get update --error-on=any; echo "exit $?"   # Get:/Hit: lines from apt-cache.worms.internal, "exit 0"
  apt-cache policy nftables | head -3                  # Candidate: 1.1.3-1
  exit
```

Before the fix this gateway got `503` for the same request.

## 5. Rebuild the devboxes (offline)

Each team's three devbox VMs are replaced (fresh disks), configured with packages from the
repaired cache, and snapshotted as configured. Teams lose what's on their devboxes. Later resets
are then a rollback of seconds. The gateways stay, with the `nftables` you installed.

```sh
worms apply --yes devbox --rebuild
worms show jobs         # one devbox_rebuild per team; wait until all are done (12–14 min each on dev, one team after another)
worms logs <number>        # the log of one that failed
worms show keys         # every cell ✓
```

**Check on one team's devbox:**

```sh
worms login devbox-t1-a                     # the prompt says devbox-t1-a, not localhost
  getent hosts scoring.worms.internal       # an address
  curl -sS -o /dev/null -w '%{http_code}\n' https://scoring.worms.internal/   # 404 (the API's answer to /), no TLS error
  apt-cache policy rsync | head -2          # Installed: a version
  exit
```

## 6. Check the whole platform

```sh
worms status              # everything running; internet access ● denied
worms test --live         # every check PASS (without a sandbox: the services and containment checks)
```

`worms test --live` creates a team `simulation-<id>` and deletes it with everything it
submitted at the end.

## 7. Create the round

On the dashboard, **Staff → Rounds**:

- **Gameplan:** `round_one` (it must show as valid).
- **Minutes per team:** at least **24**. `round_one` gives the worm 10 min on A, waits 10 more and
  churns until 5 min before the end; it's written for 60. Below 24 the round shows `invalid`
  (the conductor's log says `execute() is still going when the run ends`) and waits.
- **Start and end:** a slot per team is its minutes + 15 s + 2 min drain + 15 min overhead.
  With 4 teams × 60 min that's 4 × 77¼ min ≈ 5 h 10 min. Add **about 1 hour for the first
  attempt's sandbox build** (on dev: 35 min for OpenTofu to create the 21 VMs one at a time, 13½ min
  to configure them, about 13 min to snapshot them), and room for retries: about 6½ h in all.
- **"Shut down every devbox while its attempts run":** on, for the node's memory. It can only
  be changed before the round starts.
- **Simulate** the round once its gameplan is chosen: it must end `ok`.

Each team runs its latest **Live** upload from before the round's start. A team without one is
skipped, so tell teams to upload before the start.

## 8. During the round

```sh
worms show rounds       # the round, its attempts by state
worms show jobs         # prepare, deploy, teardown jobs
worms logs conductor -f    # what the conductor does
worms logs <number>        # one job's log: the first prepare builds the sandbox (about 1 h)
```

- **The first attempt's `prepare` is the step that has never run on production.** If it fails,
  `worms logs <number>` shows why. The conductor retries the attempt (up to 4 per team); send me
  the end of the log.
- **Stop everything:** `worms emergency-stop` hard-stops all VMs and destroys nothing.

## Don'ts

- **No `worms apply`, deploy or `worms open window` during the round.** A deploy restarts the
  conductor, and that costs the running team its attempt.
- **No `worms destroy`.** It takes the database VM and its records too.
- **No `worms apply apt-cache --packages` without egress.** It now puts the old index back, but
  achieves nothing.
- **No `worms step devbox-configure` or `worms step devbox-keys` by hand.** Without the round
  agent's variables they write empty key files: every team loses SSH access to its devbox and the
  gateway. If it happened: `worms sync keys` writes them back from the database.
- **If dpkg asks about `/etc/nftables.conf`, answer `N`** (keep your version). The gateway's
  rules are in that file.
