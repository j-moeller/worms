# Merge review: production (33dae12) → main (21976d9)

As of 2026-11-16. Production runs `33dae12` ("worms build database: the orchestrator migrates first when it must", 2026-11-09). `main` is 20 commits ahead (15 without the merges), 90 files. This page says what a deploy changes, how the database migrates, what has to be done together, and how to go back.

**Verdict.** The deploy is safe in one maintenance window outside a round. All eight migrations add something; none removes or rewrites data. They take seconds. Five things need care:
1. **Deploy everything together**, then **restart the round agent** by hand. An agent left running keeps the old gameplan code while the sandbox playbooks on disk are new, and round_one's password login would silently stop working ([Old and new side by side](#old-and-new-side-by-side)).
2. **Take a database dump first.** Nothing backs up the database ([6](06-background-processes.md#backups)).
3. **Round URLs change.** Rounds, access tokens and SSH keys are now addressed by a UUID instead of a number, in the dashboard and in the team API.
4. **The private internet needs an internet window** (`--packages`) for the new apt packages and the larger devpi seed. The devboxes are off while it runs.
5. **Going back** to `33dae12` needs one SQL statement first ([Rollback](#rollback)).

## What changed since production

| Area | Change | Deployed by |
|---|---|---|
| Database | Migrations `0010`–`0017`: a UUID for rounds (`0010`–`0012`), access tokens and devbox SSH keys (`0013`–`0015`); the host job kind `read_file` (`0016`); the table `gameplan_simulations` (`0017`). The orchestrator may insert into and delete from that table | the orchestrator (migrations), then the database's grants play |
| Dashboard | Round, progression, token and SSH key URLs use the UUID; **Staff → Simulations**; **Simulate** on a round's page; the round badge in the Dev view; a favicon | `dashboard` |
| Orchestrator | The simulation API (`/gameplans/<file>/simulation/`), run in a child process with limits ([5](05-live-environment.md#simulation)); the conductor's `read_file` step | `orchestrator` |
| Gameplan framework (`src/gameplan`) | Documents, archives, KeePass vaults, TOTP and telnet logins, `read_file`, simulation. Only additions: `round_one.py` is unchanged and loads as before | `orchestrator` (its copy) and `round-agent` (the node's) |
| Round agent (node) | The `read_file` job; new secret kinds; `python3-cryptography` (KeePass files) | `round-agent`, **plus a restart** |
| Sandbox VMs | `sandbox_vm` takes its password logins from a new key, `ssh_password_users`, that only the new `build.py` writes | the round agent, at the next sandbox build |
| Sandbox gateway | Resolves `worms.internal` through the private DNS ([`main.tf`](../opentofu/stacks/sandbox/main.tf)) | the round agent, at the next sandbox build |
| Gameplans | A second example, [`credential_chain.py`](../gameplans/credential_chain.py). It needs packages the apt cache does not hold yet | `orchestrator`, `round-agent`; packages: `apt-cache --packages` |
| apt cache | New seed packages (`inetutils-inetd`, `inetutils-telnetd`, `libpam-google-authenticator`). Each online seed now drops and refetches the cached indexes (no lasting "Hash Sum mismatch"). Clients no longer fetch translations | `apt-cache --packages` |
| devpi | 116 seed packages (some 650 MB) instead of one; downloaded in batches into `/var/tmp` | `devpi --packages` |
| Devboxes | Time from `ntp.worms.internal`. The pip index ends where production has it (`root/pypi`): GitLab's `fix/devbox-pip-ntp` moved it to a `root/offline` that does not exist, and `fix/devpi-seed-common-packages` moved it back | each devbox's next reset |

Unchanged: the platform and firewall stacks, the node's network, DNS, syslog, scoring, logging, the monitor, `group_vars` and the vault (no new secret), and the Django apps' Python requirements.

## The migrations

Production's database is at `0009_gameplans_cleanup`. The orchestrator applies the eight migrations as `django_migrator`, with its services stopped. Then the database's grants play runs ([3](03-manage-py.md#worms-build)).

| Migration | What it does | Data |
|---|---|---|
| `0010_round_uuid` | `rounds.uuid`, nullable | — |
| `0011_round_uuid_data` | A random UUID for each round | one `UPDATE` per row |
| `0012_round_uuid_required` | `NOT NULL`, `UNIQUE` | — |
| `0013_token_key_uuid` | `access_tokens.uuid`, `devbox_ssh_keys.uuid`, nullable | — |
| `0014_token_key_uuid_data` | A random UUID for each token and key | one `UPDATE` per row |
| `0015_token_key_uuid_required` | `NOT NULL`, `UNIQUE` | — |
| `0016_hostjob_read_file` | A new choice for `host_jobs.kind` | no SQL at all |
| `0017_gameplan_simulations` | The table `gameplan_simulations` | — |

- **Size.** Only `rounds`, `access_tokens` and `devbox_ssh_keys` change: tables of tens to hundreds of rows. The large tables (flows, logs, events) are not touched. Each migration is its own transaction and locks only its table, for a moment.
- **Reversible.** Every migration can be undone. Undoing `0011` and `0014` does nothing, since the columns they fill are dropped by undoing `0010` and `0013`.
- **The UUID columns have no database default.** Django sets the value. Code that does not know the column (production's) therefore cannot insert a round, token or key into the new schema. See the next section.
- **Grants.** The dashboard's `INSERT` on the three tables covers the whole table, so the new column needs no grant. `SELECT` on `ALL` covers `gameplan_simulations` once the grants play has run; it runs after the migrations in the same build.

**If `0012` or `0015` stops** with a duplicate key on `uuid`: the old dashboard inserted two rows into one table between `0011`/`0014` and the next migration, a gap of about a second. Django gives every row still empty the same UUID, which the unique constraint refuses. The run stops there; the earlier migrations stay applied. Fill the empty rows on the database VM, then build again:

```sql
UPDATE rounds SET uuid = gen_random_uuid() WHERE uuid IS NULL;
UPDATE access_tokens SET uuid = gen_random_uuid() WHERE uuid IS NULL;
UPDATE devbox_ssh_keys SET uuid = gen_random_uuid() WHERE uuid IS NULL;
```

### Rehearsed

On a scratch PostgreSQL 17 on worms-dev, with production's checkout and `main`:
1. **Production's state.** Production's code migrated a fresh database to `0009`. Rows were then added with production's own fixture ([`setup.sql`](../ansible/files/simulation/setup.sql) as of `33dae12`) and production's models: rounds, runs, VMs, tokens, a key.
2. **Forward.** `main` listed exactly `0010`–`0017` as pending and applied them in 2–3 s. Every row was kept and got its own UUID. `migrate --check` passed.
3. **Production's code on the new schema** could not create a round, a token or an SSH key (`null value in column "uuid" ... violates not-null constraint`). `main`'s code could.
4. **Rollback option 1** (below): with a database default on the three UUID columns, production's code created all three. Its `migrate --check` and `migrate` saw nothing to do. `main` still found its models and migrations in agreement.
5. **Rollback option 2:** `main`'s code migrated back to `0009`, dropping the columns and the table. Production's code then saw nothing to migrate and created rows again. Migrating forward again worked.

Not rehearsed: a deploy on the real platform, and production's row counts.

## Old and new side by side

During and after the deploy, old and new parts can meet:

| Combination | What happens | How long |
|---|---|---|
| Old dashboard, new schema | Creating a round, an access token or an SSH key fails (500). Everything else works | Within one build: until the dashboard's restart at the end of the Django play, about a minute. **For good** if only the orchestrator is built (`worms build orchestrator`) |
| New dashboard, old schema | Pages answer 500 (`column ... does not exist`) | Prevented: `worms build dashboard` brings the database and the orchestrator into the plan while migrations are pending ([`components.py`](../src/worms_cli/components.py), `plan`) |
| New orchestrator, old round agent | A gameplan using `read_file` fails its attempt: the agent does not know the job. Gameplans using the new contents (such as `credential_chain.py`) are invalid on the node | Until the agent is rebuilt and restarted |
| Agent's code synced, agent not restarted | **The dangerous one.** The running agent keeps the old `src/gameplan` in memory, but runs the new `sandbox_vm` role from disk. The old `build.py` writes no `ssh_password_users`, so the new role lets no user log in with a password. round_one's `vm2` login stops working, and its flag cannot be reached. Nothing reports an error | Until `systemctl restart worms-round-agent`. Syncing never restarts the agent ([`workspace.yml`](../ansible/roles/host/round_agent/tasks/workspace.yml)), and `worms build round-agent` restarts it only when its unit or environment file changes. Neither does here |
| Full role not run on the node | `python3-cryptography` missing: a gameplan writing a KeePass file fails on the node. The tag-limited sync (`agent-sync`, run by other builds) does not install it | Until `worms build round-agent` |
| New dashboard, old orchestrator | Staff → Simulations answers 502 (the orchestrator lacks the API) | Not in the order below |

## Procedure

1. **Pick the window.**
   - No round running, and none starting within the next hour or so. A deploy during a round costs the running team an attempt ([6](06-background-processes.md#orchestrator-vm)).
   - Tell the teams: the devboxes are off during step 7; the dashboard is briefly unavailable in step 5; round links and the team API's round ids change ([What teams and staff notice](#what-teams-and-staff-notice)).
2. **Check the platform.**
   - `worms status`: no sandbox VM exists, and the internet window is closed. A leftover sandbox blocks step 7 and would be reused by the next round of the same gameplan.
   - `worms status rounds`, `worms status jobs`: nothing running or queued.
3. **Update the controller's checkout.**
   - `git status` must be clean. The node's workspace is rsynced from the working tree, uncommitted edits included ([`round_agent/defaults`](../ansible/roles/host/round_agent/defaults/main.yml)).
   - Then `git pull --ff-only` to the commit being deployed (`21976d9` or later; write it down).
4. **Dump the database.** `worms login database`, then `sudo -i` and:
   ```sh
   df -h /var/lib/postgresql   # the dump needs room; network_flows has no retention
   sudo -u postgres pg_dump -Fc -f /var/lib/postgresql/mydb-before-21976d9.dump mydb
   ```
   Copy it off the VM if the database disk itself is the worry.
5. **Plan, then build** the database, orchestrator, dashboard and round agent:
   - `worms build --plan dashboard round-agent`. Expect `database` and `orchestrator` with "configure again: the database lacks migrations", a note naming the 8 migrations, `dashboard` and `round-agent`.
   - `worms build dashboard round-agent`. One Ansible run configures the database, orchestrator and dashboard together: the orchestrator migrates (its services stopped), the dashboard restarts, the grants are applied, the conductor restarts. Then the node's play: workspace, `python3-cryptography`, the unit.
6. **Restart the round agent** on the node, while `worms status jobs` shows nothing running, **then the conductor** on the orchestrator (`worms login orchestrator`):
   ```sh
   systemctl restart worms-round-agent
   journalctl -u worms-round-agent -n 20
   ```
   ```sh
   sudo systemctl restart orchestrator-conduct-rounds
   sudo journalctl -u orchestrator-conduct-rounds -n 20   # "version ... recorded" for each gameplan
   ```
   The conductor restarts during the build right after the migrations, before the grants play gives it the new tables. Its first gameplan check then fails (`permission denied for table gameplan_versions`), and the next one comes 10 minutes later. Restarting it, after the agent, makes it check at once, against an agent that lists every file. Seen on worms-dev on 2026-11-16: the agent still running then predated `credential_chain.py` and left it out (`the node has no such file`).
7. **Fill the private internet:** `worms build apt-cache devpi --packages`.
   - This opens the internet window and shuts the devboxes down. Both VMs are rolled back to `clean`, seeded, put back into offline mode and snapshotted as `clean` again.
   - The apt cache refetches its indexes and the new packages. devpi fetches 116 packages in 10 batches. On worms-dev, the batches took 4–5 minutes straight from PyPI; through devpi it takes longer.
   - Needed before a round uses `credential_chain.py` or any telnet or TOTP login, and for the devboxes' larger pip seed. round_one needs nothing new: this step can wait for a quieter day.
8. **Check.**
   - `worms status`: everything up, the internet window closed.
   - **Staff → Rounds** opens. A round's URL is now `/staff/rounds/<uuid>/`.
   - **Staff → Simulations** lists the gameplans; round_one simulates as `ok`.
   - As a team member: create and revoke an access token; add and delete a devbox SSH key. These write the new columns.
   - `worms status gameplans`: round_one is valid on both sides. The node's view refreshes on the conductor's next check (every 10 minutes). `credential_chain.py` is valid only after step 7.

The rest applies by itself:
- **The sandbox gateway's DNS** comes with the next sandbox build: the first attempt of the next round.
- **The devbox time sync** comes with each devbox's next reset (`worms reset devbox <team>` resets one by hand).

## What teams and staff notice

- **Links.** Old links to a round (`/staff/rounds/12/`, `/progression/rounds/12/...`) answer 404. Staff and teams open rounds from their lists again.
- **Team API.**
  - `GET /api/v1/rounds/` now gives each round's `id` as a UUID, and every `/api/v1/rounds/<round id>/...` URL takes that UUID ([8](08-user-guide.md)). Scripts that take the id from the list keep working; scripts with a numeric id written in fail with 404.
  - Access tokens themselves are unchanged and keep working.
- **Downloaded replays** made before the deploy still open in the player. Their numeric round id is dropped; nothing else changes.
- **New for staff:** **Staff → Simulations**, and **Simulate** next to a round's gameplan.
- **A new choice:** `credential_chain.py` is an example. Production offers it as a gameplan as soon as it is deployed. To keep it out of production, remove it in a commit before deploying.

## Rollback

From the dump back is the last resort: it loses everything since step 4. Two lighter ways, both rehearsed:

**Option 1: keep the new schema** (recommended; nothing is lost).
1. Give the three UUID columns a database default, on the database VM (`sudo -u postgres psql mydb`):
   ```sql
   ALTER TABLE rounds ALTER COLUMN uuid SET DEFAULT gen_random_uuid();
   ALTER TABLE access_tokens ALTER COLUMN uuid SET DEFAULT gen_random_uuid();
   ALTER TABLE devbox_ssh_keys ALTER COLUMN uuid SET DEFAULT gen_random_uuid();
   ```
2. Check out `33dae12`, run `worms build dashboard round-agent`, then restart the round agent (step 6).

Production's code ignores the new columns and `gameplan_simulations`, and creates rounds, tokens and keys again. The UUIDs stay, so moving forward later keeps the new URLs. The defaults can stay; Django does not notice them.

**Option 2: back to production's exact schema** (drops the UUIDs and the simulations).
1. Still on `main`'s code, on the orchestrator, stop its services. Then migrate back with the command line from [3](03-manage-py.md#djangos-management-commands):
   ```sh
   systemctl stop orchestrator orchestrator-conduct-rounds orchestrator-process-attempts
   cd /opt/orchestrator/src && set -a && . /etc/orchestrator/django.env && set +a && /opt/orchestrator/venv/bin/python manage.py migrate core 0009 --database=migrator
   ```
2. Check out `33dae12`, run `worms build dashboard round-agent`, then restart the round agent.

A later deploy gives every row a new UUID, so links handed out meanwhile change once more.

**The private internet** needs no rollback: production's code works with the larger caches.

## Since this review

**The gameplan framework's users, FTP and local logins, and removals** (after `21976d9`) change nothing in the procedure above. There is no migration and no new secret. The changes:
- `apt_cache_seed_packages` lists `vsftpd`. Step 7 fetches it; it is needed only by a gameplan with an FTP login.
- The sandbox build ([`sandbox_vm`](../ansible/roles/applications/sandbox_vm/tasks/main.yml)) now removes packages after the setup commands, not before them. It also refuses a removal that would take a package every VM needs.
- Telnet admits only the users of telnet logins.

**A round page with the gameplan's description** adds migration `0018_gameplan_description`: a `description` column on `gameplans`, with a database default of `""`.
- Deploying today's `main`, the plan in step 5 names 10 migrations (`0010`–`0019`), not 8.
- Code that does not know the column keeps working: production's orchestrator before the deploy, and after a rollback. Option 2 of the rollback migrates back over it like the others.
- The orchestrator fills the column on its first gameplan check after the deploy. Until then every round page shows no description.

**Gameplan versions** add migration `0019_gameplan_versions` ([5](05-live-environment.md#versions)): `gameplan_versions` and `gameplan_changes` (append-only, the bytes readable by the orchestrator and the admin only), a version on each file and each run. The description moves from the file to its version.
- **After the deploy:** the first check records the version of each file, with its bytes. It also links every run already pinned to a SHA-256 that one of those files has. A run whose file has changed since stays known by hash only.
- **Rollback option 1** works as described: production's code does not know the new tables and columns, and every new column may be null.
- **Rollback option 2** migrates back over `0019` and drops the version record. The dump from step 4 holds the runs; the bytes of what was recorded since are lost with the tables.

**Rounds that wait alone, and their progress for staff**, need no migration and no new grant. The database schema does not change.
- **The conductor's order.** It runs one round at a time: the round under way first, then the next that can begin, by start. Before, it went by round id (creation order). A round without a valid gameplan no longer blocks the rounds after it: it waits alone and is looked at every 60 s (`ROUND_GAMEPLAN_POLL`).
- **The conductor's only new write.** `rounds.gameplan_checked_at` on each look at a waiting round, a column it may already update.
- **The dashboard.** The staff pages' warnings and progress panel only read `round_runs`, `run_events`, `host_jobs`, `flag_captures` and `virtual_machines`, which its role already reads.
- **Before the deploy.** Look at **Staff → Rounds** on production for a round whose start has passed with runs still pending. Under the new order, a round created later but starting earlier can come first.
- **The editor.** A round whose first team was skipped (no upload) before its gameplan was chosen can now still be given one. Before, the skip locked the round.

**Time and log limits** (S28 in [7](07-security.md)) add the playbook `SetupTime.yml` and the package `chrony` on every platform VM. They need no migration, no secret and no firewall change.
- **Install.** chrony comes during a build with the internet window open. A component build without it (`--skip-tags packages`) leaves a VM on its old clock until the next such build.
- **The first sync steps every platform VM's clock.** On worms-dev they ran 39 s ahead, so time jumped back once. On production it may be more or less. Deploy outside a round, as for every deploy.
- **Team VMs and gateways** get the private NTP server with the next sandbox build (`SetupSandbox.yml`) and `worms build devbox` (`SetupDevbox.yml`).
- **Logs.** rsyslog's files on every platform VM rotate past 100 MB, checked hourly (role `log_limits`, a third play of `SetupAudit.yml`).
- **DNS.** BIND's logs are bounded files (S8 reopened). The DNS VM's disk had filled with failed lookups: check `df -h /` there before the deploy.

**`worms rsync`** ([3](03-manage-py.md#see)) is the controller's CLI. Devboxes need rsync for it: it is a new devbox package and apt cache seed. The apt cache fetches it in step 7 (`apt-cache --packages`); existing devboxes install it with `worms step devbox-configure`, new ones when they are built.

**Larger seeds.** The apt cache seeds some 50 more common tools (git, build-essential, gdb, tmux, nmap, tshark, ...): 353 packages and 294 MB with their dependencies, up from 155 and 116 MB. devpi seeds 66 more (182): readers for what gameplans hand out (pyotp, pykeepass, python-docx, odfpy, pyzipper, py7zr, standard-telnetlib), build backends for offline sdists, common libraries, and tensorflow-cpu and torch (with torchvision, torchaudio and, from PyPI, the CUDA libraries: some 3.5 GB in all). Step 7 fetches them, devpi in 16 batches instead of 10, each deleted once fetched, on devpi's data disk. Sandbox VMs reach both caches: a worm running as root on one can install what they hold.
- **worms-dev only:** devpi's leftover index `root/offline` and `.wheelhouse-import` go once `root/pypi` holds every project of both (checked after the seed; otherwise they stay and the build names what is missing). Production has neither.
- **devpi's data disk: 20 GB** (from 10): the seed is some 4.5 GB, and the first one runs next to the old leftovers and its own scratch. OpenTofu grows it in place; the role grows the partition and file system (`grow_disks`). apt-cache keeps 20 GB: its seed is some 400 MB.
- **Did the seed run?** After a successful seed, apt-cache and devpi hold the list they fetched (`/etc/apt-cache-package-list`, `/etc/pypi-package-list`) and the commit it came from (`/etc/seed-version`; `-dirty` when the role had uncommitted changes). An older commit there means the last seed failed or did not run.
- **The serial getty.** Every VM's getty on ttyS0, which no VM has, restarted every 10 s. Platform VMs mask it with SetupAudit.yml; devboxes with `worms step devbox-configure` or their next build, sandboxes at their next build.

None of this alters round_one or `credential_chain.py`: neither removes a package, and `credential_chain.py`'s only telnet user is its telnet login. The round agent has to be restarted after the deploy (step 6) all the more: the new `sandbox_vm` role reads `accounts` and `purge`, which only the new `build.py` writes.

## Worth doing, not needed for this deploy

- **Restart the round agent when its code changes.** Neither the sync nor `worms build round-agent` does it today. A handler on the sync task would remove step 6 and the risk above.
- **A database default for the UUIDs** (`db_default`, `gen_random_uuid()`). It would make the deploy gap harmless and rollback option 1 unnecessary.
- **Database dumps** on a schedule (S1 in [7](07-security.md)).
