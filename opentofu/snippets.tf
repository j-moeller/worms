/**
 * infrastructure (= VMs that MUST NEVER be hacked by users)
 */

resource "proxmox_virtual_environment_file" "dns_vm_static" {
  content_type = "snippets"
  datastore_id = var.datastore_id
  node_name    = var.proxmox_instance

  source_raw {
    data = templatefile(
      "${path.module}/cloud-init/private-internet-dns.tftpl",
      {
        ssh_public_key = trimspace(file("${var.ssh_key_name}.pub")),
        hostname       = "dns"
        secret         = "<none>"
      }
    )
    file_name = "private-internet-dns.yml"
  }
}

resource "proxmox_virtual_environment_file" "aptcache_vm_static" {
  content_type = "snippets"
  datastore_id = var.datastore_id
  node_name    = var.proxmox_instance

  source_raw {
    data = templatefile(
      "${path.module}/cloud-init/private-internet-aptcache.tftpl",
      {
        ssh_public_key = trimspace(file("${var.ssh_key_name}.pub")),
        hostname       = "aptcache"
        secret         = "<none>"
      }
    )
    file_name = "private-internet-aptcache.yml"
  }
}

resource "proxmox_virtual_environment_file" "network_monitor_vm_static" {
  content_type = "snippets"
  datastore_id = var.datastore_id
  node_name    = var.proxmox_instance

  source_raw {
    data = templatefile(
      "${path.module}/cloud-init/infrastructure-network-monitor.tftpl",
      {
        ssh_public_key = trimspace(file("${var.ssh_key_name}.pub")),
        hostname       = "network-monitor"
        secret         = "<none>"
      }
    )
    file_name = "infrastructure-network-monitor.yml"
  }
}

resource "proxmox_virtual_environment_file" "flask_vm_static" {
  content_type = "snippets"
  datastore_id = var.datastore_id
  node_name    = var.proxmox_instance

  source_raw {
    data = templatefile(
      "${path.module}/cloud-init/vm-flask.tftpl",
      {
        ssh_public_key = trimspace(file("${var.ssh_key_name}.pub")),
        hostname       = "flask"
        secret         = "<none>"
      }
    )
    file_name = "vm-flask.yml"
  }
}

resource "proxmox_virtual_environment_file" "database_browser_vm_static" {
  content_type = "snippets"
  datastore_id = var.datastore_id
  node_name    = var.proxmox_instance

  source_raw {
    data = templatefile(
      "${path.module}/cloud-init/infrastructure-database-browser.tftpl",
      {
        ssh_public_key = trimspace(file("${var.ssh_key_name}.pub")),
        hostname       = "database-browser"
        secret         = "<none>"
      }
    )
    file_name = "infrastructure-database-browser.yml"
  }
}

/**
 * VMs (= VMs that can be hacked by users)
 */

resource "proxmox_virtual_environment_file" "nginx_vm_static" {
  content_type = "snippets"
  datastore_id = var.datastore_id
  node_name    = var.proxmox_instance

  source_raw {
    data = templatefile(
      "${path.module}/cloud-init/vm-nginx.tftpl",
      {
        ssh_public_key = trimspace(file("${var.ssh_key_name}.pub")),
        hostname       = "nginx"
        secret         = "<none>"
      }
    )
    file_name = "vm-nginx.yml"
  }
}

resource "proxmox_virtual_environment_file" "postgresql_vm_static" {
  content_type = "snippets"
  datastore_id = var.datastore_id
  node_name    = var.proxmox_instance

  source_raw {
    data = templatefile(
      "${path.module}/cloud-init/vm-postgresql.tftpl",
      {
        ssh_public_key = trimspace(file("${var.ssh_key_name}.pub")),
        hostname       = "postgresql"
        secret         = "<none>"
      }
    )
    file_name = "vm-postgresq.yml"
  }
}

resource "proxmox_virtual_environment_file" "vm_config" {
  for_each = local.user_vms

  content_type = "snippets"
  datastore_id = var.datastore_id
  node_name    = var.proxmox_instance

  source_raw {
    data = templatefile(
      "${path.module}/cloud-init/${each.value.snippet_type}.tftpl",
      {
        ssh_public_key = trimspace(file("${var.ssh_key_name}.pub")),
        hostname       = each.key
        secret         = each.value.secret
      }
    )
    file_name = "${each.value.snippet_type}-${each.key}.yml"
  }
}
