/*
 * datacenter firewall
 */
resource "proxmox_virtual_environment_cluster_firewall" "datacenter-fw" {
  enabled = true

  ebtables       = true
  input_policy   = "DROP"
  output_policy  = "ACCEPT"
  forward_policy = "ACCEPT"
}

/*
 * worms-pve firewall
 */
resource "proxmox_node_firewall" "worms-pve-firewall" {
  node_name = var.proxmox_instance
  enabled   = true
}

/*
// This firewall rule should always exist regardless of whether we added it
// with tofu.

resource "proxmox_virtual_environment_firewall_rules" "worms-pve-inbound" {
  node_name = var.proxmox_instance

  rule {
    type   = "in"
    action = "ACCEPT"
    proto  = "tcp"
    dport  = "8006"
  }

  rule {
    type   = "in"
    action = "ACCEPT"
    macro  = "SSH"
  }

}
*/

/* 
 * vm firewall
 */
/*
resource "proxmox_virtual_environment_firewall_options" "heartbeat-firewall" {
  depends_on = [proxmox_virtual_environment_vm.heartbeat_server]

  node_name = var.proxmox_instance
  vm_id     = proxmox_virtual_environment_vm.heartbeat_server.id

  enabled       = true
  input_policy  = "DROP"
  output_policy = "ACCEPT"
}

resource "proxmox_virtual_environment_firewall_options" "vm-firewall" {
  for_each = proxmox_virtual_environment_vm.vm

  node_name = var.proxmox_instance
  vm_id     = each.value.id

  enabled       = true
  input_policy  = "ACCEPT"
  output_policy = "DROP"
}
*/


/*
resource "proxmox_virtual_environment_firewall_rules" "vm-heartbeat-fw" {
  node_name = var.proxmox_instance
  vm_id     = proxmox_virtual_environment_vm.heartbeat_server.vm_id
}
*/

/*
TODO: Add a firewall for the monitoring VM that DROPS every outgoing packet
and only allows for incoming packets.

resource "proxmox_virtual_environment_firewall_rules" "vm-heartbeat-fw" {
  node_name = var.proxmox_instance
  vm_id     = proxmox_virtual_environment_vm.heartbeat_server.vm_id
}
*/
