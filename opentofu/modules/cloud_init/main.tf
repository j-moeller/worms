# One cloud-init snippet per kind of VM, all from templates/cloud-config.tftpl.
#
# The file names are fixed: a VM's user_data_file_id is
# "<datastore>:snippets/<file>", and the provider rebuilds a VM whose
# user_data_file_id changes (the database VM included). Hostnames and file
# names are those of the former per-VM templates (tests/test_cloud_init.py).
locals {
  snippets = {
    blank           = { file = "vm-blank.yml", hostname = "vm" }
    postgresql      = { file = "vm-postgresq.yml", hostname = "postgresql" }
    django          = { file = "vm-django.yml", hostname = "django" }
    wazuh           = { file = "vm-wazuh.yml", hostname = "wazuh" }
    dns             = { file = "vm-dns.yml", hostname = "dns" }
    flask           = { file = "vm-flask.yml", hostname = "flask" }
    network_monitor = { file = "vm-network-monitor.yml", hostname = "network-monitor" }
    apt_cache       = { file = "vm-apt-cache.yml", hostname = "apt-cache" }
    devpi           = { file = "vm-devpi.yml", hostname = "devpi" }
    docker_registry = { file = "vm-docker-registry.yml", hostname = "docker-registry" }
    manpages        = { file = "vm-manpages.yml", hostname = "manpages" }
    ntp             = { file = "vm-ntp.yml", hostname = "ntp" }
    cve_search      = { file = "vm-cve-search.yml", hostname = "cve-search" }
    wikipedia       = { file = "vm-wikipedia.yml", hostname = "wikipedia" }
  }

  # Private-internet services with a data disk: where it is mounted.
  data_mounts = {
    apt_cache       = "/var/cache/apt-cacher-ng"
    devpi           = "/var/lib/devpi"
    docker_registry = "/var/lib/docker-registry"
    cve_search      = "/var/lib/mongodb"
    wikipedia       = "/var/lib/kiwix"
  }
}

resource "proxmox_virtual_environment_file" "snippet" {
  for_each = local.snippets

  content_type = "snippets"
  datastore_id = var.datastore_id
  node_name    = var.node_name

  source_raw {
    data = templatefile("${path.module}/templates/cloud-config.tftpl", {
      hostname       = each.value.hostname
      ssh_public_key = trimspace(file(var.ssh_public_key))
      data_mount     = lookup(local.data_mounts, each.key, null)
    })
    file_name = each.value.file
  }
}
