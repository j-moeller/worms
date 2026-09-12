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
    path = "../platform/terraform.tfstate"
  }
}

locals {
  platform = data.terraform_remote_state.platform.outputs.platform
}

resource "proxmox_network_linux_bridge" "vmbr_sandbox" {
  node_name = var.proxmox_instance
  name      = "vmbr1111"

  address   = "10.1.0.1/24"
  ports     = []
  autostart = true
}

locals {
  sandbox_vms = {
    user01 = {
      cpu                         = 1
      ram                         = 2048
      network_cidr                = "10.1.0.11/24"
      network_gateway_ip          = "10.1.0.1"
      maintenance_network_cidr    = "10.3.0.101/24"
      maintenance_network_gateway = "10.3.0.1"
      vmbr_network                = proxmox_network_linux_bridge.vmbr_sandbox.name
      vmbr_maintenance_network    = local.platform.networks.maintenance.name
      disk_file_id                = local.platform.os_images.debian.file_id
      user_data_file_id           = local.platform.cloud_init.nginx.file_id
    }
  }
}

module "sandbox_vms" {
  source = "../../modules/sandbox_vm"

  for_each = local.sandbox_vms

  proxmox_instance             = var.proxmox_instance
  service_network_dns_vm_ipraw = "10.2.0.11"

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

resource "proxmox_network_linux_bridge" "vmbr_sandbox" {
  node_name = var.proxmox_instance
  name      = "vmbr1111"

  address   = "10.1.0.1/24"
  ports     = []
  autostart = true
}

resource "proxmox_virtual_environment_vm" "vm" {
  name      = var.name
  node_name = var.proxmox_instance

  cpu { cores = var.cpu }
  memory { dedicated = var.ram }
  agent { enabled = true }

  tags = [
    "untrusted"
  ]

  initialization {
    datastore_id = "local-zfs"

    ip_config {
      ipv4 {
        address = var.network_cidr
        gateway = var.network_gateway_ip
      }
    }

    ip_config {
      ipv4 {
        address = var.maintenance_network_cidr
      }
    }

    dns {
      servers = [
        var.service_network_dns_vm_ipraw
      ]
      domain = "interal"
    }

    user_data_file_id = var.user_data_file_id
  }

  network_device {
    bridge = var.vmbr_network
    model  = "virtio"
  }

  network_device {
    bridge = var.vmbr_maintenance_network
    model  = "virtio"
  }

  serial_device {}

  disk {
    datastore_id = "local-zfs"
    file_id      = var.disk_file_id
    interface    = "virtio0"
    iothread     = true
    discard      = "on"
    size         = 5
  }
}