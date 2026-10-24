# `worms` — the management CLI

`src/manage.py` is the single command-line tool for operating the WORMS
platform. It **is** the build pipeline — the former Makefile recipes now live
in `src/worms_cli/pipeline.py`, and the top-level `Makefile` only delegates
(`make` → `worms build`, `make destroy` / `make destroy_all`). It also shows
**live status from the one source of truth** (the database VM, plus the Proxmox
API for infrastructure). You can run it from your laptop with the same access
tokens you use on the server, instead of developing as root on the production
host; with `WORMS_SSH` set it runs the build steps over SSH on the node.

```
alias worms='python3 /path/to/worms/src/manage.py'

worms status                      # components, internet, competition, rounds
worms teams                       # teams + members' login status
worms teams create "Neural Net"
worms teams rename "Old" "New"
worms teams delete "Neural Net"   # users are NOT affected
worms users                       # all users: team, login, one-time password
worms leaderboard                 # the production leaderboard, in the console
worms build                       # full idempotent end-to-end setup (= make)
worms build firewall              # one component; `worms build --list`
worms reset-database              # DANGER: wipe + re-migrate-init
worms emergency-stop              # freeze: hard-stop ALL VMs (does NOT destroy)
```

Everything also has `--help`, and `--dry-run` prints the `make`/SQL it would
run without doing it.

## Configuration

No secret is hardcoded. The CLI reads `WORMS_*` variables from the environment,
optionally seeded from `~/.worms/config` (or `--config path`), a file of plain
`KEY=value` lines. Precedence: real environment > `--config` > `~/.worms/config`.
The full reference — every variable with its default and where its value
comes from, plus what OpenTofu and Ansible need, and a ready example for the
node — is `docs/accounts.md` §4 "Environment".

| Variable | For | Notes |
|---|---|---|
| `WORMS_REPO_ROOT` | build | the worms checkout (default: the repo this script lives in) |
| `WORMS_SSH` | build, internet probe | `user@host` to run `make` and host probes on (e.g. `root@worms-pve`); empty = run locally on the host |
| `WORMS_DB_HOST/PORT/NAME/USER/PASSWORD` | status, teams, users, leaderboard, reset | a **privileged** DB role (the `admin` superuser or `django_migrator`) |
| `WORMS_DB_SSLMODE/SSLROOTCERT` | " | default `verify-full` against the checkout's internal CA (`ansible/pki/worms/ca.crt`) |
| `WORMS_PROXMOX_URL/USER/TOKEN_NAME/TOKEN_VALUE/NODE` | status | read-only API token is enough |
| `WORMS_PROXMOX_VERIFY_SSL` | status | keep `1` with `REQUESTS_CA_BUNDLE=/etc/pve/pve-root-ca.pem` on the node; `0` only for local experimentation |

Each command needs only the configuration for what it does: `worms build`
needs just the repo (and `WORMS_SSH` if you are remote); `worms teams` needs
the database; `worms status` uses whatever is configured and marks the rest
`n/a`. A feature whose configuration is missing says so instead of failing.

### Reaching the database and Proxmox from your laptop

The database only accepts the `admin` role from the Proxmox host (`pg_hba`,
`postgres_admin_cidrs` in `group_vars/all.yml`). From your laptop, tunnel:

```
ssh -L 5432:10.11.0.16:5432 root@worms-pve        # DB over the core bridge
# then WORMS_DB_HOST=127.0.0.1, and add the host to postgres_admin_cidrs if needed,
# or simply set WORMS_SSH=root@worms-pve and run status/build over SSH.
```

The Proxmox tokens are the ones documented in the README (`PROXMOX_API_TOKEN`
for power/management, `PROXMOX_READONLY_API_TOKEN` for read-only). `worms
status` only reads, so the read-only token suffices.

### Proxmox tokens assumed to exist

The CLI, like the orchestrator, assumes these API identities exist (document
only; create them in Proxmox once):

- `orchestrator@pam!orchestrator-prov` — VM power + management (the value is
  `vault_proxmox_api_token_value`).
- `topology@pve!readonly` — `PVEAuditor` on `/`, read-only
  (`vault_proxmox_readonly_api_token_value`). Use this one for `worms status`.

## What `worms status` shows

- **Infrastructure** (Proxmox API): firewall enabled; number of Linux bridges;
  running/total VMs per pool — infrastructure (`persistent`+`ephemeral`),
  `private_internet`, `sandbox`, `devbox`; and **internet access** (the host
  flag `/run/worms/internet-allowed`, read directly on the node, over
  `WORMS_SSH` from elsewhere).
- **Competition** (database): the current competition, the phase
  (registration / active round / next round), the number of rounds and teams,
  and the round-orchestration run states and queued host jobs (docs/rounds.md).

## Building

`worms build` (no component) runs the full pipeline end to end; `worms build
<component>` runs one step (`worms build --list` prints them: secrets, init,
platform, firewall, host-network, infrastructure, inventory, configure,
provision, offline, internet-allow/deny, round-*, devbox*, ping, …). Each step
runs the real tool (tofu, ansible-playbook, the generator scripts) — locally on
the Proxmox node, or over SSH when `WORMS_SSH` is set. The internet window
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

Tofu reads its Proxmox credentials from the operator's environment on the node
(`TF_VAR_proxmox_api_url` / `TF_VAR_proxmox_api_token` and an ssh-agent with
root's key; `docs/accounts.md` §4.1); the CLI does not manage them. The round
agent gets the same inputs from its env file (the vault keeps a copy of the
provisioning token).

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
team create/rename/delete (and that deleting a team keeps its users), the
status and leaderboard output, the user listing, and `reset-database`. The
no-configuration paths (`--help`, and `status` degrading to `n/a`) are checked
too.
