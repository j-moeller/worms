output "platform" {
  value = {
    os_images  = module.os_images
    cloud_init = module.cloud_init
    networks   = module.networks.bridges
    pools      = proxmox_virtual_environment_pool.pools

    # Attached to every VM (port isolation on the management bridges).
    hookscript_file_id = proxmox_virtual_environment_file.hookscript.id

    # Security group names by purpose.
    security_groups = {
      ssh              = proxmox_virtual_environment_cluster_firewall_security_group.ssh.name
      provisioning     = proxmox_virtual_environment_cluster_firewall_security_group.provisioning.name
      dashboard        = proxmox_virtual_environment_cluster_firewall_security_group.dashboard.name
      orchestrator     = proxmox_virtual_environment_cluster_firewall_security_group.orchestrator.name
      database         = proxmox_virtual_environment_cluster_firewall_security_group.database.name
      db_client        = proxmox_virtual_environment_cluster_firewall_security_group.db_client.name
      sandbox_api      = proxmox_virtual_environment_cluster_firewall_security_group.sandbox_api.name
      dns              = proxmox_virtual_environment_cluster_firewall_security_group.dns.name
      private_internet = proxmox_virtual_environment_cluster_firewall_security_group.private_internet.name
      services_client  = proxmox_virtual_environment_cluster_firewall_security_group.services_client.name
      sandbox          = proxmox_virtual_environment_cluster_firewall_security_group.sandbox.name
      sandbox_gw       = proxmox_virtual_environment_cluster_firewall_security_group.sandbox_gw.name
    }
  }
}

# Ground truth for the Ansible inventory (scripts/generate_inventory.py) and the
# host nftables ruleset (ansible role host_network). Everything here derives from the
# single source of truth in opentofu/shared/shared-variables.tf.
output "infrastructure" {
  description = "Static addressing consumed by the inventory generator (and Ansible)."
  value = {
    bridges              = local.bridges
    networks             = local.networks
    host_ips             = local.host_ips
    infrastructure_ips   = local.infrastructure_ips
    services_ips         = local.services_ips
    management_ips       = local.management_ips
    private_internet_ips = local.private_internet_ips
    proxmox = {
      host     = local.proxmox_host_ip
      api_port = local.proxmox_api_port
      node     = var.proxmox_instance
    }
  }
}
