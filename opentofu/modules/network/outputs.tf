output "bridges" {
  description = "Zone -> {name, address} of every bridge this module manages."
  value = {
    for zone, bridge in proxmox_network_linux_bridge.zone : zone => {
      name    = bridge.name
      address = bridge.address
    }
  }
}
