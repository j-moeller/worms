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

data "terraform_remote_state" "platform" {
  backend = "local"

  config = {
    path = "../../platform/terraform.tfstate"
  }
}

locals {
  platform = data.terraform_remote_state.platform.outputs.platform
}

# net0 = data-plane NIC in the VM's zone (default route via the host),
# net1 = management NIC (SSH from the host only).
locals {
  sg = local.platform.security_groups
  nw = local.platform.networks

  persistent = {
    database = {
      user_data_file_id = local.platform.cloud_init.postgresql.file_id
      nics = [
        { bridge = local.nw.core.name, cidr = "${local.infrastructure_ips.database}/24", gateway = local.host_ips.core },
        { bridge = local.nw.mgmt_trusted.name, cidr = "${local.management_ips.database}/24" },
      ]
      firewall_rules = [
        { group = local.sg.database, iface = "net0" },
        { group = local.sg.provisioning, iface = "net0" },
        { group = local.sg.ssh, iface = "net1" },
      ]
    }

    dashboard = {
      user_data_file_id = local.platform.cloud_init.django.file_id
      nics = [
        { bridge = local.nw.dmz.name, cidr = "${local.infrastructure_ips.dashboard}/24", gateway = local.host_ips.dmz },
        { bridge = local.nw.mgmt_exposed.name, cidr = "${local.management_ips.dashboard}/24" },
      ]
      firewall_rules = [
        { group = local.sg.dashboard, iface = "net0" },
        { group = local.sg.provisioning, iface = "net0" },
        { group = local.sg.ssh, iface = "net1" },
      ]
    }

    orchestrator = {
      user_data_file_id = local.platform.cloud_init.django.file_id
      nics = [
        { bridge = local.nw.core.name, cidr = "${local.infrastructure_ips.orchestrator}/24", gateway = local.host_ips.core },
        { bridge = local.nw.mgmt_trusted.name, cidr = "${local.management_ips.orchestrator}/24" },
      ]
      firewall_rules = [
        { group = local.sg.orchestrator, iface = "net0" },
        { group = local.sg.provisioning, iface = "net0" },
        { group = local.sg.ssh, iface = "net1" },
      ]
    }
  }
}

module "infrastructure_vms" {
  source = "../../../modules/infrastructure_vm"

  for_each = local.persistent

  proxmox_instance   = var.proxmox_instance
  name               = each.key
  nics               = each.value.nics
  firewall_rules     = each.value.firewall_rules
  user_data_file_id  = each.value.user_data_file_id
  disk_file_id       = local.platform.os_images.debian.file_id
  hookscript_file_id = local.platform.hookscript_file_id
  tag                = "persistent"
  pool_id            = local.platform.pools["persistent"].pool_id
}

# The database VM's key was renamed from db (keeps an existing VM).
moved {
  from = module.infrastructure_vms["db"]
  to   = module.infrastructure_vms["database"]
}
