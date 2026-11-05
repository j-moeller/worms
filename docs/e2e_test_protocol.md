# End-to-end test protocol (live stack)

A manual walkthrough for a human tester of the **whole platform**, from an
empty database on a freshly built node to a team seeing its round score. It is
the live-system counterpart of the two automated layers:

| Layer | What it proves | Where |
|---|---|---|
| `worms test` | the code and configuration are wired correctly (offline; a fake Proxmox/agent) | `docs/testing.md`, `tests/test_e2e_stack.py` walks this same journey in-process |
| `worms simulate` | the live **scoring path** end to end (a flag through the sandbox API becomes a capture) | `docs/testing.md`, `ansible/Simulate.yml` |
| **this protocol** | the live **operator + player** journey: provisioning, scheduling, the devbox over real SSH, a real round, the results | you, by hand |

> **Read this first.** Round orchestration and the devbox have been exercised
> offline but **not yet on a live Proxmox host** (`docs/rounds.md`,
> `docs/devbox.md`, "Status / not done here"). Stage 10 (a real round) is the
> least-proven part of the system — treat a failure there as expected teething,
> capture the logs, and do not block the earlier stages on it.

---

## 0. Conventions and preconditions

**Run `worms` from the controller** (your laptop, or the node itself). Set the
alias once:

```sh
alias worms='python3 /path/to/worms/src/manage.py'
```

Names used below (substitute your deployment's real values):

| Placeholder | What it is | Where to find it |
|---|---|---|
| `DASHBOARD` | the dashboard URL | `https://worms-dev.mlsec.tu-berlin.de` (dev) or `https://worms.mlsec.tu-berlin.de` (live): `group_vars/dashboard.yml` |
| `NODE` | the Proxmox node, SSH as root | your `WORMS_SSH` |
| `ORCH` | the orchestrator VM | `10.3.0.19` by default (`docs/rounds.md`) |
| `GATEWAY_WAN` | the devbox SSH gateway's public address:port | host DNAT `:2222` (`docs/devbox.md`); the dashboard dev view prints the full `ssh -J …` command |

Two shells you will keep open to watch the system:

```sh
# the conductor and the attempt processor (results) on the orchestrator
ssh debian@ORCH 'journalctl -u orchestrator-conduct-rounds -u orchestrator-process-attempts -f'
# the host agent (builds/tears down sandboxes and devboxes)
ssh root@NODE 'journalctl -u worms-round-agent -f'
```

And the database, for the authoritative state (read-only `admin` via the CLI):

```sh
worms status        # components, internet, competition, rounds, run states, queued jobs
worms teams         # teams + members' login status
worms users         # all users: team, login, one-time password set?
worms leaderboard    # the production leaderboard in the console
```

### Prior state — the platform is up

**Check before you start:**

```sh
worms status
```

Expect: firewall **enabled**; the infrastructure, `private_internet`, and
(after stage 3) `sandbox`/`devbox` pools reporting VMs; **internet access
denied** (the steady state); a **current competition** named `WORMS <year>`
with **0 teams** and **0 rounds**. If `worms status` shows `n/a` for a section,
the matching secret is missing (`worms secrets`) — fix that first.

```sh
ssh root@NODE 'systemctl is-active proxmox-firewall worms-host-firewall worms-mirror'   # all: active
ssh debian@ORCH 'systemctl is-active orchestrator-conduct-rounds orchestrator-process-attempts'  # active
```

If any service is not active, the platform is not ready — rebuild
(`worms build`) before testing.

---

## 1. An administrator initializes the users and assigns teams

**Prior state:** current competition exists, no students, no teams.
Check: `worms users` lists only `admin`; `worms teams` is empty.

**Do:**

1. Get the admin's one-time password and sign in:
   ```sh
   worms admin reset-password          # prints a 20-char one-time password once
   ```
   Open `DASHBOARD`, sign in as `admin` with that password; you are forced to
   `/change-password/` — set a real one.
2. (Needed later for the devbox and a dev round.) **Staff → Competition →
   create the development competition.** Optionally set a registration window.
3. **Staff → Teams → create** a team, e.g. `alpha`, `max_members` 2. Create a
   second team `bravo` if you want to test the sequencing in stage 10.
4. **Staff → Users.** Create one student (email, first/last name, team
   `alpha`); **or** import a CSV (`Vorname,Nachname,E-Mail,Team` or the English
   headers). The **one-time passwords are shown once** on the result page
   (`Cache-Control: no-store`) — copy them now.

**Expected post state & how to check:**

- `worms teams` lists `alpha` (and `bravo`), each with its members and
  "no login yet".
- `worms users` lists the new students with their team and "one-time password
  set".
- In the DB the team is tied to both competitions:
  ```sh
  ssh debian@ORCH "psql -c \"SELECT name, competition_id, development_competition_id FROM teams;\""  # dev id set
  ```
  (or just confirm on **Staff → Teams** that each team shows a devbox hint).

**Edge cases to try:**

- Re-create a user with the **same email** (any case) → "already exists",
  nothing created.
- Put a third member into a 2-person team → refused, team stays at 2.
- CSV with a row naming a **full** team, a **non-existent** team, and a
  **malformed** email → each reported, the valid rows still created.
- Staff accounts are **not** made here: `worms admin create-staff tutor@x` (the
  dashboard cannot mint staff; prove it by checking the dashboard has no such
  control).

---

## 2. The administrator creates the schedule for the first and second round

**Prior state:** teams exist, no rounds. Check: `worms status` shows 0 rounds;
**Staff → Rounds** is empty.

**Do:** **Staff → Rounds → create** two **live** rounds that do **not** overlap,
e.g. R1 tomorrow 10:00–16:00 and R2 the day after 10:00–16:00. Leave the seed
blank (random).

**Expected post state & how to check:**

- Both rounds listed on **Staff → Rounds**, ordered by start; `worms status`
  shows "2 rounds" and the next-round phase. Nothing is built yet (a round is
  only a database record):
  ```sh
  ssh root@NODE 'qm list | grep -c sandbox'     # still 0 sandbox VMs
  ```
- Opening R1 shows "Nothing is built until it starts" and its seed.

**Edge cases to try (the form must refuse each):**

- A **past** start → "Start the round in the future".
- A second round **overlapping** R1 → "Overlaps R1".
- A round **too short** to fit every team's slot (fuse + run + drain + build/
  teardown, per team, sequentially) → "N teams need H h M min … room for K".
- A **dev** round before the development competition exists → "Create the
  development competition first".

---

## 3. The game plan for the first round

The "game plan" is round 1's **sandbox parameters**, set on the same create/edit
form (stored in `rounds.sandbox_params`; `docs/rounds.md`).

**Do:** Edit R1 and set:

- **how many VMs / networks / sites / interfaces / RAM** (the sandbox size);
- **Fuse (min)** = `worm_start_delay`, **Run length (min)** = `run_duration`
  (the timing);
- **Secrets up front** (`initial_fraction`), **First VM stop (min)**
  (`first_stop_after`), **then a stop every (min)** (`stop_interval`), **VMs kept
  running** (`min_alive`), **Restart a stopped VM after (min)** (`restart_after`,
  blank = stays off) — the stop/secret schedule.

**Expected post state & how to check:** the stored parameters (minutes are
stored as seconds):

```sh
ssh debian@ORCH "psql -c \"SELECT name, sandbox_params FROM rounds WHERE name='R1';\""
```

Expect e.g. `"timing": {"worm_start_delay": 300, "run_duration": 3600}` and a
`"game_plan"` block with your values in seconds.

> The **entry / start VM is not something you set** — it is chosen per run from
> the round's seed (`gameplan.generate`), so each team gets a deterministic but
> unpredictable start VM. You will see which one it was in the timeline (stage
> 11).

Now **provision the devboxes** for the teams you created (the dashboard builds
no infrastructure):

```sh
worms devbox provision        # a Devbox per team + one queued provision job per team
worms status                  # watch the devbox pool fill as the agent runs the jobs
```

Check: `ssh root@NODE 'qm list'` shows three VMs per team plus a gateway, and
the devbox pool in `worms status` reports them running.

---

## 4. A user logs in and creates an SSH key for the devbox

**Prior state:** the student has a one-time password (stage 1) and a provisioned
devbox (stage 3) but no key registered.

**Do:**

1. Sign in to `DASHBOARD` as the student with the one-time password; set a new
   password when forced.
2. Switch to the **development** view (the nav toggle). You see the devbox: its
   VMs, **Reset**, and the connect command.
3. On your **profile** (`/profile/`), in **SSH keys**, paste your **public** key
   (e.g. `~/.ssh/id_ed25519.pub`) and add it.

**Expected post state & how to check:**

- The key is listed on the profile.
- A reconfigure reached the host agent (no VM rebuild): on the agent shell you
  see a `devbox_reconfigure` job run; in the DB:
  ```sh
  ssh debian@ORCH "psql -c \"SELECT kind, state FROM host_jobs ORDER BY id DESC LIMIT 3;\""
  ```

**Edge cases to try (each refused, nothing stored, no job):**

- A key whose **comment contains a template** (`x={{7*7}}`) or shell characters
  → "not allowed / not a single-line OpenSSH public key" (this is the SSTI
  guard, `docs/security-assessment.md` D6).
- A **non-key** string, or a key with an unknown type.
- **Another team's** exact key blob → "already registered by another team" (the
  DB refuses one key under two teams, D7).
- Remove the key → it disappears and a reconfigure is enqueued. **Leaving the
  team** likewise removes that member's keys and reconfigures (access revoked).

---

## 5. The user logs into the SSH gateway onto the dev environment

**Prior state:** a key registered (stage 4); the agent has pushed it to the
gateway and the devbox VMs.

**Do:** copy the exact command from the dashboard dev view ("how to connect"),
which is of the form:

```sh
ssh -A -J team@GATEWAY_WAN root@<your A VM's fabric IP>
```

**Expected post state & how to check:** you land on **your team's VM A** as
root. Check `cat /etc/worms/flag` exists, and `hostname` is `devbox-<team>-a`.

**Edge cases to try — the confinement (`docs/devbox.md`, "SSH access"):**

- From A, pivot to **B** and **C** inside the devbox network (that is the worm's
  job; SSH between them works because they share the devbox bridges).
- Try to `permitopen` elsewhere — open a forwarded port to **another host**
  (e.g. another team's A, or the scoring server) through the gateway: it must be
  **refused** (`permitopen` is limited to *your* A only).
- Confirm the team account cannot rewrite its own confinement: on the gateway
  the `team` user's `~/.ssh/authorized_keys` is **root-owned** (you cannot
  `ssh team@GATEWAY_WAN` to a shell — the gateway is jump-only, `restrict`).
- From a devbox VM, try to reach the **sandbox**, the Proxmox host, the
  database, the dashboard, another team's devbox → all must fail (isolation, the
  firewall). `ping`/`curl` time out or are refused.

---

## 6. The user performs arbitrary operations on the devbox

**Prior state:** you are on devbox A (stage 5); the dev score is 0.

**Do:** act as a worm would. Read your VM's token and submit it to scoring, and
send a log line to logging (from the devbox VM, over its own DNS; the sample is
`worm.py` in the repo and `docs/worm-contract.md`):

```python
python3 - <<'PY'
import json, urllib.request
flag = open("/etc/worms/flag").read().strip()
def post(host, body):
    req = urllib.request.Request(f"https://{host}.worms.internal/submit",
        data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=10) as r: print(host, r.status)
post("scoring", {"vm_flag": flag})
post("logging", {"log_data": {"event": "captured A", "note": "manual e2e"}})
PY
```

(Move to B and C and submit their tokens too, for a dev score of 2–3.)

**Expected post state & how to check:** each POST returns **201**; TLS verifies
against the internal CA (no `--insecure`). The submissions are recorded as
**devbox** attempts attributed to your team by source address:

```sh
ssh debian@ORCH "psql -c \"SELECT devbox, count(*) FROM flag_capture_attempts GROUP BY devbox;\""
```

**Edge cases to try (`docs/worm-contract.md`, "Limits"):** a malformed body →
**400**; a GET or other method → **405**; a body over 4 KiB → **413**; well past
~30 requests/min → **429** then a short cooldown. A flag that is **not** your
VM's does not score.

---

## 7. The user observes the feedback in the dashboard

**Prior state:** devbox submissions made (stage 6); the attempt processor
(`orchestrator-process-attempts`) is running.

**Do:** on the dashboard **development** view, refresh.

**Expected post state & how to check:**

- The **profile's Team panel** shows the score: the number of distinct tokens you
  submitted (1–3). On the dev view, each captured VM is marked `Captured`.
- **Logs**, on the **progression** page, shows your submitted log entries (newest
  first), each expandable to its JSON.
- These appear within a second or two of the submission (the processor is
  continuous). In the DB:
  ```sh
  ssh debian@ORCH "psql -c \"SELECT count(*) FROM devbox_captures; SELECT count(*) FROM devbox_logs;\""
  ```

**Edge case:** submitting **another team's** token from your devbox never
credits you (it is not your VM). The production view (leaderboard) shows
**nothing** from the devbox — dev and competition feedback are separate.

---

## 8. The user resets the devbox

**Prior state:** a non-zero dev score and some logs (stage 7).

**Do:** on the dev view, **Reset** → confirm ("Yes, rebuild my devbox").

**Expected post state & how to check:**

- The dashboard reports the reset was requested; the agent runs a
  `devbox_rebuild` job (watch the agent shell): the three VMs are rebuilt with
  **fresh disks and new tokens**, and your keys are re-installed.
- The **score starts over at 0** on the profile, but **no records are deleted**
  — the version was bumped:
  ```sh
  ssh debian@ORCH "psql -c \"SELECT name, devbox_version FROM teams WHERE name='alpha';\""  # incremented
  ssh debian@ORCH "psql -c \"SELECT devbox_version, count(*) FROM devbox_captures GROUP BY devbox_version;\""  # old rows kept
  ```
- After the rebuild, SSH back in (stage 5) and confirm `/etc/worms/flag` is a
  **new** token; re-submitting your old captures scores again at the new version.

**Edge cases:**

- Press **Reset twice** quickly → the second reports "a rebuild is already in
  progress" and does **not** bump the version again (no piled-up rebuilds).
- Reset with **no devbox** (a team you never provisioned) → an error, the
  version unchanged.

---

## 9. The user makes a submission by uploading a program

**Prior state:** the student is in a team; the production view is selected.

**Do:** **Upload** page → upload a `.zip` with **`main.py` at its top level**
(`worm.py` in the repo renamed to `main.py` is a working skeleton;
`docs/worm-contract.md`). Do this **before R1's start time** — the team's latest
upload before `round_start` is the one that runs.

**Expected post state & how to check:**

- The page confirms "Uploaded <name>" and lists it in your upload history.
- The file lands in the one directory the agent reads:
  ```sh
  ssh debian@DASHBOARD 'ls -R /opt/dashboard/media/malware-artifacts | head'
  ssh debian@ORCH "psql -c \"SELECT u.created_at, t.name FROM uploads u JOIN teams t ON t.id=u.team_id;\""
  ```

**Edge cases to try (each refused, nothing stored):** not a zip → "That is not a
.zip archive"; a zip with `main.py` **nested** in a folder, or none at all → "no
main.py at its top level"; a file **> 100 MiB** → refused. Uploading the **same
bytes** again reuses the same stored key (content-addressed).

---

## 10. The first round is scheduled and runs

This is the least-proven stage (see the banner at the top). It needs **no manual
trigger**: the conductor plans and runs R1 on its own once `round_start` passes.
Keep the two watch shells open.

**Prior state:** R1 has a game plan (stage 3); every participating team has an
upload before `round_start` (stage 9); no sandbox exists
(`ssh root@NODE 'qm list | grep sandbox'` empty). Ensure an internet window is
**not** open and nothing else is building.

**Do:** wait for `round_start` (or, to test now, edit R1's start to a minute from
now — the form will not let you set the past, so set it a minute ahead). Watch.
For each team, in order, you should see the lifecycle (`docs/rounds.md`):

```
PENDING → PREPARING → CHECKING → DEPLOYING → ARMED → RUNNING → ENDING → TEARING_DOWN → FINISHED
```

While a team is RUNNING, SSH is **not** how the worm submits — its own code
captures VMs via the scoring server. To sanity-check the live scoring path
independently, you can run the built-in rehearsal against a deployed sandbox:

```sh
worms simulate         # creates a throwaway test team/token, submits, verifies, cleans up
```

**Expected post state & how to check (per team, then overall):**

```sh
ssh debian@ORCH "psql -c \"SELECT team_id, state, position FROM round_runs ORDER BY position;\""
ssh debian@ORCH "psql -c \"SELECT kind, state, error FROM host_jobs ORDER BY id DESC LIMIT 12;\""
ssh root@NODE 'qm list | grep sandbox'   # VMs exist during the run, GONE after teardown
```

Expect: one `round_run` per team reaching **finished**; the host jobs
`prepare → check → deploy → (release_secrets…) → teardown` all **done**; the
sandbox VMs **destroyed** at the end (teardown). On **Staff → Rounds → R1** the
runs, their host jobs, and what each `prepare` built are shown; the **TOPOLOGY**
page plays each run's timeline.

**Edge cases to try:**

- A team with **no upload** before the start → its run is **skipped** with the
  reason (not a crash); `state = skipped`.
- A round whose remaining time cannot fit a team's whole slot → that run is
  **skipped** ("not enough time left").
- Kill a `prepare`/`deploy` (e.g. break a param) → the run **fails**, its sandbox
  is torn down, and the **next** run does not build until that teardown
  completes (one sandbox at a time).
- Only **one** sandbox exists at any moment — confirm team 2 does not prepare
  until team 1 has torn down.

---

## 11. The user sees the results (score) and the logs of the run

**Prior state:** R1 finished (stage 10); the attempt processor has run.

**Do & check — three surfaces, all for the signed-in team:**

1. **Leaderboard / score.** `worms leaderboard` (console) and the dashboard
   production home: the team's **round score = number of VMs it captured**; the
   round tab ranks teams.
2. **Progression page.** The **PROGRESSION** nav → pick R1: the timeline replays
   what the team's worm did — the VMs it captured, its flag submissions on
   scoring, its log lines on logging, and its network flows (the "video").
3. **Team API** (a read-only token the member creates on their profile →
   "Access tokens"):
   ```sh
   TOKEN=...   # the raw token, shown once when created
   curl -s -H "Authorization: Bearer $TOKEN" "$DASHBOARD/api/v1/rounds/" | jq .
   curl -s -H "Authorization: Bearer $TOKEN" "$DASHBOARD/api/v1/rounds/<R1_ID>/scoring/" | jq '.summary, .items[0]'
   curl -s -H "Authorization: Bearer $TOKEN" "$DASHBOARD/api/v1/rounds/<R1_ID>/logging/" | jq '.items[0]'
   curl -s -H "Authorization: Bearer $TOKEN" "$DASHBOARD/api/v1/rounds/<R1_ID>/progression.zip" -o progression.zip
   ```

**Expected post state:** `scoring` lists each submission with a `result`
(`captured`/`duplicate`/`not_counted`/`invalid`/`pending`) and `summary.captured`
= the score; `logging` returns the run's stored log records byte-for-byte;
`progression(.zip)` is the same document the page replays.

**Edge cases to try — scoping (a team sees only its own):**

- The response contains **no other team's** name/uuid, **no VM the team did not
  capture**, and **no infrastructure** addresses.
- A round from **another competition**, or another team's data, via your token →
  **404** / empty.
- **No / malformed / revoked** bearer token, or a token in the URL → **401**.
- A **devbox** submission never appears in the round `scoring`/`logging` (those
  are the competition only).

---

## Appendix — one-glance checklist

| # | Stage | One-line pass criterion |
|---|---|---|
| 0 | platform up | `worms status`: firewall on, services active, internet denied |
| 1 | users + teams | `worms teams`/`users` show them; team tied to both competitions |
| 2 | schedule | two non-overlapping rounds; nothing built |
| 3 | game plan | `rounds.sandbox_params` holds the values; devboxes provisioned |
| 4 | SSH key | key listed; `devbox_reconfigure` job ran; bad keys refused |
| 5 | gateway login | land on team A as root; cannot reach anything else |
| 6 | devbox ops | scoring/logging return 201; recorded with `devbox=true` |
| 7 | feedback | dev score + logs update within seconds |
| 8 | reset | version bumped, score 0, old rows kept, new tokens |
| 9 | upload | `main.py`-at-top zip stored under the media root |
| 10 | round runs | per team PENDING→…→FINISHED; sandbox gone after teardown |
| 11 | results | score + logs + progression visible to the team, and only its own |

**If something fails:** capture the matching watch-shell output and the two SQL
queries from stage 10, note the stage, and compare against
`tests/test_e2e_stack.py` (the same journey, asserted in-process) to localise
whether it is the code path or the live infrastructure.
