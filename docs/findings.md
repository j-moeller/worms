# Findings, state and changes: network monitor, monitoring database, testing

Work on branch `claude/jolly-ride-5az76k` (based on `feature/auditd-wazuh`).
Details: [monitoring.md](monitoring.md), [testing.md](testing.md).

## What already existed (assessment)

| Building block | State found | Now |
|---|---|---|
| `opentofu/modules/network_monitor_vm` | Not deployed anywhere. No pool, no `ipfilter`/`macfilter`, the `security_groups` variable unused, the SSH rule commented out, so no way in. Only a management and a mirror NIC: **no path to the database**. The mirror NIC had the Proxmox firewall on with input policy DROP, which would drop every mirrored frame (they are addressed to other VMs). | Reworked like `infrastructure_vm` and deployed in the ephemeral stack: data NIC on ingest, management on mgmt-exposed, capture NIC on the mirror bridge without firewall and address. |
| Addresses (`shared-variables.tf`) | `monitor` already had `10.12.0.100` (ingest) and `10.5.0.100` (mgmt-exposed). | Used as is. |
| Mirror bridge `vmbr3` | Created by the platform stack, host has no address on it; host nftables drops it at the inet level. | Plus: bridge-level drop of everything entering it (receive-only). |
| Proxmox firewall | The `database` group already accepted `monitor`; `db_client` ("and later the monitor") reusable. | Used as is. |
| Host nftables | Already routed `monitor → database:5432`. | Used as is. |
| Database role | `dbuser_network_monitor` existed (pg_hba from `10.12.0.100/32`, vault password) with **no table privileges** (`[]`) and no tables. | Tables `network_flows`/`network_events`, INSERT-only grants, connection limit. |
| Mirroring (`helpers/monitor_network.sh`) | Manual. Mirrored the **bridge device**, which misses every frame forwarded between VMs (only broadcasts and host traffic pass the bridge device's qdisc), and mirrored **into `vmbr4`, which is now the sandbox management bridge**. | Replaced by the `worms-mirror` service on the host (per-port, automatic). The helper now exits with a warning. |
| cloud-init `vm-network-monitor` | Listed `zeek` (not in Debian 13: the whole package step would fail) and `tshark` twice. | Zeek comes from the Zeek project's OBS repository via Ansible. |

## Bugs found (and fixed)

1. **Scoring and logging could not write to the database.** The sandbox
   APIs insert only `(team_token_hash, vm_flag_hash | log_data)`, as their
   INSERT-only roles allow. But `processed`, `created_at` and the logging
   attempt's `uuid` only had **Python-side** Django defaults, no database
   defaults, so every INSERT failed with a NOT NULL violation (the API answers
   503). This matches the unchecked "Writing to DB" items in `TODO`. Found by
   `tests/test_database.py`. Fix: database defaults for `processed`/`created_at`
   (migration `0004`); the logging API generates the `uuid` and may insert
   that one column. `UUID4()` as a database default is not available on
   SQLite, which the DEBUG setup uses.
2. **`makemigrations` under the orchestrator settings planned to drop most
   tables.** `core/models/__init__.py` imported only two models; the rest were
   registered only through the dashboard's URLconf. Fix: it imports every
   model module.
3. **worms-mirror itself** (during development, caught by its test): the
   iproute2 keyword is `mcast_snooping`, not `multicast_snooping`.

## Open issues (not changed: outside this task)

- **The port-isolation hookscript is not attached to any VM.**
  `hook_script_file_id` is commented out ("TODO: HOOK") in
  `infrastructure_vm`, `private_internet_vm`, the sandbox VMs and sandbox-gw,
  so the README's "management bridges are port-isolated" does not hold. The
  isolation on the management bridges currently rests on the Proxmox firewall
  alone (output policy DROP, `ssh` group inbound only), and
  `make simulate` checks that from a sandbox VM. Possibly because Proxmox lets only
  `root@pam` set hookscripts, not API tokens.
- The sandbox VMs do not trust the internal CA (README "Round start":
  "distribute the self-signed CA" is not implemented for the sandbox group).
  (Resolved: the sandbox guests now trust the internal CA; SetupSandbox.yml,
  run by the agent's prepare job.)
- `opentofu/stacks/sandbox/generated_vms.tf` is an old, fully commented-out
  topology using `10.5.x.0/24`, which overlaps mgmt-exposed. Harmless while
  commented; `generated_vms_01.tf` is not `tofu fmt`-clean (cosmetic).
  (Note: the round agent now overwrites `generated_vms.tf` and removes other
  `generated_vms*.tf` at prepare time, so these committed samples are replaced
  at runtime.)
- Processing of attempts: **resolved.** The scoring/logging servers record the
  running team's uuid directly (no access token; the agent writes
  `/etc/sandbox-api/current-team` at deploy), and a continuous daemon
  (`manage.py process_attempts`) turns attempts into `flag_captures` / `logging`
  rows and `captured` timeline events. See docs/rounds.md.
- Infrastructure VMs have no time synchronisation configured. Flow
  timestamps come from the monitor's clock, submission timestamps from the
  database's.
- Retention of `network_flows` (see monitoring.md, Limitations).

## Changes (by commit)

1. *Deploy the network monitor VM and mirror the sandbox traffic to it:*
   OpenTofu module + ephemeral stack, cloud-init output, `host_vars` in the
   inventory generator, `worms-mirror` + nftables, inventory groups, test
   fixtures, mirror tests.
2. *Monitoring schema, INSERT-only grants, zeek-shipper; fix attempt
   inserts:* Django models/migrations `0003`/`0004`, grants, `zeek_shipper.py`,
   the scoring/logging fix, database/API/shipper tests.
3. *Ansible role network_monitor:* Zeek + capture NIC + shipper + archive
   cleanup, `SetupNetworkMonitor.yml` in `make provision`, Ansible tests.
4. *Test suite and end-to-end simulation:* `Simulate.yml` + helper scripts,
   `make test`, `make simulate`, tofu/simulation tests.
5. *Documentation:* this file, monitoring.md, testing.md, README, TODO.

## How it was verified, and what was not

- Offline suite: 138 passed, 2 skipped. The skips are kernel features
  missing in the development container (nftables bridge family for `nft -c`,
  tc `gact` for the drop test).
- `tofu validate` of all six stacks, with OpenTofu 1.13.2 and bpg/proxmox
  0.118.0 built from source.
- The `worms-mirror` logic ran against real kernel bridges, veths and tc in
  network namespaces, with exactly-once delivery checked by tcpdump.
- **Not run on a Proxmox host** (none available): VM creation, the Proxmox
  side of the firewall, Zeek itself (its OBS repository was not reachable
  from the development environment), the systemd hardening in practice.
  First steps on the real host: `make` (or `make infrastructure host-network
  provision`), deploy a sandbox, `make simulate`.

## Infrastructure visualization (dashboard topology view)

Extended the topology view to show the network security zones (shaded,
labelled bands derived dynamically from the live bridge comments), the Linux
bridges and firewalls, with the firewall rules shown on hover (VM / bridge /
zone / flow). Built so the planned round/team overlays can layer on top without
reshaping the data. Details: `docs/visualization.md`. Covered by
`tests/test_topology.py`.

Bug found and fixed along the way: `.topo-empty` set `display:flex`, which
overrides the `hidden` attribute, so that overlay stayed on top of the canvas
(`inset:0`) and swallowed every hover and click over the graph. Fixed with
`.topo-empty[hidden]{display:none}`.

## Round orchestration (second task)

Added the round orchestration (`docs/rounds.md`): the conductor on the
orchestrator, the round agent on the Proxmox host, the data model, the game
plan, capture processing and the timeline export.

Bugs found and fixed:

- **`orchestrator/management/commands/deploy_worms.py` was dead code**: it
  imported `ProductionVirtualMachine` (does not exist), queried a
  non-existent `virtual_machine__round` relation, and ended in `assert False`
  with the real work behind `if False:`. Removed; the conductor replaces it.
- **The release-secret state bug** (caught by `test_rounds.py`): a game-plan
  step that requested a host job and moved to REQUESTED was never revisited,
  because the step loop only looked at PENDING steps, so a delayed secret was
  never applied. `_run_due_steps` now drains REQUESTED steps each tick.

Open issues (noted, not changed here):

- **`MEDIA_ROOT` is not configured.** `dashboard/views/uploads.py` saves
  artifacts through `default_storage` with no `MEDIA_ROOT`, so they land
  relative to the process's working directory. The round agent reads them
  from `round_agent_dashboard_media_root` (`/opt/dashboard/media`); the
  dashboard must be made to store them there (set `MEDIA_ROOT`) before deploy
  can fetch a worm. Until then the `deploy` job's artifact fetch fails.
- **The sandbox guests are not configured** beyond cloud-init: no CA
  distribution, and the worm start assumes the image has `python3`/`unzip`.
- The conductor runs **one run at a time** (one sandbox per host). Parallel
  sandboxes would need per-run bridge/pool namespacing.
- Still **not run on a real Proxmox host** (see the monitoring section): the
  tofu/qm/ssh paths of the agent are only validated offline.
