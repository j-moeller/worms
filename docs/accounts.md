# Accounts: admin, competition and student logins

How the dashboard is initialised with an administrator and a default
competition, and how student accounts are created and first sign in.

## The admin account and the default competition (`manage.py init`)

`core/management/commands/init.py` creates, **idempotently**:

- the **admin account**: username `admin`, `is_staff=True` and `is_superuser=True`
  (so it can use the Django admin at `/admin/` and the staff-only dashboard
  pages). Its password comes from the environment, never from code;
- the **default competition** `WORMS <current year>` (e.g. `WORMS 2026`), and
  the `application_setting` row `current` pointing at it, unless a current
  competition is already set.

It is safe to run repeatedly: existing objects are left as they are (an
existing admin password is never reset; an existing current competition is not
changed). Re-run it after setting the password in the vault, or to create a new
year's competition (`--competition "WORMS 2027"`).

Writes go through the **migrator** database connection when it is configured
(the orchestrator has it; the public dashboard role cannot create users or
competitions), otherwise through the default connection (local DEBUG/sqlite).

### Where the admin password lives — the vault key

Like every other internal secret, the admin password is **generated once** by
`scripts/generate_secrets.py` into the encrypted Ansible vault
(`ansible/group_vars/vault.yml`) under the key:

```
vault_django_admin_password
```

You never type or see it. To read or change it:

```
cd ansible
ansible-vault view --vault-password-file .vault_pass group_vars/vault.yml   # read
ansible-vault edit --vault-password-file .vault_pass group_vars/vault.yml   # change
```

The orchestrator receives it as `DJANGO_ADMIN_PASSWORD` in its root-owned
`0600` env file (`group_vars/orchestrator.yml` → `django_extra_env`), and runs
`manage.py init` once per `worms build provision` (the `django_post_migrate_commands`
hook of the `django_app` role, right after the migrations). If the password is
not set yet, the admin is created with login **disabled**; set the key and
re-provision to enable it.

Override the username/email with `DJANGO_ADMIN_USERNAME` / `DJANGO_ADMIN_EMAIL`
or the `--admin-username` / `--admin-email` flags.

## Student accounts from a CSV (`manage.py create_users`)

`core/management/commands/create_users.py` turns a CSV of students into login
accounts, each with a **one-time random password**.

```
manage.py create_users students.csv                 # prints email,password CSV to stdout
manage.py create_users students.csv --output creds.csv
manage.py create_users students.csv --password-length 16
manage.py create_users tutors.csv --staff           # is_staff accounts
```

**Input** (header row required; columns matched case-insensitively, English and
the German export headers both accepted):

| first name | surname | email |
|---|---|---|
| `first name` / `firstname` / `vorname` | `surname` / `last name` / `name` / `nachname` | `email` / `e-mail` / `e-mail-adresse` |

Only `email` is mandatory. For each email **not already present** a user is
created with:

- `username = email`, `is_active = True`, `is_staff = False` (unless `--staff`);
- a freshly generated password (unambiguous alphabet, no `O/0/l/1/I`);
- `must_change_password = True`.

Existing emails are skipped (no new password), so you can append rows to the
CSV and re-run.

**Output**: an `email,password` CSV on **stdout** (and, with `--output`, to a
file) — the only time the password is shown. A short summary (counts, skipped
emails) goes to stderr, so `create_users students.csv > creds.csv` yields a
clean CSV. Hand each student their line over a secure channel and delete the
file afterwards.

## First login forces a password change

An account created by `create_users` carries `must_change_password = True`
(the `must_change_password` column on `users`, migration `0008`). On login the
dashboard's `ForcePasswordChangeMiddleware` redirects the user to
`/change-password/` for every page until they set their own password; the view
(`dashboard/views/password.py`, Django's `SetPasswordForm`) saves the new
password, keeps the session alive (`update_session_auth_hash`) and clears the
flag.

The public dashboard database role (`django_dashboard`) is granted exactly
`UPDATE (password, must_change_password)` on `users` for this — it still cannot
grant itself staff or touch any other column (checked in
`tests/test_accounts.py`).

## Tests

`tests/test_accounts.py` (offline, part of `worms test`) runs both commands
against a throwaway PostgreSQL with the real migrations and least-privilege
grants: `init` creates the admin and competition and is idempotent;
`create_users` creates one account per row with a one-time password and
`must_change_password`, skips existing emails, and reads the German headers;
the dashboard role can set a password and clear the flag but nothing else.

# Infrastructure accounts and privileges

Every account needed to run the platform, with the **minimum** privilege it
needs and where it is configured. Nothing here is hardcoded: internal secrets
are generated into the Ansible vault (`scripts/generate_secrets.py`), the
Proxmox tokens are the external secrets you supply.

## Proxmox API identities

| Identity | Privilege | Scope | Used by | Where |
|---|---|---|---|---|
| **provisioning token** (`PROXMOX_API_TOKEN`, vault `vault_proxmox_api_token_value`) | administrative — create/configure VMs, bridges, datastores, pools, firewall, upload snippets/hookscript. Needs `VM.Allocate`, `VM.Config.*`, `VM.PowerMgmt`, `VM.Audit`, `Datastore.Allocate`+`Datastore.AllocateSpace`, `Sys.Modify` (bridges/SDN), `Pool.Allocate`, `SDN.*` and the firewall rights; in practice a token of **`root@pam`** (the hookscript/snippet `file_mode` needs `root@pam`) plus a **root SSH key on the node** (the OpenTofu provider's `ssh { username = root }`). | `/` (whole node) | OpenTofu (every stack's provider) | `opentofu/stacks/*/main.tf`; you set `TF_VAR_proxmox_api_url` / `TF_VAR_proxmox_api_token` on the node |
| **orchestrator power token** `orchestrator@pam!orchestrator-prov` | role `WormsSandboxPower` = **`VM.PowerMgmt` + `VM.Audit`** only | `/pool/sandbox` | orchestrator (conductor game plan) and dashboard→orchestrator sandbox power | `group_vars/all.yml` (`proxmox_api_*`); ACL created by `roles/host/round_agent` |
| **read-only topology token** `topology@pve!readonly` | **`PVEAuditor`** (read-only), on the user AND the token (privilege separation) | `/` | `manage.py collect_topology` (the admin topology view) | `group_vars/all.yml` (`proxmox_readonly_api_*`), secret `vault_proxmox_readonly_api_token_value` |

The provisioning token is root-equivalent by necessity (it builds the whole
platform); keep it off any VM. The orchestrator (a VM) only ever holds the
power token (pool-scoped), so "orchestrator compromised" is not "node
compromised" (see `docs/rounds.md`). The round agent on the node needs no
Proxmox token — it is already root there.

## PostgreSQL roles

TLS-only (`hostssl`, `scram-sha-256`); every role may connect **only from its
own VM's `/32`** (`pg_hba`, `roles/database/setup_db_base`). Table/column grants
are in `roles/database/setup_db_permissions` (least privilege; verified by
`tests/test_database.py`). The database is a dead end: only these roles reach
it, and the malicious VMs never can.

| Role | Privilege | Connects from | Notes |
|---|---|---|---|
| `admin` | **SUPERUSER** | Proxmox host `/32` only | manual administration / the `worms` CLI; never used by a service |
| `django_migrator` | owns every table (DDL) | orchestrator `/32` | runs the migrations; the dashboard never holds it |
| `django_dashboard` | SELECT all; INSERT `uploads`/`malware_artifacts`/`access_tokens`; UPDATE `access_tokens`(revoked), `users`(last_login, password, must_change_password); INSERT/UPDATE/DELETE `django_session`; INSERT/DELETE `devbox_ssh_keys` | dashboard `/32` | public-facing, so read-mostly |
| `django_orchestrator` | SELECT all; INSERT/UPDATE `round_runs`/`game_plan_steps`/`virtual_machines`/`topology_snapshots`/`devboxes`/`devbox_vms`; INSERT `host_jobs`/`run_events`/`flag_captures`/`logging`/`devbox_captures`/`devbox_logs`; UPDATE `teams`(devbox_version), attempts(processed); | orchestrator `/32` | writes only its own results |
| `dbuser_scoring` | INSERT `(team_uuid, vm_flag_hash, devbox)` on `flag_capture_attempts` **only** | scoring `/32`, conn-limit 8 | sandbox-facing, hostile input |
| `dbuser_logging` | INSERT `(uuid, team_uuid, log_data, devbox)` on `logging_attempts` **only** | logging `/32`, conn-limit 8 | sandbox-facing, hostile input |
| `dbuser_network_monitor` | INSERT on `network_flows`/`network_events` **only** (cannot read) | monitor `/32`, conn-limit 4 | parses attacker traffic |
| `round_agent` | SELECT `host_jobs`; UPDATE `host_jobs`(state, timestamps, result, error) | Proxmox host `/32`, conn-limit 4 | job queue only |

## Dashboard application accounts

- **admin** — Django superuser, `is_staff=True`; password from the vault
  (`vault_django_admin_password`); created by `manage.py init`.
- **students** — created from a CSV with one-time passwords, `is_staff=False`,
  `must_change_password=True` (`manage.py create_users`).

## VM OS / service accounts (least privilege)

Every VM boots from cloud-init with SSH **key only**: the Proxmox host key is
authorised for the `debian` login user (Ansible/the agent connect as `debian`
and use `sudo`); `PermitRootLogin prohibit-password`, `PasswordAuthentication
no`, `KbdInteractive/ChallengeResponse no` (the `10-worms-hardening.conf`
drop-in in every template). No other login method works. Services run as
dedicated non-root users wherever possible:

| VM(s) | Service | Runs as |
|---|---|---|
| dashboard, orchestrator | gunicorn (Django) + management timers/daemons | `django` (group `www-data`) |
| scoring, logging | the sandbox API (Flask) | `sandbox-api` (group `www-data`) |
| monitor | Zeek / the flow shipper | `zeek` / `zeek-shipper` |
| database | PostgreSQL | `postgres` |
| dns | dnsmasq | `dnsmasq` |
| all nginx hosts | nginx | `www-data` |
| wazuh | Wazuh manager/indexer/dashboard | `wazuh` (installer-managed) |
| private internet | devpi / cve-search / kiwix / apt-cacher-ng / registry | `devpi` / `cve-search` / `kiwix` / package defaults |
| Proxmox node | the round agent | **root** (it drives tofu/ansible/qm/ssh; cannot be unprivileged) |
| sandbox / devbox VMs | — | rooted/untrusted by design; isolation is the network + firewall, not in-guest privilege |
