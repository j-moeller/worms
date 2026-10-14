output "node_name" {
  value = proxmox_virtual_environment_vm.infrastructure.node_name
}

output "vm_id" {
  value = proxmox_virtual_environment_vm.infrastructure.vm_id
}

output "name" {
  value = proxmox_virtual_environment_vm.infrastructure.name
}

# Management address (last NIC): what SSH and Ansible connect to.
output "management_ip" {
  value = split("/", var.nics[length(var.nics) - 1].cidr)[0]
}
