resource "proxmox_pool_membership" "pool_membership" {
  pool_id = var.pool_id
  vm_id   = proxmox_virtual_environment_vm.infrastructure.vm_id
}

# Default deny in both directions; ipfilter pins every NIC to its configured
# address (ipsets below), so the VM cannot impersonate another VM or the host.
resource "proxmox_virtual_environment_firewall_options" "infrastructure" {
  node_name = var.proxmox_instance
  vm_id     = proxmox_virtual_environment_vm.infrastructure.vm_id

  enabled       = true
  input_policy  = "DROP"
  output_policy = "DROP"
  ipfilter      = true
  macfilter     = true
}

resource "proxmox_virtual_environment_firewall_ipset" "ipfilter" {
  count = length(var.nics)

  node_name = var.proxmox_instance
  vm_id     = proxmox_virtual_environment_vm.infrastructure.vm_id
  name      = "ipfilter-net${count.index}"
  comment   = "Only the configured address may be sent from net${count.index}"

  cidr {
    name = split("/", var.nics[count.index].cidr)[0]
  }
}

# Security groups, each optionally pinned to one NIC (iface = "netN").
resource "proxmox_virtual_environment_firewall_rules" "infrastructure" {
  node_name = var.proxmox_instance
  vm_id     = proxmox_virtual_environment_vm.infrastructure.vm_id

  dynamic "rule" {
    for_each = var.firewall_rules

    content {
      security_group = rule.value.group
      iface          = rule.value.iface
      comment        = "Apply security group: ${rule.value.group}${rule.value.iface == null ? "" : " on ${rule.value.iface}"}"
    }
  }
}

resource "proxmox_virtual_environment_vm" "infrastructure" {
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

  # Isolates the ports on management bridges (platform stack: hookscript).
  hook_script_file_id = var.hookscript_file_id

  initialization {
    datastore_id = "local-zfs"

    dynamic "ip_config" {
      for_each = var.nics

      content {
        ipv4 {
          address = ip_config.value.cidr
          gateway = ip_config.value.gateway
        }
      }
    }

    user_data_file_id = var.user_data_file_id
  }

  dynamic "network_device" {
    for_each = var.nics

    content {
      bridge   = network_device.value.bridge
      model    = "virtio"
      firewall = true
    }
  }

  disk {
    datastore_id = "local-zfs"
    file_id      = var.disk_file_id
    interface    = "virtio0"
    iothread     = true
    discard      = "on"
    size         = 5
  }
}
