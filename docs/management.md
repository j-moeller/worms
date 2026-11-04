# Managing the platform

You manage the platform with two tools:

- **The `worms` CLI** (`src/manage.py`), for anything that needs the Proxmox host: builds, devboxes, the admin account, status.
- **The dashboard's staff pages** (`/staff/`), for anything that only changes the database: teams, student accounts, rounds.

**Rounds and sandboxes have never run on a real node.** Sections on them come from the code and its offline tests. The commands marked as such under [Stop and remove things](#stop-and-remove-things) have not been run either. Everything else here ran on this node on 2026-11-04 (`docs/e2e-run-2026-11-04/protocol.md`).

## Quick reference

| Task | Command or page |
|---|---|
| See what is running | `worms status` |
| Build everything, including the database | `worms build` |
| Clear the database | `worms reset-database --yes` |
| One-time password for the admin | `worms admin reset-password` |
| Create teams | `/staff/teams/` |
| Create users | `/staff/users/` |
| Build the devboxes | `worms devbox provision` |
| Remove the devboxes | `/usr/local/sbin/worms-node-run opentofu/stacks/devbox 'tofu destroy -auto-approve -parallelism=1'` |
| Create and configure a round | `/staff/rounds/` |
| Start a round | Nothing to press: it starts at its **Starts** time |
| Watch a round | `/staff/rounds/` |
| See the results | `worms leaderboard` |

For the whole journey step by step, with every input and expected result, see `docs/manual-devbox-run.md`.

## Before you start

Run `worms` as root on the node, or from a laptop with `WORMS_SSH` set ([Configuration](#configuration)). On this node:

```sh
alias worms='/root/app/.venv-test/bin/python /root/app/src/manage.py'
```

- The database commands need Django and psycopg2. `.venv-test` has them, and `worms test` creates it.
- Every command has `--help`.
- `worms --dry-run <command>` prints what would happen and changes nothing. The flag goes before the command.
- `[PRODUCTION] We are running in PRODUCTION mode` at the start of the output is expected.

**The dashboard** is `https://worms-dev.mlsec.tu-berlin.de/` on `worms-dev`, and `https://worms.mlsec.tu-berlin.de/` in production.

- Open it from outside the node, because the port forward only matches outside traffic.
- It answers on HTTPS and by name only: the IP address gets `400`.
- Browsers warn about the certificate, because it comes from the internal CA.
- **All times on the staff pages are UTC.**

## Set up the platform and the database

```sh
worms build                                 # everything, end to end (= make). Safe to repeat
worms build --list                          # the individual steps
worms build provision                       # Ansible only, on VMs that already exist
worms build provision -- --limit dashboard  # redeploy one VM, about 4 minutes
```

The database has no setup command of its own, because the build covers it:

1. It configures the database VM and its roles.
2. The orchestrator runs the migrations.
3. `init` creates the `admin` account, with no password yet, and the competition `WORMS <year>`.

Then give the admin a password:

```sh
worms admin reset-password    # the last line is a one-time password, shown once
```

**These builds need the internet window, which is refused while any devbox or sandbox VM exists**, because neither may have internet access. Remove those VMs first ([Stop and remove things](#stop-and-remove-things)). The window closes again by itself, even if the build fails.

Afterwards, check with:

```sh
worms status
worms secrets check     # do the stored Proxmox tokens work, with exactly their rights?
worms build ping        # one line per VM: can Ansible reach it?
```

## Clean the database

```sh
# 1. Remove the devbox VMs first: a wipe leaves VMs running.
/usr/local/sbin/worms-node-run opentofu/stacks/devbox 'tofu destroy -auto-approve -parallelism=1'

# 2. Wipe. Without --yes it asks first.
worms reset-database --yes

# 3. The admin has no password after a wipe.
worms admin reset-password
```

| Deleted | Kept |
|---|---|
| Teams, users, sessions, tokens | The schema and migration history |
| Captures, logs, rounds, runs, host jobs | All VMs |
| Devbox records and SSH keys | Uploaded files on the dashboard VM (only their records go) |

It then runs `init` again, so `admin` and the competition `WORMS <year>` exist afterwards.

Do not wipe while a round or a host job is running. Check first with `worms status`, which lists queued jobs and active runs.

To remove just one team, with everything it owns:

```sh
worms teams delete "Neural Net"     # its users stay, without a team
```

## Create users and teams

| Account | How |
|---|---|
| Admin | `worms admin reset-password` |
| Staff (tutor) | `worms admin create-staff tutor@example.org`, optionally with `--first-name` and `--last-name` |
| Student | Dashboard: `/staff/users/` |
| Team | Dashboard: `/staff/teams/` |

**The CLI cannot create teams or students.** `manage.py create_users` exists on the orchestrator, but it ignores the team column, so use the dashboard.

Sign in as `admin` with the one-time password and choose a new one when asked. Then:

1. **Development competition.** On `/staff/`, press **Create it**. The page calls this optional, but Dev rounds need it, and every test run has done it. If teams already exist, press **Link every team** there too.
2. **Teams.** On `/staff/teams/`, enter a **Name** and **Members at most** (default 2, up to 20), then press **Create team**. Repeat for each team.
3. **Users.** On `/staff/users/`, paste this into **…or paste the CSV** and press **Import**. The team must already exist and have room, and an email that already exists is left unchanged.

   ```csv
   Vorname,Nachname,E-Mail,Team
   Anna,Alpha,anna.alpha@example.org,alpha
   Ben,Bravo,ben.bravo@example.org,bravo
   ```

   English headers work too: `email`, `first name`, `surname`, `team`. For one person, use the panel **New account** instead.
4. **Copy the one-time passwords** from the page that follows. They are not shown again. Hand each person their line over a secure channel.
5. **Optional: registration window.** On `/staff/`, set the window if students should join teams themselves. Without one, only staff can assign them.

Check the result:

```sh
worms teams     # each team with its members and who has signed in
worms users     # every user: team, login, whether the one-time password is still in use
```

**Later changes** are on the user's own page, reached from `/staff/users/`: **New one-time password**, **Set team** and **Deactivate**. Accounts are deactivated, not deleted, so their uploads and results stay attributable.

## Build the devboxes

A devbox is a team's own three VMs, A, B and C, for developing its worm (`docs/devbox.md`).

```sh
worms devbox provision    # enqueues a host job; the round agent on the node does the work
```

| | |
|---|---|
| **Takes** | About 17 minutes for 3 teams |
| **Builds** | 3 VMs and a gateway per team, plus one shared SSH gateway: 13 VMs for 3 teams |
| **If the job fails with `got timeout`** | Run the same command again. It is the disk timeout F5 (`docs/check-with-user.md`, 3d) |

Watch it with:

```sh
journalctl -u worms-round-agent -f
qm list | grep -c devbox
```

**Run it again after adding a team, but not while teams are working.** It writes new tokens on every team's VMs, not only the new team's (`_devbox_report` in `scripts/round_agent/handlers.py`). This comes from reading the code: no test has added a team to running devboxes.

**After that, teams do the rest on the dashboard**, in the **Dev** view:

| Action | Where | Then |
|---|---|---|
| Add an SSH key | Panel **SSH keys**, **Add key** | A `devbox_reconfigure` job installs it, in about 3 minutes |
| Connect | The command in panel **Connect** | Port 2222 is not published yet (`docs/check-with-user.md`, 3b). Until then, from the node: `ssh -J team@10.7.0.2 root@<team's A>` |
| Reset | **Reset devbox**, then **Yes, rebuild my devbox** | A `devbox_rebuild` job replaces that team's 3 VMs, in about 20 minutes. Other teams are not touched |

## Run a round

**Not yet run on a real node.** The conductor and agent are tested offline only (`docs/rounds.md`, "What is not done here"). Also, a worm has no designed way from one sandbox VM to the next yet, because seeded vulnerabilities are not built. So a worm can capture only its start VM's flag.

A round evaluates every team's worm, one team after another. Each team gets a freshly built sandbox and its own game plan.

### Create and configure

On `/staff/rounds/`, fill in **New round** and press **Create round**. Nothing is built yet.

| Section | Field | Default | Allowed | Meaning |
|---|---|---|---|---|
| Round | Environment | Live | Live or Dev | Dev is only a time window for the devbox progression: it builds nothing and ignores all fields below **Seed** |
| | Name | | | |
| | Starts | | In the future | UTC. Also the upload deadline |
| | Ends | | After **Starts** | UTC. Runs that cannot finish by then are skipped |
| | Seed | Random | 0 or more | Every team's sandbox and game plan derive from it |
| Each team's sandbox | VMs | 8 | 1–250 | |
| | Networks | 4 | 1–30 | No more than **VMs** |
| | Sites | 1 | 1–8 | |
| | Interfaces per VM at most | 3 | 1–8 | No more than **Networks** |
| | RAM per VM (MB) | 512 | 256–8192 | A ceiling: the host reclaims what a VM does not use |
| Each run | Fuse (min) | 5 | 0–1440 | From deploy to the worm's start |
| | Run length (min) | 60 | 1–1440 | From the worm's start to the end of its run |
| Game plan | Secrets up front | 0.5 | 0–1 | Share of the other VMs whose flag exists before the worm starts |
| | First VM stop (min) | 10 | 0–1440 | The start VM is switched off first |
| | Then a stop every (min) | 10 | 1–1440 | |
| | VMs kept running | 2 | 0–250 | Fewer than **VMs** |
| | Restart a stopped VM after (min) | Empty | 1–1440 | Empty: stopped VMs stay off |

**The window must fit every team, because runs happen one at a time.** The form refuses a Live round that is too short, or one that overlaps another round. One team's slot is:

> Fuse + Run length + 2 minutes to drain + 15 minutes to build and tear down

With the defaults, that is **82 minutes per team**, so 3 teams need at least **4 hours 6 minutes**.

### Start

**There is no start button.** A round starts by itself when its **Starts** time passes.

Before then:

1. **Teams upload their worm** on `/upload/`: a zip with `main.py` at its top level (`docs/worm-contract.md`). Each run uses the team's latest upload before **Starts**.
2. **Check that the services are up**, from the node:

   ```sh
   systemctl is-active worms-round-agent
   ssh -i /etc/worms/ssh/worms debian@10.3.0.19 'systemctl is-active orchestrator-conduct-rounds orchestrator-process-attempts'
   ```

At **Starts**, the conductor plans one run per team, in an order shuffled by the seed, and works through them. A team's run is skipped if the team has no upload, or if too little time is left for a whole run.

### Change or stop

| When | What you can do, on `/staff/rounds/<id>/` |
|---|---|
| Before **Starts** | Change anything except the environment, or delete the round |
| After **Starts** | Only move **Ends**, and delete only until the conductor has planned its runs |

**You cannot pause or abort a round from the dashboard.** Moving **Ends** earlier stops later runs from starting, but from the code, a run in progress carries on. The only hard stop is `worms emergency-stop`, which stops every VM on the node, including the database and the dashboard.

### Watch

`/staff/rounds/` has a panel **Right now**, with the active run, host jobs and recent failures. `/staff/rounds/<id>/` lists each team's run with its state, error and captures, and has **Play** to replay a run.

A run moves through these states:

```
pending → preparing → checking → deploying → armed → running → ending → tearing_down → finished
```

It can also end as `skipped`, or as `failed`, where the sandbox is torn down and the next run starts after that.

To follow the services themselves, from the node:

```sh
journalctl -u worms-round-agent -f                                                                 # host agent
ssh -i /etc/worms/ssh/worms debian@10.3.0.19 'sudo journalctl -u orchestrator-conduct-rounds -f'   # conductor
ssh -i /etc/worms/ssh/worms debian@10.3.0.19 'sudo journalctl -u orchestrator-process-attempts -f' # captures and logs
```

Results:

```sh
worms leaderboard
```

### Between rounds

To rebuild the ephemeral and private internet VMs for a clean slate:

```sh
worms build round-reset-services
```

It needs the internet window, so no sandbox or devbox VM may exist.

## See the status

```sh
worms status
```

```
── infrastructure
firewall            ● enabled
bridges / network   ● 18 Linux bridge(s)
infrastructure VMs  ● 8/8 running
private internet    ● 8/8 running
sandbox VMs         ○ none
devbox VMs          ● 12/12 running
internet access     ● denied

── competition (database)
competition  ● WORMS 2026
phase        ○ no active or upcoming round
rounds       ● 0
teams        ● 3
```

The infrastructure part comes live from the Proxmox API and the node. The competition part comes from the database, and also shows any active run, run counts by state, and queued host jobs. **`internet access` should read `denied`** except during a build. The SSH gateway counts as an infrastructure VM here.

| To see | Use |
|---|---|
| Teams, members, who has signed in | `worms teams` |
| All users and their login state | `worms users` |
| The leaderboard | `worms leaderboard` |
| Host jobs, including devbox jobs, and the active run | `/staff/rounds/`, panel **Right now** |
| All VMs on the node | `qm list` |
| Can Ansible reach each VM? | `worms build ping` |
| Which secrets are set, never their values | `worms secrets` |
| Do the Proxmox tokens work? | `worms secrets check` |
| What the round agent is doing | `journalctl -u worms-round-agent -f` |
| VMs stopped by the firewall watchdog | `journalctl -u worms-firewall-watchdog --since today` |

To query the database directly, use the read-only `sql` helper from `docs/manual-devbox-run.md`:

```sh
sql "SELECT left(uuid::text, 8) AS job, kind, state, started_at, finished_at, left(error, 100) AS error FROM host_jobs ORDER BY requested_at DESC LIMIT 10"
sql "SELECT round_id, position, team_id, state, left(error, 100) AS error FROM round_runs ORDER BY round_id, position"
```

## Stop and remove things

From the narrowest to the widest. All of these run on the node.

| Command | What it does | Run on a real node? |
|---|---|---|
| `/usr/local/sbin/worms-node-run opentofu/stacks/devbox 'tofu destroy -auto-approve -parallelism=1'` | Removes the devbox VMs and the SSH gateway. Leaves the database alone | Yes, 5 minutes |
| `/usr/local/sbin/worms-node-run opentofu/stacks/sandbox 'tofu destroy -auto-approve -parallelism=1'` | Removes the sandbox VMs. During a round the agent does this itself, so do not run it then | No |
| `worms emergency-stop` | Hard-stops **every VM on the node**, including the database and dashboard. Destroys nothing | No |
| `worms destroy` | Destroys the sandbox, devbox, infrastructure and private internet VMs. Keeps the firewall and platform | No |
| `worms destroy-all` | The same, plus the firewall and platform | No |
| `worms terminate` | Stops every VM, runs `destroy-all`, then destroys **every VM left on the node, whatever it is** | No |

- **Keep `-parallelism=1`.** VMs destroyed in parallel can leave a volume behind, and a new VM given that ID then fails with `dataset already exists`.
- The `worms` commands ask first, unless you add `--yes`. **`make destroy` and `make destroy_all` do not ask.**

To look for leftover volumes after a teardown, run this. It should print nothing:

```sh
for id in $(zfs list -H -o name -t volume -r rpool | grep -oE 'vm-[0-9]+-' | cut -d- -f2 | sort -un); do
  qm status "$id" >/dev/null 2>&1 || echo "leftover volumes of VM $id"
done
```

To remove one, for example `rpool/vm-118-cloudinit`, run `pvesm free local-zfs-sandbox:vm-118-cloudinit`.

## Other commands

```sh
worms secrets                       # the vault: what is set, never the values
worms secrets issue proxmox-token   # create or rotate a Proxmox token, and store it
worms secrets set ssh-key < key     # store a secret by hand
worms node trust                    # once, for VMs that predate the WORMS key
worms build internet-deny           # close the internet window by hand, if a build reports it still open
worms debug dashboard               # Ansible connection diagnostics, for one host or group
worms test                          # the offline test suite
worms simulate                      # the live simulation (docs/testing.md)
```

## Configuration

One settings file and the vault. `~/.worms/config` (or `--config path`, or the environment; plain `KEY=value` lines) says where the node is. Every secret is in the vault (`worms secrets`): the three Proxmox tokens you enter once, and everything else generated. The full reference is `docs/accounts.md`, §4.

| Setting | Default | Notes |
|---|---|---|
| `WORMS_SSH` | Empty: this machine is the node | `root@<node>` makes this machine the controller |
| `WORMS_PROXMOX_URL` | `https://<WORMS_SSH host>:8006/` | If the node's API has another name from here |
| `WORMS_REPO_ROOT` | The checkout this script is in | |

The database commands connect as `admin` with the vault's password, over TLS checked against the internal CA, and through an SSH tunnel via the node when run from a laptop. `worms status` reads Proxmox with the topology token. A feature whose secret is missing says so (`n/a`) instead of failing. `WORMS_DB_*` and `WORMS_PROXMOX_*` replace these connections for development and tests.

**Where the steps run:** each build step runs the real tool (`tofu`, `ansible-playbook`, the generator scripts) on the machine running `worms`. The sandbox and devbox stacks and their playbooks run in the round agent's workspace on the node, `/opt/worms`, through `worms-node-run`, after that workspace is synced with this checkout.

**The database is the source of truth.** Team membership, logins, captures and the leaderboard live only there. The CLI reuses the dashboard's own models and scoring code, so `worms leaderboard` and the dashboard cannot disagree. What is running comes live from Proxmox, not from a local file.

**Tests:** `tests/test_manage_cli.py` runs the database commands against a throwaway PostgreSQL. `tests/test_secrets.py` covers the vault, `tests/test_laptop.py` where each step runs, and `tests/test_pipeline.py` the order of each build's steps. The live journey is in `tests/live` (`docs/testing.md`).
