output "platform" {
  value = {
    os_images  = module.os_images
    cloud_init = module.cloud_init.snippets
    networks   = module.networks.bridges
    pools      = proxmox_virtual_environment_pool.pools

    # Attached to every VM (port isolation on the management bridges).
    hookscript_file_id = proxmox_virtual_environment_file.hookscript.id
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
    services_ports       = local.private_internet_ports
    # Devbox (docs/devbox.md): the trusted SSH gateway and the services-bridge
    # block reserved for the per-team devbox gateways' SNAT source.
    ssh_gateway_ips         = local.ssh_gateway_ips
    ssh_gateway_public_port = local.ssh_gateway_public_port
    devbox_services_block   = local.devbox_services_block
    proxmox = {
      host     = local.proxmox_host_ip
      api_port = local.proxmox_api_port
      node     = var.proxmox_instance
    }
  }
}
