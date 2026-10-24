output "name" {
  value = proxmox_virtual_environment_vm.vm.name
}

output "vm_id" {
  value = proxmox_virtual_environment_vm.vm.vm_id
}

# Management address (last NIC): what Ansible connects to.
output "management_ip" {
  value = split("/", var.management.cidr)[0]
}
