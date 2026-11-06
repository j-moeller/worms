# Feature audit for the first product (2026-11-09)

On 2026-11-06, 11:48–12:00, before the first production deploy on `worms-pve`. The four
journeys of the first product, against the bar the user set: each one **feature complete**, that
is, **explainable** (a student, or an admin, can learn from the dashboard, CLI and docs what to do
and what happened) and **traceable** (for every action and outcome, both sides can see what
happened, and important actions leave a forensic record). Three read-only audits of the code, one
per journey, plus checks on `worms-dev`. Nothing was written there except a throwaway database
schema for the migration check, which was dropped. *Live* marks what was seen on worms-dev; the
rest is from the code.

## Verdict

| Journey | Works | Explainable | Traceable for the student | Traceable for the admin |
|---|---|---|---|---|
| Admins create the environment | Yes, live | Yes: `docs/fresh-install.md`, `docs/management.md` | — | Partly: forensic events on the console; devbox state and job failures hard to find |
| Teams sign in | Yes, live | Yes | No history | Yes: `login`, `password` events, last sign-in |
| Teams reach and test the devbox | Yes, live | **No**: nothing on the dashboard says what the VMs are, what they reach, or about the LLM endpoint | **No**: key installed? reset running or failed? | Console only (`worms audit`); the gateway's client address is not yet proven live |
| Uploads run on the devbox | Yes, live once (2026-11-05) | Partly: the upload page has the outline, not the contract | **No**: failure reasons, `main.sh` output and exit status are shown to nobody | **No**: the run's error is stored and read by nothing; no event for uploads or runs |

The mechanics are done; what is missing is what a student and an organiser see when something
does not go as planned. Most of it is dashboard text and two or three read-only panels, not new
mechanics.

## Before today's production deploy

1. **Done in this audit**: `docs/fresh-install.md` updated for the merge and the event tables;
   the migration chain 0001–0006 ran from an empty schema on worms-dev's PostgreSQL;
   `test_ansible`'s watchdog test no longer writes into the live event tables (it had recorded a
   fake `platform.watchdog_stopped` at 2026-11-06 03:36 UTC; the row cannot be deleted, the tables
   are INSERT-only, so ignore it).
2. **On worms-pve the first devbox reset is the first rollback ever run.** New VMs get their
   `clean` snapshot when provisioned, so a reset or dev upload there rolls back
   (`scripts/round_agent/handlers.py:545`); on worms-dev no VM has a snapshot and no rollback has
   run. Run the end-to-end check in `docs/fresh-install.md` §5 right after `devbox provision`,
   and do the upload in it twice.
3. **The SSH gateway's client address has never been recorded** *(live: 0 of 46 logins have one;
   the kernel log rule has matched nothing since it was deployed, because nobody has logged in
   since)*. The same §5 check covers it: one login from outside.
4. Not in the deploy: the reworded lead of `devbox.html` in this working tree is uncommitted
   (someone else's), so `worms-pve` gets the old text.
5. Browsers warn on the dashboard (the internal CA, D-9). Tell the students, or decide D-9.

## Blockers for Monday

| # | What | Where | Fix, size |
|---|---|---|---|
| B1 | **Deactivating a student leaves their SSH access.** Deactivation only clears `is_active`; their keys stay, and every key of the team is pushed to the devbox and the gateway. Nothing warns about it | `dashboard/views/staff_users.py:184-194`, `orchestrator/rounds/devbox.py:36-37`, keys are removed only on leaving a team (`dashboard/membership.py:22-30`) | Delete the user's keys and request a reconfigure on deactivation, as `remove_member` does; a test and a docs line. 45 min |
| B2 | **Students land in the Live view.** Sign-in goes to the profile in Live (`is_dev_environment` defaults to False), which says "No round has started yet" and has no reset button. The nav has no Devbox link; the devbox page is the home page of the Dev view only. An upload made in Live waits for a round that does not exist | `dashboard/middleware.py:204`, `templates/base.html:31-41`, `views/profile.py:72-76` | Default to Dev while there are no rounds, and a Devbox link in the nav. 30 min |
| B3 | **The environment and the worm contract are not on the dashboard.** Not said anywhere: what A, B and C are; what they reach (scoring, logging, the caches, `teachllm`); the limits (100 MiB, 30 uploads an hour, 2 GiB, one hour of `main.sh`); the working directory, user `debian` with sudo; that file modes in the zip are not kept; the 10 s fuse; that what runs on B and C outlives the hour. `docs/status.md` §1 says "Only in this repository" | `templates/devbox.html:7`, `templates/uploads.html:7,27,63-72`; the content is in `docs/worm-contract.md` | One "How it works" panel on the devbox page and the upload page, from `docs/worm-contract.md`. 2 h |
| B4 | **A failed run or reset tells nobody why.** The team sees fixed text ("Could not be started", `views/uploads.py:51`); `DevboxRun.error` is written (`orchestrator/rounds/devbox_runs.py:144-148`) and read by no view, template or command. A failed manual reset still looks like success (F25): the version is bumped before the job runs, and the devbox page never reads the job. Devbox job failures appear only on Staff → Rounds, the last five, without team, time or job id (`templates/staff/rounds.html:26-38`), so nothing leads to `/var/log/worms/jobs/<id>.log` | as given | A "Devbox jobs" panel on the staff team page (state, error, job id, time) and the team's latest job on its devbox page (queued, running since, failed: "your old VMs are still up, the organisers can see why"). 3 h |
| B5 | **`main.sh`'s output and exit status go nowhere.** It runs as the unit `worms-start` on A, so its output is only in A's journal; the run shows "Running" for the whole hour even if `main.sh` died at once, then "Finished" (`devbox_runs.py:83-85`). No page or doc mentions `worms-start` | `scripts/round_agent/handlers.py:31,303-315` | Now: tell teams `sudo journalctl -u worms-start` on A, and say "started at …" / "its hour is up" instead. 20 min. Collecting exit status and output tail: after Monday, ½–1 day |

## Should, before Monday if there is time

| What | Fix, size |
|---|---|
| A key's state is unknown: the success message says "being updated now", the job takes about 3 minutes and waits behind rebuilds; the key list has no "added by", date or pending/installed (`templates/profile.html:72-81`; the model has `created_by`, `created_at`) | By and Added columns (20 min); "pending" until a devbox job requested after it is done (1–2 h) |
| The admin cannot reset one team's devbox: the staff team page is read-only, view-as refuses writes, the CLI has only `devbox provision` and `reconfigure` | a `reset <team>` subcommand of `worms devbox` that calls `request_rebuild`, with an event. 45 min |
| The admin sees no per-team devbox state, keys or jobs on the staff team page; `worms teams` has no devbox column. The staff topology view does show VM states, but no doc points there | A docs line (5 min); running/total in `worms teams` (30 min); keys with their owner on the staff team page (20 min) |
| No forensic event for: uploads; the reset an upload triggers (a manual one writes `team.devbox_reset`); run outcomes; team rename or size changes (`staff_teams.py:100-113`); `worms devbox provision`/`reconfigure` from the console. Uploads lose their history when their user or team is deleted | `events.team(...)` for uploads, upload resets and team edits, platform events from the CLI (30 min). An `upload_events` table with hash, size, view and address: migration plus grants, 1–2 h |
| No queue position: one job at a time, a team's deploy job is created only after its rebuild and waits behind any rebuild queued meanwhile, while the page says "Copying it to VM A" | "N jobs ahead" (45 min) |
| After a reset, SSH warns "REMOTE HOST IDENTIFICATION HAS CHANGED"; the connect panel does not say so | One line with `ssh-keygen -R`. 15 min |
| "Permission denied (publickey)" is all a student gets for a wrong or not yet installed key, and failed gateway logins are not recorded (by design) | A "Can't connect?" list on the devbox page. 30 min |
| A user without a team gets a bare "User not assigned to team yet" (400) | Wording with the next step. 10 min |
| Past dev runs show a state only, no score, file name or size | 30–60 min |

## Nice

Teams cannot download their past uploads. Students see no history of their own (sign-ins, key
removals by a teammate, resets). The zip check does not look at the unpacked size or encrypted
entries, so a zip bomb fails at deployment with no reason given. A `python3 main.py` zip gets "no
main.sh at its top level" with no hint of the change. `worms users`/`worms teams` do not show
deactivated accounts; `worms audit` cannot filter by user or team and cuts details at 100
characters. The staff team page does not mark dev uploads and still says "the latest before a
round's start runs". Too many sign-ins show nginx's bare 429 page. Token, flag and captured are
used for the same thing.

## Who sees what, today

| Action or outcome | The student | The admin, dashboard | The admin, console |
|---|---|---|---|
| Sign-in, failed sign-in | — | Last sign-in on the user page | `login.ok`, `login.failed` |
| Password changed, reset by staff | A message | One-time password page, once | `password.changed`, `password.reset`; `password_resets` |
| View-as | — | — | `view_as.started`, `view_as.ended` |
| Teams, accounts, membership | Team on the profile | Staff pages | `team.*`, `account.*` |
| Deactivation | Cannot sign in, **but SSH still works** (B1) | User page | `account.deactivated` |
| SSH key added, removed | Key list, no state | — | `credential.ssh_key_added`, `_removed` |
| Key installed on the devbox | — | — | The host job and its log |
| Gateway login | — | — | `ssh_gateway.login`, the key's owner; client address unproven |
| Manual reset | A flash message, the version | The version | `team.devbox_reset`, the host job |
| Failed reset or rebuild | — (F25) | Staff → Rounds, no team | The host job and its log |
| Upload | The upload list with a state | User and team pages | — (the `uploads` row) |
| A run's failure and reason | Fixed text | — | Only in the database (`devbox_runs.error`) |
| `main.sh` output, exit status | Only on A, if they know where | — | — |
| The worm's logs and captures | Progression, the dev score | View-as | `logging`, scoring tables |
| `worms devbox provision`, `reconfigure` | — | — | The host job only |

## Checked live on worms-dev, read-only

`worms status`: 3 teams, 8/8 infrastructure and 12/12 devbox VMs running, 16 host jobs, all
done. One dev upload ever (charlie, 2026-11-05 12:18 UTC): rebuild by replacement 8 min,
deploy 3 s, the worm captured a VM and logged within 15 s, the run ended after its hour; its job
log names the content hash and A's address, not the team or upload. No VM A has a snapshot. 4 of
7 students have never signed in; three of alpha's have no team. The event tables hold 46 gateway
logins (none with a client address), one failed sign-in, the admin's view-as and the test's fake
watchdog row; no `login.ok`, `password`, `credential` or `team` rows yet, because nobody has done
those things since 03:46. Those writers are covered by `tests/test_events.py`; the fresh-install
check (§5) is their first live use.
