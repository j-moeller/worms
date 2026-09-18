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
    generic = {
      /*
      heartbeat = {
        maintenance_network_cidr = "10.3.0.13/24"
        maintenance_network_gateway = "10.3.0.1"
        user_data_file_id        = local.platform.cloud_init.nginx_vm_static.id
        disk_file_id             = proxmox_virtual_environment_file.debian_image.id
      }
      */
      scoring = {
        maintenance_network_cidr    = "10.3.0.14/24"
        maintenance_network_gateway = "10.3.0.1"
        user_data_file_id           = local.platform.cloud_init.flask.file_id
        disk_file_id                = local.platform.os_images.debian.file_id
        tag                         = "ephemeral"
      }
      logging = {
        maintenance_network_cidr    = "10.3.0.15/24"
        maintenance_network_gateway = "10.3.0.1"
        user_data_file_id           = local.platform.cloud_init.flask.file_id
        disk_file_id                = local.platform.os_images.debian.file_id
        tag                         = "ephemeral"
      }
    }
    /*
    specific = {
      monitor = {
        maintenance_network_cidr    = "10.3.0.100/24"
        maintenance_network_gateway = "10.3.0.1"
        user_data_file_id           = local.platform.cloud_init.network_monitor_vm_static.id
        disk_file_id                = proxmox_virtual_environment_file.debian_image.id
        tag                         = "ephemeral"
      }
    }
    */
  }
}

module "infrastructure_vms" {
  source = "../../../modules/infrastructure_vm"

  for_each = local.infrastructure.generic

  proxmox_instance         = var.proxmox_instance
  vmbr_maintenance_network = local.platform.networks.maintenance.name

  name                        = each.key
  maintenance_network_cidr    = each.value.maintenance_network_cidr
  maintenance_network_gateway = each.value.maintenance_network_gateway
  user_data_file_id           = each.value.user_data_file_id
  disk_file_id                = each.value.disk_file_id
  tag                         = each.value.tag
}
/*

module "network_monitor_vm" {
  source = "../../../modules/network_monitor_vm"

  proxmox_instance         = var.proxmox_instance
  vmbr_maintenance_network = proxmox_network_linux_bridge.vmbr_maintenance_network.name
  vmbr_monitor_network     = proxmox_network_linux_bridge.vmbr_monitoring_network.name

  maintenance_network_cidr    = local.infrastructure.specific.monitor.maintenance_network_cidr
  maintenance_network_gateway = local.infrastructure.specific.monitor.maintenance_network_gateway
  user_data_file_id           = local.infrastructure.specific.monitor.user_data_file_id
  disk_file_id                = local.infrastructure.specific.monitor.disk_file_id
}
*/