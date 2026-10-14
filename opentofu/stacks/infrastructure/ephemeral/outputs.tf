# The VMs in this stack's state, for the Ansible inventory
# (scripts/generate_inventory.py): Ansible contacts exactly these.
output "ansible_hosts" {
  value = {
    for key, vm in merge({ dns = module.dns }, module.sandbox_apis) : key => {
      name          = vm.name
      vm_id         = vm.vm_id
      management_ip = vm.management_ip
    }
  }
}
