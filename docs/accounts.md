# Accounts and privileges

Two things, kept separate:

1. **[Required accounts](#1-required-accounts)** — every account you must have
   for the platform to run (Proxmox, PostgreSQL, the dashboard/Django accounts,
   and the credential stores), each with its minimum privilege.
2. **[Per-VM privilege map](#2-per-vm-privilege-map)** — every VM and the
   privilege level each piece of functionality runs at, with anything that is
   **not** least-privilege flagged.

The operational how-to for the dashboard accounts (create the admin, import
students) is in [Managing dashboard accounts](#3-managing-dashboard-accounts),
and **every environment variable** the CLI, OpenTofu, Ansible and the
services expect is in [Environment](#4-environment).

Nothing is hardcoded: every secret lives in one encrypted vault on the
controller (`worms secrets`, §4.1); the Proxmox tokens are the only ones you
enter, everything else is generated.

---

## 1. Required accounts

### 1.1 Proxmox

Three API tokens, three different jobs. They are defined in one place,
`src/worms_cli/tokens.py` (user, role, privileges, scope); their values live in the
vault and nowhere else.

| Token | Role and privileges | Scope | Used by | Vault key |
|---|---|---|---|---|
| **provisioning** `worms-provision@pve!worms` | `WormsProvision`: VMs, storage, pools, bridges, firewall (`tokens.py` lists them). Cannot grant rights, manage users, reach a console or power off the node | `/` | OpenTofu: `worms build` on the controller **and** the round agent on the node (sandbox/devbox stacks) | `vault_tofu_proxmox_api_token` (full form `user!id=secret`) |
| **orchestrator power** `orchestrator@pam!orchestrator-prov` | `WormsSandboxPower`: `VM.Audit`, `VM.PowerMgmt` | `/pool/sandbox` | the orchestrator VM (game plan) | `vault_proxmox_api_token_value` |
| **read-only topology** `topology@pve!readonly` | `WormsTopology`: `Pool.Audit`, `Sys.Audit`, `VM.Audit` | `/` | the orchestrator VM (`collect_topology`) and `worms status` | `vault_proxmox_readonly_api_token_value` |

All three are privilege-separated, with their role on the user **and** the token,
so a token's rights are the intersection of both. The provisioning token lives only
in the controller's vault and the round agent's `0600` env file on the node, never on
a VM. It does not need `root@pam`. OpenTofu also logs in to the node as root over SSH
to upload snippets, with your ssh-agent on a laptop or the WORMS key for the round
agent. The orchestrator VM holds only its two scoped tokens, so "orchestrator
compromised" is not "node compromised".

Issue them with one command, which creates the role and user if needed, creates or
rotates the token, removes any other grant of that identity, and stores the value:

```sh
worms secrets issue proxmox-token        # first: OpenTofu needs it
worms secrets issue orchestrator-token
worms secrets issue topology-token
worms secrets check                      # does each work, with exactly its rights?
worms build provision                    # after a rotation: deliver to the VMs and the agent
```

Never store a token anywhere else. OpenTofu prefers `*.auto.tfvars` to the values
`worms` gives it, so `worms` refuses to run while such a file exists.

### 1.2 Database (PostgreSQL)

TLS-only (`hostssl`, `scram-sha-256`); each role may connect **only from its own
VM's `/32`** (`pg_hba`, `roles/database/setup_db_base`). Grants in
`roles/database/setup_db_permissions`, verified by `tests/test_database.py`.

| Role | Minimum privilege | Connects from |
|---|---|---|
| `admin` | `SUPERUSER` — manual administration / the `worms` CLI; no service uses it | Proxmox host `/32` |
| `django_migrator` | owns the tables (DDL); runs migrations | orchestrator `/32` |
| `django_orchestrator` | `SELECT` all; `INSERT`/`UPDATE` only its own result tables (`round_runs`, `game_plan_steps`, `virtual_machines`, `topology_snapshots`, `devboxes`, `devbox_vms`), `INSERT` `host_jobs`/`run_events`/`flag_captures`/`logging`/`devbox_captures`/`devbox_logs`, `UPDATE teams(devbox_version)` and attempts(`processed`) | orchestrator `/32` |
| `django_dashboard` | `SELECT` all; `INSERT` `uploads`/`malware_artifacts`/`access_tokens`; `UPDATE access_tokens(revoked)`, `users(last_login, password, must_change_password)`; sessions; `INSERT`/`DELETE devbox_ssh_keys`, `team_memberships`. Staff administration (§3): `INSERT users` — student rows only, a trigger refuses staff/superuser rows from this role — and `UPDATE users(first_name, last_name, email, is_active)`; `INSERT`/`DELETE teams`, `UPDATE teams(name, max_members, development_competition_id)`; `INSERT competitions`, `UPDATE competitions(name, registration_*, development_competition_id)`; `INSERT`/`DELETE rounds`, `UPDATE rounds(name, seed, round_start, round_end, sandbox_params)`. Never `username`, `is_staff`, `is_superuser`; no delete on anything a team or round owns (a referenced team/round cannot be deleted) | dashboard `/32` |
| `dbuser_scoring` | `INSERT (team_uuid, vm_flag_hash, devbox)` on `flag_capture_attempts` — only | scoring `/32` (conn-limit 8) |
| `dbuser_logging` | `INSERT (uuid, team_uuid, log_data, devbox)` on `logging_attempts` — only | logging `/32` (conn-limit 8) |
| `dbuser_network_monitor` | `INSERT` on `network_flows`/`network_events` — only (cannot read) | monitor `/32` (conn-limit 4) |
| `round_agent` | `SELECT` `host_jobs`; `UPDATE host_jobs(state, timestamps, result, error)` | Proxmox host `/32` (conn-limit 4) |

### 1.3 Dashboard / Django

| Account | Privilege | Source |
|---|---|---|
| **admin** | Django superuser, `is_staff=True` (Django admin + staff-only pages) | `manage.py init` (login disabled); one-time password from `worms admin reset-password` |
| **students** | plain users, `is_staff=False`, `must_change_password=True` | the dashboard, Staff → Users (one by one or a CSV → one-time passwords); or `manage.py create_users` |
| **tutors/staff** (optional) | `is_staff=True`: the staff pages | `worms admin create-staff EMAIL` (console; the dashboard cannot create staff) |
| **team access tokens** | read-only: the team's own round scoring, logs and progression through the team API (`docs/team_api.md`) — nothing of other teams, nothing writable | created/revoked by team members on their profile; stored as SHA-256 only |

### 1.4 Other credentials (the stores)

| Credential | Privilege / purpose | Where |
|---|---|---|
| **the vault** (`ansible/.vault_pass` + `group_vars/vault.yml`) | every secret: the Proxmox tokens, DB passwords, Django `SECRET_KEY`s, CA passphrase, API tokens, the WORMS SSH key | `worms secrets` (§4.1); on the controller, git-ignored |
| **WORMS SSH key** | the one key authorised on every VM (and for root on the node, for the round agent's OpenTofu); Ansible and the round agent log in to the VMs with it | generated into the vault (§4.1); on VMs via cloud-init; on the node via role `round_agent` (`/etc/worms/ssh/worms`); existing VMs: `worms node trust` |
| **internal CA** (`pki_ca_*`, passphrase `vault_pki_ca_passphrase`) | signs the TLS certs the VMs trust; the private key stays on the controller | `roles/pki/ca_create` |
| **orchestrator API token** (`vault_orchestrator_dashboard_token`) | bearer token for dashboard→orchestrator (devbox reset) | vault; delivered to both VMs' `0600` env files |

---

## 2. Per-VM privilege map

What runs where, and at which privilege. SSH into every VM is **key-only** (the
Proxmox host key; `PermitRootLogin prohibit-password`, `PasswordAuthentication
no`), from the node on the management network only. The login user is `debian`
(+ `sudo`); services run as dedicated users with `NoNewPrivileges` wherever
possible.

Legend: ✅ least-privilege · ⚠️ runs as root/elevated — reason given ·
◎ untrusted by design (the privilege level is irrelevant — the VM *is* the threat
and is contained by the network/firewall, not by in-guest privilege).

### Infrastructure VMs

| VM | Functionality | Runs as | |
|---|---|---|---|
| **database** | PostgreSQL | `postgres` | ✅ |
| | auditd / rsyslog (ship to the syslog VM) | root | ⚠️ system logging daemons (expected) |
| **dashboard** | gunicorn (Django app) | `django` (grp `www-data`) | ✅ `NoNewPrivileges` |
| | nginx (TLS front end) | root master → `www-data` workers | ✅ standard privileged-port bind |
| | fail2ban | root | ⚠️ must edit the firewall (system service) |
| **orchestrator** | gunicorn + `conduct_rounds`, `process_attempts`, `collect_topology` | `django` | ✅ `NoNewPrivileges` |
| | nginx | root → `www-data` | ✅ |
| **scoring** | sandbox API (Flask/gunicorn) | `sandbox-api` | ✅ `NoNewPrivileges`, empty `CapabilityBoundingSet` |
| | nginx | root → `www-data` | ✅ |
| **logging** | sandbox API | `sandbox-api` | ✅ as scoring |
| | nginx | root → `www-data` | ✅ |
| **monitor** | Zeek (capture) | `zeek` | ✅ only `CAP_NET_RAW`+`CAP_NET_ADMIN` (ambient), no IP networking |
| | flow shipper → DB | `zeek-shipper` | ✅ `NoNewPrivileges`, no capabilities |
| | set capture NIC promiscuous (one-shot) | root | ⚠️ minimal boot one-shot (needs `CAP_NET_ADMIN`) |
| **dns** | dnsmasq | root → `dnsmasq` | ✅ standard (binds :53, drops) |
| **syslog** | rsyslog (receives the audit logs) | root | ⚠️ system logging daemon (expected; binds :514) |

### Private-internet VMs (caches; a dead end — ingress only)

| VM | Functionality | Runs as | |
|---|---|---|---|
| **apt_cache** | apt-cacher-ng | `apt-cacher-ng` | ✅ |
| **devpi** | devpi (PyPI mirror) | `devpi` | ✅ |
| **docker_registry** | registry | `docker-registry` | ✅ |
| **manpages** | apache2 (+ mandoc CGI) | root → `www-data` | ✅ standard |
| **ntp** | chrony | root → `_chrony` | ✅ standard (binds :123, drops) |
| **cve_search** | cve-search + MongoDB | `cve-search` / `mongodb` | ✅ |
| **wikipedia** | kiwix-serve | `kiwix` | ✅ |

### The Proxmox node itself

| Functionality | Runs as | |
|---|---|---|
| round agent (pulls host jobs; drives tofu/ansible/qm/ssh for the sandbox and devbox stacks, from `/opt/worms`) | root | ⚠️ root **by necessity** — it performs the privileged host build; bounded by its job-queue-only DB role and a fixed job menu (`docs/rounds.md`); its workspace holds no vault and no CA key |
| `worms-mirror`, host firewall + watchdog (one-shots/timers) | root | ⚠️ host-level networking (necessary) |

### Untrusted VMs (assumed rooted / malicious)

| VM | Functionality | Runs as | |
|---|---|---|---|
| **sandbox VMs** | the submitted worm | `debian` (started via `systemd-run --uid`) | ◎ unprivileged start, but the VM is assumed rootable; contained by the network/firewall |
| **devbox VMs (A/B/C)** | the team probing their environment | teams log in as **root** (their own playground) | ◎ untrusted by design |
| **sandbox-gw / devbox-gw** | nftables router (SNAT) | root (boot config via cloud-init) | ◎ untrusted router; bounded by the host-side Proxmox firewall on its NICs |
| **devbox SSH gateway** (trusted) | sshd jump host | root master; teams log in as the shared `team` user | ✅ team logins confined per-key (`restrict,port-forwarding,permitopen=…`) |

**Not least-privilege, summarised:** the round agent and the host one-shots run
as root (unavoidable — they *are* the host tooling); `fail2ban` and the
auditd/rsyslog daemons are root system services. The
untrusted sandbox/devbox VMs and their gateways are root/untrusted by design —
their containment is the network and firewall, not in-guest privilege. Every
application service we control (Django, the sandbox APIs, Zeek, the caches) runs
as a dedicated non-root user with `NoNewPrivileges`.

---

## 3. Managing dashboard accounts

Operational how-to for the accounts in [§1.3](#13-dashboard--django). (Network
and infrastructure accounts are not managed here — see §1.1/§1.2.)

What needs only database writes is done on the dashboard by staff (the staff
menu, top right: **Competition, Users, Teams, Rounds**; `dashboard/views/staff*.py`).
What needs more stays on the console: the admin account itself, staff
accounts, and everything that builds infrastructure.

| Task | Where |
|---|---|
| registration window, development competition | dashboard: Staff → Competition |
| student accounts: create, CSV import, edit, new one-time password, (de)activate, team | dashboard: Staff → Users |
| teams: create, rename, size, members; delete while nothing refers to them | dashboard: Staff → Teams |
| rounds: create, edit/delete before their runs exist; runs, host jobs, errors | dashboard: Staff → Rounds (`docs/rounds.md`) |
| the admin's one-time password | `worms admin reset-password` |
| staff (tutor) accounts | `worms admin create-staff EMAIL` |
| devboxes for new teams | `worms devbox provision` (`docs/devbox.md`) |
| a team with history (uploads, runs, tokens, devbox), with everything it owns | `worms teams delete NAME` |

### Admin + default competition — `manage.py init` (idempotent)

Creates the `admin` account (`is_staff` + superuser) and the default competition
`WORMS <year>` with its `current` setting, after every migration run (`django_app`
role's `django_post_migrate_commands`). Safe to re-run (never resets an existing
password or current competition). The admin is created **without a password**
(login disabled).

### Admin password — `worms admin reset-password`

```
$ worms admin reset-password
admin: reset 'admin'; one-time password below (shown once, not stored; the dashboard asks for a new one at the first login):
q7XbVfE3kHtWm9cPzR2a
```

Sets a random 20-character one-time password and prints it **once, on your
terminal** (stdout; the notice goes to stderr, so `| pbcopy` takes only the
password). It is stored nowhere but as Django's password hash — not in the
vault, not in a log, not in Ansible output. The first login lands on
`/change-password/` (`must_change_password`, as for students). Resetting also
ends every session of the account (Django ties sessions to the password
hash). Run it for the first login and whenever the password is lost; it asks
before replacing a password the admin has already chosen (`--yes` skips
that). It creates the account if it is missing and refuses an existing
account that is not a superuser (`--username` cannot promote a student).
Same as `manage.py reset_admin_password` on the orchestrator.

### Staff accounts — `worms admin create-staff EMAIL`

A tutor account (`is_staff`, not superuser) with a one-time password printed
once, like the admin's. The dashboard cannot make one: its database role may
not insert a staff row or set `is_staff` (a public-facing service must not be
able to mint staff accounts, even if compromised). On the dashboard staff
manage student accounts; the admin also manages staff accounts' passwords
and activation; nobody manages the admin there.

### Students — Staff → Users (or `manage.py create_users <csv>`)

One by one (email, names, team) or from a CSV with a header row (email, first
name, surname, optionally team; English or German headers: `core/accounts.py`).
Each new email becomes a user with a one-time random password and
`must_change_password`; existing emails are skipped and listed. The passwords
are shown **once**, on the page that answers the request (`Cache-Control:
no-store`), also as an `email,password` CSV to copy. A team named in the CSV
is joined if it exists and has room; otherwise the row is reported. Later:
a new one-time password, deactivation (no login; the account and its results
stay), another team.

`manage.py create_users` on the orchestrator does the same import from a file
and prints an `email,password` CSV to stdout (`--output creds.csv` to a file).

### Password resets are recorded

Each new one-time password issued from Staff → Users adds a row to the
`password_resets` table: when, who asked (uuid, username, `admin` or `staff`) and
whose account it was (uuid, username, `student` or `staff`). It holds no password
and no hash. There is no page for it yet. To read it:

```sh
ssh -i /etc/worms/ssh/worms debian@10.3.0.16 "sudo -u postgres psql -d mydb -c \
  'SELECT created_at, actor_username, actor_role, target_username, target_role FROM password_resets ORDER BY created_at DESC'"
```

| Rule | How |
|---|---|
| No reset without a row | The password and the row commit in one transaction (`core/accounts.py`, `reset_password`). If the row cannot be written, the password stays as it was |
| Who asked comes from the session | It is the signed-in account. Nothing in the request can name someone else |
| Rows cannot be changed or removed by the dashboard | Its database role may only add and read them, so this holds even if the dashboard is compromised |
| Rows outlive the accounts | They hold plain values, not foreign keys |

**What it does not cover:**

- A person who controls the dashboard VM could add false rows or set a password
  with no row, because that role may do both. Rows already there stay as they are.
- Not recorded, because no one resets someone else's account here: the admin's
  password on the console (`worms admin reset-password`), a user changing their
  own password, and the first password of a new account.
- `worms reset-database` empties this table with all the others.

### First login forces a password change

An account with `must_change_password` is held on `/change-password/` by
`ForcePasswordChangeMiddleware` until it sets its own password
(`dashboard/views/password.py`). The public `django_dashboard` DB role is granted
`UPDATE(password, must_change_password)` on `users` for this; the view writes
exactly those two columns (a full-row save would be refused).

Covered offline by `tests/test_accounts.py` and `tests/test_staff_admin.py`
(the staff pages and the first-login change run as `django_dashboard` with
its real grants).


---

### View as — the admin only

The admin can see the dashboard as another user does: **Staff → Users**, then **View as** on a row, or **View as this user** on an account's page. A banner shows whose view it is. **Back to admin** leaves it.

| Rule | How it is enforced |
|---|---|
| Only the admin | The account must be an active superuser, checked on every request. Staff are not superusers, and the dashboard's database role cannot set that flag |
| It cannot be forged | The state lives only in the server-side session, under `_view_as`. No request parameter, header or cookie is read. It records which admin started it, and a mismatch is dropped. Starting takes a POST with a CSRF token |
| Read only | While viewing, every write is refused by default (`ViewAsMiddleware`). Only three are allowed: leaving, signing out and the Live/Dev switch. So nothing can be done in the user's name: no SSH key, reset, upload, token or password |
| Whom | Any active account except the admin. Deactivated accounts cannot be viewed, because they cannot sign in |
| It ends by itself | As soon as the admin loses the flag or is deactivated, the user is deactivated or made admin, or the admin's password changes |
| It touches nothing | Nobody is signed in: the session stays the admin's. The user's sessions, password and `last_login` do not change |

Starting, leaving and signing out while viewing are logged by the dashboard service: `journalctl -u dashboard | grep view-as` on the dashboard VM.

The team API is not affected. It authenticates by token and ignores sessions.

## 4. Environment

**Where things run.** The controller is your laptop: it holds the checkout, the
vault, the internal CA and the OpenTofu state of the platform, firewall,
infrastructure and private-internet stacks, and runs OpenTofu and Ansible for
them. The Proxmox node keeps the round agent and its workspace `/opt/worms`:
the sandbox and devbox stacks with their state (the agent builds those for
every round), synced from your checkout by `SetupRoundAgent.yml` — `worms`
runs its `agent-sync` part before every step it runs on the node. Ansible
reaches the VMs through the node (ProxyJump). `worms` also works on the node
itself (`WORMS_SSH` empty), which is how the round agent runs its steps.

**What you set: one setting and three secrets, once.** No environment
variables: no `TF_VAR_*`, no `PROXMOX_*`. SSH to the node is your own, as for
`ssh root@<node>`: your ssh-agent with a key root on the node accepts (OpenTofu's
Proxmox provider logs in with the agent's keys; `worms` checks that before it
runs tofu).

```sh
echo 'WORMS_SSH=root@<node>' >> ~/.worms/config   # the only setting
worms secrets issue proxmox-token        # §1.1, token 1
worms secrets issue orchestrator-token   # token 2
worms secrets issue topology-token       # token 3
worms build
```

`worms node trust` is needed only for VMs that existed before the WORMS key
(an existing deployment, §4.5), or if you have no ssh-agent of your own (it then
lets the WORMS key log in to the node as well).

### 4.1 The vault — `worms secrets`

One store for every secret: `ansible/group_vars/vault.yml`, encrypted with
ansible-vault (AES256), with its key `ansible/.vault_pass` (generated, `0600`);
both git-ignored (`src/worms_cli/secrets.py`). `worms secrets` shows what is set
and what is missing, never a value. `worms secrets issue <name>` creates or rotates a
Proxmox token and stores it, and `worms secrets check` asks Proxmox whether each
stored value works with exactly its rights. `worms secrets set <name>` stores a value
by hand, prompting without echo (or reading stdin). Everything that is not a Proxmox
token is generated the first time it is missing (any `worms build`, `worms secrets`)
and never rotated.

| Secret | | Used by |
|---|---|---|
| `proxmox-token` (`vault_tofu_proxmox_api_token`) | issued | OpenTofu: on the laptop through the CLI, on the node through the agent's env file |
| `orchestrator-token` (`vault_proxmox_api_token_value`) | issued | the orchestrator: sandbox VM power in the game plan |
| `topology-token` (`vault_proxmox_readonly_api_token_value`) | issued, optional | the orchestrator's `collect_topology`; `worms status` |
| `ssh-key` (`vault_worms_ssh_key`, `vault_worms_ssh_public_key`) | generated (ed25519); `worms secrets set ssh-key < key` imports one | the one key every VM authorizes (cloud-init); Ansible on the laptop and the round agent log in to the VMs with it; root on the node accepts it for the round agent's own OpenTofu runs (role `round_agent`) |
| 8 PostgreSQL role passwords, the CA passphrase, 2 Django `SECRET_KEY`s, the orchestrator API token, devpi's root password | generated | delivered by Ansible into the services' `0600` env files (§4.6) |

How they reach their consumers — each through exactly one path:

- **OpenTofu** (laptop): the CLI puts `TF_VAR_proxmox_api_token`,
  `TF_VAR_proxmox_api_url` (`https://<WORMS_SSH host>:8006/`, or
  `WORMS_PROXMOX_URL`) and `TF_VAR_ssh_public_key` (the WORMS public key, a
  temporary file) into the `tofu` process's environment only. The bpg provider
  uploads snippets over SSH as root with the keys of **your** ssh-agent
  (`SSH_AUTH_SOCK`); `worms` first checks that such a login works (agent keys
  only, no prompt) and says what to do if not. The token never touches the disk
  outside the vault.
- **Ansible**: `-e @group_vars/vault.yml`, plus `-e internet_enabled=<the
  node's flag>`.
- **Services**: rendered by Ansible into root-owned `0600` env files (§4.6).
- **Round agent**: its env file (`TF_VAR_*`) and its key
  `/etc/worms/ssh/worms` (`0600`), both from the vault (role `round_agent`).
- **The CLI's own access**: the database admin password and the topology
  token, through SSH tunnels via the node (§4.4).

Back up `ansible/group_vars/vault.yml` + `ansible/.vault_pass`, the CA
(`ansible/pki/worms/`) and the state of the controller's stacks
(`opentofu/stacks/*/terraform.tfstate`) together; without them the platform
has to be rebuilt.

### 4.2 OpenTofu — `worms build` (`tofu` in `opentofu/stacks/*`)

The CLI supplies the three `TF_VAR_*` above. Optional, not secret, still read
from the environment if you set them:

| Variable | Default | |
|---|---|---|
| `TF_VAR_proxmox_instance` | `worms-pve` | the Proxmox node name; becomes `proxmox.node` in the inventory |
| `TF_VAR_datastore_id` | `local` | datastore for images and snippets (`snippets` content type) |
| `TF_VAR_gateway_user` | `team` | devbox stack: the SSH gateway's login user |
| `TF_CLI_CONFIG_FILE` | — | an OpenTofu CLI config (e.g. a provider mirror) |

The provider connects to the node's API (`:8006`, TLS not verified:
`insecure = true` in every stack) and over SSH to the address the API reports
for the node; both must be reachable from the laptop.

### 4.3 Ansible — `worms build <component>` (`ansible-playbook` in `ansible/`)

**No environment variables**; everything is in files:

| File | From | Purpose |
|---|---|---|
| `ansible/ansible.cfg` | the repo | inventories, `vault_password_file = .vault_pass`, roles path, no host-key prompts for the VMs; speed: 20 forks, facts cached for one `worms build` run (`ansible/.facts`, emptied by the CLI at the start and after every tofu apply/destroy), SSH connections reused for 10 min |
| `ansible/.vault_pass`, `ansible/group_vars/vault.yml` | `worms` (§4.1) | every secret; passed as `-e @group_vars/vault.yml` |
| `ansible/inventory.generated.yml` | `worms build inventory` (`scripts/generate_inventory.py`) | every VM's addresses. From a laptop: the node as `root@<node>` over SSH (host key checked against your `known_hosts`), every VM through it (`-o ProxyJump=root@<node>`), the sandbox/devbox hosts read from the node's state |
| `ansible/pki/worms/ca.{crt,key}` | `CreateCA.yml` (key encrypted with `vault_pki_ca_passphrase`) | the internal CA; the key stays on the controller (only `ca.crt` goes to the node and the VMs) |

Ansible logs in to the node (and through it, the ProxyJump hop) with your own
SSH setup, and to the VMs with the WORMS key: `worms` holds it in a private
ssh-agent for the run and the generated inventory points the VM connections at
it (`-o IdentityAgent=${WORMS_SSH_AUTH_SOCK}`, expanded by ssh; OpenSSH ≥ 8.4).
Without an agent of your own the WORMS key is used for the node too (`worms
node trust` then lets it in). Collections used
beyond `ansible.builtin`: `community.crypto`, `community.postgresql`,
`ansible.posix`. `worms build <component> -- <args>` appends extra
`ansible-playbook` arguments. Run playbooks through `worms`: by hand, from a
laptop, `internet_enabled` would read the laptop's filesystem (pass `-e
internet_enabled=true|false`).

### 4.4 The `worms` CLI (`src/manage.py`)

Settings (`~/.worms/config`, `--config` or the environment; `KEY=value`
lines, `#` only at the start of a line; the real environment wins). No secrets:

| Setting | Default | |
|---|---|---|
| `WORMS_SSH` | empty = this machine is the node | `root@<node>` (or an alias from `~/.ssh/config`): this machine is the controller |
| `WORMS_PROXMOX_URL` | `https://<WORMS_SSH host>:8006/` | the node's API, if it has another name from here |
| `WORMS_REPO_ROOT` | the checkout the CLI runs from | |
| `NO_COLOR` | colours on a TTY | any value: no colours |

The CLI's database commands (`status`, `teams`, `users`, `leaderboard`,
`admin reset-password`, `reset-database`) connect as `admin` with the vault's
password to the database's address (from the platform stack), through `ssh -L`
via the node: `pg_hba` admits `admin` from the node's address only. TLS stays
`verify-full` against the internal CA, checked against the database's own name
(libpq `hostaddr` is the tunnel). `worms status` reads the Proxmox API with the
topology token through a tunnel to the node's `127.0.0.1:8006`, verified
against the node's PVE CA (fetched over SSH). The internet flag
(`/run/worms/internet-allowed`) is read on the node.

For development and tests, explicit settings replace the vault-derived ones:
`WORMS_DB_HOST/PORT/NAME/USER/PASSWORD/SSLMODE/SSLROOTCERT` and
`WORMS_PROXMOX_USER/TOKEN_NAME/TOKEN_VALUE/NODE/VERIFY_SSL`.

**On the laptop:** Python 3, OpenTofu, Ansible (the `ansible` package), the
OpenSSH client, rsync; SSH (22) and the API (8006) of the node reachable.

### 4.5 Moving an existing deployment from the node to a laptop

1. Copy from the node's checkout (e.g. `/root/worms`) into the laptop's, same
   paths: `ansible/group_vars/vault.yml`, `ansible/.vault_pass`,
   `ansible/pki/worms/`, and `opentofu/stacks/{platform,firewall,
   infrastructure/persistent,infrastructure/ephemeral,private_internet}/terraform.tfstate`.
   Leave the sandbox and devbox state where it is: the first `SetupRoundAgent.yml`
   from the laptop moves it into `/opt/worms` (it finds the old checkout through
   `REPO_ROOT` in the agent's env file).
2. `worms secrets`: the vault keeps every existing password and gains the
   WORMS key; if `proxmox-token` shows `MISSING`, set it (it used to be
   `TF_VAR_proxmox_api_token` in root's shell).
3. `worms node trust`: the existing VMs only accept root's old key on the node;
   this authorizes the WORMS key on every one of them (logging in from the node
   with that old key), and for root on the node as well. Alternatively make the
   node's old key the WORMS key: `ssh root@<node> cat /root/.ssh/id_rsa | worms
   secrets set ssh-key` (then that private key lives in the laptop's vault).
4. `worms build provision`: the round agent moves to `/opt/worms`. Delete the
   old shell setup (`~/.worms/env`, `~/.worms/config` secrets) on the node.

### 4.6 Generated for the services (never set by hand)

Rendered by Ansible from the vault and the inventory into root-owned `0600`
files read by systemd; listed so you know what each service sees.

| Service | File | Variables |
|---|---|---|
| round agent (node) | `/etc/worms/round-agent.env` | `REPO_ROOT` (`/opt/worms`), `PYTHONPATH`; `DB_HOST/PORT/NAME/USER/PASSWORD/SSLMODE/SSLROOTCERT` (role `round_agent`); `DASHBOARD_HOST`, `DASHBOARD_MEDIA_ROOT`, `SCORING_HOST`, `LOGGING_HOST`; **OpenTofu**: `TF_VAR_proxmox_api_url` (`https://127.0.0.1:8006/`), `TF_VAR_proxmox_api_token` (`vault_tofu_proxmox_api_token`), `TF_VAR_proxmox_instance`, `TF_VAR_ssh_public_key` (`/etc/worms/ssh/worms.pub`). `worms-node-run` loads it and starts the service (and the controller's node steps) in an `ssh-agent` with the WORMS key `/etc/worms/ssh/worms`. |
| dashboard, orchestrator (Django) | `django.env` (role `django_app`) | `PRODUCTION=1`, `DJANGO_SETTINGS_MODULE`, `DJANGO_SECRET_KEY`, `DJANGO_ALLOWED_HOSTS`, `DB_NAME/HOST/PORT/SSLMODE/SSLROOTCERT`, `DB_USER/PASSWORD` (the VM's own role); orchestrator also `DB_MIGRATOR_USER/PASSWORD`, `ORCHESTRATOR_DASHBOARD_TOKEN`, `PROXMOX_HOST/PORT/NODE/USER/TOKEN_NAME/TOKEN_VALUE` (power token), `PROXMOX_READONLY_USER/TOKEN_NAME/TOKEN_VALUE`, optional `PROXMOX_VERIFY_SSL`, `PROXMOX_CA_BUNDLE`, `PROXMOX_TIMEOUT`; dashboard also `DJANGO_MEDIA_ROOT` (the uploads, `dashboard_media_root`: the one path its unit may write, `ReadWritePaths`), `ORCHESTRATOR_URL`, `ORCHESTRATOR_TLS_NAME`, `ORCHESTRATOR_API_TOKEN`, optional `DJANGO_CSRF_TRUSTED_ORIGINS` (only if another proxy in front rewrites the Host header). `DJANGO_ALLOWED_HOSTS` is `dashboard.worms.internal`, the dashboard's IP and `dashboard_public_hosts` (`group_vars/dashboard.yml`: add the node's public name when the dashboard is published on the WAN) |
| scoring, logging (sandbox API) | `sandbox-api.env` (role `sandbox_api`) | `SANDBOX_API_KIND`, `DB_HOST/PORT/NAME/USER/PASSWORD/SSLMODE/SSLROOTCERT` (the INSERT-only `dbuser_scoring` / `dbuser_logging`), `DB_POOL_SIZE`, `CURRENT_TEAM_FILE`, `DEVBOX_TEAMS_FILE`, `CLIENT_IP_HEADER`, the limits (`MAX_BODY_BYTES`, `LOG_MAX_BYTES/DEPTH/ITEMS`, `TEAM_RATE_PER_MINUTE`, `TEAM_BURST`, `TEAM_BAN_AFTER/SECONDS`) |
| monitor (zeek-shipper) | `zeek-shipper.env` (role `network_monitor`) | `DB_*` (the INSERT-only `dbuser_network_monitor`), `ZEEK_LOG_DIR`, `ZEEK_ARCHIVE_DIR`, `STATE_FILE`, `EVENT_LOGS`, the rate limits (`MAX_FLOWS_PER_SECOND`, `FLOW_BURST`, `MAX_EVENTS_PER_SECOND`, `EVENT_BURST`, `MAX_EVENT_BYTES`), `REPORT_SECONDS` |

The orchestrator verifies the Proxmox API certificate. PVE's default certificate
is issued by PVE's own CA and names the node, but not `10.11.0.1`, the address the
orchestrator reaches it on. So `SetupDbAndDashboard.yml` gives the orchestrator a
copy of PVE's CA (`PROXMOX_CA_BUNDLE`) and maps the node's name to that address in
`/etc/hosts`, and the orchestrator connects by name (`PROXMOX_HOST`). The node's
certificate stays as PVE manages it.

