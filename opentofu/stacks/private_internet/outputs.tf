output "private_internet" {
  description = "Private-internet services and their private / maintenance IPs"
  value = {
    for k, v in local.private_internet : k => {
      network_cidr             = v.network_cidr
      maintenance_network_cidr = v.maintenance_network_cidr
    }
  }
}
