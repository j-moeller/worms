# The VMs in this stack's state, for the Ansible inventory
# (scripts/generate_inventory.py): Ansible contacts exactly these. `host_vars`
# become host variables (the monitor's capture interface MAC).
output "ansible_hosts" {
  value = merge(
    {
      for key, vm in merge({ dns = module.dns }, module.sandbox_apis) : key => {
        name          = vm.name
        vm_id         = vm.vm_id
        management_ip = vm.management_ip
      }
    },
    {
      monitor = {
        name          = module.monitor.name
        vm_id         = module.monitor.vm_id
        management_ip = module.monitor.management_ip
        host_vars = {
          monitor_capture_mac = module.monitor.capture_mac
        }
      }
    },
  )
}
