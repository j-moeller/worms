# Dev rounds and other open items

Written 2026-11-05, at commit `211d6ac`. Nothing here has been started. Items marked **Decide** are waiting for a decision.

## Should dev rounds be removed?

- [ ] **Decide:** remove dev rounds entirely? Recommended, because the devbox has no rounds.

Removing them means a future need for dev rounds would have to be rebuilt. Until then, the code below is unused by the dev view but can still be reached.

**What is already done:** the dev view no longer uses rounds. Its Progression page replays one progression, from the devbox's last reset up to now, which never finishes. See `build_devbox` in `projects/dashboard/orchestrator/rounds/progression.py`.

**What the button did:** on 2026-11-05, staff pressed the button on `/staff/competition/`. It created the competition `WORMS 2026 · dev` and linked the teams to it. It created no round, and the database still holds 0 rounds. It is harmless and did not start anything.

**What still exists for dev rounds:**

| Where | What | If removed |
|---|---|---|
| `templates/staff/competition.html`, lines 37 and 67-77 | The checklist item and the **Create it** / **Link every team** buttons. The text says the dev view shows "its rounds' progression", which is no longer true | Remove |
| `views/staff.py`, `development_competition`, and its URL `staff/competition/development/` | Creates the competition and links teams | Remove |
| `templates/staff/rounds.html`, lines 62-72 and 97 | The **Dev rounds** list and a note about them | Remove |
| `views/staff_rounds.py`, lines 40, 118, 129, 171 and 187, and `forms.py`, lines 244 and 272-275 | Creating and editing dev rounds | Remove |
| `api.py`, `_competitions` and `_round_info` | Dev rounds in `/api/v1/rounds/` | Live rounds only |
| `views/progression.py`, `team_rounds`, `_team_round`, `_team_document` | The session endpoints still serve a dev round in the dev view | Live rounds only |
| `orchestrator/rounds/progression.py`, `environment_of`, `window`, `_dev` | The devbox within a round's window | Keep `_devbox_activity`, remove the rest |
| `middleware.py`, lines 98-108 | `request.dev_competition` and the dev view's round state | Needs a rethink, see below |
| `core/models`, `Competition.is_dev_competition`, `Competition.development_competition`, `Team.development_competition` | The fields themselves | Needs a migration. Keep them unless the rest is clean without them |
| `views/debug.py`, lines 113-137, `views/staff_teams.py`, line 54, and `views/util.py`, line 63 | Demo data, new teams and the leaderboard's team filter | Follow the fields |
| `tests/test_progression.py` (`rd`, "Dev week"), `tests/test_api_roundtrip.py`, `tests/test_e2e_stack.py` | Tests that build a dev round or a dev competition | Update |
| `docs/progression.md`, `docs/rounds.md`, `docs/team_api.md`, `docs/management.md`, `docs/accounts.md`, `docs/requirements.md`, `docs/visualization.md`, `docs/testing.md`, `docs/e2e_test_protocol.md`, `docs/manual-devbox-run.md`, `docs/check-with-user.md` | Mentions of dev rounds or the development competition | Update |

If they stay, at least correct the text in `templates/staff/competition.html`, because it invites staff to press a button the dev view no longer needs.

## The header says a round is running when none is

- [ ] Stop the header showing `ROUND IN PROGRESS (DEV)` with 0 rounds.

`get_competition_state` in `dashboard/middleware.py` treats any competition with no registration dates as a running dev round. `views/util.py`, line 112, then shows that label.

- **Dev view:** `WORMS 2026 · dev` has no dates, so the label is always on. Seen on alpha's real page. From the code, a team with no linked dev competition gets "No competition right now" instead, so pressing the button probably made the label appear. That would look like starting a round. The before and after were not observed.
- **Live view:** the same happens to `WORMS 2026` while it has no dates. Already listed in `docs/check-with-user.md`.

Fix this together with the decision above. The dev view should say nothing about rounds.

## A manual Reset can still hold up a round

- [ ] **Decide:** leave it, wait for the whole round, or wait only near a round's timed jobs.

A rebuild takes about 8 minutes and the agent does one job at a time and cannot interrupt it. A dev upload waits for the round, and the agent takes a round's queued jobs first, but `POST /devbox/<team>/reset/` still queues a rebuild at once. This is not new. From the code, not observed, a round job caught behind a rebuild has these effects:

| Job | Effect of up to 8 minutes' delay |
|---|---|
| `prepare`, `check`, `deploy` | None on the team: the worm's hour starts when the fuse is armed. Later teams start later |
| `release_secrets`, during the run | The secret comes late and the run's end does not move, so that team has less time to find it. In the default plan these jobs fall at 10, 20 and 30 minutes |
| `teardown` | The next team starts later. A team's run is skipped if it no longer fits before the round's end |
| Any but `release_secrets`, behind a hung rebuild | The job fails the run after 30 minutes in the queue. The agent lets a rebuild's `tofu apply` run for up to 90 minutes |

Stopping and starting VMs in a round does not go through the agent, so that is not delayed.

**What waiting costs:** runs go one after another, each about 82 minutes by default (`core/schedule.py`), and the next starts at once. So "wait for the round" means about 4 hours with 3 teams and 14 hours with 10. A dev upload already waits this long.

| Option | A round is delayed | A team waits |
|---|---|---|
| Leave it | Up to 8 minutes per job | Not at all |
| Wait for the whole round | Never, apart from a rebuild already running when the round starts | 82 minutes per team in the round |
| Wait only near timed jobs: start a rebuild when the next agent job is at least 15 minutes away | Only if a rebuild overruns | At most about one run, 82 minutes, with the default plan of 8 VMs. A round of 30 VMs has no such gap, so a rebuild would wait for the round |

Whichever is chosen should apply to dev uploads too. If a Reset waits, the version bump has to wait with it, otherwise the score is cleared hours before the VMs change. A second worker for devbox jobs would remove the conflict, but that is the parallel work that is on hold.

## Which CPU type for the team VMs?

- [ ] **Decide:** `docs/check-with-user.md`, 3h.

`cpu: host` lets root on a devbox VM use KVM (`docs/security-assessment.md`). Sandbox VMs stay on `qemu64` until this is decided, so a worm that depends on the CPU can still work on the devbox and fail in a round. They do have the devbox VMs' memory floor now.

## Should the profile list only a user's own SSH keys?

- [ ] **Decide:** whole team, or only your own?

The profile lists the whole team's keys, and any member can remove any of them. That lets a teammate revoke a lost key, the same way access tokens work. Listing only your own would suit a personal page better, but then only staff could remove a key for someone who is away.

## Not proven yet

- [ ] **`bash main.sh` on a real sandbox.** No round has run since the change. It has run on a real devbox, through a dev upload (`docs/devbox.md`), and the agent's commands are the same. Upload `worm.sh`, renamed to `main.sh`, and run a real round.
- [ ] **The LLM endpoint from a real sandbox.** It works from all 9 devbox VMs. The sandbox gateway's rules are only checked by tests.
- [ ] **The 512 MB floor on a real sandbox.** No sandbox has been built since the change.
- [ ] **Live view of the profile's Team panel with real rounds.** The system has no rounds, so it says `No round has started yet`. Its numbers and layout have only been checked with test data.
- [ ] **Port 2222 from outside.** Run `ssh -p 2222 team@worms-dev.mlsec.tu-berlin.de` from a laptop. Expect `Permission denied (publickey)`. A timeout may mean the network in front of the node blocks it. The node cannot test its own port forward.
- [ ] **`Permission denied (publickey)` from A to B on alpha.** Seen by the user, not resolved. The hop worked on charlie's VMs with a key in the agent. Check `ssh-add -L` on the laptop, and if it lists the key, look at `ssh -v root@10.128.2.12`.

## Tests

- [ ] Run the full offline suite. It has not been run on `abcde6a` or `211d6ac`.
- [ ] Find out why 13 setup errors happened in the last long run, and whether they recur.
- [ ] Run the live suite (`tests/live`) from start to finish. It was updated for the new pages, has never finished, and needs the devboxes torn down first.

The last long run took 02:21 to 02:40 on 2026-11-05, on 19 test files, before the corrections below. It had 6 failures and 18 setup errors.

| Result | Tests | Status |
|---|---|---|
| Known failures | `test_playbook_syntax[inventory.generated.yml]`, `test_two_teams_run_one_after_another` | Unrelated, listed in `docs/progress-setup-infrastructure.md` |
| A test damaged by a find-and-replace | `test_round_agent_unit_wraps_an_ssh_agent` | Fixed, passes |
| Tests still expecting the old pages | `test_end_to_end_competition_journey`, `test_progression_page_and_navigation` | Updated, pass |
| Failed, then passed unchanged | `test_progression_data_for_the_team` | Cause unknown |
| Setup errors, passed on their own | 5: both in `test_devbox.py`, `test_loop_runs_a_job_and_writes_the_result`, `test_devbox_reset_is_post_only_and_for_known_teams`, `test_join_and_leave` | Cause unknown |
| Setup errors, not re-run | 13: 5 in `test_uploads.py`, 5 more in `test_round_agent.py`, 3 more in `test_team_registration.py` | Cause unknown |

The reasons for the errors were not kept: that run saved only the test names, so keep the full output next time. The tests use their own temporary database, so dashboard activity could only have affected them through load. Three key-push jobs were queued during that run, and two of them ran.

## Housekeeping

- [ ] **Leftover test databases.** Four temporary PostgreSQL servers from interrupted test runs are still running, about 25 MB each, with their directories in `/tmp`. They are harmless. A safety check refused the cleanup, so it has to be run by hand, when no tests are running:

  ```sh
  ps -eo pid,ppid,comm,args | awk '$2==1 && $3=="postgres" && /\/tmp\/worms-pg-/ {print $1}' | xargs -r kill; rm -rf /tmp/worms-pg-*
  ```

- [ ] **Stray empty file `tile`** in the repo root. Not tracked by git. Delete it if it is not yours.
- [ ] **Local settings not to commit:** `opentofu/shared/shared-variables.tf` and `opentofu/stacks/private_internet/main.tf`. They differ from the repository on purpose.
- [ ] **Connection-tracking settings do not hold.** The role sets `nf_conntrack_max` and the established-connection timeout, but the live values stay at Proxmox's defaults. Proxmox's firewall is the likely cause. Not urgent.

## For reference: decided and done

- **Apt:** devbox and sandbox VMs and their gateways use `apt-cache.worms.internal`. Private-internet VMs keep `10.2.0.20`, because their firewall allows the cache but not DNS (`pi_apt_client`). A name there would leave them with no packages.
- **`ID` in the download commands** is a placeholder for a round's number, which `GET /api/v1/rounds/` lists. The Progression page fills in the selected round's number. The dev view needs none: `/api/v1/devbox/progression.zip`.
- **Worm file modes:** the zip's file modes are not kept, so anything `main.sh` starts must be run through its interpreter or made executable first. See `docs/worm-contract.md`.
