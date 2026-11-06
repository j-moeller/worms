# Managing the platform

Two tools:

- **The `worms` CLI** (`src/manage.py`), for anything that needs the Proxmox node: builds, devboxes, the admin and staff accounts, status.
- **The dashboard's staff pages** (`/staff/`), for anything that only changes the database: teams, students, rounds.

**What has run on this node:** the build, accounts, devbox builds, keys, SSH, dev uploads and resets by replacing VMs (`docs/archive/`). **What has not:** any round or sandbox, any snapshot or rollback (of the services, a sandbox or a devbox), and the commands marked "No" under [Stop and remove things](#stop-and-remove-things).

## Quick reference

| Task | Command or page |
|---|---|
| See what is running | `worms status` |
| Build everything | `worms build` |
| One-time password for the admin | `worms admin reset-password` |
| Create teams, then users | `/staff/teams/`, then `/staff/users/` |
| Build the devboxes | `worms devbox provision` |
| Create a round | `/staff/rounds/`. It starts by itself at **Starts** |
| See the results | `worms leaderboard` |
| Reset the services between rounds | Nothing: each round does it. By hand: `worms build round-reset-services` |
| Clear the database | `worms reset-database --yes` |

The whole devbox journey step by step: `docs/manual-devbox-run.md`.

## Before you start

On the node:

```sh
alias worms='/root/app/.venv-test/bin/python /root/app/src/manage.py'
```

- The database commands need Django and psycopg2, which only `.venv-test` has (`worms test` creates it).
- `worms --help` groups the commands by purpose; every command has its own `--help`.
- Every command starts with `internet access: ● denied`. If it reads `ALLOWED` with no build running, run `worms build internet-deny`. `unknown` means the node could not be asked.
- `[PRODUCTION] We are running in PRODUCTION mode` at the start is expected.

**The dashboard** is `https://worms-dev.mlsec.tu-berlin.de/` on `worms-dev` and `https://worms.mlsec.tu-berlin.de/` on `worms-pve`. Open it from outside the node (the port forward only matches outside traffic). It answers by name only (the IP gets `400`), its certificate comes from the internal CA, and **all times on the staff pages are UTC**.

## Build and deploy

```sh
worms build                                 # everything, end to end (= make). Safe to repeat
worms build --list                          # the individual steps
worms build provision                       # Ansible only, on VMs that already exist
worms build provision -- --limit dashboard  # one VM, about 4 minutes
```

The build covers the database: it configures the database VM and its roles, the orchestrator runs the migrations, and `init` creates `admin` (no password) and the competition `WORMS <year>`. Then `worms admin reset-password` prints a one-time password, once.

**A build keeps the service VMs' snapshot current.** The service VMs are scoring, logging, the monitor, DNS and the private internet. Their snapshot `clean` is what a reset returns them to. A build rolls them back to it before configuring them and takes it again afterwards, shutting each down for a moment. A service VM that was up without a snapshot gets none, because team VMs may have reached it: the build says so, and `worms build rebuild-services` makes one.

**A build opens the internet window, so it stops the devboxes.** It is refused while any sandbox VM exists (a round). Running devbox VMs are shut down until the new snapshot is taken, then started again, so **teams lose their devbox for the length of a build**. The window closes by itself, also on failure. If it cannot, the devboxes stay off and the build names the command that starts them.

**To redeploy only the Django code without stopping the devboxes** (no internet window, no package steps, about 3 minutes, restarts the dashboard and orchestrator):

```sh
cd /root/app/ansible
ssh-agent sh -c 'ssh-add -q /etc/worms/ssh/worms && WORMS_SSH_AUTH_SOCK="$SSH_AUTH_SOCK" exec ansible-playbook -e @group_vars/vault.yml SetupDbAndDashboard.yml --limit orchestrator,dashboard --skip-tags packages'
```

Keep `--skip-tags packages`: without internet, `pip` hangs for minutes. If the change adds a table or a grant, apply the grants too, then check the grant itself on the database VM (the play's `changed` count has been wrong):

```sh
ssh-agent sh -c 'ssh-add -q /etc/worms/ssh/worms && WORMS_SSH_AUTH_SOCK="$SSH_AUTH_SOCK" exec ansible-playbook -e @group_vars/vault.yml SetupDbAndDashboard.yml --limit database_hosts --start-at-task "List the tables in the public schema"'
sudo -u postgres psql -d mydb -c "SELECT grantee, privilege_type FROM information_schema.role_table_grants WHERE table_name = '<table>'"
```

Afterwards: `worms status`, `worms secrets check` (do the Proxmox tokens work, with exactly their rights?), `worms build ping` (can Ansible reach every VM?).

## Accounts and teams

| Account | How |
|---|---|
| Admin | `worms admin reset-password` |
| Staff (tutor) | `worms admin create-staff tutor@example.org` (`--first-name`, `--last-name`) |
| Team | `/staff/teams/`: **Name**, **Members at most** (default 2, up to 20) |
| Students | `/staff/users/`: one by one, or paste a CSV and press **Import** |

The CLI cannot create teams or students; use the dashboard. The CSV (German or English headers: `email`, `first name`, `surname`, `team`):

```csv
Vorname,Nachname,E-Mail,Team
Anna,Alpha,anna.alpha@example.org,alpha
```

The team must exist and have room; an existing email is left unchanged. **Copy the one-time passwords from the page that follows**: they are not shown again, and nothing emails them. Hand each over a secure channel. Optional: a registration window on `/staff/` lets students join teams themselves.

Later changes are on the user's page from `/staff/users/`: **New one-time password**, **Set team**, **Deactivate**. Accounts are deactivated, not deleted. Check with `worms teams` and `worms users`. To remove a team with everything it owns: `worms teams delete "Neural Net"` (its users stay, without a team).

## Devboxes

```sh
worms devbox provision         # one host job; the round agent on the node builds every team's devbox
tail -F /var/log/worms/jobs/latest.log
```

About 17 minutes for 3 teams (13 VMs: 3 VMs and a gateway per team, plus the shared SSH gateway). If the job fails with `got timeout`, run it again (the disk timeout F5, `docs/status.md`). Run it again after adding a team, **but not while teams are working**: it writes new tokens on every team's VMs.

Teams do the rest in the dashboard's **Dev** view:

| Action | Where | Then |
|---|---|---|
| Add an SSH key | `/profile/`, **SSH keys** | A `devbox_reconfigure` job installs it, in about 3 minutes |
| Connect | Panel **Connect**: `ssh -A -J team@<public name>:2222 root@<A>`, then the hops to B and C | On the node itself: `ssh -A -J team@10.7.0.2 root@<A>` |
| Reset | **Reset devbox**, **Yes, rebuild my devbox** | A `devbox_rebuild` job rolls the team's 3 VMs back to their snapshot and writes the keys and new tokens. Replacing them took 8 minutes; the rollback should save about 3.5 (estimate). VMs built before snapshots are replaced once more, then snapshotted |
| Upload a worm | `/upload/` in the Dev view | Resets the devbox, then runs `main.sh` on A for up to an hour (`docs/devbox.md`) |

`worms devbox reconfigure` pushes every team's keys and the devbox map again.

## Rounds

**Never run on a real node.** Also, a worm has no designed way from one sandbox VM to the next: seeded vulnerabilities are not built, so a worm can capture only its start VM (`docs/status.md`).

On `/staff/rounds/`, fill in **New round**. Nothing is built until it starts.

| Field | Default | Allowed | Meaning |
|---|---|---|---|
| Starts / Ends | | Future / after Starts | UTC. **Starts** is also the upload deadline |
| Seed | Random | 0 or more | The sandbox and each team's game plan derive from it |
| VMs / Networks / Sites / Interfaces per VM | 8 / 4 / 1 / 3 | 1–250 / 1–30 / 1–8 / 1–8 | The round's sandbox |
| RAM per VM (MB) | 512 | 256–8192 | A ceiling; the host reclaims what a VM does not use |
| Fuse / Run length (min) | 5 / 60 | 0–1440 / 1–1440 | Deploy to start; start to end |
| Secrets up front | 0.5 | 0–1 | Share of the other VMs whose flag exists from the start |
| First VM stop / then every (min) | 10 / 10 | | The start VM is switched off first |
| VMs kept running | 2 | 0–250 | |
| Restart a stopped VM after (min) | Empty | 1–1440 | Empty: stopped VMs stay off |

Runs are one at a time, so the window must fit every team: per team, fuse + run length + 2 minutes drain + 15 minutes build and teardown. The defaults need **82 minutes per team**. The form refuses a Live round that is too short or overlaps another.

**There is no start button.** At **Starts** the conductor plans one run per team in a seed-shuffled order, using each team's latest Live upload before **Starts**; a team without one, or a run that no longer fits, is skipped. **The first run builds the round's sandbox:** the agent shuts the devboxes down, rolls the service VMs back to their snapshot, builds and snapshots the sandbox, and starts the devboxes again. Later runs roll the sandbox back; the last one destroys it.

```
pending → preparing → checking → deploying → armed → running → ending → tearing_down → finished
```

A run can also end `skipped` or `failed` (its VMs stopped, then torn down). Before **Starts** a round can be changed or deleted; afterwards only **Ends** moves. **There is no pause or abort**: moving **Ends** earlier stops later runs, not the current one. The only hard stop is `worms emergency-stop`, which stops every VM on the node.

Watch on `/staff/rounds/` (**Right now**, and each round's runs with **Play**), or from the node:

```sh
systemctl is-active worms-round-agent
tail -F /var/log/worms/jobs/latest.log
ssh -i /etc/worms/ssh/worms debian@10.3.0.19 'sudo journalctl -u orchestrator-conduct-rounds -u orchestrator-process-attempts -f'
```

### Between rounds

Nothing to do: the next round's first run rolls the service VMs back first, and its prepare job reports it under `services`. Without a snapshot on every service VM it leaves them as they are and says so. By hand, outside a round: `worms build round-reset-services`.

| | |
|---|---|
| What | Rolls the 11 service VMs back to `clean` and starts them; about 2 minutes (estimate) |
| Needs | No internet window, so devboxes can stay. Refused during a round, and does nothing unless every one of them has a snapshot |
| Kept | The database, the dashboard, the orchestrator, the syslog VM's logs |
| Lost | Whatever exists only on those VMs, such as flows not yet shipped |

Scoring, logging and DNS are down meanwhile. **The devbox map comes back afterwards:** `/etc/sandbox-api/devbox-teams` on scoring and logging tells them which devbox gateway address is which team; every devbox job writes it, so a rollback loses it. The agent writes it again at once from its copy (`/var/lib/worms/devbox-teams`); after a reset by hand or a build, the CLI enqueues a `devbox_reconfigure` (or tells you to run `worms devbox reconfigure`).

To make a new snapshot outside a build:

| Command | When |
|---|---|
| `worms build rebuild-services` | Destroys the service VMs, builds them from nothing and snapshots them. Needs the internet window (devboxes off), 20–60 minutes. **Once on a system whose service VMs ran without a snapshot, such as this one** |
| `worms build snapshot-services` | Snapshots them as they are. Refused while any team VM exists. Only if no team VM has reached them since they were clean |

## Status and debugging

`worms status` shows the infrastructure live from Proxmox (firewall, bridges, VMs per group, internet access) and the competition from the database (phase, rounds, teams, active run, queued jobs).

| To see | Use |
|---|---|
| Teams, members, sign-ins / all users | `worms teams` / `worms users` |
| The leaderboard | `worms leaderboard` |
| Who signed in, changed a password, viewed as someone, logged in through the SSH gateway | `worms audit` (`docs/monitoring.md`, "Event tables") |
| Host jobs and the active run | `/staff/rounds/`, **Right now** |
| What the round agent is doing | `tail -F /var/log/worms/jobs/latest.log`; `journalctl -u worms-round-agent` |
| VMs stopped by the firewall watchdog | `journalctl -t worms-firewall-watchdog --since today` |
| A VM's snapshot | `qm listsnapshot <vmid>` |
| Which secrets are set / do the tokens work | `worms secrets` / `worms secrets check` |
| Can Ansible reach each VM | `worms build ping`; `worms debug <host>` steps through address, TCP 22, login, Python, sudo, cloud-init |
| One VM, verbose | `worms build provision -- --limit database -vvv` (`-e hide_secrets=false` shows `no_log` output: secrets!) |

**Job logs:** every host job writes every command, its exit code and duration, and the full OpenTofu and Ansible output to `/var/log/worms/jobs/<job id>.log` (`latest.log` follows the current one). Root only, kept 30 days. After a failure look there: the job's `error` holds only the start of stderr.

Read-only SQL through the CLI's connection: the `sql` helper in `docs/manual-devbox-run.md`, for example:

```sh
sql "SELECT left(uuid::text, 8) AS job, kind, state, started_at, finished_at, left(error, 100) AS error FROM host_jobs ORDER BY requested_at DESC LIMIT 10"
```

## Clean the database

```sh
/usr/local/sbin/worms-node-run opentofu/stacks/devbox 'tofu destroy -auto-approve -parallelism=1'   # first: a wipe leaves VMs running
worms reset-database --yes
worms admin reset-password
```

It deletes teams, users, sessions, tokens, captures, logs, rounds, runs, host jobs, devbox records and keys, then runs `init` again. It keeps the schema, every VM, and uploaded files on the dashboard VM. Not while a round or host job runs (`worms status`).

## Stop and remove things

From the narrowest to the widest. All run on the node.

| Command | What it does | Run on a real node? |
|---|---|---|
| `/usr/local/sbin/worms-node-run opentofu/stacks/devbox 'tofu destroy -auto-approve -parallelism=1'` | Removes the devbox VMs and the SSH gateway | Yes, 5 minutes |
| `/usr/local/sbin/worms-node-run opentofu/stacks/sandbox 'tofu destroy -auto-approve -parallelism=1'` | Removes the sandbox. Not during a round | No |
| `worms emergency-stop` | Hard-stops **every VM on the node**. Destroys nothing | No |
| `worms destroy` | Destroys the sandbox, devbox, infrastructure and private internet VMs | No |
| `worms destroy-all` | The same, plus the firewall and platform | No |
| `worms terminate` | Stops every VM, `destroy-all`, then destroys **every VM left on the node** | No |

Keep `-parallelism=1`: parallel destroys can leave a volume behind, and a new VM with that ID then fails with `dataset already exists`. The `worms` commands ask first unless given `--yes`; **`make destroy` and `make destroy_all` do not ask.** Leftover volumes (should print nothing):

```sh
for id in $(zfs list -H -o name -t volume -r rpool | grep -oE 'vm-[0-9]+-' | cut -d- -f2 | sort -un); do
  qm status "$id" >/dev/null 2>&1 || echo "leftover volumes of VM $id"
done
```

Remove one with `pvesm free local-zfs-sandbox:vm-118-cloudinit`.

## Other commands

```sh
worms secrets issue proxmox-token   # create or rotate a Proxmox token, and store it
worms secrets set ssh-key < key     # store a secret by hand
worms node trust                    # once, for VMs that predate the WORMS key
worms build internet-deny           # close the internet window by hand
worms test                          # the offline suite
worms simulate                      # the live check (docs/testing.md)
```

## Configuration

`~/.worms/config` (or `--config`, or the environment; `KEY=value` lines) says where the node is; every secret is in the vault (`docs/accounts.md` §4).

| Setting | Default | |
|---|---|---|
| `WORMS_SSH` | Empty: this machine is the node | `root@<node>` makes this machine the controller |
| `WORMS_PROXMOX_URL` | `https://<WORMS_SSH host>:8006/` | |
| `WORMS_REPO_ROOT` | The checkout the CLI is in | |

The database commands connect as `admin` over TLS checked against the internal CA, through an SSH tunnel when run from a laptop; `worms status` reads Proxmox with the topology token. A feature whose secret is missing says `n/a`. The sandbox and devbox stacks and their playbooks run in the agent's workspace `/opt/worms` through `worms-node-run`, after it is synced with this checkout. The CLI reuses the dashboard's models and scoring code, so `worms leaderboard` and the dashboard cannot disagree.
