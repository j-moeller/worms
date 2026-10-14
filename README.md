# Quick start

```
export PROXMOX_API_TOKEN=<the value of your Proxmox API token>   # the one external secret
make init secrets apply          # OpenTofu stacks + host firewall (Ansible)
host/allow-internet.sh           # packages / cache population need the internet
make                             # idempotent: ... -> inventory -> provision
host/deny-internet.sh            # required before any sandbox round
make private_internet            # caches switch to offline mode
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
place**: `opentofu/shared/shared-variables.tf` (`bridges`, `networks`,
`host_ips`, `infrastructure_ips` (data plane), `management_ips` (SSH),
`services_ips`, `private_internet_ips`). That file is symlinked into every
stack, so OpenTofu, the firewall rules, the VM definitions, the Ansible
inventory (and through it the host firewall) all consume the same values — nothing is
hardcoded twice.

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
`{{ infrastructure_ips.* }}` / `{{ management_ips.* }}` / `{{ services_ips.* }}` /
`{{ private_internet_ips.* }}` reference resolved
from the generated file. So changing an address in OpenTofu + `make inventory`
reconfigures Ansible with no edits to `inventory.yml`.

# Init (what `make` does, idempotently)

`make` runs these in order; each step is safe to re-run:

- `make secrets` — generate management secrets once (no-op if already present).
- `tofu apply` the base stacks (platform, private_internet, persistent,
  ephemeral). Addressing comes from `opentofu/shared/shared-variables.tf` (the
  single source of truth); the platform stack emits the `infrastructure` output.
    - `SetupHostNetwork.yml` (host sysctls + nftables) right after the platform stack
    - persistent: database, dashboard, orchestrator
    - ephemeral: DNS, scoring, logging
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

# Network design

Risk zones, each on its own bridge (addresses: `opentofu/shared/shared-variables.tf`):

| Bridge | Zone | Members | Host IP |
|---|---|---|---|
| vmbr20 | dmz | dashboard (internet via host DNAT :443) | 10.10.0.1 (gateway) |
| vmbr21 | core | database, orchestrator | 10.11.0.1 (gateway) |
| vmbr22 | ingest | scoring/logging database leg | 10.12.0.1 (gateway) |
| vmbr23 | services | scoring/logging sandbox leg, private internet, DNS, sandbox-gw | none |
| vmbr1xxx | sandbox | generated networks (10.64.0.0/10), sandbox VMs, sandbox-gw | none |
| vmbr3 | mirror | traffic mirror for the monitor | none |
| vmbr2 | mgmt-trusted | SSH: database, orchestrator | 10.3.0.1 |
| vmbr5 | mgmt-exposed | SSH: dashboard, scoring, logging, DNS, private internet | 10.5.0.1 |
| vmbr4 | mgmt-sandbox | SSH: sandbox VMs, sandbox-gw | 10.4.0.1 |

Allowed flows (everything else is dropped twice: by the per-VM Proxmox firewall,
input and output policy DROP with `ipfilter`, and for routed traffic by the
host nftables FORWARD chain):

- internet → dashboard:443 (host DNAT, `host_publish_dashboard: true` on `proxmox_host`)
- dashboard → database:5432, orchestrator:443
- scoring/logging → database:5432 (INSERT into one table each)
- orchestrator → Proxmox API:8006 (scoped tokens)
- sandbox VMs ↔ sandbox VMs (the experiment), routed by **sandbox-gw**, a
  router VM: sandbox → services bridge on the scoring/logging/private-internet
  ports only, SNATed to sandbox-gw's address. The host has no IP on any bridge
  a sandbox VM can reach and never routes their traffic.
- host → every VM:22 on its management NIC (management bridges are
  port-isolated, so VMs cannot reach each other there; no VM can open a
  connection to the host except orchestrator → API).

No VM has internet access unless `host/allow-internet.sh` allows it (package
installs and cache population for the dmz/core/ingest/mgmt-exposed VMs through
host NAT); `host/deny-internet.sh` revokes it. Allowing is refused while sandbox
VMs exist; while it is allowed, the sandbox stack (and `make startround`)
refuses to deploy. The state lives in
`/run/worms/internet-allowed` and runtime-only nftables rules, so a reboot
always comes back denied.

The database is the choke point between the sandbox side and the
infrastructure; rows written by scoring/logging are attacker-controlled data.
The dashboard never holds the migrator credentials (migrations run on the
orchestrator).

# Where things are configured

- **OpenTofu**: everything Proxmox-level — bridges, VMs, the per-VM firewall
  (security groups, `ipfilter`), and the hookscript every VM carries
  (`worms-hook.sh`: isolates VM ports on the management bridges, so VMs there
  can only reach the host).
- **Ansible**: VM configuration, and the Proxmox host's own OS settings
  (`SetupHostNetwork.yml`, role `host_network`, runs locally on the node):
  sysctls, the nftables zone firewall + boot unit (`/etc/worms/worms.nft`),
  and the Proxmox host firewall rule orchestrator → API.
- **Scripts** (runtime state only): `host/allow-internet.sh` /
  `host/deny-internet.sh`.

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
