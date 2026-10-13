output "sandbox" {
  description = "Dynamic sandbox VMs. `hosts` is the flattened view the Ansible inventory generator consumes (maintenance IP + sandbox-facing addresses per VM)."
  value = {
    sandbox_vms = local.sandbox_vms

    hosts = {
      for name, vm in local.sandbox_vms : name => {
        maintenance_ip = split("/", vm.maintenance_network_cidr)[0]
        addresses      = [for cfg in vm.ip_configs : split("/", cfg.network_cidr)[0]]
      }
    }
  }
}
