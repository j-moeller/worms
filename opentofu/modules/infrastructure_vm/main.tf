resource "proxmox_virtual_environment_vm" "infrastructure_server" {
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