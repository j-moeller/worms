# Backups

**Nothing is backed up today.** This is the plan, and a first version of its main part in
OpenTofu, which is off until you turn it on.

## What has to survive

| What | Where | Lost without a backup? |
|---|---|---|
| Users, teams, uploads' records, captures, logs, rounds, devbox history | the database VM (`mydb`) | **Yes**: it is the source of truth |
| The uploaded zips | the dashboard VM, `/opt/dashboard/media` (`dashboard_media_root`) | **Yes** |
| The audit logs | the syslog VM, `/var/log/remote` (data disk) | Yes, 30 days of them |
| The vault, the CA, the controller's OpenTofu state | the controller (on `worms-pve`, the node's checkout): `ansible/group_vars/vault.yml`, `ansible/.vault_pass`, `ansible/pki/worms/`, `opentofu/stacks/*/terraform.tfstate` | **Yes**: without them the platform has to be rebuilt and every password reset |
| The sandbox and devbox stacks' state, the agent's copy of the devbox map | the node, `/opt/worms`, `/var/lib/worms` | Partly: the VMs can be rebuilt, but OpenTofu loses track of the running ones |
| The orchestrator | its VM | No: it holds no data of its own beyond the database, but backing it up makes a restore quicker |
| Scoring, logging, monitor, DNS, the private internet | | No: rebuilt or rolled back to their snapshot (`docs/management.md`) |
| Sandbox and devbox VMs | | No: never; they are assumed hostile |

On worms-dev the four stateful VMs use about 6 GB together (2026-11-06: database 1.4 GB, dashboard
1.6 GB, orchestrator 1.6 GB, syslog 1.5 GB).

## The plan, in three parts

**1. A nightly Proxmox backup job of the stateful VMs (OpenTofu, drafted).**
`opentofu/stacks/infrastructure/persistent/backup.tf`: a `proxmox_backup_job` (bpg provider) for
database, dashboard, orchestrator and syslog, every night at 02:30, `vzdump` in snapshot mode with
zstd, keeping 7 daily and 4 weekly backups. It lives in the persistent stack because that stack
creates those VMs and knows their IDs. vzdump runs on the node as root, so no new credential is
needed. Without a guest agent a snapshot backup is crash-consistent: PostgreSQL recovers from it as
from a power cut. A restore is a whole VM (`qmrestore`).

**2. A logical dump of the database (Ansible, not written yet).** A systemd timer on the database
VM runs `pg_dump -Fc mydb` as `postgres` into a local directory shortly before the backup job, so
the dump is inside the VM's backup. It restores into any PostgreSQL, also a single table, and it
survives a broken VM image. Belongs in role `setup_db_base`.

**3. Copies off the node (decision needed).** A backup on the node's own pool (`local` is
`/var/lib/vz` on `rpool`, like every VM disk) protects against mistakes, such as a wipe or a bad
migration, but not against losing the disk or the node. The options: a Proxmox Backup Server, an
NFS or CIFS share as a Proxmox storage (then `backup_storage` names it and nothing else changes),
or an `rsync`/`rclone` timer on the node that copies `/var/lib/vz/dump` plus a tarball of the vault,
the CA and the states to TU storage. The vault tarball is the most sensitive file of the
platform: encrypt it, or keep it on a medium only the admins reach.

## Turn the backup job on

```sh
TF_VAR_backup_storage=local worms build infrastructure    # creates the job; worms passes TF_VAR_* through
pvesh get /cluster/backup --output-format json-pretty     # the job worms-stateful
vzdump <database vmid> --storage local --mode snapshot --compress zstd   # one backup now, to try it
ls -l /var/lib/vz/dump/
```

A later `worms build` without the variable removes the job again, so once it works, make it
permanent: set `default = "local"` (or the off-node storage) in `backup.tf`.

**Restore test,** never next to the original: a restored VM has the same addresses and MACs.

```sh
qmrestore /var/lib/vz/dump/vzdump-qemu-<vmid>-<date>.vma.zst 9<vmid> --storage local-zfs --unique 1
qm config 9<vmid>    # look, do not start it while the original runs; then: qm destroy 9<vmid>
```

## Not verified

- **The job has not been created on a node.** `tofu validate` passes with provider 0.113.1. The
  provisioning token has `Sys.Modify` and `Datastore.Allocate*`, but not `VM.Backup`, which Proxmox
  may ask for on each VM of the job. If the apply is refused, add `VM.Backup` to `WormsProvision`
  in `src/worms_cli/tokens.py` and run `worms secrets issue proxmox-token` again.
- **No restore has been tried**, nor PostgreSQL's recovery from such a backup.
- How long the nightly run takes and how much it slows the node's single disk.
