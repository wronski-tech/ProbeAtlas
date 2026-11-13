locals {
  tags = { project = "probeatlas", environment = var.environment, managed_by = "terraform" }

  # One agent per provider, keyed by ProbeAtlas region label. Location fields are shown to customers as
  # operator assertions: they follow each provider's documented region, never IP geolocation.
  agents = {
    "eu-west"      = { cloud = "azure", name = "azure-westeurope-1", country_code = "NL", city = "", network = "Microsoft Azure (AS8075)" }
    "us-east"      = { cloud = "aws", name = "aws-us-east-1-1", country_code = "US", city = "Northern Virginia", network = "AWS (AS16509)" }
    "ap-southeast" = { cloud = "gcp", name = "gcp-asia-southeast1-1", country_code = "SG", city = "Singapore", network = "Google Cloud (AS396982)" }
    "au-east"      = { cloud = "ovh", name = "ovh-syd1-1", country_code = "AU", city = "Sydney", network = "OVHcloud (AS16276)" }
  }

  # Agents exist once their enrollment token is set (phase 2, after the control plane is up). Region labels
  # are not secret, so the token map's keys may drive for_each.
  enabled_agents = { for region in nonsensitive(keys(var.agent_enrollment_tokens)) : region => local.agents[region] }
  aws_agents     = { for k, v in local.enabled_agents : k => v if v.cloud == "aws" }
  azure_agents   = { for k, v in local.enabled_agents : k => v if v.cloud == "azure" }
  gcp_agents     = { for k, v in local.enabled_agents : k => v if v.cloud == "gcp" }
  ovh_agents     = { for k, v in local.enabled_agents : k => v if v.cloud == "ovh" }

  agent_user_data = {
    for region, a in local.enabled_agents : region => templatefile("${path.module}/../../agent/cloud-init.yaml.tftpl", {
      api_url          = "https://${var.api_domain}"
      enrollment_token = var.agent_enrollment_tokens[region]
      region           = region
      name             = a.name
      country_code     = a.country_code
      city             = a.city
      network          = a.network
      nameservers      = "1.1.1.1,8.8.8.8"
      info_url         = "https://${var.app_domain}/probes"
      image_repo       = var.image_repo
      image_tag        = coalesce(var.agent_image_tag, var.image_tag)
      agent_run_b64    = filebase64("${path.module}/../../agent/probeatlas-agent-run")
    })
  }
}
