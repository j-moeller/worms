resource "proxmox_pool_membership" "pool_membership" {
  pool_id = var.pool_id
  vm_id   = proxmox_virtual_environment_vm.infrastructure.vm_id
}

resource "proxmox_virtual_environment_firewall_options" "infrastructure" {
  node_name = var.proxmox_instance
  vm_id     = proxmox_virtual_environment_vm.infrastructure.vm_id

  enabled       = true
  input_policy  = "DROP"
  output_policy = "DROP"
}

resource "proxmox_virtual_environment_firewall_rules" "infrastructure" {
  node_name = var.proxmox_instance
  vm_id     = proxmox_virtual_environment_vm.infrastructure.vm_id

  dynamic "rule" {
    for_each = var.security_groups

    content {
      security_group = rule.value.name
      comment        = "Apply security group: ${rule.value.name}"
    }
  }
}

resource "proxmox_virtual_environment_vm" "infrastructure" {
  name      = var.name
  node_name = var.proxmox_instance

  cpu { cores = 2 }
  memory { dedicated = 1024 }
  agent { enabled = true }

  tags = ["infrastructure", var.tag]

  initialization {
    datastore_id = "local-zfs"

    ip_config {
      ipv4 {
        address = var.maintenance_network_cidr
        gateway = var.maintenance_network_gateway
      }
    }

    user_data_file_id = var.user_data_file_id
  }

  network_device {
    bridge   = var.vmbr_maintenance_network
    model    = "virtio"
    firewall = true
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