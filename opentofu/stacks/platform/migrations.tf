# The firewall resources moved to the firewall stack. `make firewall-migrate`
# (run by `make platform`) moves them from this state into the firewall
# state. These blocks are the safety net if this stack is applied before that:
# OpenTofu then forgets the resources instead of destroying the security groups
# every VM references. No-ops once the resources are gone from this state;
# delete this file once every deployment has migrated.

removed {
  from = proxmox_virtual_environment_cluster_firewall.main
  lifecycle {
    destroy = false
  }
}

removed {
  from = proxmox_node_firewall.worms_pve
  lifecycle {
    destroy = false
  }
}

removed {
  from = proxmox_virtual_environment_cluster_firewall_security_group.ssh
  lifecycle {
    destroy = false
  }
}

removed {
  from = proxmox_virtual_environment_cluster_firewall_security_group.provisioning
  lifecycle {
    destroy = false
  }
}

removed {
  from = proxmox_virtual_environment_cluster_firewall_security_group.dashboard
  lifecycle {
    destroy = false
  }
}

removed {
  from = proxmox_virtual_environment_cluster_firewall_security_group.orchestrator
  lifecycle {
    destroy = false
  }
}

removed {
  from = proxmox_virtual_environment_cluster_firewall_security_group.database
  lifecycle {
    destroy = false
  }
}

removed {
  from = proxmox_virtual_environment_cluster_firewall_security_group.db_client
  lifecycle {
    destroy = false
  }
}

removed {
  from = proxmox_virtual_environment_cluster_firewall_security_group.sandbox_api
  lifecycle {
    destroy = false
  }
}

removed {
  from = proxmox_virtual_environment_cluster_firewall_security_group.dns
  lifecycle {
    destroy = false
  }
}

removed {
  from = proxmox_virtual_environment_cluster_firewall_security_group.private_internet
  lifecycle {
    destroy = false
  }
}

removed {
  from = proxmox_virtual_environment_cluster_firewall_security_group.services_client
  lifecycle {
    destroy = false
  }
}

removed {
  from = proxmox_virtual_environment_cluster_firewall_security_group.sandbox
  lifecycle {
    destroy = false
  }
}

removed {
  from = proxmox_virtual_environment_cluster_firewall_security_group.sandbox_gw
  lifecycle {
    destroy = false
  }
}

# The cloud-init snippets were one resource each (<key>_vm_static); now one
# for_each. Moving them keeps each file and its id: without the moves OpenTofu
# would delete and upload files of the same name in no fixed order. No-ops once
# applied; delete with the blocks above.

moved {
  from = module.cloud_init.proxmox_virtual_environment_file.blank_vm_static
  to   = module.cloud_init.proxmox_virtual_environment_file.snippet["blank"]
}

moved {
  from = module.cloud_init.proxmox_virtual_environment_file.postgresql_vm_static
  to   = module.cloud_init.proxmox_virtual_environment_file.snippet["postgresql"]
}

moved {
  from = module.cloud_init.proxmox_virtual_environment_file.django_vm_static
  to   = module.cloud_init.proxmox_virtual_environment_file.snippet["django"]
}

moved {
  from = module.cloud_init.proxmox_virtual_environment_file.wazuh_vm_static
  to   = module.cloud_init.proxmox_virtual_environment_file.snippet["wazuh"]
}

moved {
  from = module.cloud_init.proxmox_virtual_environment_file.dns_vm_static
  to   = module.cloud_init.proxmox_virtual_environment_file.snippet["dns"]
}

moved {
  from = module.cloud_init.proxmox_virtual_environment_file.flask_vm_static
  to   = module.cloud_init.proxmox_virtual_environment_file.snippet["flask"]
}

moved {
  from = module.cloud_init.proxmox_virtual_environment_file.network_monitor_vm_static
  to   = module.cloud_init.proxmox_virtual_environment_file.snippet["network_monitor"]
}

moved {
  from = module.cloud_init.proxmox_virtual_environment_file.apt_cache_vm_static
  to   = module.cloud_init.proxmox_virtual_environment_file.snippet["apt_cache"]
}

moved {
  from = module.cloud_init.proxmox_virtual_environment_file.devpi_vm_static
  to   = module.cloud_init.proxmox_virtual_environment_file.snippet["devpi"]
}

moved {
  from = module.cloud_init.proxmox_virtual_environment_file.docker_registry_vm_static
  to   = module.cloud_init.proxmox_virtual_environment_file.snippet["docker_registry"]
}

moved {
  from = module.cloud_init.proxmox_virtual_environment_file.manpages_vm_static
  to   = module.cloud_init.proxmox_virtual_environment_file.snippet["manpages"]
}

moved {
  from = module.cloud_init.proxmox_virtual_environment_file.ntp_vm_static
  to   = module.cloud_init.proxmox_virtual_environment_file.snippet["ntp"]
}

moved {
  from = module.cloud_init.proxmox_virtual_environment_file.cve_search_vm_static
  to   = module.cloud_init.proxmox_virtual_environment_file.snippet["cve_search"]
}

moved {
  from = module.cloud_init.proxmox_virtual_environment_file.wikipedia_vm_static
  to   = module.cloud_init.proxmox_virtual_environment_file.snippet["wikipedia"]
}
