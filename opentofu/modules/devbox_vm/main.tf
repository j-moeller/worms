# One devbox VM (A/B/C) or a per-team devbox gateway (docs/devbox.md). Same
# containment as a sandbox VM (own storage, CPU/NIC/disk caps, no guest agent,
# SSH-only, hard stop) with ipfilter pinning every NIC to its own address.
#
# NIC order (net0..):
#   segment NICs (the devbox bridges this VM is on)
#   then the "middle" NIC   -- services (gateway) OR SSH fabric (plain VM)
#   then the management NIC  -- always last (vmbr6, host SSH for Ansible)

terraform {
  required_providers {
    proxmox = {
      source = "bpg/proxmox"
    }
  }
}

locals {
  middle = var.is_gateway ? var.services : var.ssh_fabric

  # The full, ordered NIC list.
  nics = concat(
    var.segment_nics,
    local.middle == null ? [] : [{ bridge = local.middle.bridge, cidr = local.middle.cidr, gateway = null }],
    [{ bridge = var.management.bridge, cidr = var.management.cidr, gateway = null }],
  )

  n_segments = length(var.segment_nics)
  middle_idx = local.n_segments       # index of the services/ssh-fabric NIC
  mgmt_idx   = length(local.nics) - 1 # management NIC is always last

  segment_group = var.is_gateway ? var.security_groups["devbox_gw"] : var.security_groups["devbox"]
  middle_group  = var.is_gateway ? var.security_groups["services_client"] : var.security_groups["devbox_ssh_target"]
}

resource "proxmox_virtual_environment_vm" "vm" {
  name      = var.name
  node_name = var.proxmox_instance

  cpu {
    cores = 1
    limit = 1
    units = var.limits.cpu_units
  }
  # Ballooning (docs/resources.md): reclaimable down to ram_min under pressure.
  memory {
    dedicated = var.ram
    floating  = var.ram_min
  }

  agent { enabled = false }
  tablet_device   = false
  stop_on_destroy = true
  vga { type = "none" }

  tags = ["untrusted", var.is_gateway ? "devbox-gw" : "devbox"]

  # Port isolation: isolate this VM's ports on the management bridge (vmbr6) and,
  # for a plain devbox VM, on the devbox SSH fabric (vmbr7), so teams' devbox VMs
  # cannot reach each other at layer 2 -- only the gateway and the host. Left
  # commented because attaching a hookscript needs the provider to act as
  # root@pam; the provisioning API token cannot. Enable when applying with
  # root@pam credentials (docs/security-assessment.md, S4; docs/devbox.md).
  # hook_script_file_id = var.hookscript_file_id

  initialization {
    datastore_id = var.datastore

    dynamic "ip_config" {
      for_each = local.nics
      content {
        ipv4 {
          address = ip_config.value.cidr
          gateway = ip_config.value.gateway
        }
      }
    }

    dns {
      servers = [var.dns_server]
      domain  = "worms.internal"
    }

    user_data_file_id = var.user_data_file_id
  }

  dynamic "network_device" {
    for_each = local.nics
    content {
      bridge     = network_device.value.bridge
      model      = "virtio"
      firewall   = true
      rate_limit = var.is_gateway ? var.limits.gw_nic_mbps : var.limits.nic_rate_mbps
    }
  }

  disk {
    datastore_id = var.datastore
    file_id      = var.disk_file_id
    interface    = "virtio0"
    iothread     = true
    discard      = "on"
    size         = 5
    speed {
      iops_read  = var.limits.disk_iops
      iops_write = var.limits.disk_iops
      read       = var.limits.disk_rate_mbps
      write      = var.limits.disk_rate_mbps
    }
  }
}

resource "proxmox_pool_membership" "vm" {
  pool_id = var.pool_id
  vm_id   = proxmox_virtual_environment_vm.vm.vm_id
}

resource "proxmox_virtual_environment_firewall_options" "vm" {
  node_name     = var.proxmox_instance
  vm_id         = proxmox_virtual_environment_vm.vm.vm_id
  enabled       = true
  input_policy  = "DROP"
  output_policy = "DROP"
  ipfilter      = true
  macfilter     = true
}

# Pin every NIC to its own address (no spoofing another VM or a gateway).
resource "proxmox_virtual_environment_firewall_ipset" "ipfilter" {
  count     = length(local.nics)
  node_name = var.proxmox_instance
  vm_id     = proxmox_virtual_environment_vm.vm.vm_id
  name      = "ipfilter-net${count.index}"
  cidr {
    name = split("/", local.nics[count.index].cidr)[0]
  }
}

resource "proxmox_virtual_environment_firewall_rules" "vm" {
  node_name = var.proxmox_instance
  vm_id     = proxmox_virtual_environment_vm.vm.vm_id

  # Experiment / routing on the segment NICs.
  dynamic "rule" {
    for_each = range(local.n_segments)
    content {
      security_group = local.segment_group
      iface          = "net${rule.value}"
      comment        = "devbox segment"
    }
  }

  # Middle NIC: services egress (gateway) or SSH-in from the gateway (VM).
  dynamic "rule" {
    for_each = local.middle == null ? [] : [1]
    content {
      security_group = local.middle_group
      iface          = "net${local.middle_idx}"
      comment        = var.is_gateway ? "services egress" : "SSH from the gateway"
    }
  }

  # Management NIC: host SSH (Ansible).
  rule {
    security_group = var.security_groups["ssh"]
    iface          = "net${local.mgmt_idx}"
    comment        = "SSH from the Proxmox host"
  }
}
