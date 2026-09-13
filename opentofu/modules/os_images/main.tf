resource "proxmox_virtual_environment_file" "debian" {
  content_type = "import"
  datastore_id = var.datastore_id
  node_name    = var.node_name

  source_file {
    path = "${path.module}/images/debian-13-generic-amd64-with-guest-agent.qcow2"
  }
}

resource "proxmox_virtual_environment_file" "ubuntu" {
  content_type = "import" # not "iso"
  datastore_id = var.datastore_id
  node_name    = var.node_name

  source_file {
    path = "${path.module}/images/resolute-server-cloudimg-amd64-with-guest-agent.qcow2"
  }
}
