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

locals {
  infrastructure = {
    persistent = {
      db = {
        maintenance_network_cidr    = "10.3.0.16/24"
        maintenance_network_gateway = "10.3.0.1"
        user_data_file_id           = local.platform.cloud_init.postgresql.file_id
        disk_file_id                = local.platform.os_images.debian.file_id
        tag                         = "persistent"
      }

      dashboard = {
        maintenance_network_cidr    = "10.3.0.18/24"
        maintenance_network_gateway = "10.3.0.1"
        user_data_file_id           = local.platform.cloud_init.nginx.file_id
        disk_file_id                = local.platform.os_images.debian.file_id
        tag                         = "persistent"
      }
    }
  }
}

module "infrastructure_vms" {
  source = "../../../modules/infrastructure_vm"

  for_each = local.infrastructure.persistent

  proxmox_instance         = var.proxmox_instance
  vmbr_maintenance_network = local.platform.networks.maintenance.name

  name                        = each.key
  maintenance_network_cidr    = each.value.maintenance_network_cidr
  maintenance_network_gateway = each.value.maintenance_network_gateway
  user_data_file_id           = each.value.user_data_file_id
  disk_file_id                = each.value.disk_file_id
  tag                         = each.value.tag
}
