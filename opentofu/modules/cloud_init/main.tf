resource "proxmox_virtual_environment_file" "blank_vm_static" {
  content_type = "snippets"
  datastore_id = var.datastore_id
  node_name    = var.node_name

  source_raw {
    data = templatefile(
      "${path.module}/templates/vm-blank.tftpl",
      {
        ssh_public_key = trimspace(file("${var.ssh_public_key}")),
        hostname       = "vm"
      }
    )
    file_name = "vm-blank.yml"
  }
}

resource "proxmox_virtual_environment_file" "dns_vm_static" {
  content_type = "snippets"
  datastore_id = var.datastore_id
  node_name    = var.node_name

  source_raw {
    data = templatefile(
      "${path.module}/templates/vm-dns.tftpl",
      {
        ssh_public_key = trimspace(file("${var.ssh_public_key}")),
        hostname       = "dns"
      }
    )
    file_name = "vm-dns.yml"
  }
}

resource "proxmox_virtual_environment_file" "network_monitor_vm_static" {
  content_type = "snippets"
  datastore_id = var.datastore_id
  node_name    = var.node_name

  source_raw {
    data = templatefile(
      "${path.module}/templates/vm-network-monitor.tftpl",
      {
        ssh_public_key = trimspace(file("${var.ssh_public_key}")),
        hostname       = "network-monitor"
      }
    )
    file_name = "vm-network-monitor.yml"
  }
}

resource "proxmox_virtual_environment_file" "flask_vm_static" {
  content_type = "snippets"
  datastore_id = var.datastore_id
  node_name    = var.node_name

  source_raw {
    data = templatefile(
      "${path.module}/templates/vm-flask.tftpl",
      {
        ssh_public_key = trimspace(file("${var.ssh_public_key}")),
        hostname       = "flask"
      }
    )
    file_name = "vm-flask.yml"
  }
}

/**
 * VMs (= VMs that can be hacked by users)
 */

resource "proxmox_virtual_environment_file" "nginx_vm_static" {
  content_type = "snippets"
  datastore_id = var.datastore_id
  node_name    = var.node_name

  source_raw {
    data = templatefile(
      "${path.module}/templates/vm-nginx.tftpl",
      {
        ssh_public_key = trimspace(file("${var.ssh_public_key}")),
        hostname       = "nginx"
      }
    )
    file_name = "vm-nginx.yml"
  }
}

resource "proxmox_virtual_environment_file" "postgresql_vm_static" {
  content_type = "snippets"
  datastore_id = var.datastore_id
  node_name    = var.node_name

  source_raw {
    data = templatefile(
      "${path.module}/templates/vm-postgresql.tftpl",
      {
        ssh_public_key = trimspace(file("${var.ssh_public_key}")),
        hostname       = "postgresql"
      }
    )
    file_name = "vm-postgresq.yml"
  }
}