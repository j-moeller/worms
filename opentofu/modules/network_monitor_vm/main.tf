# Network monitor: receives a copy of every frame on the sandbox networks
# (mirrored into the mirror bridge by the Proxmox host, ansible role
# host_network: worms-mirror) and writes flow records to the database.
#
#   net0  data      ingest bridge, default route; database:5432 + syslog only
#   net1  mgmt      mgmt-exposed bridge; SSH from the host only
#   net2  capture   mirror bridge; no address, promiscuous, receive-only
#
# It parses attacker traffic, so it is treated like scoring/logging (high
# risk): management on mgmt-exposed, INSERT-only database role.

resource "proxmox_pool_membership" "pool_membership" {
  pool_id = var.pool_id
  vm_id   = proxmox_virtual_environment_vm.monitor.vm_id
}

# Default deny in both directions; ipfilter pins net0/net1 to their addresses.
# The capture NIC has the Proxmox firewall disabled (below): the mirrored
# frames are addressed to other VMs and would be dropped by it. It is made
# receive-only on the host instead (worms-mirror drops everything the VM sends
# into the mirror bridge; the bridge has no other port and no host address).
resource "proxmox_virtual_environment_firewall_options" "monitor" {
  node_name = var.proxmox_instance
  vm_id     = proxmox_virtual_environment_vm.monitor.vm_id

  enabled       = true
  input_policy  = "DROP"
  output_policy = "DROP"
  ipfilter      = true
  macfilter     = true
}

resource "proxmox_virtual_environment_firewall_ipset" "ipfilter" {
  for_each = {
    "ipfilter-net0" = var.data_nic.cidr
    "ipfilter-net1" = var.management_nic.cidr
  }

  node_name = var.proxmox_instance
  vm_id     = proxmox_virtual_environment_vm.monitor.vm_id
  name      = each.key
  comment   = "Only the configured address may be sent from ${trimprefix(each.key, "ipfilter-")}"

  cidr {
    name = split("/", each.value)[0]
  }
}

resource "proxmox_virtual_environment_firewall_rules" "monitor" {
  node_name = var.proxmox_instance
  vm_id     = proxmox_virtual_environment_vm.monitor.vm_id

  dynamic "rule" {
    for_each = var.firewall_rules

    content {
      security_group = rule.value.group
      iface          = rule.value.iface
      comment        = "Apply security group: ${rule.value.group}${rule.value.iface == null ? "" : " on ${rule.value.iface}"}"
    }
  }
}

resource "proxmox_virtual_environment_vm" "monitor" {
  name      = var.name
  node_name = var.proxmox_instance

  cpu { cores = var.cpu }
  memory { dedicated = var.ram }

  # Only SSH is used: no guest agent (the host never parses guest-controlled
  # agent replies; stop/kill works without it), no serial console, no display,
  # no USB tablet. A VM that ignores the ACPI shutdown is hard-stopped on
  # destroy.
  agent { enabled = false }
  tablet_device   = false
  stop_on_destroy = true
  vga { type = "none" }

  tags = ["infrastructure", var.tag]

  initialization {
    datastore_id = "local-zfs"

    # ipconfig0 / ipconfig1 only: the capture NIC (net2) gets no address.
    ip_config {
      ipv4 {
        address = var.data_nic.cidr
        gateway = var.data_nic.gateway
      }
    }

    ip_config {
      ipv4 {
        address = var.management_nic.cidr
      }
    }

    user_data_file_id = var.user_data_file_id
  }

  network_device {
    bridge   = var.data_nic.bridge
    model    = "virtio"
    firewall = true
  }

  network_device {
    bridge   = var.management_nic.bridge
    model    = "virtio"
    firewall = true
  }

  network_device {
    bridge   = var.capture_bridge
    model    = "virtio"
    firewall = false
  }

  disk {
    datastore_id = "local-zfs"
    file_id      = var.disk_file_id
    interface    = "virtio0"
    iothread     = true
    discard      = "on"
    size         = var.disk_size
  }
}
