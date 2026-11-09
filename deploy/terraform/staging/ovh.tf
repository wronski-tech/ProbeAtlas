# au-east agent on OVHcloud Public Cloud (Sydney), on the public Ext-Net network.

resource "openstack_compute_keypair_v2" "operator" {
  count      = length(local.ovh_agents) > 0 ? 1 : 0
  name       = "probeatlas-${var.environment}-operator"
  public_key = var.ssh_public_key
}

resource "openstack_networking_secgroup_v2" "agent" {
  count       = length(local.ovh_agents) > 0 ? 1 : 0
  name        = "probeatlas-${var.environment}-agent"
  description = "ProbeAtlas agent: outbound only, SSH from operators"
}

resource "openstack_networking_secgroup_rule_v2" "agent_ssh" {
  for_each          = length(local.ovh_agents) > 0 ? toset(var.operator_cidrs) : toset([])
  security_group_id = openstack_networking_secgroup_v2.agent[0].id
  direction         = "ingress"
  ethertype         = "IPv4"
  protocol          = "tcp"
  port_range_min    = 22
  port_range_max    = 22
  remote_ip_prefix  = each.value
}

resource "openstack_compute_instance_v2" "agent" {
  for_each        = local.ovh_agents
  name            = each.value.name
  flavor_name     = var.ovh_agent_flavor
  image_name      = "Ubuntu 24.04"
  key_pair        = openstack_compute_keypair_v2.operator[0].name
  security_groups = [openstack_networking_secgroup_v2.agent[0].name]
  user_data       = local.agent_user_data[each.key]
  metadata        = { project = "probeatlas", environment = var.environment, region_label = each.key }

  network {
    name = "Ext-Net"
  }

  lifecycle {
    ignore_changes = [user_data, image_name]
  }
}
