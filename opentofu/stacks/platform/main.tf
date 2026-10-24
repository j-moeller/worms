terraform {
  required_providers {
    proxmox = {
      source = "bpg/proxmox"
    }
  }
}

provider "proxmox" {
  endpoint  = var.proxmox_api_url
  api_token = var.proxmox_api_token
  insecure  = true

  ssh {
    agent    = true
    username = "root"
  }
}

module "os_images" {
  source = "../../modules/os_images"

  datastore_id = var.datastore_id
  node_name    = var.proxmox_instance
}


module "cloud_init" {
  source = "../../modules/cloud_init"

  datastore_id   = var.datastore_id
  node_name      = var.proxmox_instance
  ssh_public_key = var.ssh_public_key
}


module "networks" {
  source = "../../modules/network"

  node_name = var.proxmox_instance
  bridges   = local.bridges
  networks  = local.networks
  host_ips  = local.host_ips
}

# Hookscript attached to every VM: isolates the VM's ports on the management
# bridges (VMs there can reach the host only, never each other). Proxmox runs
# hookscripts directly, so the snippet must be executable (file_mode needs the
# provider to act as root@pam, as it does for the other snippets via SSH).
resource "proxmox_virtual_environment_file" "hookscript" {
  content_type = "snippets"
  datastore_id = var.datastore_id
  node_name    = var.proxmox_instance
  file_mode    = "0755"

  source_raw {
    data = templatefile("${path.module}/templates/worms-hook.sh.tftpl", {
      # Every management bridge plus the devbox SSH fabric (vmbr7), whose
      # port isolation keeps different teams' devbox VMs apart.
      mgmt_bridges = join(" ", [
        local.bridges.mgmt_trusted, local.bridges.mgmt_exposed, local.bridges.mgmt_sandbox,
        local.bridges.mgmt_devbox, local.bridges.devbox_ssh,
      ])
    })
    file_name = "worms-hook.sh"
  }
}

resource "proxmox_virtual_environment_pool" "pools" {
  for_each = toset([
    "ephemeral",
    "persistent",
    "sandbox",
    "devbox",
    "private_internet",
  ])

  pool_id = each.value
}

# The Proxmox firewall (cluster/node options and security groups) lives in the
# firewall stack (opentofu/stacks/firewall).
