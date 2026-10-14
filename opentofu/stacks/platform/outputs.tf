output "platform" {
  value = {
    os_images  = module.os_images
    cloud_init = module.cloud_init
    networks   = module.networks
    pools      = proxmox_virtual_environment_pool.pools
    security_group_names = {
      ssh : proxmox_virtual_environment_cluster_firewall_security_group.ssh
      webserver : proxmox_virtual_environment_cluster_firewall_security_group.webserver
      private_internet : proxmox_virtual_environment_cluster_firewall_security_group.private_internet
      sandbox_rest_api : proxmox_virtual_environment_cluster_firewall_security_group.sandbox_rest_api
      dashboard : proxmox_virtual_environment_cluster_firewall_security_group.dashboard
      orchestrator : proxmox_virtual_environment_cluster_firewall_security_group.orchestrator
      database : proxmox_virtual_environment_cluster_firewall_security_group.database
      dns : proxmox_virtual_environment_cluster_firewall_security_group.dns
    }
  }
}

# Ground truth for the Ansible inventory. scripts/generate_inventory.py reads
# `tofu output -json infrastructure` from this (always-applied) stack and writes
# ansible/inventory.generated.yml. Everything here derives from the single
# source of truth in opentofu/shared/shared-variables.tf.
output "infrastructure" {
  description = "Static infrastructure addressing consumed by the Ansible inventory generator."
  value = {
    infrastructure_ips   = local.infrastructure_ips
    dns_maintenance_ip   = local.dns_maintenance_ip
    private_internet_ips = local.private_internet_ips
    networks             = local.networks
    proxmox = {
      host                     = local.proxmox_host_ip
      sandbox_maintenance_host = local.sandbox_maintenance_host_ip
      api_port                 = local.proxmox_api_port
      node                     = var.proxmox_instance
    }
  }
}