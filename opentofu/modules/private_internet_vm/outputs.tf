output "vm_id" {
  value = proxmox_virtual_environment_vm.service_vm.vm_id
}

output "name" {
  value = proxmox_virtual_environment_vm.service_vm.name
}

# Management address (net1): what SSH and Ansible connect to.
output "management_ip" {
  value = split("/", var.maintenance_network_cidr)[0]
}
