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

Nothing is hardcoded: internal secrets are generated into the encrypted Ansible
vault (`scripts/generate_secrets.py`); the Proxmox tokens are the external
secrets you supply.

---

## 1. Required accounts

### 1.1 Proxmox

Three API tokens, three different jobs — do not reuse one for another.

| Token | Minimum privilege | Scope | Used by | Supplied as |
|---|---|---|---|---|
| **provisioning token**, e.g. `root@pam!worms` | root-equivalent: `VM.Allocate`, `VM.Config.*`, `VM.PowerMgmt`, `VM.Audit`, `Datastore.Allocate`+`AllocateSpace`, `Sys.Modify` (bridges/SDN), `Pool.Allocate`, `SDN.*`, firewall rights — in practice a **`root@pam`** token without privilege separation, plus **root's SSH key in an ssh-agent** (the OpenTofu provider uploads snippets/the hookscript over SSH; `file_mode` requires `root@pam`) | `/` (whole node) | OpenTofu, on the node only: your `make` / `worms build` **and** the round agent (sandbox/devbox stacks) | `TF_VAR_proxmox_api_token` = `root@pam!worms=<secret>` (full form); vault copy for the agent: `vault_tofu_proxmox_api_token` |
| **orchestrator power token** `orchestrator@pam!orchestrator-prov` | `VM.PowerMgmt` + `VM.Audit` (role `WormsSandboxPower`) — nothing else | `/pool/sandbox` | orchestrator (game plan) | `PROXMOX_API_TOKEN` = the **secret only** → vault `vault_proxmox_api_token_value` |
| **read-only topology token** `topology@pve!readonly` | `PVEAuditor` (read-only; on the user *and* the token, privilege-separated) | `/` | `collect_topology` (admin topology view); also enough for `worms status` | `PROXMOX_READONLY_API_TOKEN` = the **secret only** → vault `vault_proxmox_readonly_api_token_value` |

The provisioning token is root-equivalent **by necessity** (it builds the whole
platform) — keep it on the node, never on a VM. The orchestrator VM only ever
holds the pool-scoped power token, so "orchestrator compromised" ≠ "node
compromised". Identities are in `ansible/group_vars/all.yml`; the sandbox-pool
ACL (user and token) is created by `roles/host/round_agent`.

Create them once, as root on the node (`pveum` prints each token's secret
**once**):

```sh
# 1. provisioning (OpenTofu): root@pam, no privilege separation
pveum user token add root@pam worms --privsep 0
#    -> TF_VAR_proxmox_api_token='root@pam!worms=<secret>'

# 2. orchestrator power (VM power on the sandbox pool; the ACL is set by SetupRoundAgent.yml)
pveum user add orchestrator@pam --comment "WORMS orchestrator: sandbox VM power"
pveum user token add orchestrator@pam orchestrator-prov --privsep 1
#    -> PROXMOX_API_TOKEN=<secret>

# 3. read-only topology (PVEAuditor on the user AND the privilege-separated token)
pveum user add topology@pve --comment "WORMS topology: read-only"
pveum acl modify / --users topology@pve --roles PVEAuditor
pveum user token add topology@pve readonly --privsep 1
pveum acl modify / --tokens 'topology@pve!readonly' --roles PVEAuditor
#    -> PROXMOX_READONLY_API_TOKEN=<secret>
```

### 1.2 Database (PostgreSQL)

TLS-only (`hostssl`, `scram-sha-256`); each role may connect **only from its own
VM's `/32`** (`pg_hba`, `roles/database/setup_db_base`). Grants in
`roles/database/setup_db_permissions`, verified by `tests/test_database.py`.

| Role | Minimum privilege | Connects from |
|---|---|---|
| `admin` | `SUPERUSER` — manual administration / the `worms` CLI; no service uses it | Proxmox host `/32` |
| `django_migrator` | owns the tables (DDL); runs migrations | orchestrator `/32` |
| `django_orchestrator` | `SELECT` all; `INSERT`/`UPDATE` only its own result tables (`round_runs`, `game_plan_steps`, `virtual_machines`, `topology_snapshots`, `devboxes`, `devbox_vms`), `INSERT` `host_jobs`/`run_events`/`flag_captures`/`logging`/`devbox_captures`/`devbox_logs`, `UPDATE teams(devbox_version)` and attempts(`processed`) | orchestrator `/32` |
| `django_dashboard` | `SELECT` all; `INSERT` `uploads`/`malware_artifacts`/`access_tokens`; `UPDATE access_tokens(revoked)`, `users(last_login, password, must_change_password)`; sessions; `INSERT`/`DELETE devbox_ssh_keys` | dashboard `/32` |
| `dbuser_scoring` | `INSERT (team_uuid, vm_flag_hash, devbox)` on `flag_capture_attempts` — only | scoring `/32` (conn-limit 8) |
| `dbuser_logging` | `INSERT (uuid, team_uuid, log_data, devbox)` on `logging_attempts` — only | logging `/32` (conn-limit 8) |
| `dbuser_network_monitor` | `INSERT` on `network_flows`/`network_events` — only (cannot read) | monitor `/32` (conn-limit 4) |
| `round_agent` | `SELECT` `host_jobs`; `UPDATE host_jobs(state, timestamps, result, error)` | Proxmox host `/32` (conn-limit 4) |

### 1.3 Dashboard / Django

| Account | Privilege | Source |
|---|---|---|
| **admin** | Django superuser, `is_staff=True` (Django admin + staff-only pages) | `manage.py init` (login disabled); one-time password from `worms admin reset-password` |
| **students** | plain users, `is_staff=False`, `must_change_password=True` | `manage.py create_users` (CSV → one-time passwords) |
| **tutors/staff** (optional) | `is_staff=True` | `manage.py create_users --staff` |
| **team access tokens** | read-only: the team's own round scoring and logs through the team API (`docs/team_api.md`) — nothing of other teams, nothing writable | created/revoked by team members on the ACCESS page; stored as SHA-256 only |

### 1.4 Other credentials (the stores)

| Credential | Privilege / purpose | Where |
|---|---|---|
| **Ansible vault** (`ansible/.vault_pass` + `group_vars/vault.yml`) | decrypts every internal secret (DB passwords, Django `SECRET_KEY`s, CA passphrase, API tokens) | generated by `scripts/generate_secrets.py`; git-ignored |
| **Proxmox host SSH key** (root) | the one key authorised on every VM; Ansible/the round agent connect with it | provided on init (`ssh_public_key`); distributed via cloud-init |
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
| | auditd / rsyslog (ship to wazuh) | root | ⚠️ system logging daemons (expected) |
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
| **wazuh** | Wazuh manager / indexer / dashboard | `wazuh` / `wazuh-indexer` | ⚠️ vendor installer-managed (not hardened by us) |

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
| round agent (pulls host jobs; drives tofu/ansible/qm/ssh) | root | ⚠️ root **by necessity** — it performs the privileged host build; bounded by its job-queue-only DB role and a fixed job menu (`docs/rounds.md`) |
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
auditd/rsyslog daemons are root system services; Wazuh is vendor-managed. The
untrusted sandbox/devbox VMs and their gateways are root/untrusted by design —
their containment is the network and firewall, not in-guest privilege. Every
application service we control (Django, the sandbox APIs, Zeek, the caches) runs
as a dedicated non-root user with `NoNewPrivileges`.

---

## 3. Managing dashboard accounts

Operational how-to for the accounts in [§1.3](#13-dashboard--django). (Network
and infrastructure accounts are not managed here — see §1.1/§1.2.)

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

### Students — `manage.py create_users <csv>`

CSV with first name / surname / email (English or German headers). Each new
email becomes a user with a one-time random password and `must_change_password`;
existing emails are skipped. Prints an `email,password` CSV to stdout (the only
time the password is shown; `--output creds.csv` to a file). `--staff` for
tutors.

### First login forces a password change

An account with `must_change_password` is held on `/change-password/` by
`ForcePasswordChangeMiddleware` until it sets its own password
(`dashboard/views/password.py`). The public `django_dashboard` DB role is granted
exactly `UPDATE(password, must_change_password)` on `users` for this and nothing
more.

Covered offline by `tests/test_accounts.py` (`worms test`).


---

## 4. Environment

Everything the tools read from the environment, who needs it, and where the
value comes from. Secrets you **supply** are only the three Proxmox tokens
(§1.1); everything else is generated into the vault and delivered by Ansible.

Where things run: `make` / `worms build`, OpenTofu and Ansible run **as root on
the Proxmox node** (it is the Ansible controller: `proxmox_host` is
`ansible_connection: local`). The `worms` CLI runs there too, or on a laptop
with `WORMS_SSH` (build steps then run on the node over SSH).

### 4.1 OpenTofu — `make`, `worms build` (`tofu` in `opentofu/stacks/*`)

| Variable | Required | Value | Notes |
|---|---|---|---|
| `TF_VAR_proxmox_api_url` | **yes** | `https://127.0.0.1:8006/` on the node (or `https://<node>:8006/`) | bpg provider endpoint; TLS is not verified (`insecure = true` in every stack) |
| `TF_VAR_proxmox_api_token` | **yes** | `root@pam!worms=<secret>` — the **full** provisioning token (§1.1) | sensitive; without it `tofu apply -input=false` stops with "No value for required variable" |
| `SSH_AUTH_SOCK` | **yes** | an ssh-agent holding root's key for the node: `eval "$(ssh-agent)" && ssh-add /root/.ssh/id_rsa` | the provider uploads snippets/the hookscript over SSH as root (`ssh { agent = true, username = "root" }`) |
| `TF_VAR_ssh_public_key` | no (`~/.ssh/id_rsa.pub`) | path of the public key cloud-init installs on every VM | must be the public half of the key Ansible and the round agent log in with (root's default identity on the node) |
| `TF_VAR_proxmox_instance` | no (`worms-pve`) | the Proxmox node name | becomes `proxmox.node` in the inventory |
| `TF_VAR_datastore_id` | no (`local`) | datastore for images and snippets | must allow the `snippets` content type |
| `TF_VAR_gateway_user` | no (`team`) | devbox stack: the SSH gateway's login user | |
| `TF_CLI_CONFIG_FILE` | no | an OpenTofu CLI config (e.g. a provider mirror for an offline node) | |

The variables apply to every stack (they share `shared-variables.tf`); a
`terraform.tfvars` per stack works too but has to be repeated six times.

### 4.2 Secrets generation — `worms build secrets` (first `make`)

`scripts/generate_secrets.py` creates `ansible/.vault_pass` and the encrypted
`ansible/group_vars/vault.yml` once, and on later runs only **adds** keys that
are missing (it never rotates). It reads:

| Variable | Value | Stored as | If unset |
|---|---|---|---|
| `PROXMOX_API_TOKEN` | the **secret** of `orchestrator@pam!orchestrator-prov` | `vault_proxmox_api_token_value` | placeholder + a notice; the orchestrator cannot stop/start VMs |
| `PROXMOX_READONLY_API_TOKEN` | the **secret** of `topology@pve!readonly` | `vault_proxmox_readonly_api_token_value` | empty; the TOPOLOGY view says "not configured" |
| `TF_VAR_proxmox_api_token` | the **full** provisioning token (as in §4.1) | `vault_tofu_proxmox_api_token` | empty; the round agent's `tofu apply` (sandbox, devbox) fails |

Set or change them later with
`cd ansible && ansible-vault edit group_vars/vault.yml` (the key file comes
from `ansible.cfg`), then re-run `worms build provision` (or the one playbook
that delivers it: `SetupRoundAgent.yml` for the provisioning token,
`SetupDbAndDashboard.yml` for the two others).

### 4.3 Ansible — `worms build <component>` (`ansible-playbook` in `ansible/`)

**No environment variables are needed**; everything is in files:

| File | From | Purpose |
|---|---|---|
| `ansible/ansible.cfg` | the repo | inventories, `vault_password_file = .vault_pass`, roles path, no host-key prompts; speed: 20 forks, facts cached for one `worms build` run (`ansible/.facts`, emptied by the CLI at the start and after every tofu apply/destroy), SSH connections reused for 10 min |
| `ansible/.vault_pass` | `generate_secrets.py` (0600, git-ignored) | decrypts the vault — never prompts |
| `ansible/group_vars/vault.yml` | `generate_secrets.py` (encrypted, git-ignored) | every secret; passed as `-e @group_vars/vault.yml` |
| `ansible/inventory.generated.yml` | `worms build inventory` (`scripts/generate_inventory.py` reads `tofu output -json`; needs no credentials) | every VM's addresses; re-generated after each `tofu apply` |
| `/root/.ssh/id_rsa` | you (the node's root key) | Ansible logs in to the VMs as `debian` with the default identity — the private half of `TF_VAR_ssh_public_key` |
| `ansible/pki/worms/ca.{crt,key}` | `CreateCA.yml` (key encrypted with `vault_pki_ca_passphrase`) | the internal CA |

Collections used beyond `ansible.builtin`: `community.crypto`,
`community.postgresql`, `ansible.posix`. Standard `ANSIBLE_*` variables
still work but override `ansible.cfg`; `worms build <component> -- <args>`
appends extra `ansible-playbook` arguments.

### 4.4 The `worms` CLI (`src/manage.py`)

Read from the environment or `~/.worms/config` (`--config` for another file):
`KEY=value` lines, `#` only at the start of a line (no inline comments, no
quotes). Real environment > `--config` > `~/.worms/config`. Only `WORMS_*` keys
are taken from a config file.

| Variable | Needed for | Default | Value |
|---|---|---|---|
| `WORMS_DB_HOST` | `teams`, `users`, `leaderboard`, `reset-database`, the competition part of `status` | — (unset: "no database configured") | `10.11.0.16` (the database's core address) |
| `WORMS_DB_PORT` | " | `5432` | |
| `WORMS_DB_NAME` | " | `mydb` | `postgres_db` in `group_vars/all.yml` |
| `WORMS_DB_USER` | " | `admin` | `admin` (superuser; `pg_hba` admits it only from the node's own address `10.11.0.1`) |
| `WORMS_DB_PASSWORD` | " | — | `vault_postgres_admin_password` |
| `WORMS_DB_SSLMODE` | " | `verify-full` | `verify-ca` through an SSH tunnel (the certificate names `10.11.0.16`, not `127.0.0.1`) |
| `WORMS_DB_SSLROOTCERT` | " | the checkout's `ansible/pki/worms/ca.crt` (system bundle if absent) | the internal CA |
| `WORMS_PROXMOX_URL` | the infrastructure part of `status` | — (unset: "no Proxmox API configured") | `https://127.0.0.1:8006` on the node |
| `WORMS_PROXMOX_USER` | " | `root@pam` | `topology@pve` |
| `WORMS_PROXMOX_TOKEN_NAME` | " | — | `readonly` |
| `WORMS_PROXMOX_TOKEN_VALUE` | " | — | `vault_proxmox_readonly_api_token_value` (read-only is enough) |
| `WORMS_PROXMOX_NODE` | " | `worms-pve` | the node name |
| `WORMS_PROXMOX_VERIFY_SSL` | " | `1` | keep `1` and point `REQUESTS_CA_BUNDLE` at PVE's CA (below); `0` only for local experiments |
| `REQUESTS_CA_BUNDLE` | " (environment only, not the config file) | — | `/etc/pve/pve-root-ca.pem` on the node: the CA of PVE's own API certificate, which names `localhost`/`127.0.0.1` |
| `WORMS_SSH` | build steps and the internet probe from a laptop | empty = this machine is the node | `root@<node>` |
| `WORMS_REPO_ROOT` | build | the checkout the CLI runs from | the checkout **on the node** (build steps `cd` there) |
| `NO_COLOR` | output | colours on a TTY | any value: no colours |

On the node with `WORMS_SSH` empty, `status` reads the internet flag
(`/run/worms/internet-allowed`) directly; elsewhere it needs `WORMS_SSH`.

### 4.5 Example: root's environment on the node

```sh
# ~/.worms/env -- source it from ~/.bashrc (chmod 600)
export TF_VAR_proxmox_api_url=https://127.0.0.1:8006/
export TF_VAR_proxmox_api_token='root@pam!worms=<provisioning secret>'
export PROXMOX_API_TOKEN='<orchestrator-prov secret>'        # read by the first `make`
export PROXMOX_READONLY_API_TOKEN='<readonly secret>'         # read by the first `make`
export REQUESTS_CA_BUNDLE=/etc/pve/pve-root-ca.pem
[ -n "$SSH_AUTH_SOCK" ] || { eval "$(ssh-agent -s)" >/dev/null && ssh-add -q /root/.ssh/id_rsa; }
```

```ini
# ~/.worms/config (chmod 600) -- the CLI
WORMS_DB_HOST=10.11.0.16
WORMS_DB_USER=admin
WORMS_DB_PASSWORD=<vault_postgres_admin_password>
WORMS_PROXMOX_URL=https://127.0.0.1:8006
WORMS_PROXMOX_USER=topology@pve
WORMS_PROXMOX_TOKEN_NAME=readonly
WORMS_PROXMOX_TOKEN_VALUE=<vault_proxmox_readonly_api_token_value>
```

The two vault values, after the first `make`:

```sh
cd ansible && ansible-vault view group_vars/vault.yml \
  | grep -E '^(vault_postgres_admin_password|vault_proxmox_readonly_api_token_value):'
```

From a laptop: `WORMS_SSH=root@<node>` for the build and the internet probe,
and an SSH tunnel for the database — the connection then comes from the node's
address, which `pg_hba` admits for `admin`:

```sh
ssh -N -L 5432:10.11.0.16:5432 root@<node> &
WORMS_DB_HOST=127.0.0.1 WORMS_DB_SSLMODE=verify-ca \
WORMS_DB_SSLROOTCERT=<copy of ansible/pki/worms/ca.crt> worms teams
```

### 4.6 Generated for the services (never set by hand)

Rendered by Ansible from the vault and the inventory into root-owned `0600`
files read by systemd; listed so you know what each service sees.

| Service | File | Variables |
|---|---|---|
| round agent (node) | `/etc/worms/round-agent.env` | `REPO_ROOT`, `PYTHONPATH`; `DB_HOST/PORT/NAME/USER/PASSWORD/SSLMODE/SSLROOTCERT` (role `round_agent`); `DASHBOARD_HOST`, `DASHBOARD_MEDIA_ROOT`, `SCORING_HOST`, `LOGGING_HOST`, `RESET_SERVICES`; **OpenTofu**: `TF_VAR_proxmox_api_url` (`https://127.0.0.1:8006/`), `TF_VAR_proxmox_api_token` (`vault_tofu_proxmox_api_token`), `TF_VAR_proxmox_instance`, `TF_VAR_ssh_public_key`. The service runs under its own `ssh-agent` with `/root/.ssh/id_rsa` (`round_agent_ssh_key`). |
| dashboard, orchestrator (Django) | `django.env` (role `django_app`) | `PRODUCTION=1`, `DJANGO_SETTINGS_MODULE`, `DJANGO_SECRET_KEY`, `DJANGO_ALLOWED_HOSTS`, `DB_NAME/HOST/PORT/SSLMODE/SSLROOTCERT`, `DB_USER/PASSWORD` (the VM's own role); orchestrator also `DB_MIGRATOR_USER/PASSWORD`, `ORCHESTRATOR_DASHBOARD_TOKEN`, `PROXMOX_HOST/PORT/NODE/USER/TOKEN_NAME/TOKEN_VALUE` (power token), `PROXMOX_READONLY_USER/TOKEN_NAME/TOKEN_VALUE`, optional `PROXMOX_VERIFY_SSL`, `PROXMOX_CA_BUNDLE`, `PROXMOX_TIMEOUT`; dashboard also `ORCHESTRATOR_URL`, `ORCHESTRATOR_TLS_NAME`, `ORCHESTRATOR_API_TOKEN`, optional `DJANGO_CSRF_TRUSTED_ORIGINS` (only if another proxy in front rewrites the Host header). `DJANGO_ALLOWED_HOSTS` is `dashboard.worms.internal`, the dashboard's IP and `dashboard_public_hosts` (`group_vars/dashboard.yml`: add the node's public name when the dashboard is published on the WAN) |
| scoring, logging (sandbox API) | `sandbox-api.env` (role `sandbox_api`) | `SANDBOX_API_KIND`, `DB_HOST/PORT/NAME/USER/PASSWORD/SSLMODE/SSLROOTCERT` (the INSERT-only `dbuser_scoring` / `dbuser_logging`), `DB_POOL_SIZE`, `CURRENT_TEAM_FILE`, `DEVBOX_TEAMS_FILE`, `CLIENT_IP_HEADER`, the limits (`MAX_BODY_BYTES`, `LOG_MAX_BYTES/DEPTH/ITEMS`, `TEAM_RATE_PER_MINUTE`, `TEAM_BURST`, `TEAM_BAN_AFTER/SECONDS`) |
| monitor (zeek-shipper) | `zeek-shipper.env` (role `network_monitor`) | `DB_*` (the INSERT-only `dbuser_network_monitor`), `ZEEK_LOG_DIR`, `ZEEK_ARCHIVE_DIR`, `STATE_FILE`, `EVENT_LOGS`, the rate limits (`MAX_FLOWS_PER_SECOND`, `FLOW_BURST`, `MAX_EVENTS_PER_SECOND`, `EVENT_BURST`, `MAX_EVENT_BYTES`), `REPORT_SECONDS` |

Open item: the orchestrator verifies the Proxmox API certificate
(`PROXMOX_VERIFY_SSL=1` by default), but PVE's default certificate is
self-signed by PVE's own CA and does not name `10.11.0.1`, the address the
orchestrator connects to. Until the node's API certificate is issued for that
address (e.g. by the internal CA, which the VMs trust) — or
`PROXMOX_CA_BUNDLE` / `PROXMOX_VERIFY_SSL` are set through `django_extra_env`
— the orchestrator's Proxmox calls fail TLS verification (`docs/findings.md`).

