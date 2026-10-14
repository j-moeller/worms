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

resource "proxmox_pool_membership" "pool_membership" {
  for_each = local.sandbox_vms

  pool_id = local.platform.pools["sandbox"].pool_id
  vm_id   = proxmox_virtual_environment_vm.vm[each.key].vm_id
}

resource "proxmox_virtual_environment_firewall_options" "vm" {
  for_each = local.sandbox_vms

  node_name = var.proxmox_instance
  vm_id     = proxmox_virtual_environment_vm.vm[each.key].vm_id

  enabled       = true
  input_policy  = "DROP"
  output_policy = "DROP"
}

# Sandbox VMs accept SSH only from the Proxmox host, and only on their
# management NIC (the last NIC, after the sandbox-facing ones). Nothing else is
# allowed in or out (DROP/DROP above).
resource "proxmox_virtual_environment_firewall_rules" "vm" {
  for_each = local.sandbox_vms

  node_name = var.proxmox_instance
  vm_id     = proxmox_virtual_environment_vm.vm[each.key].vm_id

  rule {
    security_group = local.platform.security_group_names.ssh.name
    iface          = "net${length(each.value.network_devices)}"
    comment        = "SSH from the Proxmox host on the management NIC"
  }
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
      bridge   = network_device.value.bridge
      model    = "virtio"
      firewall = true
    }
  }

  # Management NIC on the sandbox-only management bridge (vmbr4), never on the
  # infrastructure maintenance network (vmbr2).
  network_device {
    bridge   = local.platform.networks.sandbox_maintenance.name
    model    = "virtio"
    firewall = true
  }

  serial_device {}

  disk {
    datastore_id = "local-zfs"
    file_id      = each.value.disk_file_id

    interface = "virtio0"
    iothread  = true
    discard   = "on"
    size      = 5
  }
}