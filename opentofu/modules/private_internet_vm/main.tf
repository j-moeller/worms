
/**
 * DNS
 */
resource "proxmox_pool_membership" "pool_membership" {
  pool_id = var.pool_id
  vm_id   = proxmox_virtual_environment_vm.service_vm.vm_id
}

resource "proxmox_virtual_environment_vm" "service_vm" {
  name      = var.name
  node_name = var.proxmox_instance

  cpu { cores = var.cpu }
  memory { dedicated = var.ram }
  agent { enabled = true }

  tags = [
    "private_internet"
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