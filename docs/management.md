# `worms` — the management CLI

`src/manage.py` is the single command-line tool for operating the WORMS
platform. It **is** the build pipeline — the former Makefile recipes now live
in `src/worms_cli/pipeline.py`, and the top-level `Makefile` only delegates
(`make` → `worms build`, `make destroy` / `make destroy_all`). It also shows
**live status from the one source of truth** (the database VM, plus the Proxmox
API for infrastructure). It runs on your laptop (`WORMS_SSH=root@<node>`):
OpenTofu and Ansible run there with the secrets of the vault, and only the
node's own part (the round agent's sandbox and devbox stacks) runs on the node
(`docs/accounts.md` §4).

```
alias worms='python3 /path/to/worms/src/manage.py'

worms secrets                     # the vault: what is set (never values)
worms secrets set proxmox-token   # enter a Proxmox token (prompted; or from stdin)
worms node trust                  # once, for VMs that predate the WORMS key (or without an agent)
worms status                      # components, internet, competition, rounds
worms teams                       # teams + members' login status
worms teams delete "Neural Net"   # a team with its history; users are NOT affected
worms devbox provision            # build the devboxes of new teams (host job)
worms users                       # all users: team, login, one-time password
worms admin reset-password        # one-time password for the dashboard admin (printed once)
worms admin create-staff EMAIL    # a tutor account, one-time password printed once
worms leaderboard                 # the production leaderboard, in the console
worms build                       # full idempotent end-to-end setup (= make)
worms build firewall              # one component; `worms build --list`
worms reset-database              # DANGER: wipe + re-migrate-init
worms emergency-stop              # freeze: hard-stop ALL VMs (does NOT destroy)
```

Everything also has `--help`, and `--dry-run` prints the `make`/SQL it would
run without doing it.

## Console or dashboard

What only writes the database is done by staff on the dashboard (staff menu:
Competition, Users, Teams, Rounds): student accounts, teams and their
members, the registration window, round definitions (`docs/accounts.md` §3).
The console keeps what needs the Proxmox host or more than the public
dashboard may do: building the platform and the devboxes (`worms build`,
`worms devbox provision`), the admin and staff accounts (`worms admin`),
deleting a team with its history, and the live status (`worms status`).
Teams used to be created here (`worms teams create`); a team created on the
dashboard gets its devbox with `worms devbox provision`.

## Configuration

One setting and the vault. `~/.worms/config` (or `--config path`, or the
environment; plain `KEY=value` lines) says where the node is; every secret is
in the vault (`worms secrets`): the three Proxmox tokens you enter once,
everything else generated. The full reference — what each tool gets, the
vault's contents, moving an existing deployment to a laptop — is
`docs/accounts.md` §4 "Environment".

| Setting | | Notes |
|---|---|---|
| `WORMS_SSH` | `root@<node>` | this machine is the controller; empty = this machine is the node |
| `WORMS_PROXMOX_URL` | `https://<WORMS_SSH host>:8006/` | if the node's API has another name from here |
| `WORMS_REPO_ROOT` | the checkout this script is in | |

The database commands (`status`, `teams`, `users`, `leaderboard`, `admin`,
`reset-database`) connect as `admin` with the vault's password through an SSH
tunnel via the node (`pg_hba` admits `admin` from the node only), TLS
`verify-full` against the internal CA; `worms status` reads Proxmox with the
topology token through a tunnel to the node's API (verified against its PVE
CA). A feature whose secret is missing says so instead of failing (`n/a`).
`WORMS_DB_*` / `WORMS_PROXMOX_*` replace the vault-derived connections for
development and tests.

## What `worms status` shows

- **Infrastructure** (Proxmox API): firewall enabled; number of Linux bridges;
  running/total VMs per pool — infrastructure (`persistent`+`ephemeral`),
  `private_internet`, `sandbox`, `devbox`; and **internet access** (the host
  flag `/run/worms/internet-allowed`, read on the node).
- **Competition** (database): the current competition, the phase
  (registration / active round / next round), the number of rounds and teams,
  and the round-orchestration run states and queued host jobs (docs/rounds.md).

## Building

`worms build` (no component) runs the full pipeline end to end; `worms build
<component>` runs one step (`worms build --list` prints them: secrets, init,
platform, firewall, host-network, infrastructure, inventory, configure,
provision, offline, internet-allow/deny, round-*, devbox*, ping, …). Each step
runs the real tool (tofu, ansible-playbook, the generator scripts) on this
machine; the sandbox and devbox stacks and their playbooks run in the round
agent's workspace on the node (`worms-node-run` over SSH), after the workspace
is synced with this checkout (`SetupRoundAgent.yml --tags agent-sync`). The internet window
around infrastructure setup is handled automatically (allowed for that step,
always denied again afterwards). `worms test` runs the offline suite and
`worms simulate` the live simulation; `worms debug <host>` runs the Ansible
connection diagnostics.

Destructive operations are separate subcommands that confirm first (`--yes` to
skip): `destroy`, `destroy-all`, `terminate` (stop + destroy everything),
`emergency-stop` (freezes everything: hard-stops every VM without destroying
it), and `reset-database` (a Django management command run through the CLI's
own database connection; it truncates the application tables and re-runs
`init`). The Makefile's `make destroy` / `make destroy_all` call these.

OpenTofu gets its Proxmox credentials from the vault: the CLI puts them into
the `tofu` process's environment only; the provider logs in to root on the node
with your ssh-agent's keys, which `worms` checks first (`docs/accounts.md`
§4.1). The VMs accept the WORMS key, which Ansible gets from a private agent of
the CLI (the inventory's `IdentityAgent`). The round agent gets the same
inputs from its env file, rendered from the same vault.

## Why the database is the source of truth

Team membership, logins, captures and the leaderboard live only in the
database. The CLI reuses the dashboard's own models and scoring functions
(bootstrapped via `config.settings.manage`), so `worms leaderboard` and the
dashboard can never disagree, and `worms teams`/`users` report exactly what the
dashboard sees. Infrastructure state (what is actually running) comes live from
Proxmox, not from a local file that can drift.

## Tests

`tests/test_manage_cli.py` (offline, part of `worms test`) runs the DB-backed
commands against a throwaway PostgreSQL with the real migrations and grants:
the team listing and delete (deleting a team keeps its users), `devbox
provision` (a Devbox per team and one queued provision job), the status and
leaderboard output, the user listing, `reset-database`, `admin
reset-password` and `admin create-staff`. The no-configuration paths (`--help`, and `status`
degrading to `n/a`) are checked too. `tests/test_secrets.py` covers the vault,
`tests/test_laptop.py` where each step runs and what it gets, the laptop
inventory and the round agent's workspace sync, `tests/test_pipeline.py` the
order of every component's steps.
