resource "proxmox_network_linux_bridge" "vmbr_vm_private_network" {
  node_name = var.proxmox_instance
  name      = "vmbr1"

  address   = "10.1.0.1/24"
  ports     = []
  autostart = true
}

resource "proxmox_network_linux_bridge" "vmbr_private_internet" {
  node_name = var.proxmox_instance
  name      = "vmbr2"

  address   = "10.2.0.1/24"
  ports     = []
  autostart = true
}

resource "proxmox_network_linux_bridge" "vmbr_maintenance_network" {
  node_name = var.proxmox_instance
  name      = "vmbr3"

  address   = "10.3.0.1/24"
  ports     = []
  autostart = true
}

resource "proxmox_network_linux_bridge" "vmbr_monitoring_network" {
  node_name = var.proxmox_instance
  name      = "vmbr4"

  address   = "10.250.0.1/24"
  ports     = []
  autostart = true
}
