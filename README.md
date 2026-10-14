# Quick start

```
export PROXMOX_API_TOKEN=<the value of your Proxmox API token>   # the one external secret
make            # idempotent: init -> secrets -> tofu apply -> inventory -> provision
```

`make` prints the VM names and IP addresses it is configuring, and is safe to
re-run: once everything is in place it changes nothing. `make show-config`
reprints the configuration at any time.

# Secrets (you don't manage these)

All internal management credentials — every PostgreSQL role password, the
Django `SECRET_KEY`s and the internal CA passphrase — are generated **once** by
`scripts/generate_secrets.py` (run via `make secrets`) into
`ansible/group_vars/vault.yml`, encrypted with ansible-vault using an
auto-generated `ansible/.vault_pass`. You never type, see or track them, and
because they persist, re-running `make` does not rotate them (which would break
the already-configured database). Both files are git-ignored.

The only secret you must supply is the **Proxmox API token** (it must equal the
token Proxmox issued you). Set `PROXMOX_API_TOKEN` before the first `make`, or
add it later with:

```
cd ansible && ansible-vault edit --vault-password-file .vault_pass group_vars/vault.yml
```

`group_vars/all.yml` references all of these as `{{ vault_* }}`; the Django
hosts and the database receive their credentials through root-owned `0600`
files, so secrets are passed securely at initialization and never land in a
world-readable location. `group_vars/vault.yml.example` lists the keys for
reference.

# Addressing: OpenTofu is the single source of truth

All non-sandbox IP addresses, subnets and the Proxmox host are defined in **one
place**: `opentofu/shared/shared-variables.tf` (`infrastructure_ips`,
`private_internet_ips`, `networks`, proxmox host). That file is symlinked into
every stack, so OpenTofu, the firewall rules and the VM definitions all consume
the same values — nothing is hardcoded twice.

Sandbox VM addresses are **not** here: they are dynamic and owned entirely by
`opentofu/stacks/sandbox` (the generated `generated_vms*.tf`).

Ansible does not define IPs at all. It is generated from OpenTofu:

```
# after `tofu apply` on the platform stack (and sandbox, if deploying sandboxes):
make inventory        # runs scripts/generate_inventory.py
```

This reads `tofu output -json` from the platform and sandbox stacks and writes
`ansible/inventory.generated.yml` (ground-truth vars + the dynamic `sandbox`
group). Always run Ansible with **both** inventories:

Both inventories and the vault key are wired up in `ansible/ansible.cfg`, so a
single playbook run is just:

```
cd ansible && ansible-playbook -e @group_vars/vault.yml <playbook>.yml
```

`inventory.yml` keeps the semantic groups/roles (which host is a Django vs Flask
host, its settings module, DNS entries, TLS names); every IP in it is a
`{{ infrastructure_ips.* }}` / `{{ private_internet_ips.* }}` reference resolved
from the generated file. So changing an address in OpenTofu + `make inventory`
reconfigures Ansible with no edits to `inventory.yml`.

# Init (what `make` does, idempotently)

`make` runs these in order; each step is safe to re-run:

- `make secrets` — generate management secrets once (no-op if already present).
- `tofu apply` the base stacks (platform, private_internet, persistent,
  ephemeral). Addressing comes from `opentofu/shared/shared-variables.tf` (the
  single source of truth); the platform stack emits the `infrastructure` output.
    - persistent: database, dashboard, orchestrator (each on its own
      maintenance IP, defined in shared-variables.tf)
    - ephemeral: scoring, logging
- `make inventory` — generate `ansible/inventory.generated.yml` from OpenTofu
  and print the VM/IP configuration.
- `make provision` — Ansible sets up:
    - DNS
    - CA
    - DB (creates least-privilege users, TLS-only)
    - Logging
    - Scoring
    - Dashboard + Orchestrator (Django hosts; each started with its own
      settings module: `config.settings.dashboard` / `config.settings.orchestrator`)
- Django migrates DB tables (as the `django_migrator` role)
- Ansible sets up:
    - DB (sets per-user least-privilege table permissions)

# Components & least-privilege communication

- Dashboard (public-facing later) → database:5432 (read-only role +
  INSERT on uploads), and → orchestrator:443.
- Orchestrator → Proxmox API:8006 (proxmoxer, scoped API token) and
  → database:5432 (writes virtual_machines / worm_deployment_plans /
  worm_deployments, reads the rest).
- Database accepts :5432 only from the named service IPs; each DB role may
  connect only from its own VM (`/32` in `pg_hba`, `scram-sha-256`, TLS).

- Orchestrator → Proxmox API:8006 with a second, **read-only** token
  (PVEAuditor) to collect the topology; it writes it to `topology_snapshots`,
  which the dashboard reads. The dashboard never talks to Proxmox (its egress
  policy is DROP with only db:5432 and orchestrator:443 allowed).

- Management/SSH: infrastructure VMs on `vmbr2` (10.3.0.0/24), sandbox VMs on
  their own `vmbr4` (10.4.0.0/16), so untrusted VMs share no network with the
  infrastructure. The `ssh` group accepts port 22 only from the Proxmox host
  (10.3.0.1 / 10.4.0.1); sandbox VMs only on their management NIC.

The firewall security groups in `opentofu/stacks/platform/main.tf` enforce the
network side; the PostgreSQL roles/grants in the `setup_db_*` roles enforce the
data side.

# Round start

- Python generates sandbox, secrets
- Add round + sandbox (including secrets) to DB
- OpenTofu sets up:
    - sandbox
    - monitor VM
- `make inventory` — refresh `inventory.generated.yml` so the new sandbox VMs
  (with the IPs OpenTofu just assigned) appear in Ansible's `sandbox` group.
- Python sets up "mirroring" of vmbrs to monitor network
- Python generates worm deployment (adds to DB)
- Ansible sets up (over the sandbox group):
    - distribute the self-signed CA / certificates
    - CTF flags in `/etc/flags`, other sandbox config
- Orchestrator delivers files to the sandboxes

# Round end

- cd opentofu/stacks/sandbox && tofu destroy

# Topology view (dashboard → TOPOLOGY, staff only)

Shows every VM, bridge, NIC and the connections the Proxmox firewall actually
permits (security groups expanded, first-match evaluation, egress of the source
AND ingress of the target), with toggles for bridges, pools, services/ports and
security groups. It also states explicitly who can reach the Proxmox API.

Data flow: `orchestrator` runs `manage.py collect_topology` every 2 minutes
(systemd timer `orchestrator-collect-topology.timer`), reads Proxmox with a
read-only token (GET only, config fields whitelisted, no cloud-init secrets),
and upserts one row in `topology_snapshots`. The dashboard analyses that row
(`dashboard/topology.py`) and serves it at `/topology/data/`.

Credentials (the user/token are assumed to exist, role `PVEAuditor` on `/`
for the user and, with privilege separation, for the token):

- user + token id: `proxmox_readonly_api_user` / `proxmox_readonly_api_token_name`
  in `ansible/group_vars/all.yml` (default `topology@pve` / `readonly`)
- token secret: `vault_proxmox_readonly_api_token_value` in the vault:

  ```
  cd ansible && ansible-vault edit --vault-password-file .vault_pass group_vars/vault.yml
  ```

  (or export `PROXMOX_READONLY_API_TOKEN` before the very first `make`).

Then `make provision` (or `ansible-playbook -e @group_vars/vault.yml
SetupDbAndDashboard.yml`) delivers it to the orchestrator's 0600 env file.
