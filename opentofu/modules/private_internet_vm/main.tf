
/**
 * Private-internet service VM.
 *
 * Two NICs: services bridge (net0, what sandbox VMs reach, no gateway) and
 * mgmt-exposed (net1, SSH from the host; its gateway is the host, which only
 * forwards it to the internet while internet access is allowed (Internet.yml)).
 * Optional dedicated data disk (virtio1) so a service's payload (Mongo CVE db,
 * Kiwix ZIM, caches) lives on its own volume, separate from the OS/root disk.
 *
 * NOTE on persistence: the data disk is a separate volume from the root disk,
 * so rebuilding the OS does not touch it. A full `tofu destroy` of this stack
 * still removes it (bpg has no standalone disk resource). These VMs live in
 * the long-lived private_internet stack and are not torn down per round.
 */
resource "proxmox_pool_membership" "pool_membership" {
  pool_id = var.pool_id
  vm_id   = proxmox_virtual_environment_vm.service_vm.vm_id
}

resource "proxmox_virtual_environment_firewall_options" "service_vm" {
  node_name = var.proxmox_instance
  vm_id     = proxmox_virtual_environment_vm.service_vm.vm_id

  enabled       = true
  input_policy  = "DROP"
  output_policy = "DROP"
  ipfilter      = true
  macfilter     = true
}

resource "proxmox_virtual_environment_firewall_ipset" "ipfilter" {
  for_each = {
    "ipfilter-net0" = var.network_cidr
    "ipfilter-net1" = var.maintenance_network_cidr
  }

  node_name = var.proxmox_instance
  vm_id     = proxmox_virtual_environment_vm.service_vm.vm_id
  name      = each.key
  comment   = "Only the configured address may be sent from this NIC"

  cidr {
    name = split("/", each.value)[0]
  }
}

resource "proxmox_virtual_environment_firewall_rules" "service_vm" {
  node_name = var.proxmox_instance
  vm_id     = proxmox_virtual_environment_vm.service_vm.vm_id

  dynamic "rule" {
    for_each = var.firewall_rules

    content {
      security_group = rule.value.group
      iface          = rule.value.iface
      comment        = "Apply security group: ${rule.value.group}${rule.value.iface == null ? "" : " on ${rule.value.iface}"}"
    }
  }
}

resource "proxmox_virtual_environment_vm" "service_vm" {
  name      = var.name
  node_name = var.proxmox_instance

  cpu {
    cores = var.cpu
    type  = var.cpu_type
  }
  # Ballooning: the host may reclaim down to the floor (ram_min, default
  # half the ceiling) when it is under memory pressure. Lets the node run
  # more VMs than the sum of their ceilings (docs/resources.md).
  memory {
    dedicated = var.ram
    floating  = var.ram_min != null ? var.ram_min : max(256, floor(var.ram / 2))
  }

  # Only SSH is used: no guest agent (the host never parses guest-controlled
  # agent replies; stop/kill works without it), no serial console, no display,
  # no USB tablet. A VM that ignores the ACPI shutdown is hard-stopped on
  # destroy.
  agent { enabled = false }
  tablet_device   = false
  stop_on_destroy = true
  vga { type = "none" }

  tags = [
    "private_internet"
  ]

  # Port isolation on the management bridges: isolate this VM's ports so VMs on a
  # management bridge cannot reach each other at layer 2 (defense in depth behind
  # the per-VM firewall). Left commented because attaching a hookscript needs the
  # provider to act as root@pam; the provisioning API token cannot. Enable when
  # applying with root@pam credentials (docs/security-assessment.md, S4).
  # hook_script_file_id = var.hookscript_file_id

  initialization {
    datastore_id = var.datastore_id

    ip_config {
      ipv4 {
        address = var.network_cidr
      }
    }

    ip_config {
      ipv4 {
        address = var.maintenance_network_cidr
        gateway = var.maintenance_network_gateway
      }
    }

    user_data_file_id = var.user_data_file_id
  }

  network_device {
    bridge   = var.vmbr_network
    model    = "virtio"
    firewall = true
  }

  network_device {
    bridge   = var.vmbr_maintenance_network
    model    = "virtio"
    firewall = true
  }

  disk {
    datastore_id = var.datastore_id
    import_from  = var.disk_file_id
    interface    = "virtio0"
    iothread     = true
    discard      = "on"
    size         = var.disk_size
  }

  dynamic "disk" {
    for_each = var.data_disk_size > 0 ? [1] : []
    content {
      datastore_id = var.datastore_id
      interface    = "virtio1"
      iothread     = true
      discard      = "on"
      size         = var.data_disk_size
    }
  }
}

moved {
  from = proxmox_virtual_environment_firewall_rules.service_vm_ssh
  to   = proxmox_virtual_environment_firewall_rules.service_vm
}
