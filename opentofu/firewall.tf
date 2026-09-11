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
// with tofu. We thus do not manage it with tofu.

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

/**
 * # TODO: Specify ports
 *
 * Firewall rules:
 *  - DB:
 *     (reads)
 *    - Dashboard --> DB:5432 (allow)
 *    - DBBrowser --> DB:5432 (allow)
 *     (writes)
 *    - Heartbeat --> DB:5432 (allow)
 *    - Monitor --> DB:5432 (allow)
 *    - Logging --> DB:5432 (allow)
 *    - Scoring --> DB:5432 (allow)
 *    - else (drop)
 *
 *  - Monitor
 *    - ??? --> Monitor (allow) # How do we allow promiscous mode via FW?
 *    - Monitor --> DB:5432 (allow)
 *    - else (drop)
 *
 *  - Heartbeat
 *    - Heartbeat --> User VM (allow)
 *    - Heartbeat --> DB:5432 (allow)
 *    - else (drop)
 *
 *  - Scoring
 *    - User VM --> Scoring (allow)
 *    - Scoring --> DB:5432 (allow)
 *    - else (drop)
 *
 *  - Logging
 *    - User VM --> Logging (allow)
 *    - Logging --> DB:5432 (allow)
 *    - else (drop)
 *
 *  - Private Internet VMs (except apt-cache-ng VM, or other caches)
 *    - User VM --> Private Internet VM (allow)
 *    - else (drop)
 *
 *  - apt-cache-ng VM, or other caches
 *    - User VM --> Private Internet VM (allow)
 *    - Private Internet VM --> Internet (allow)
 *    - else (drop)
 *
 *  - DNS
 *    - any --> DNS:53 (allow)
 *    - else (drop)
 *
 *  - User VMs:
 *    - User VM <--> User VM (accept)
 *    - User VM --> Scoring (accept)
 *    - User VM --> Logging (accept)
 *    - User VM --> Private Internet Gateway (accept)
 *    - User VM --> Private Internet VMs (accept)
 *    - Heartbeat --> User VM (accept)
 *    - ??? --> User VM (accept) # SSH maintenance
 *    - else (drop)
 */