# ProbeAtlas staging infrastructure: control plane on AWS, one measurement agent each on Azure, AWS, GCP,
# and OVHcloud. Runbook: docs/staging.md.
terraform {
  required_version = ">= 1.10"

  # State holds the database password and enrollment tokens: keep it in a private, encrypted bucket.
  # Configure with `terraform init -backend-config=backend.hcl` (see backend.hcl.example).
  backend "s3" {}

  required_providers {
    aws       = { source = "hashicorp/aws", version = "~> 6.0" }
    azurerm   = { source = "hashicorp/azurerm", version = "~> 5.0" }
    google    = { source = "hashicorp/google", version = "~> 8.0" }
    openstack = { source = "terraform-provider-openstack/openstack", version = "~> 3.0" }
    random    = { source = "hashicorp/random", version = "~> 3.6" }
  }
}

provider "aws" {
  region = var.aws_region
  default_tags { tags = local.tags }
}

# Backups go to another region than the database.
provider "aws" {
  alias  = "backup"
  region = var.aws_backup_region
  default_tags { tags = local.tags }
}

provider "aws" {
  alias  = "us_east"
  region = "us-east-1"
  default_tags { tags = local.tags }
}

provider "azurerm" {
  features {}
  subscription_id = var.azure_subscription_id
}

provider "google" {
  project = var.gcp_project
  region  = "asia-southeast1"
}

# OVHcloud Public Cloud is OpenStack: credentials come from the project's openrc file (OS_* variables).
provider "openstack" {
  region = var.ovh_region
}
