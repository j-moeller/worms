output "blank" {
  value = {
    file_id      = proxmox_virtual_environment_file.blank_vm_static.id
    datastore_id = var.datastore_id
    node_name    = var.node_name
  }
}

output "dns" {
  value = {
    file_id      = proxmox_virtual_environment_file.dns_vm_static.id
    datastore_id = var.datastore_id
    node_name    = var.node_name
  }
}

output "flask" {
  value = {
    file_id      = proxmox_virtual_environment_file.flask_vm_static.id
    datastore_id = var.datastore_id
    node_name    = var.node_name
  }
}

output "nginx" {
  value = {
    file_id      = proxmox_virtual_environment_file.nginx_vm_static.id
    datastore_id = var.datastore_id
    node_name    = var.node_name
  }
}

output "django" {
  value = {
    file_id      = proxmox_virtual_environment_file.django_vm_static.id
    datastore_id = var.datastore_id
    node_name    = var.node_name
  }
}

output "postgresql" {
  value = {
    file_id      = proxmox_virtual_environment_file.postgresql_vm_static.id
    datastore_id = var.datastore_id
    node_name    = var.node_name
  }
}
output "apt_cache" {
  value = {
    file_id      = proxmox_virtual_environment_file.apt_cache_vm_static.id
    datastore_id = var.datastore_id
    node_name    = var.node_name
  }
}

output "devpi" {
  value = {
    file_id      = proxmox_virtual_environment_file.devpi_vm_static.id
    datastore_id = var.datastore_id
    node_name    = var.node_name
  }
}

output "docker_registry" {
  value = {
    file_id      = proxmox_virtual_environment_file.docker_registry_vm_static.id
    datastore_id = var.datastore_id
    node_name    = var.node_name
  }
}

output "manpages" {
  value = {
    file_id      = proxmox_virtual_environment_file.manpages_vm_static.id
    datastore_id = var.datastore_id
    node_name    = var.node_name
  }
}

output "ntp" {
  value = {
    file_id      = proxmox_virtual_environment_file.ntp_vm_static.id
    datastore_id = var.datastore_id
    node_name    = var.node_name
  }
}

output "cve_search" {
  value = {
    file_id      = proxmox_virtual_environment_file.cve_search_vm_static.id
    datastore_id = var.datastore_id
    node_name    = var.node_name
  }
}

output "wikipedia" {
  value = {
    file_id      = proxmox_virtual_environment_file.wikipedia_vm_static.id
    datastore_id = var.datastore_id
    node_name    = var.node_name
  }
}

output "wazuh" {
  value = {
    file_id      = proxmox_virtual_environment_file.wazuh_vm_static.id
    datastore_id = var.datastore_id
    node_name    = var.node_name
  }
}
