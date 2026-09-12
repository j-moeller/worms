resource "proxmox_network_linux_bridge" "vmbr_private_internet" {
  node_name = var.node_name
  name      = "vmbr1"

  address   = var.private_internet_cidr
  ports     = []
  autostart = true
}

resource "proxmox_network_linux_bridge" "vmbr_maintenance" {
  node_name = var.node_name
  name      = "vmbr2"

  address   = var.maintenance_cidr
  ports     = []
  autostart = true
  comment   = "Every VM (sandbox, infrastructure, ...) is connected to this network. The network is used to connect to VMs via SSH."
}

resource "proxmox_network_linux_bridge" "vmbr_monitoring" {
  node_name = var.node_name
  name      = "vmbr3"

  address   = var.monitoring_cidr
  ports     = []
  autostart = true
  comment   = "This network is a mirror of the network(s) the sandbox VMs are communicating over."
}
