output "sandbox" {
  description = "Dynamic sandbox VMs. `hosts` is the flattened view the Ansible inventory generator consumes (maintenance IP + sandbox-facing addresses per VM)."
  value = {
    sandbox_vms = local.sandbox_vms

    gateway = {
      name           = proxmox_virtual_environment_vm.gw.name
      maintenance_ip = local.management_ips.sandbox_gw
      services_ip    = local.services_ips.sandbox_gw
      sandbox_legs   = local.gw_sandbox_nics
    }

    hosts = {
      for name, vm in local.sandbox_vms : name => {
        maintenance_ip = split("/", vm.maintenance_network_cidr)[0]
        addresses      = [for cfg in vm.ip_configs : split("/", cfg.network_cidr)[0]]
      }
    }
  }
}
