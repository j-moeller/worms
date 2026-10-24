# Per snippet (key as in main.tf): the file id VMs use as user_data_file_id.
output "snippets" {
  value = {
    for key, file in proxmox_virtual_environment_file.snippet : key => {
      file_id      = file.id
      datastore_id = var.datastore_id
      node_name    = var.node_name
    }
  }
}
