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
  for_each = local.sandbox_bridges

  node_name = var.proxmox_instance
  name      = each.value.name

  address   = each.value.network_cidr
  ports     = []
  autostart = true
}

resource "proxmox_virtual_environment_vm" "vm" {
  for_each = local.sandbox_vms

  name      = each.key
  node_name = var.proxmox_instance

  cpu {
    cores = each.value.cpu
  }

  memory {
    dedicated = each.value.ram
  }

  agent {
    enabled = true
  }

  tags = [
    "untrusted"
  ]

  initialization {
    datastore_id = "local-zfs"

    dynamic "ip_config" {
      for_each = each.value.ip_configs

      content {
        ipv4 {
          address = ip_config.value.network_cidr
          gateway = ip_config.value.gateway
        }
      }
    }

    ip_config {
      ipv4 {
        address = each.value.maintenance_network_cidr
      }
    }

    dns {
      servers = [local.infrastructure_ips.dns]
      domain  = "interal"
    }

    user_data_file_id = each.value.user_data_file_id
  }

  dynamic "network_device" {
    for_each = each.value.network_devices

    content {
      bridge = network_device.value.bridge
      model  = "virtio"
    }
  }

  network_device {
    bridge = local.platform.networks.maintenance.name
    model  = "virtio"
  }

  serial_device {}

  disk {
    datastore_id = "local-zfs"
    import_from  = (each.value.os_image.content_type == "import") ? each.value.os_image.file_id : null
    file_id      = (each.value.os_image.content_type == "import") ? null : each.value.os_image.file_id

    interface = "virtio0"
    iothread  = true
    discard   = "on"
    size      = 5
  }
}