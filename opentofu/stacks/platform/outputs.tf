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