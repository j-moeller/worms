# For the Ansible inventory (scripts/generate_inventory.py): devbox VMs land in
# group `devbox`, the per-team gateways in `devbox_gw`, and the shared SSH
# gateway in `ssh_gateway`. Each host carries its team and (for VMs) its role.
output "ansible_hosts" {
  value = merge(
    {
      for key, vm in module.devbox_vm : key => {
        name          = vm.name
        vm_id         = vm.vm_id
        management_ip = vm.management_ip
        host_vars = {
          devbox_team   = local.devbox_vms[key].team
          devbox_ssh_ip = split("/", local.devbox_vms[key].ssh_fabric_cidr)[0]
        }
      }
    },
    {
      for key, gw in module.devbox_gw : "devbox-${key}-gw" => {
        name          = gw.name
        vm_id         = gw.vm_id
        management_ip = gw.management_ip
        host_vars     = { devbox_team = local.devbox_gws[key].team }
      }
    },
    {
      ssh_gateway = {
        name          = proxmox_virtual_environment_vm.ssh_gateway.name
        vm_id         = proxmox_virtual_environment_vm.ssh_gateway.vm_id
        management_ip = local.ssh_gateway_ips.maintenance
      }
    },
  )
}

# The devbox environment, for the database (round agent devbox_provision) and
# for the scoring/logging servers' source->team map: per team, the gateway's
# services-bridge SNAT source (what scoring/logging attribute to the team), the
# VMs (name, role, ssh-fabric and maintenance addresses) and the segment nets.
output "devbox" {
  value = {
    for team in distinct([for g in values(local.devbox_gws) : g.team]) : team => {
      services_source_ip = split("/", [for g in values(local.devbox_gws) : g.services_cidr if g.team == team][0])[0]
      vms = [
        for key, vm in local.devbox_vms : {
          name           = vm.name
          ssh_fabric_ip  = split("/", vm.ssh_fabric_cidr)[0]
          maintenance_ip = split("/", vm.maintenance_cidr)[0]
          segments       = vm.segments
        } if vm.team == team
      ]
    }
  }
}
