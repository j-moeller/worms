output "node_name" {
  value = proxmox_virtual_environment_vm.monitor.node_name
}

output "vm_id" {
  value = proxmox_virtual_environment_vm.monitor.vm_id
}

output "name" {
  value = proxmox_virtual_environment_vm.monitor.name
}

# What SSH and Ansible connect to.
output "management_ip" {
  value = split("/", var.management_nic.cidr)[0]
}

# Ansible identifies the capture interface inside the VM by this MAC.
output "capture_mac" {
  value = lower(proxmox_virtual_environment_vm.monitor.network_device[2].mac_address)
}
