output "debian" {
  value = {
    file_id      = proxmox_virtual_environment_file.debian.id
    content_type = proxmox_virtual_environment_file.debian.content_type
    datastore_id = var.datastore_id
    node_name    = var.node_name
  }
}

output "ubuntu" {
  value = {
    file_id      = proxmox_virtual_environment_file.ubuntu.id
    content_type = proxmox_virtual_environment_file.ubuntu.content_type
    datastore_id = var.datastore_id
    node_name    = var.node_name
  }
}