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

locals {
  networks = {
    user_vms         = "10.1.0.0/24"
    private_internet = "10.2.0.0/24"
    maintenance      = "10.3.0.0/24"
  }

  infrastructure_ips = {
    dns       = "10.2.0.11"
    heartbeat = "10.3.0.13"
    scoring   = "10.3.0.14"
    logging   = "10.3.0.15"
    db        = "10.3.0.16"
    dbbrowser = "10.3.0.17"
    dashboard = "10.3.0.18"
    monitor   = "10.3.0.100"
  }
}

locals {
  private_internet = {
    dns = {
      cpu                         = 1
      ram                         = 1024
      network_cidr                = "10.2.0.11/24"
      network_gateway_ip          = "10.2.0.1"
      maintenance_network_cidr    = "10.3.0.11/24"
      maintenance_network_gateway = "10.3.0.1"
      user_data_file_id           = proxmox_virtual_environment_file.dns_vm_static.id
      vmbr_network                = proxmox_network_linux_bridge.vmbr_private_internet.name
      vmbr_maintenance_network    = proxmox_network_linux_bridge.vmbr_maintenance_network.name
      disk_file_id                = proxmox_virtual_environment_file.debian_image.id
    }
  }

  infrastructure = {
    generic = {
      /*
      heartbeat = {
        maintenance_network_cidr = "10.3.0.13/24"
        maintenance_network_gateway = "10.3.0.1"
        user_data_file_id        = proxmox_virtual_environment_file.nginx_vm_static.id
        disk_file_id             = proxmox_virtual_environment_file.debian_image.id
      }
      */
      scoring = {
        maintenance_network_cidr    = "10.3.0.14/24"
        maintenance_network_gateway = "10.3.0.1"
        user_data_file_id           = proxmox_virtual_environment_file.flask_vm_static.id
        disk_file_id                = proxmox_virtual_environment_file.debian_image.id
      }
      logging = {
        maintenance_network_cidr    = "10.3.0.15/24"
        maintenance_network_gateway = "10.3.0.1"
        user_data_file_id           = proxmox_virtual_environment_file.flask_vm_static.id
        disk_file_id                = proxmox_virtual_environment_file.debian_image.id
      }
      db = {
        maintenance_network_cidr    = "10.3.0.16/24"
        maintenance_network_gateway = "10.3.0.1"
        user_data_file_id           = proxmox_virtual_environment_file.postgresql_vm_static.id
        disk_file_id                = proxmox_virtual_environment_file.debian_image.id
      }
      dbbrowser = {
        maintenance_network_cidr    = "10.3.0.17/24"
        maintenance_network_gateway = "10.3.0.1"
        user_data_file_id           = proxmox_virtual_environment_file.database_browser_vm_static.id
        disk_file_id                = proxmox_virtual_environment_file.debian_image.id
      }
      dashboard = {
        maintenance_network_cidr    = "10.3.0.18/24"
        maintenance_network_gateway = "10.3.0.1"
        user_data_file_id           = proxmox_virtual_environment_file.nginx_vm_static.id
        disk_file_id                = proxmox_virtual_environment_file.debian_image.id
      }
    }
    specific = {
      monitor = {
        maintenance_network_cidr    = "10.3.0.100/24"
        maintenance_network_gateway = "10.3.0.1"
        user_data_file_id           = proxmox_virtual_environment_file.network_monitor_vm_static.id
        disk_file_id                = proxmox_virtual_environment_file.debian_image.id
      }
    }
  }

  user_vms = {
    user01 = {
      cpu                         = 1
      ram                         = 2048
      network_cidr                = "10.1.0.11/24"
      network_gateway_ip          = "10.1.0.1"
      maintenance_network_cidr    = "10.3.0.101/24"
      maintenance_network_gateway = "10.3.0.1"
      vmbr_network                = proxmox_network_linux_bridge.vmbr_vm_private_network.name
      vmbr_maintenance_network    = proxmox_network_linux_bridge.vmbr_maintenance_network.name
      disk_file_id                = proxmox_virtual_environment_file.debian_image.id
      secret                      = "63a106e0cafad63cc1305434762cf5901d86b3f4d19fb578e275af90c84bbdb4"
      snippet_type                = "vm-nginx"
    }
    /*
    db02 = {
      cpu                      = 1
      ram                      = 2048
      network_cidr             = "10.1.0.12/24"
      network_gateway_ip       = "10.1.0.1"
      maintenance_network_cidr = "10.3.0.102/24"
      maintenance_network_gateway = "10.3.0.1"
      vmbr_network             = proxmox_network_linux_bridge.vmbr_vm_private_network.name
      vmbr_maintenance_network = proxmox_network_linux_bridge.vmbr_maintenance_network.name
      disk_file_id             = proxmox_virtual_environment_file.debian_image.id
      secret                   = "63a106e0cafad63cc1305434762cf5901d86b3f4d19fb578e275af90c84bbdb4"
      snippet_type             = "vm-nginx"
    }
    db03 = {
      cpu                      = 1
      ram                      = 2048
      network_cidr             = "10.1.0.13/24"
      network_gateway_ip       = "10.1.0.1"
      maintenance_network_cidr = "10.3.0.103/24"
      maintenance_network_gateway = "10.3.0.1"
      vmbr_network             = proxmox_network_linux_bridge.vmbr_vm_private_network.name
      vmbr_maintenance_network = proxmox_network_linux_bridge.vmbr_maintenance_network.name
      disk_file_id             = proxmox_virtual_environment_file.debian_image.id
      secret                   = "63a106e0cafad63cc1305434762cf5901d86b3f4d19fb578e275af90c84bbdb4"
      snippet_type             = "vm-nginx"
    }
    db04 = {
      cpu                      = 1
      ram                      = 2048
      network_cidr             = "10.1.0.14/24"
      network_gateway_ip       = "10.1.0.1"
      maintenance_network_cidr = "10.3.0.104/24"
      maintenance_network_gateway = "10.3.0.1"
      vmbr_network             = proxmox_network_linux_bridge.vmbr_vm_private_network.name
      vmbr_maintenance_network = proxmox_network_linux_bridge.vmbr_maintenance_network.name
      disk_file_id             = proxmox_virtual_environment_file.debian_image.id
      secret                   = "63a106e0cafad63cc1305434762cf5901d86b3f4d19fb578e275af90c84bbdb4"
      snippet_type             = "vm-nginx"
    }
    db05 = {
      cpu                      = 1
      ram                      = 2048
      network_cidr             = "10.1.0.15/24"
      network_gateway_ip       = "10.1.0.1"
      maintenance_network_cidr = "10.3.0.105/24"
      maintenance_network_gateway = "10.3.0.1"
      vmbr_network             = proxmox_network_linux_bridge.vmbr_vm_private_network.name
      vmbr_maintenance_network = proxmox_network_linux_bridge.vmbr_maintenance_network.name
      disk_file_id             = proxmox_virtual_environment_file.debian_image.id
      secret                   = "63a106e0cafad63cc1305434762cf5901d86b3f4d19fb578e275af90c84bbdb4"
      snippet_type             = "vm-nginx"
    }
    db06 = {
      cpu                      = 1
      ram                      = 2048
      network_cidr             = "10.1.0.16/24"
      network_gateway_ip       = "10.1.0.1"
      maintenance_network_cidr = "10.3.0.106/24"
      maintenance_network_gateway = "10.3.0.1"
      vmbr_network             = proxmox_network_linux_bridge.vmbr_vm_private_network.name
      vmbr_maintenance_network = proxmox_network_linux_bridge.vmbr_maintenance_network.name
      disk_file_id             = proxmox_virtual_environment_file.debian_image.id
      secret                   = "63a106e0cafad63cc1305434762cf5901d86b3f4d19fb578e275af90c84bbdb4"
      snippet_type             = "vm-nginx"
    }
    db07 = {
      cpu                      = 1
      ram                      = 2048
      network_cidr             = "10.1.0.17/24"
      network_gateway_ip       = "10.1.0.1"
      maintenance_network_cidr = "10.3.0.107/24"
      maintenance_network_gateway = "10.3.0.1"
      vmbr_network             = proxmox_network_linux_bridge.vmbr_vm_private_network.name
      vmbr_maintenance_network = proxmox_network_linux_bridge.vmbr_maintenance_network.name
      disk_file_id             = proxmox_virtual_environment_file.debian_image.id
      secret                   = "63a106e0cafad63cc1305434762cf5901d86b3f4d19fb578e275af90c84bbdb4"
      snippet_type             = "vm-nginx"
    }
    db08 = {
      cpu                      = 1
      ram                      = 2048
      network_cidr             = "10.1.0.18/24"
      network_gateway_ip       = "10.1.0.1"
      maintenance_network_cidr = "10.3.0.108/24"
      maintenance_network_gateway = "10.3.0.1"
      vmbr_network             = proxmox_network_linux_bridge.vmbr_vm_private_network.name
      vmbr_maintenance_network = proxmox_network_linux_bridge.vmbr_maintenance_network.name
      disk_file_id             = proxmox_virtual_environment_file.debian_image.id
      secret                   = "63a106e0cafad63cc1305434762cf5901d86b3f4d19fb578e275af90c84bbdb4"
      snippet_type             = "vm-nginx"
    }
    */
  }
}

module "private_internet" {
  source = "./modules/private_internet"

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
}

module "user_vms" {
  source = "./modules/user_vms"

  for_each = local.user_vms

  proxmox_instance             = var.proxmox_instance
  service_network_dns_vm_ipraw = "10.2.0.11"

  name                     = each.key
  cpu                      = each.value.cpu
  ram                      = each.value.ram
  network_cidr             = each.value.network_cidr
  network_gateway_ip       = each.value.network_gateway_ip
  maintenance_network_cidr = each.value.maintenance_network_cidr
  user_data_file_id        = proxmox_virtual_environment_file.vm_config[each.key].id
  vmbr_network             = each.value.vmbr_network
  vmbr_maintenance_network = each.value.vmbr_maintenance_network
  disk_file_id             = each.value.disk_file_id
}

module "infrastructure" {
  source = "./modules/infrastructure"

  for_each = local.infrastructure.generic

  proxmox_instance         = var.proxmox_instance
  vmbr_maintenance_network = proxmox_network_linux_bridge.vmbr_maintenance_network.name

  name                        = each.key
  maintenance_network_cidr    = each.value.maintenance_network_cidr
  maintenance_network_gateway = each.value.maintenance_network_gateway
  user_data_file_id           = each.value.user_data_file_id
  disk_file_id                = each.value.disk_file_id
}

module "network_monitor" {
  source = "./modules/network_monitor"

  proxmox_instance         = var.proxmox_instance
  vmbr_maintenance_network = proxmox_network_linux_bridge.vmbr_maintenance_network.name
  vmbr_monitor_network     = proxmox_network_linux_bridge.vmbr_monitoring_network.name

  maintenance_network_cidr    = local.infrastructure.specific.monitor.maintenance_network_cidr
  maintenance_network_gateway = local.infrastructure.specific.monitor.maintenance_network_gateway
  user_data_file_id           = local.infrastructure.specific.monitor.user_data_file_id
  disk_file_id                = local.infrastructure.specific.monitor.disk_file_id
}
