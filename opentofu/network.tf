resource "proxmox_network_linux_bridge" "vmbr_vm_private_network" {
  node_name = var.proxmox_instance
  name      = "vmbr1"

  address   = "10.0.1.1/24"
  ports     = []
  autostart = true
}

resource "proxmox_network_linux_bridge" "vmbr_private_internet" {
  node_name = var.proxmox_instance
  name      = "vmbr2"

  address   = "10.0.2.1/24"
  ports     = []
  autostart = true
}

resource "proxmox_network_linux_bridge" "vmbr_maintenance_network" {
  node_name = var.proxmox_instance
  name      = "vmbr3"

  address   = "10.0.3.1/24"
  ports     = []
  autostart = true
}

resource "proxmox_network_linux_bridge" "vmbr_monitoring_network" {
  node_name = var.proxmox_instance
  name      = "vmbr4"

  address   = "10.0.250.1/24"
  ports     = []
  autostart = true
}