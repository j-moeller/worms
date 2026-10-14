output "private_internet" {
  description = "Private-internet services and their private / maintenance IPs"
  value = {
    for k, v in local.services : k => {
      network_cidr             = "${local.private_internet_ips[k].network}/24"
      maintenance_network_cidr = "${local.private_internet_ips[k].maintenance}/24"
    }
  }
}

# The VMs in this stack's state, for the Ansible inventory
# (scripts/generate_inventory.py): Ansible contacts exactly these, so a
# commented-out service is skipped after `tofu apply`.
output "ansible_hosts" {
  value = {
    for key, vm in module.private_internet_vms : key => {
      name          = vm.name
      vm_id         = vm.vm_id
      management_ip = vm.management_ip
    }
  }
}
