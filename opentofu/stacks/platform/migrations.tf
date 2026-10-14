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
