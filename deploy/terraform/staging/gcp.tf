# ap-southeast agent on Google Cloud (asia-southeast1, Singapore), in the project's default network.

resource "google_compute_firewall" "agent_ssh" {
  count         = length(local.gcp_agents) > 0 && length(var.operator_cidrs) > 0 ? 1 : 0
  name          = "probeatlas-${var.environment}-agent-ssh"
  network       = "default"
  source_ranges = var.operator_cidrs
  target_tags   = ["probeatlas-agent"]
  allow {
    protocol = "tcp"
    ports    = ["22"]
  }
}

resource "google_compute_instance" "agent" {
  for_each     = local.gcp_agents
  name         = each.value.name
  machine_type = var.gcp_agent_machine_type
  zone         = "asia-southeast1-a"
  tags         = ["probeatlas-agent"]
  labels       = { project = "probeatlas", environment = var.environment, region_label = each.key }

  boot_disk {
    initialize_params {
      image = "ubuntu-os-cloud/ubuntu-2404-lts-amd64"
      size  = 20
      type  = "pd-standard"
    }
  }

  network_interface {
    network = "default"
    access_config {} # ephemeral public IP for outbound access
  }

  # No service account: the agent needs no Google Cloud API access.
  metadata = {
    user-data              = local.agent_user_data[each.key]
    ssh-keys               = "probeatlas:${var.ssh_public_key}"
    block-project-ssh-keys = "true"
  }

  shielded_instance_config {
    enable_secure_boot = true
  }

  lifecycle {
    ignore_changes = [metadata["user-data"]]
  }
}
