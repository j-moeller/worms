resource "proxmox_virtual_environment_firewall_options" "monitor_server" {
  node_name = var.proxmox_instance
  vm_id     = proxmox_virtual_environment_vm.monitor_server.vm_id

  enabled       = true
  input_policy  = "DROP"
  output_policy = "DROP"
}

/*
resource "proxmox_virtual_environment_firewall_rules" "monitor_server_ssh" {
  node_name = var.proxmox_instance
  vm_id     = proxmox_virtual_environment_vm.monitor_server.vm_id

  rule {
    type    = "in"
    action  = "ACCEPT"
    proto   = "tcp"
    dport   = "22"
    source  = "10.3.0.1"
    comment = "Allow SSH from Proxmox host"
  }
}
*/

resource "proxmox_virtual_environment_vm" "monitor_server" {
  name      = "monitor-server"
  node_name = var.proxmox_instance

  agent { enabled = true }
  cpu { cores = 2 }
  memory { dedicated = 1024 }

  tags = [
    "infrastructure"
  ]

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

  network_device {
    bridge   = var.vmbr_monitor_network
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
