# Nightly backups of the VMs that hold state (docs/06-background-processes.md). A first version,
# off by default: it is created only when backup_storage names a Proxmox storage
# with the `backup` content type, e.g. `local` (/var/lib/vz/dump):
#
#   TF_VAR_backup_storage=local worms build infrastructure
#
# vzdump runs on the node as root at the scheduled time. Without a guest agent,
# a snapshot backup of a running VM is crash-consistent: PostgreSQL recovers
# from it as from a power cut. A storage on the node's own pool only protects
# against mistakes (a wipe, a bad migration), not against losing the disk:
# copy the dumps off the node as well (docs/06-background-processes.md).

variable "backup_storage" {
  description = "Proxmox storage for the nightly backups of the stateful VMs; empty = no backup job."
  type        = string
  default     = ""
}

locals {
  # The database is the source of truth; the dashboard keeps the uploaded worms;
  # the orchestrator its state files; syslog the audit logs.
  backed_up = ["database", "dashboard", "orchestrator", "syslog"]
}

resource "proxmox_backup_job" "stateful" {
  count = var.backup_storage == "" ? 0 : 1

  id       = "worms-stateful"
  node     = var.proxmox_instance
  storage  = var.backup_storage
  vmid     = [for name in local.backed_up : tostring(module.infrastructure_vms[name].vm_id)]
  schedule = "*-*-* 02:30"
  mode     = "snapshot"
  compress = "zstd"
  enabled  = true
  prune_backups = {
    "keep-daily"  = "7"
    "keep-weekly" = "4"
  }
}
