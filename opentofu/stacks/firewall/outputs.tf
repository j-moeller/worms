# Security group names by purpose. The VM stacks attach them per NIC
# (data "terraform_remote_state" "firewall").
output "security_groups" {
  value = {
    ssh                = proxmox_virtual_environment_cluster_firewall_security_group.ssh.name
    provisioning       = proxmox_virtual_environment_cluster_firewall_security_group.provisioning.name
    dashboard          = proxmox_virtual_environment_cluster_firewall_security_group.dashboard.name
    orchestrator       = proxmox_virtual_environment_cluster_firewall_security_group.orchestrator.name
    database           = proxmox_virtual_environment_cluster_firewall_security_group.database.name
    db_client          = proxmox_virtual_environment_cluster_firewall_security_group.db_client.name
    sandbox_api        = proxmox_virtual_environment_cluster_firewall_security_group.sandbox_api.name
    dns                = proxmox_virtual_environment_cluster_firewall_security_group.dns.name
    private_internet   = proxmox_virtual_environment_cluster_firewall_security_group.private_internet.name
    services_client    = proxmox_virtual_environment_cluster_firewall_security_group.services_client.name
    pi_apt_client      = proxmox_virtual_environment_cluster_firewall_security_group.pi_apt_client.name
    sandbox            = proxmox_virtual_environment_cluster_firewall_security_group.sandbox.name
    sandbox_gw         = proxmox_virtual_environment_cluster_firewall_security_group.sandbox_gw.name
    wazuh              = proxmox_virtual_environment_cluster_firewall_security_group.wazuh.name
    log_client         = proxmox_virtual_environment_cluster_firewall_security_group.log_client.name
    devbox             = proxmox_virtual_environment_cluster_firewall_security_group.devbox.name
    devbox_gw          = proxmox_virtual_environment_cluster_firewall_security_group.devbox_gw.name
    devbox_api         = proxmox_virtual_environment_cluster_firewall_security_group.devbox_api.name
    devbox_ssh_gateway = proxmox_virtual_environment_cluster_firewall_security_group.devbox_ssh_gateway.name
    devbox_ssh_target  = proxmox_virtual_environment_cluster_firewall_security_group.devbox_ssh_target.name
  }
}
