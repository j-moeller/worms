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
  private_internet = {
    dns = {
      cpu                         = 1
      ram                         = 1024
      network_cidr                = "${local.infrastructure_ips.dns}/24"
      network_gateway_ip          = "10.2.0.1"
      maintenance_network_cidr    = "${local.dns_maintenance_ip}/24"
      maintenance_network_gateway = "10.3.0.1"
      user_data_file_id           = local.platform.cloud_init.dns.file_id
      vmbr_network                = local.platform.networks.private_internet.name
      vmbr_maintenance_network    = local.platform.networks.maintenance.name
      disk_file_id                = local.platform.os_images.debian.file_id
      security_groups = [
        local.platform.security_group_names.ssh,
        local.platform.security_group_names.dns
      ]
    }
  }
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
        maintenance_network_cidr    = "${local.infrastructure_ips.scoring}/24"
        maintenance_network_gateway = "10.3.0.1"
        user_data_file_id           = local.platform.cloud_init.flask.file_id
        disk_file_id                = local.platform.os_images.debian.file_id
        tag                         = "ephemeral"
        security_groups = [
          local.platform.security_group_names.ssh,
          local.platform.security_group_names.sandbox_rest_api
        ]
      }
      logging = {
        maintenance_network_cidr    = "${local.infrastructure_ips.logging}/24"
        maintenance_network_gateway = "10.3.0.1"
        user_data_file_id           = local.platform.cloud_init.flask.file_id
        disk_file_id                = local.platform.os_images.debian.file_id
        tag                         = "ephemeral"
        security_groups = [
          local.platform.security_group_names.ssh,
          local.platform.security_group_names.sandbox_rest_api
        ]
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

module "private_internet_vms" {
  source = "../../../modules/private_internet_vm"

  for_each = local.private_internet

  proxmox_instance         = var.proxmox_instance
  name                     = each.key
  cpu                      = each.value.cpu
  ram                      = each.value.ram
  network_cidr             = each.value.network_cidr
  network_gateway_ip       = each.value.network_gateway_ip
  maintenance_network_cidr = each.value.maintenance_network_cidr
  user_data_file_id        = each.value.user_data_file_id
  vmbr_network             = each.value.vmbr_network
  vmbr_maintenance_network = each.value.vmbr_maintenance_network
  disk_file_id             = each.value.disk_file_id
  pool_id                  = local.platform.pools["private_internet"].pool_id
  security_groups          = each.value.security_groups
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
  pool_id                     = local.platform.pools["ephemeral"].pool_id
  security_groups             = each.value.security_groups
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