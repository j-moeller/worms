output "private_internet" {
  value = {
    node_name = var.node_name
    name      = proxmox_network_linux_bridge.vmbr_private_internet.name
    address   = proxmox_network_linux_bridge.vmbr_private_internet.address
  }
}

output "maintenance" {
  value = {
    node_name = var.node_name
    name      = proxmox_network_linux_bridge.vmbr_maintenance.name
    address   = proxmox_network_linux_bridge.vmbr_maintenance.address
  }
}

output "monitor" {
  value = {
    node_name = var.node_name
    name      = proxmox_network_linux_bridge.vmbr_monitoring.name
    address   = proxmox_network_linux_bridge.vmbr_monitoring.address
  }
}