
/**
 * Private-internet service VM.
 *
 * Two NICs: private_internet (10.2, with gateway) + maintenance (10.3).
 * Optional dedicated data disk (virtio1) so a service's payload (Mongo CVE db,
 * Kiwix ZIM, caches) lives on its own volume, separate from the OS/root disk.
 *
 * NOTE on persistence: the data disk is a separate volume from the root disk,
 * so rebuilding the OS does not touch it. A full `tofu destroy` of this stack
 * still removes it (bpg has no standalone disk resource). These VMs live in
 * the long-lived private_internet stack and are not torn down per round.
 */
resource "proxmox_virtual_environment_vm" "service_vm" {
  name      = var.name
  node_name = var.proxmox_instance

  cpu {
    cores = var.cpu
    type  = var.cpu_type
  }
  memory { dedicated = var.ram }
  agent { enabled = true }

  tags = [
    "private_internet"
  ]

  initialization {
    datastore_id = var.datastore_id

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
    datastore_id = var.datastore_id
    file_id      = var.disk_file_id
    interface    = "virtio0"
    iothread     = true
    discard      = "on"
    size         = var.disk_size
  }

  dynamic "disk" {
    for_each = var.data_disk_size > 0 ? [1] : []
    content {
      datastore_id = var.datastore_id
      interface    = "virtio1"
      iothread     = true
      discard      = "on"
      size         = var.data_disk_size
    }
  }
}
