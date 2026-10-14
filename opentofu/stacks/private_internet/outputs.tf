output "private_internet" {
  description = "Private-internet services and their private / maintenance IPs"
  value = {
    for k, v in local.services : k => {
      network_cidr             = "${local.private_internet_ips[k].network}/24"
      maintenance_network_cidr = "${local.private_internet_ips[k].maintenance}/24"
    }
  }
}
