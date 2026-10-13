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

resource "proxmox_virtual_environment_file" "django_vm_static" {
  content_type = "snippets"
  datastore_id = var.datastore_id
  node_name    = var.node_name

  source_raw {
    data = templatefile(
      "${path.module}/templates/vm-django.tftpl",
      {
        ssh_public_key = trimspace(file("${var.ssh_public_key}")),
        hostname       = "django"
      }
    )
    file_name = "vm-django.yml"
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

/**
 * Private-internet service snippets
 */
resource "proxmox_virtual_environment_file" "apt_cache_vm_static" {
  content_type = "snippets"
  datastore_id = var.datastore_id
  node_name    = var.node_name

  source_raw {
    data = templatefile(
      "${path.module}/templates/vm-apt-cache.tftpl",
      {
        ssh_public_key = trimspace(file("${var.ssh_public_key}")),
        hostname       = "apt-cache"
      }
    )
    file_name = "vm-apt-cache.yml"
  }
}

resource "proxmox_virtual_environment_file" "devpi_vm_static" {
  content_type = "snippets"
  datastore_id = var.datastore_id
  node_name    = var.node_name

  source_raw {
    data = templatefile(
      "${path.module}/templates/vm-devpi.tftpl",
      {
        ssh_public_key = trimspace(file("${var.ssh_public_key}")),
        hostname       = "devpi"
      }
    )
    file_name = "vm-devpi.yml"
  }
}

resource "proxmox_virtual_environment_file" "docker_registry_vm_static" {
  content_type = "snippets"
  datastore_id = var.datastore_id
  node_name    = var.node_name

  source_raw {
    data = templatefile(
      "${path.module}/templates/vm-docker-registry.tftpl",
      {
        ssh_public_key = trimspace(file("${var.ssh_public_key}")),
        hostname       = "docker-registry"
      }
    )
    file_name = "vm-docker-registry.yml"
  }
}

resource "proxmox_virtual_environment_file" "manpages_vm_static" {
  content_type = "snippets"
  datastore_id = var.datastore_id
  node_name    = var.node_name

  source_raw {
    data = templatefile(
      "${path.module}/templates/vm-manpages.tftpl",
      {
        ssh_public_key = trimspace(file("${var.ssh_public_key}")),
        hostname       = "manpages"
      }
    )
    file_name = "vm-manpages.yml"
  }
}

resource "proxmox_virtual_environment_file" "ntp_vm_static" {
  content_type = "snippets"
  datastore_id = var.datastore_id
  node_name    = var.node_name

  source_raw {
    data = templatefile(
      "${path.module}/templates/vm-ntp.tftpl",
      {
        ssh_public_key = trimspace(file("${var.ssh_public_key}")),
        hostname       = "ntp"
      }
    )
    file_name = "vm-ntp.yml"
  }
}

resource "proxmox_virtual_environment_file" "cve_search_vm_static" {
  content_type = "snippets"
  datastore_id = var.datastore_id
  node_name    = var.node_name

  source_raw {
    data = templatefile(
      "${path.module}/templates/vm-cve-search.tftpl",
      {
        ssh_public_key = trimspace(file("${var.ssh_public_key}")),
        hostname       = "cve-search"
      }
    )
    file_name = "vm-cve-search.yml"
  }
}

resource "proxmox_virtual_environment_file" "wikipedia_vm_static" {
  content_type = "snippets"
  datastore_id = var.datastore_id
  node_name    = var.node_name

  source_raw {
    data = templatefile(
      "${path.module}/templates/vm-wikipedia.tftpl",
      {
        ssh_public_key = trimspace(file("${var.ssh_public_key}")),
        hostname       = "wikipedia"
      }
    )
    file_name = "vm-wikipedia.yml"
  }
}

