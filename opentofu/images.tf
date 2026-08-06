/*
resource "proxmox_download_file" "ubuntu_image" {
  content_type = "iso"
  datastore_id = var.datastore_id
  node_name    = var.proxmox_instance
  url          = "https://cloud-images.ubuntu.com/resolute/current/resolute-server-cloudimg-amd64.img"
  file_name    = "resolute-server-cloudimg-amd64.img"
}

resource "proxmox_download_file" "debian_image" {
  content_type = "import"
  datastore_id = var.datastore_id
  node_name    = var.proxmox_instance
  url          = "https://cloud.debian.org/images/cloud/trixie/latest/debian-13-generic-amd64.raw"
  file_name    = "debian-13-genericcloud-amd64.raw"
}
*/

resource "proxmox_virtual_environment_file" "debian_image" {
  content_type = "import"
  datastore_id = var.datastore_id
  node_name    = var.proxmox_instance

  source_file {
    path = "${path.module}/images/debian-13-generic-amd64-with-guest-agent.qcow2"
  }
}

resource "proxmox_virtual_environment_file" "ubuntu_image" {
  content_type = "iso"
  datastore_id = var.datastore_id
  node_name    = var.proxmox_instance

  source_file {
    path = "${path.module}/images/resolute-server-cloudimg-amd64-with-guest-agent.img"
  }
}

/*

// Stupid alpine doesn't work.

resource "proxmox_virtual_environment_file" "alpine_image" {
  content_type = "import"
  datastore_id = var.datastore_id
  node_name    = var.proxmox_instance

  source_file {
    path = "${path.module}/images/generic_alpine-3.24.1-x86_64-bios-cloudinit-r0.qcow2"
  }
}
*/
