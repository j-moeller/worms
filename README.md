# Quick start

```
export PROXMOX_API_TOKEN=<the value of your Proxmox API token>   # the one external secret
make                             # idempotent, end to end (as root on the Proxmox node)
```

`make` applies the platform stack and the host firewall, then **allows internet
access**, creates the infrastructure VMs (cloud-init installs packages) and
configures them with Ansible (the private-internet caches populate in online
mode). Afterwards it **denies internet access again** — always, also when a step
fails or you interrupt it — and switches DNS and the caches to offline mode.
`make provision` does the same for the Ansible part only.

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

This reads `tofu output -json` and writes `ansible/inventory.generated.yml`:

- **hosts** come from the `ansible_hosts` output of every VM stack
  (persistent, ephemeral, private_internet, sandbox), i.e. from the VMs in the
  OpenTofu **state**. Each VM lands in the group named after its OpenTofu key
  (`db`, `dashboard`, `apt_cache`, ...), with its management IP, Proxmox VMID
  and stack as host vars. Comment out a VM and `tofu apply`: it disappears from
  the inventory and no play contacts it (plays on an empty group are skipped).
- **addressing** (`infrastructure_ips`, `management_ips`, `services_ips`,
  `private_internet_ips`, `networks`, ...) comes from the platform stack.

`ansible/inventory.yml` contains **groups only**: it maps the VM groups to role
groups (`tls_hosts`, `django_hosts`, ...). Per-VM settings live in
`ansible/group_vars/<vm group>.yml`. `make configure`/`make provision`
regenerate the inventory first, so Ansible always follows the current state.

Both inventories and the vault key are wired up in `ansible/ansible.cfg`, so a
single playbook run is just:

```
cd ansible && ansible-playbook -e @group_vars/vault.yml <playbook>.yml
```

# Debugging

```
make inventory                      # hosts Ansible will contact + VMs it skips
make ping                           # one line per host: reachable or not
make debug HOST=db                  # step by step: resolved address, TCP 22,
                                    # SSH login, Python, sudo, cloud-init
make debug HOST=db ARGS=-vvv        # ... with the full SSH command and error
make provision ARGS="--limit db -vvv"           # run only against one VM
make provision ARGS="--limit orchestrator -e hide_secrets=false"
                                    # show output of no_log tasks (secrets!)
```

Every host carries `proxmox_vm_id` (`qm status <vmid>`) and `opentofu_stack`.

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

No VM has internet access unless it is allowed (package installs and cache
population for the dmz/core/ingest/mgmt-exposed VMs through host NAT): `make`
allows it only while provisioning, and `make internet-allow` /
`make internet-deny` toggle it by hand (`ansible/Internet.yml`). Allowing is
refused while sandbox VMs exist; while it is allowed, the sandbox stack refuses
to deploy. The state lives in
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
  and the Proxmox host firewall rule orchestrator → API. Internet access is
  runtime state toggled by `Internet.yml` (same role, `tasks/internet.yml`).
- **Makefile**: the order, and the internet window around provisioning.

# Containment of rogue sandbox VMs

- **Resources** (OpenTofu, `stacks/sandbox`): sandbox VMs and sandbox-gw live on
  the separate Proxmox storage `local-zfs-sandbox` (must exist), with CPU limit
  and lower CPU weight, NIC rate limits and disk IOPS/bandwidth limits
  (`local.limits`).
- **Per-VM network limits** (Ansible `host_network`, bridge table in
  `/etc/worms/worms.nft`): new TCP connections and UDP packet rates per sandbox
  VM address, enforced on the host before sandbox-gw SNATs everything; plus a
  stateless allow-list on the services bridge that holds even if the Proxmox
  firewall is down. Conntrack capacity and timeouts are tuned via sysctls.
- **Firewall watchdog** (`worms-firewall-watchdog.timer`, every 15 s): if
  `proxmox-firewall` is not active, a host table is missing, or a running
  sandbox VM has no rules in the guest ruleset, all sandbox VMs are hard-stopped
  (`qm stop`) and the event is logged to syslog (`auth.crit`).
  Check with `journalctl -t worms-firewall-watchdog`.
- **Sandbox APIs** (Ansible `sandbox_api`, scoring + logging): only `POST
  /submit`; body limits in nginx and Flask; global nginx rate/connection caps;
  team tokens validated against `access_tokens` (column-level SELECT), per-team
  token buckets with temporary bans; payload size/depth/item limits; INSERT-only
  DB roles with connection limits, TLS-verified, statement timeout.
- **Devices** (all VMs): no QEMU guest agent, no serial console, no display, no
  USB tablet; access is SSH only. `qm stop` (and `stop_on_destroy` in
  OpenTofu) kills a VM without guest cooperation.

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
