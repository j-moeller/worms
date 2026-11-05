# Accounts, privileges and secrets

Every account the platform needs, with its minimum privilege; what runs where at which
privilege; the dashboard's account rules; and where every secret lives. How to create accounts
day to day: `docs/management.md`.

## 1. Accounts

### 1.1 Proxmox: three API tokens

Defined in `src/worms_cli/tokens.py` (user, role, privileges, scope); values only in the vault.

| Token | Role | Scope | Used by |
|---|---|---|---|
| `worms-provision@pve!worms` | `WormsProvision`: VMs, storage, pools, bridges, firewall. No rights management, users, console or node power | `/` | OpenTofu from the controller, and the round agent on the node |
| `orchestrator@pam!orchestrator-prov` | `WormsSandboxPower`: `VM.Audit`, `VM.PowerMgmt` | `/pool/sandbox` | the orchestrator, for the game plan |
| `topology@pve!readonly` | `WormsTopology`: `Pool.Audit`, `Sys.Audit`, `VM.Audit` | `/` | the orchestrator's `collect_topology`, `worms status` |

All are privilege-separated (a token's rights are the intersection of its user's and its own).
The provisioning token is never on a VM. OpenTofu also logs in to the node as root over SSH to
upload snippets, and the round agent runs as root, so the token does not bound what those can do.

```sh
worms secrets issue proxmox-token        # creates role and user if needed, rotates, stores
worms secrets issue orchestrator-token
worms secrets issue topology-token
worms secrets check                      # does each work, with exactly its rights?
worms build provision                    # after a rotation: deliver to the VMs and the agent
```

`worms` refuses to run while a `*.auto.tfvars` exists, because OpenTofu would prefer it to the vault.

### 1.2 Database roles

TLS only (`hostssl`, `scram-sha-256`); each role connects only from its own VM's `/32`. Grants:
`roles/database/setup_db_permissions`, checked by `tests/test_database.py`.

| Role | Privilege | From |
|---|---|---|
| `admin` | superuser; the `worms` CLI and manual work | the node |
| `django_migrator` | owns the tables, runs migrations | orchestrator |
| `django_orchestrator` | reads all; writes its result tables (runs, game plan, VMs, topology, devboxes), inserts host jobs, events, captures, logs; bumps `teams.devbox_version` | orchestrator |
| `django_dashboard` | reads all; inserts uploads, tokens, SSH keys, memberships, password-reset rows; staff pages: student rows only (a trigger refuses staff/superuser rows), named columns of users, teams, competitions, rounds. Never `username`, `is_staff`, `is_superuser` | dashboard |
| `dbuser_scoring` / `dbuser_logging` | INSERT into `flag_capture_attempts` / `logging_attempts` only, 8 connections | scoring / logging |
| `dbuser_network_monitor` | INSERT into `network_flows`/`network_events` only, 4 connections | monitor |
| `round_agent` | read `host_jobs`, update their state and result, 4 connections | the node |

### 1.3 Dashboard accounts

| Account | Privilege | Created by |
|---|---|---|
| `admin` | superuser and staff | `manage.py init`, login disabled until `worms admin reset-password` |
| staff (tutors) | the staff pages | `worms admin create-staff EMAIL`; the dashboard cannot create staff |
| students | none beyond their team | Staff → Users, one by one or a CSV; one-time password, changed at first sign-in |
| team access tokens | read the team's own results through the team API | team members on their profile; stored as SHA-256 |

### 1.4 Other credentials

| Credential | Purpose | Where |
|---|---|---|
| The vault | every secret | `ansible/group_vars/vault.yml` + `ansible/.vault_pass` on the controller, git-ignored |
| WORMS SSH key | the one key every VM authorizes, and root on the node for the agent; Ansible, the CLI and the agent log in with it | vault; VMs via cloud-init; node `/etc/worms/ssh/worms` |
| Internal CA | signs the VMs' TLS certificates; the key stays on the controller, encrypted | `ansible/pki/worms/` |
| Orchestrator API token | dashboard → orchestrator (devbox reset, key changes) | vault; both VMs' `0600` env files |

## 2. What runs at which privilege

SSH into every VM is key-only, from the node on the management network. Services run as dedicated
users with `NoNewPrivileges` wherever possible.

| Where | Runs as root, and why |
|---|---|
| Infrastructure VMs | auditd and rsyslog (system logging), fail2ban on the dashboard (edits the firewall), nginx masters, dnsmasq until it binds, the monitor's one-shot that sets the capture NIC promiscuous |
| The node | the round agent (it does the privileged host build; bounded by its job-queue-only database role and fixed job menu; no vault, no CA key), `worms-mirror`, the host firewall and its watchdog |

Everything else is a dedicated user: gunicorn as `django`, the sandbox APIs as `sandbox-api`
(no capabilities), Zeek as `zeek` (`CAP_NET_RAW`/`CAP_NET_ADMIN` only), the shipper, the caches
(`apt-cacher-ng`, `devpi`, `docker-registry`, `cve-search`, `kiwix`).

**Untrusted by design** (their privilege does not matter; the network contains them): sandbox VMs
(the worm runs as `debian` with sudo), devbox VMs (teams log in as root), sandbox-gw and
devbox-gws (routers configured by cloud-init). The devbox **SSH gateway** is trusted: teams log
in as the shared `team` user, confined per key (`docs/devbox.md`).

## 3. Dashboard account rules

**First sign-in:** an account with `must_change_password` is held on `/change-password/` until it
sets its own password (`ForcePasswordChangeMiddleware`). The dashboard role may update exactly
`password` and `must_change_password` for this.

**Admin password:** `worms admin reset-password` sets a random 20-character one-time password and
prints it once (stdout; the notice goes to stderr). It is stored nowhere but as Django's hash, and
ends every session of the account. It asks before replacing a password the admin chose (`--yes`
skips that) and refuses to promote a non-superuser.

**Password resets are recorded.** Each one-time password from Staff → Users adds a row to
`password_resets` (when, who asked, whose account; no password or hash), in the same transaction
as the reset. The dashboard role may add and read rows, never change or remove them. Not
recorded: the admin's own reset on the console, users changing their own password, first
passwords. No page shows the table yet:

```sh
ssh -i /etc/worms/ssh/worms debian@10.3.0.16 "sudo -u postgres psql -d mydb -c \
  'SELECT created_at, actor_username, actor_role, target_username, target_role FROM password_resets ORDER BY created_at DESC'"
```

**View as (admin only):** Staff → Users → **View as** shows the dashboard as that user, with a
banner; **Back to admin** leaves it.

| Rule | How |
|---|---|
| Only the admin | An active superuser, checked on every request |
| Cannot be forged | Lives only in the server-side session (`_view_as`), records which admin started it; starting is a POST with CSRF |
| Read only | Every write is refused except leaving, signing out and the Live/Dev switch |
| Ends by itself | When the admin loses the flag or its password changes, or the user is deactivated or made admin |
| Touches nothing | The session stays the admin's; the user's sessions, password and `last_login` do not change |
| Always the Live view | Starting or leaving it resets the Dev switch, and without a team there is no Dev view |

Logged on the dashboard VM: `journalctl -u dashboard | grep view-as`. The team API ignores sessions.

## 4. Where things run, and the vault

**The controller** (a laptop, or the node itself with `WORMS_SSH` empty) holds the checkout, the
vault, the CA and the OpenTofu state of the platform, firewall, infrastructure and
private-internet stacks, and runs OpenTofu and Ansible for them. **The node** holds the round agent
and its workspace `/opt/worms` with the sandbox and devbox stacks and their state, synced from the
checkout by `SetupRoundAgent.yml` before every step that runs there.

What you set, once: `WORMS_SSH=root@<node>` in `~/.worms/config` and the three Proxmox tokens.
SSH to the node is your own (`ssh root@<node>` must work with a key in your ssh-agent; OpenTofu's
provider logs in that way). `worms node trust` is only for VMs older than the WORMS key, or without
an agent of your own.

### 4.1 The vault

ansible-vault (AES256), `0600`, git-ignored (`src/worms_cli/secrets.py`). `worms secrets` shows
what is set; `worms secrets set <name>` stores a value by hand. Everything except the Proxmox
tokens is generated when missing and never rotated: 8 PostgreSQL passwords, the CA passphrase, two
Django `SECRET_KEY`s, the orchestrator API token, devpi's root password, the WORMS SSH key.

Each consumer reads it through one path: OpenTofu gets `TF_VAR_*` in its process environment from
the CLI; Ansible gets `-e @group_vars/vault.yml` plus `-e internet_enabled=<the node's flag>`;
services get root-owned `0600` env files rendered by Ansible; the round agent gets
`/etc/worms/round-agent.env` and `/etc/worms/ssh/worms`; the CLI reaches the database and the
Proxmox API through SSH tunnels via the node.

**Back up together:** the vault and its key, the CA (`ansible/pki/worms/`) and the controller's
stack states (`opentofu/stacks/*/terraform.tfstate`). Without them the platform has to be rebuilt.
Nothing backs up the database (`docs/status.md`).

### 4.2 OpenTofu, Ansible and the CLI

| | Settings |
|---|---|
| OpenTofu | From the CLI: `TF_VAR_proxmox_api_token`, `TF_VAR_proxmox_api_url`, `TF_VAR_ssh_public_key`. Optional: `TF_VAR_proxmox_instance` (default `worms-pve`), `TF_VAR_datastore_id` (`local`), `TF_VAR_gateway_user` (`team`), `TF_CLI_CONFIG_FILE`. The provider does not verify the API's TLS (`insecure = true`, finding C1) |
| Ansible | No environment variables. `ansible.cfg` (inventories, vault key, roles path, no host-key checks, 20 forks, cached facts), `inventory.generated.yml` from `worms build inventory` (from a laptop: the node as `root@<node>`, every VM through it). Run playbooks through `worms`, or pass `-e internet_enabled=true|false` |
| CLI | `WORMS_SSH`, `WORMS_PROXMOX_URL`, `WORMS_REPO_ROOT`, `NO_COLOR`. For development and tests: `WORMS_DB_*` and `WORMS_PROXMOX_*` replace the vault-derived connections |

**On a laptop:** Python 3, OpenTofu, Ansible, OpenSSH, rsync; the node's SSH (22) and API (8006).

### 4.3 Generated for the services

| Service | File | Holds |
|---|---|---|
| Round agent (node) | `/etc/worms/round-agent.env` | `REPO_ROOT=/opt/worms`, its database role, the dashboard/scoring/logging hosts, OpenTofu's `TF_VAR_*` with the provisioning token. `worms-node-run` loads it in an ssh-agent with the WORMS key |
| Dashboard, orchestrator | `django.env` (role `django_app`) | settings module, `SECRET_KEY`, allowed hosts, its own database role; orchestrator: the migrator, the orchestrator API token, the power and read-only Proxmox tokens and PVE's CA; dashboard: `DJANGO_MEDIA_ROOT` (the only path it may write), the orchestrator's URL and token |
| Scoring, logging | `sandbox-api.env` (role `sandbox_api`) | its INSERT-only role, `CURRENT_TEAM_FILE`, `DEVBOX_TEAMS_FILE`, the request limits |
| Monitor | `zeek-shipper.env` | its INSERT-only role, log paths, rate limits |

The orchestrator verifies the Proxmox API with a copy of PVE's CA and reaches it by the node's
name, mapped to `10.11.0.1` in `/etc/hosts`.

### 4.4 Moving a node-run deployment to a laptop

1. Copy into the laptop's checkout, same paths: the vault and its key, `ansible/pki/worms/`, and
   the state of `platform`, `firewall`, `infrastructure/persistent`, `infrastructure/ephemeral`
   and `private_internet`. Leave the sandbox and devbox state on the node.
2. `worms secrets`; set `proxmox-token` if it shows `MISSING`.
3. `worms node trust`, so every VM and the node accept the WORMS key.
4. `worms build provision`, then delete the old shell setup on the node.
