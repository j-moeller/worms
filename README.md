# Quick start

From your laptop (the controller: it runs OpenTofu and Ansible), with root SSH
access to the Proxmox node and the three Proxmox API tokens created
(`docs/accounts.md` §1.1 has the `pveum` commands):

```
echo 'WORMS_SSH=root@<node>' >> ~/.worms/config
worms secrets set proxmox-token        # root@pam!worms=<secret>   (OpenTofu)
worms secrets set orchestrator-token   # orchestrator@pam!orchestrator-prov
worms secrets set topology-token       # topology@pve!readonly (optional)
make                                   # = worms build: idempotent, end to end
```

SSH to the node is your own (`ssh root@<node>` must work with a key in your
ssh-agent: OpenTofu's provider logs in that way). `worms node trust` is only
needed once for VMs that existed before (moving a node-run deployment to the
laptop) or without an ssh-agent of your own.

(`alias worms='python3 /path/to/worms/src/manage.py'`.) No environment
variables: the vault is the only place secrets enter (`docs/accounts.md` §4,
which also covers moving an existing node-run deployment to a laptop).

`make` applies the platform and firewall stacks and the host firewall, then **allows internet
access**, creates the infrastructure VMs (cloud-init: first boot only) and
installs and configures them with Ansible (the private-internet caches
populate in online mode). Afterwards it **denies internet access again** — always, also when a step
fails or you interrupt it — and switches DNS and the caches to offline mode.
`worms build provision` does the same for the Ansible part only.

`make` prints the VM names and IP addresses it is configuring, and is safe to
re-run: once everything is in place it changes nothing. `worms build show-config`
reprints the configuration at any time.

# Secrets: one vault

Every secret lives in one place, `ansible/group_vars/vault.yml` on the
controller, encrypted with ansible-vault (key `ansible/.vault_pass`, both
git-ignored; `src/worms_cli/secrets.py`). `worms secrets` shows what is set
(never a value).

- **You enter** only the **Proxmox API tokens**, once (`worms secrets set
  <name>`, prompted without echo): they must equal what Proxmox issued.
- **Generated** the first time they are missing, never rotated: every
  PostgreSQL role password, the Django `SECRET_KEY`s, the internal CA
  passphrase, the orchestrator API token and the **WORMS SSH key**, the one key
  every VM and the node accept (OpenTofu, Ansible, the CLI and the round agent
  log in with it). The CA private key (`ansible/pki/worms/ca.key`, git-ignored)
  is always encrypted with the passphrase; `CreateCA.yml` refuses to run
  without it.

Each consumer reads the vault through one path: OpenTofu gets its token in its
process environment from the CLI, Ansible with `-e @group_vars/vault.yml`, the
services (and the round agent on the node) through root-owned `0600` files that
Ansible renders, the CLI's database and Proxmox access through SSH tunnels via
the node. The dashboard admin has no stored password at all: `worms admin
reset-password` prints a one-time password once.

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
worms build inventory        # runs scripts/generate_inventory.py
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
`ansible/group_vars/<vm group>.yml`. `worms build configure`/`worms build provision`
regenerate the inventory first, so Ansible always follows the current state.

Both inventories and the vault key are wired up in `ansible/ansible.cfg`, so a
single playbook run is just:

```
cd ansible && ansible-playbook -e @group_vars/vault.yml <playbook>.yml
```

# Debugging

```
worms build inventory                      # hosts Ansible will contact + VMs it skips
worms build ping                           # one line per host: reachable or not
worms debug database                # step by step: resolved address, TCP 22,
                                    # SSH login, Python, sudo, cloud-init
worms debug database -- -vvv    # ... with the full SSH command and error
worms build provision -- --limit database -vvv        # run only against one VM
worms build provision -- --limit orchestrator -e hide_secrets=false
                                    # show output of no_log tasks (secrets!)
```

Every host carries `proxmox_vm_id` (`qm status <vmid>`) and `opentofu_stack`.

# Init (what `make` does, idempotently)

`make` runs these in order; each step is safe to re-run:

- `worms build secrets` — generate management secrets once (no-op if already present).
- `tofu apply` the base stacks. Addressing comes from
  `opentofu/shared/shared-variables.tf` (the single source of truth).
    - platform: bridges, images, cloud-init snippets, pools, hookscript; emits
      the `infrastructure` output (addressing for Ansible)
    - firewall: Proxmox cluster/node firewall and all security groups; emits
      `security_groups`, which every VM stack attaches
    - `SetupHostNetwork.yml` (host sysctls + nftables, `worms build host-network`)
    - private_internet: the private-internet VMs
    - persistent: database, dashboard, orchestrator, wazuh
    - ephemeral: DNS, scoring, logging, monitor
- `worms build inventory` — generate `ansible/inventory.generated.yml` from OpenTofu
  and print the VM/IP configuration.
- `worms build provision` — Ansible sets up:
    - DNS
    - CA
    - DB (creates least-privilege users, TLS-only)
    - Logging
    - Scoring
    - Dashboard + Orchestrator (Django hosts; each started with its own
      settings module: `config.settings.dashboard` / `config.settings.orchestrator`)
    - Network monitor: Zeek + zeek-shipper (`SetupNetworkMonitor.yml`, see
      "Network monitoring")
    - Wazuh + auditd on every infrastructure VM (`SetupAudit.yml`, see
      "Central logging")
- Django migrates DB tables (as the `django_migrator` role)
- Ansible sets up:
    - DB (sets per-user least-privilege table permissions)

# Network design

Risk zones, each on its own bridge (addresses: `opentofu/shared/shared-variables.tf`):

| Bridge | Zone | Members | Host IP |
|---|---|---|---|
| vmbr20 | dmz | dashboard (internet via host DNAT :443) | 10.10.0.1 (gateway) |
| vmbr21 | core | database, orchestrator | 10.11.0.1 (gateway) |
| vmbr22 | ingest | scoring/logging/monitor database leg | 10.12.0.1 (gateway) |
| vmbr24 | siem | wazuh (central log server) | 10.13.0.1 (gateway) |
| vmbr23 | services | scoring/logging sandbox leg, private internet, DNS, sandbox-gw, devbox-gws | none |
| vmbr1xxx | sandbox | generated networks (10.64.0.0/10), sandbox VMs, sandbox-gw | none |
| vmbr9xxx | devbox | generated per-team devbox networks (10.128.0.0/10), devbox VMs, devbox-gws | none |
| vmbr3 | mirror | copy of all sandbox traffic, monitor capture NIC (receive-only) | none |
| vmbr2 | mgmt-trusted | SSH: database, orchestrator | 10.3.0.1 |
| vmbr5 | mgmt-exposed | SSH: dashboard, scoring, logging, monitor, DNS, private internet, wazuh, SSH gateway | 10.5.0.1 |
| vmbr4 | mgmt-sandbox | SSH: sandbox VMs, sandbox-gw | 10.4.0.1 |
| vmbr6 | mgmt-devbox | SSH: devbox VMs, devbox-gws (untrusted maintenance) | 10.6.0.1 |
| vmbr7 | devbox-ssh | devbox SSH fabric: the SSH gateway ↔ devbox VMs (port-isolated) | 10.7.0.1 |

Allowed flows (everything else is dropped twice: by the per-VM Proxmox firewall,
input and output policy DROP with `ipfilter`, and for routed traffic by the
host nftables FORWARD chain):

- internet → dashboard:443 (host DNAT, `host_publish_dashboard: true` on `proxmox_host`)
- dashboard → database:5432, orchestrator:443
- scoring/logging → database:5432 (INSERT into one table each)
- monitor → database:5432 (INSERT into network_flows / network_events)
- host → mirror bridge: a copy of every frame entering a sandbox bridge
  (`worms-mirror`); nothing the monitor sends there goes anywhere
- orchestrator → Proxmox API:8006 (scoped tokens)
- every infrastructure VM → wazuh:514 (syslog, audit logs), routed by the host
  from dmz/core/ingest and, for DNS and the private-internet VMs (whose default
  route is their management NIC), from mgmt-exposed
- sandbox VMs ↔ sandbox VMs (the experiment), routed by **sandbox-gw**, a
  router VM: sandbox → services bridge on the scoring/logging/private-internet
  ports only, SNATed to sandbox-gw's address. The host has no IP on any bridge
  a sandbox VM can reach and never routes their traffic.
- host → every VM:22 on its management NIC (management bridges are
  port-isolated, so VMs cannot reach each other there; no VM can open a
  connection to the host except orchestrator → API).

No VM has internet access unless it is allowed (package installs and cache
population for the dmz/core/ingest/mgmt-exposed VMs through host NAT): `make`
allows it only while provisioning, and `worms build internet-allow` /
`worms build internet-deny` toggle it by hand (`ansible/Internet.yml`). Allowing is
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
  (security groups in `opentofu/stacks/firewall`, `ipfilter` per VM), and the
  hookscript every VM carries
  (`worms-hook.sh`: isolates VM ports on the management bridges, so VMs there
  can only reach the host).
- **Ansible**: VM configuration (roles grouped by component under
  `ansible/roles/`, see `ansible/roles/README.md`), and the Proxmox host's own OS settings
  (`SetupHostNetwork.yml`, role `host_network`, runs locally on the node):
  sysctls, the nftables zone firewall + boot unit (`/etc/worms/worms.nft`),
  and the Proxmox host firewall rule orchestrator → API. Internet access is
  runtime state toggled by `Internet.yml` (same role, `tasks/internet.yml`).
- **Makefile**: the order, and the internet window around provisioning.

Stack dependencies (each reads the others' local state via
`terraform_remote_state`): `platform` and `firewall` are independent; the VM
stacks (`private_internet`, `infrastructure/*`, `sandbox`) need both. Destroy in
reverse (`make destroy_all`). Existing deployments made before the firewall
stack are migrated by handled automatically by `worms build platform` / `worms build firewall` (state only).

# Central logging (Wazuh + auditd)

`SetupAudit.yml` (part of `worms build provision`):

- **wazuh** VM (persistent stack, siem bridge, 4 vCPU / 2 GB / 50 GB; Wazuh recommends
  8 GB, so the indexer heap is capped at 512 MB): Wazuh
  all-in-one (manager, indexer, dashboard), installed with the official
  installation assistant (role `wazuh_server`). The manager listens for syslog
  on tcp 514 from the infrastructure zones and archives **every** event, not
  only those matching an alert rule.
- **every infrastructure VM** (`audit_hosts`: database, dashboard,
  orchestrator, scoring, logging, monitor, DNS, private internet; not the sandbox):
  `auditd` records executed commands and changes to accounts, sudo, SSH,
  systemd units, cron and kernel modules (`roles/audit/auditd/templates/worms.rules.j2`);
  `rsyslog` ships `/var/log/audit/audit.log` to wazuh over TCP and queues on
  disk while wazuh is unreachable. The audit keys (`audit-wazuh-c`, ...) are
  the ones Wazuh's built-in audit rules know.

The dashboard is reachable from the Proxmox host only. From your machine:

```
ssh -L 8443:10.13.0.30:443 root@<proxmox>      # then open https://localhost:8443
```

Log in as `admin`; the generated passwords are on the wazuh VM:

```
ssh debian@10.5.0.30 sudo tar -O -xf /root/wazuh-install-files.tar wazuh-install-files/wazuh-passwords.txt
```

Alerts are in *Threat Hunting*. To search all raw audit events, create the index
pattern `wazuh-archives-*` once (*Dashboard management → Index patterns*,
time field `timestamp`) and use *Discover*. Events from the VMs show the
sending VM's address in `location` and its hostname in `predecoder.hostname`.

# Network monitoring

The **monitor** VM (ephemeral stack) records every connection on the sandbox
networks in the database: `worms-mirror` on the host copies each frame that
enters a sandbox bridge into the mirror bridge (`vmbr3`); Zeek on the
monitor's capture NIC analyses it; `zeek-shipper` inserts one row per
connection into `network_flows` (time, addresses, ports, tcp/udp/icmp,
application protocol detected from the payload, bytes and packets per
direction, TCP state, ...) and application details (DNS names, HTTP requests,
TLS server names, ...) plus heartbeats into `network_events`. INSERT-only
role, TLS-verified. Design, research and limitations: `docs/monitoring.md`.

```
worms-mirror status                      # on the host: which ports are mirrored
SELECT * FROM network_flows ORDER BY ts DESC LIMIT 20;
```

# Testing

```
worms test        # offline suite: OpenTofu/Ansible checks, DB privileges, APIs,
                 # zeek-shipper, worms-mirror (network namespaces); no Proxmox needed
worms simulate    # end to end on the deployed platform: a sandbox VM submits to
                 # scoring/logging and makes traffic; checks the database
                 # (submissions, flows exactly once with exact sizes), services
                 # and containment; PASS/FAIL report
```

Details: `docs/testing.md`.

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
  no access token (only one team runs at a time, so the round agent tells them
  the running team's uuid, which they record on each attempt — see
  `docs/rounds.md`), a token bucket with temporary bans; payload
  size/depth/item limits; INSERT-only DB roles with connection limits,
  TLS-verified, statement timeout.
- **Devices** (all VMs): no QEMU guest agent, no serial console, no display, no
  USB tablet; access is SSH only. `qm stop` (and `stop_on_destroy` in
  OpenTofu) kills a VM without guest cooperation.

# Devbox (team development environments)

Each team gets its own always-on, untrusted clone of the sandbox VMs (three VMs
A–B–C) to develop against, reachable only by that team over a locked-down SSH
gateway, completely isolated from the sandbox and from other teams' devboxes.
Teams see it in the dashboard's **development view** (devbox score, logs, SSH
keys, reset); scoring/logging attribute a devbox submission to its team by
source address, so it never interferes with a running round. Full design,
the devbox⇔sandbox isolation and the SSH gateway: **`docs/devbox.md`**.

```
worms build devbox                 # build + configure devboxes and the SSH gateway
worms devbox provision             # after creating teams on the dashboard: their devboxes
ssh -J team@<proxmox-wan>:2222 root@<devbox VM>
```

# Accounts and the management CLI

- **Admin + competition**: `manage.py init` (idempotent) creates the `admin`
  account (login disabled) and the default competition `WORMS <year>`;
  `worms admin reset-password` prints a one-time password for it, once.
- **Staff pages on the dashboard** (staff menu, top right): the registration
  window and the development competition, student accounts (one by one or a
  CSV, one-time passwords shown once, the dashboard forces a change at the
  first login), teams and members, and the rounds (definition, then runs,
  host jobs and errors). Database records only: the dashboard's role is
  column-scoped and cannot create staff. Staff accounts: `worms admin
  create-staff`. See **`docs/accounts.md`** §3.
- **`worms` CLI** (`src/manage.py`): building, the admin and staff accounts,
  devbox provisioning, live status from the database and Proxmox — run from
  your laptop. See **`docs/management.md`**.

# Rounds (orchestration)

Each team's worm is evaluated, one team after another, in a freshly built
sandbox with its own game plan. This is **automated**: the conductor
(`manage.py conduct_rounds`, a daemon on the orchestrator) plans the runs of
any round whose deadline has passed and drives them through their lifecycle;
the privileged host work (building the sandbox, deploying the worm, releasing
secrets, teardown) is done by the **round agent** on the Proxmox host, which
pulls jobs from the database. The orchestrator itself only controls sandbox VM
power (a pool-scoped token), for the game plan.

Full design, lifecycle, game plan, the timeline data for the animated view,
operations and open items: **`docs/rounds.md`**.

```
worms build provision     # installs the conductor daemon, the round agent and the
                   # pool-scoped power ACL; then rounds run on their own
# watch:
ssh debian@10.3.0.19 journalctl -u orchestrator-conduct-rounds -f
ssh root@<proxmox>  journalctl -u worms-round-agent -f
```

Mirroring of the sandbox bridges to the monitor is automatic (`worms-mirror`
on the host picks up every new sandbox NIC), so the monitor records the
round's traffic without any per-round step.

# Team API (scoring, logs and round progression)

Teams download their own scoring (every flag their worm submitted in a round,
with the result) and logs of a round as JSON with an access token from their
profile: `GET /api/v1/rounds/`, `/api/v1/rounds/<id>/scoring/`,
`/api/v1/rounds/<id>/logging/` with `Authorization: Bearer <token>`. Paged
with an opaque cursor; page sizes, bytes per page, query time and request
rates are capped (nginx + the app). A round's whole **progression** — its
clock, every event, log records and traffic — is at
`/api/v1/rounds/<id>/progression/` (JSON, records and traffic fetched
separately) and `/api/v1/rounds/<id>/progression.zip` (all in one; also for
unfinished rounds). Details: `docs/team_api.md`, `docs/progression.md`.

# Topology view (dashboard → TOPOLOGY, staff only)

Shows the infrastructure in a fixed layout (Proxmox host, orchestrator and
dashboard on top, the database in the middle, scoring and logging below it,
then the devboxes, the sandbox and the private internet), every VM with its
Proxmox status, and only the operational connections: the database's ingress
as its firewall allows it, sandbox/devbox → scoring, logging and the private
internet (verified against the firewall), the sandbox networks and the devbox
A–B/B–C links. Hovering a VM shows its bridges, security groups and rules.

Tabs select a round and then a team to **replay that run** like a video:
connections as dots along the edges, events (worm start, secrets, VMs stopped,
captures, flag submissions, logs) as icons on the VMs — click one to pause and
see the details — and a seekable timeline. **Download** saves the run as a
zip (its progression, log records and traffic) or what is shown as one JSON
file; **Open file…** views either again, offline (read locally, validated,
never uploaded). Details: `docs/visualization.md`.

Teams get the same player on their **PROGRESSION** page: their own rounds,
with only the VMs they captured, scoring, logging and the private internet —
no infrastructure, no firewall — and the round's start, end and now. They can
download a round as a zip there or through the team API. Format and rules:
`docs/progression.md`.

Data flow: `orchestrator` runs `manage.py collect_topology` every 2 minutes
(systemd timer `orchestrator-collect-topology.timer`), reads Proxmox with a
read-only token (GET only, config fields whitelisted, no cloud-init secrets),
and upserts one row in `topology_snapshots`. The dashboard analyses that row
(`dashboard/infra_view.py`) and serves it at `/topology/data/`; a run's replay
comes from `/rounds/<uuid>/progression/` (`orchestrator/rounds/progression.py`).

Credentials (the user/token are assumed to exist, role `PVEAuditor` on `/`
for the user and, with privilege separation, for the token):

- user + token id: `proxmox_readonly_api_user` / `proxmox_readonly_api_token_name`
  in `ansible/group_vars/all.yml` (default `topology@pve` / `readonly`)
- token secret: `vault_proxmox_readonly_api_token_value` in the vault:

  ```
  cd ansible && ansible-vault edit --vault-password-file .vault_pass group_vars/vault.yml
  ```

  (or export `PROXMOX_READONLY_API_TOKEN` before the very first `make`).

Then `worms build provision` (or `ansible-playbook -e @group_vars/vault.yml
SetupDbAndDashboard.yml`) delivers it to the orchestrator's 0600 env file.
