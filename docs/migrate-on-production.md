# Migrating production: `33dae12` → `3ce1751`

An operational runbook for the cutover on the real platform, for the specific
situation at hand: **the database has never been dumped**, we will **tear down
every devbox** (they need the private‑internet changes), and we want nginx on
the dashboard brought up to the **latest security build** (which is *not* a code
change — see [§6](#6-nginx-security-update-on-the-dashboard)).

Read this together with **[`16-10-merge.md`](16-10-merge.md)**, which is the
full code/migration review of this exact range (production `33dae12` → `main`).
This page is the *procedure*; that page is the *why* for every migration and the
detailed rollback. Where they overlap, that page is authoritative.

> **One‑line answer to "can I just `worms build all`?"** No — not cleanly, and
> not for what you want. `build all` opens the internet window, which **refuses
> while any devbox or sandbox VM exists**, so it would stop until the devboxes
> are down; it does not, on its own, dump the database first; and it does not
> upgrade nginx unless you add `--packages`. Do the ordered steps below instead.
> They are mostly `worms build <component>`, just sequenced so nothing is lost.

---

## Command sequence (copy‑paste)

Run on the **controller** (where `worms` = `src/manage.py`) unless a line is
marked *inside …*. Pick a window with no round running. Each numbered block is
explained in [Procedure](#procedure) below.

```sh
# 0. PRE-FLIGHT — all must be clean/empty before going on
worms status            # components up, NO sandbox VM, internet window closed
worms status rounds     # nothing running
worms status jobs       # nothing running or queued
git status              # clean working tree (the node workspace rsyncs from it)

# 1. BACK UP THE DATABASE FIRST (nothing else does)
worms login database
# inside the database VM:
  df -h /var/lib/postgresql
  sudo -u postgres pg_dump -Fc -f /var/lib/postgresql/mydb-before-3ce1751.dump mydb
  ls -lh /var/lib/postgresql/mydb-before-3ce1751.dump
  exit
# recommended: copy the dump off the VM (address from `worms show database`):
#   scp -J root@<node> debian@<database-ip>:/var/lib/postgresql/mydb-before-3ce1751.dump .

# 2. UPDATE THE CHECKOUT to the target
git pull --ff-only      # -> 3ce1751 (origin/main is bdb461f = 3ce1751 + this doc)
git rev-parse --short HEAD

# 3. DEPLOY CODE + MIGRATIONS (no internet window)
worms build --plan dashboard round-agent    # review: must name migrations 0010-0019
worms build dashboard round-agent            # migrates (services stopped), restarts dashboard + conductor

# 4. RESTART THE ROUND AGENT, THEN THE CONDUCTOR (by hand — the silent-failure step)
worms login node
# inside the node:
  systemctl restart worms-round-agent
  journalctl -u worms-round-agent -n 20
  exit
worms login orchestrator
# inside the orchestrator:
  sudo systemctl restart orchestrator-conduct-rounds
  sudo journalctl -u orchestrator-conduct-rounds -n 20     # "version ... recorded" per gameplan
  exit

# 5. PRIVATE INTERNET SEED + NGINX SECURITY UPDATE (one internet window; devboxes go down)
worms build apt-cache devpi dashboard --packages

# 6. TEAR DOWN + REBUILD EVERY DEVBOX FROM NOTHING (fresh disks; new pip index + offline NTP)
worms build devbox --rebuild    # every team (--team <name> repeatable for a subset); prompts to DESTROY
worms status jobs               # watch the devbox_rebuild jobs drain on the node
# lighter alternative (snapshot rollback + reconfigure; also applies the update): worms reset devbox --all-teams

# 7. VERIFY
worms status            # everything up, internet window CLOSED
worms status gameplans  # round_one valid on both sides
# by hand: Staff -> Rounds (URLs are now /staff/rounds/<uuid>/), Staff -> Simulations;
#          create+revoke a token; add+delete a devbox key; SSH a rebuilt devbox
#          (pip resolves, `timedatectl` shows ntp.worms.internal); dashboard: `nginx -v`.
```

**If a migration stops** on a duplicate `uuid` (steps 3): on the database VM,
`UPDATE rounds SET uuid = gen_random_uuid() WHERE uuid IS NULL;` (and the same
for `access_tokens`, `devbox_ssh_keys`), then re-run step 3.

**Rollback** (only if needed; full detail in [`16-10-merge.md`](16-10-merge.md#rollback)) —
keep the new schema, let old code run:
```sh
worms login database
# inside the database VM, as postgres (psql mydb):
  ALTER TABLE rounds          ALTER COLUMN uuid SET DEFAULT gen_random_uuid();
  ALTER TABLE access_tokens   ALTER COLUMN uuid SET DEFAULT gen_random_uuid();
  ALTER TABLE devbox_ssh_keys ALTER COLUMN uuid SET DEFAULT gen_random_uuid();
# then on the controller: git checkout 33dae12 && worms build dashboard round-agent
# and redo step 4 (restart the agent, then the conductor).
```

---

## What's actually changing (at a glance)

27 commits, ~110 files. Nothing touches the platform, firewall, host network,
scoring/logging/monitor, `group_vars` or the vault, and **no new secret**. The
parts that matter for a deploy:

| Area | Change | Lands with |
|---|---|---|
| **Database** | 10 migrations `0010`–`0019`: UUIDs for rounds, access tokens and devbox SSH keys (`0010`–`0015`); `host_jobs.read_file` (`0016`); `gameplan_simulations` (`0017`); `gameplans.description` (`0018`); `gameplan_versions` + `gameplan_changes` (`0019`). Only small tables are rewritten; seconds. | the orchestrator build (migrates with its services stopped), then the DB grants play |
| **Dashboard / API** | Rounds, tokens, SSH keys and progression are addressed by **UUID**, not a number. New **Staff → Simulations** and **Simulate** on a round. Favicon, Dev‑view round badge. | `worms build dashboard` |
| **Gameplans** (new feature) | `src/gameplan`: documents, archives, KeePass vaults, TOTP/telnet logins, `read_file`, simulation, immutable versions. A second example, `credential_chain.py`. | orchestrator + round agent |
| **Round agent (node)** | the `read_file` job, new secret kinds, `python3-cryptography`. **Must be restarted by hand** after the deploy. | `worms build round-agent` **+ `systemctl restart`** |
| **Devbox** | pip now points at the seeded devpi mirror; time syncs from `ntp.worms.internal` (offline). | **`worms build devbox --rebuild`** ([§7](#7-tear-down-and-rebuild-every-devbox)) |
| **Private internet** | larger devpi seed (116 pkgs), new apt‑cache packages, stale‑index guard. | `worms build apt-cache devpi --packages` ([§4](#4-refresh-the-private-internet-internet-window)) |
| **DNS** | every BIND log is a bounded file (failed lookups were filling the DNS VM's disk). | `worms build dns` (or the full build) |

**Good news already in production:** `33dae12` already contains the
migration‑deadlock fix (services stopped before `migrate`, `lock_timeout`) and
the sshd‑bind fix, so the migration step will not hang.

---

## The three things you flagged

1. **The database is not backed up.** Dump it *first* ([§1](#1-back-up-the-database-first-nothing-else-does)). The UUID steps `0011`/`0014` rewrite rows; a dump is your floor. Nothing in the platform backs the database up on its own.
2. **Tearing down every devbox.** Use **`worms build devbox --rebuild`** ([§7](#7-tear-down-and-rebuild-every-devbox)) — it rebuilds every team's three VMs **from nothing** (fresh disks), re‑applies the new pip index + NTP and re‑snapshots them clean; dev scores restart, dev records are kept. (`worms reset devbox --all-teams` is the lighter option — a snapshot rollback + reconfigure that still applies the update but leaves the clean snapshot on the old config.) **Do _not_ use `worms destroy`** — it tears down the VM stacks **and the database VM and its records**. See [Don'ts](#donts).
3. **nginx security update.** This is *not* in `3ce1751`. nginx is a plain Debian package installed `state: present` (install, never upgrade), so a normal build never moves it. The security build comes from an **apt upgrade during an internet window** — i.e. a `--packages` build of the dashboard. Details and the "newer than Debian stable" case in [§6](#6-nginx-security-update-on-the-dashboard).

---

## Procedure

Run everything from the controller (the laptop/host where `worms` = `src/manage.py`).
Pick a window with **no round running and none starting within ~1 hour** (a
deploy mid‑round costs that team its attempt), and tell teams that devboxes will
be down for a while and that **round links and the team‑API round ids change to
UUIDs**.

### 0. Pre‑flight

```sh
worms status                 # components up; NO sandbox VM exists; internet window closed
worms status rounds          # nothing running
worms status jobs            # nothing running or queued
git -C <checkout> status      # clean: the node workspace is rsynced from the working tree, uncommitted edits included
```
A leftover sandbox or a running job blocks the internet window later. Resolve before continuing.

### 1. Back up the database first (nothing else does)

```sh
worms login database
sudo -i
df -h /var/lib/postgresql        # the dump needs room; network_flows has no retention
sudo -u postgres pg_dump -Fc -f /var/lib/postgresql/mydb-before-3ce1751.dump mydb
ls -lh /var/lib/postgresql/mydb-before-3ce1751.dump
exit; exit
```
Then **copy it off the VM** (e.g. `scp` via the node) if the database disk itself
is the worry. Keep it until the deploy is proven. This is the line that makes
everything below reversible.

### 2. Update the checkout

```sh
git -C <checkout> pull --ff-only     # to 3ce1751 (write the hash down)
git -C <checkout> rev-parse --short HEAD
```

### 3. Deploy the code + migrations (no internet window)

```sh
worms build --plan dashboard round-agent
```
Expect the plan to pull in **database** and **orchestrator** ("configure again:
the database lacks migrations"), naming migrations **`0010`–`0019`**, then
**dashboard** and **round-agent**. If the plan does *not* name all ten
migrations, stop and investigate before applying.

```sh
worms build dashboard round-agent
```
One Ansible run configures the database, orchestrator and dashboard **together**
(so the dashboard is never new against an old schema): the orchestrator migrates
with its services stopped, the dashboard restarts, the DB grants play runs, the
conductor restarts. Then the node play: workspace sync, `python3-cryptography`,
the unit. **No internet window is opened** (packages are skipped without
`--packages`) — nginx is *not* touched here; that is [§6](#6-nginx-security-update-on-the-dashboard).

> If `0012` or `0015` stops on a duplicate `uuid` (the old dashboard inserted a
> row in the one‑second gap), fill the empties and re‑run — the SQL is in
> [`16-10-merge.md` §The migrations](16-10-merge.md#the-migrations).

### 4. Restart the round agent, then the conductor (the step that bites)

The build **syncs** the agent's code but **does not restart it**. A running
agent keeps the old gameplan code in memory while the new sandbox role is on
disk — `round_one`'s password login would then silently stop working. So, while
`worms status jobs` shows nothing running:

```sh
# on the node:
systemctl restart worms-round-agent
journalctl -u worms-round-agent -n 20
```
```sh
worms login orchestrator
sudo systemctl restart orchestrator-conduct-rounds
sudo journalctl -u orchestrator-conduct-rounds -n 20   # "version ... recorded" for each gameplan
exit
```
Restart the conductor **after** the agent, so its first gameplan check runs
against an agent that already lists every file (otherwise the new
`credential_chain.py` shows up only on the next 10‑minute check).

### 5. Refresh the private internet (internet window)

This is what the devboxes' new pip index needs, and it also carries the nginx
upgrade if you fold it in (next step). It **shuts the devboxes down**, opens the
window, seeds, closes the window, snapshots `clean`:

```sh
worms build apt-cache devpi --packages
```
- devpi fetches ~116 packages in batches; this is the slow part (many minutes).
- `round_one` needs nothing from here, so this step can wait for a quieter hour
  if you must — but the devbox rebuild in §7 depends on it, so do it before §7.

### 6. nginx security update on the dashboard

Fold the nginx upgrade into the same window (do it in the same sitting as §5):

```sh
worms build dashboard --packages
```
`--packages` opens the internet window and runs `UpdateVMs.yml`
(`apt upgrade: safe`) on the dashboard, which apts **directly from Debian
stable + security** (the dashboard is *not* behind the offline apt‑cache). nginx
upgrades to the current security build and its handler reloads. A plain
`worms build dashboard` would **not** move nginx (`state: present`).

> **If you need a version newer than Debian stable ships** (a CVE fixed only in
> nginx mainline or a backport), apt can't give it to you — that needs a repo +
> pin added to the `nginx_tls` role. That's a code change, not in `3ce1751`;
> tell me and I'll add it. For an ordinary Debian security update, the command
> above is all you need.

You can combine §5 and §6 into one window: `worms build apt-cache devpi dashboard --packages`.

### 7. Tear down and rebuild every devbox

Now that the private internet is seeded and the window is closed:

```sh
worms build devbox --rebuild    # every team; add --team <name> (repeatable) for a subset
worms status jobs               # watch the devbox_rebuild jobs run and drain
```
This is the literal "tear down all devbox VMs": through the orchestrator → round
agent, each team's three VMs are **replaced from nothing (fresh disks)**, keys
re‑installed, the **new pip index + NTP drop‑in applied**, and a fresh `clean`
snapshot taken (so later team resets roll back to the new config). Dev scores
restart; earlier dev records are kept. It prompts to DESTROY first (`--yes` skips).

> `worms reset devbox --all-teams` is the lighter alternative — it rolls each
> devbox back to its *pre‑deploy* `clean` snapshot and then reconfigures, which
> **also** applies the new pip/NTP, but is not a fresh teardown and leaves the
> snapshot on the old config. Either picks up the private‑internet change;
> `--rebuild` is the one that matches "tear down". Never use `worms destroy` —
> it takes the database VM too.

### 8. Verify

```sh
worms status                 # everything up; internet window CLOSED
worms status gameplans       # round_one valid both sides; credential_chain valid after §5
```
- **Staff → Rounds** opens; a round's URL is now `/staff/rounds/<uuid>/`.
- **Staff → Simulations** lists the gameplans; `round_one` simulates `ok`.
- As a team member: create+revoke an access token, add+delete a devbox SSH key (these write the new UUID columns).
- SSH into a rebuilt devbox and check `pip` resolves and `timedatectl` shows NTP from `ntp.worms.internal`.
- Dashboard over HTTPS still loads (nginx reloaded cleanly): `nginx -v` on the dashboard shows the new build.

---

## What teams and staff notice

- **Round links change.** Old numeric links (`/staff/rounds/12/`,
  `/progression/rounds/12/...`) answer 404; open rounds from the lists again.
- **Team API:** `GET /api/v1/rounds/` returns each round's `id` as a UUID and
  every `/api/v1/rounds/<id>/...` takes that UUID. Scripts that read the id from
  the list keep working; scripts with a numeric id hard‑coded get 404. Access
  **tokens themselves are unchanged**.
- Downloaded replays made before the deploy still open.
- `credential_chain.py` becomes an offered gameplan the moment it deploys — to
  keep it out of production, delete it in a commit before deploying.

---

## Rollback

Full detail (two rehearsed, data‑preserving options) is in
**[`16-10-merge.md` §Rollback](16-10-merge.md#rollback)**. In short:

- **Option 1 (recommended, nothing lost):** give the three UUID columns a DB
  default, then check out `33dae12` and `worms build dashboard round-agent`:
  ```sql
  ALTER TABLE rounds           ALTER COLUMN uuid SET DEFAULT gen_random_uuid();
  ALTER TABLE access_tokens    ALTER COLUMN uuid SET DEFAULT gen_random_uuid();
  ALTER TABLE devbox_ssh_keys  ALTER COLUMN uuid SET DEFAULT gen_random_uuid();
  ```
  Production's code then ignores the new columns and works. The UUIDs stay, so a
  later re‑deploy keeps the URLs.
- **Option 2 (back to the exact old schema):** on `main`'s code, stop the
  orchestrator services and `manage.py migrate core 0009 --database=migrator`,
  then check out `33dae12` and rebuild.
- **From the dump** (step 1) is the last resort — it loses everything since the
  dump. The private internet needs no rollback.

---

## Don'ts

- **Don't `worms destroy`** to clear devboxes — it destroys the VM stacks **and
  the database VM and its records**. Use `worms build devbox --rebuild` (or
  `worms reset devbox --all-teams` for the lighter rollback+reconfigure).
  (`worms destroy --all` is even broader.) There is no undo but the step‑1 dump.
- **Don't deploy during a round** — the running team loses its attempt.
- **Don't skip the agent restart** (§4). Nothing reports the error; a flag just
  becomes unreachable.
- **Don't open the internet window while a sandbox or devbox exists** — any
  `--packages` build shuts devboxes down for you, but a stray sandbox VM will
  make it refuse (`worms internet disable`, remove the sandbox, retry).
- **Don't run `migrate` by hand** on a VM whose services are up — the deploy
  stops them for you; doing it manually re‑introduces the lock contention.

---

## Worth doing soon (not required for this cutover)

- **Scheduled database dumps** (security review S1) — this cutover is the second
  time "nothing backs it up" has been the first risk.
- **Restart the agent on code change** automatically (a handler on the sync),
  which would remove §4 and its silent‑failure risk.
- **`db_default` on the UUID columns**, which would make rollback option 1
  unnecessary and the migration gap harmless.
