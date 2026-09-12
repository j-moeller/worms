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

output "postgresql" {
  value = {
    file_id      = proxmox_virtual_environment_file.postgresql_vm_static.id
    datastore_id = var.datastore_id
    node_name    = var.node_name
  }
}