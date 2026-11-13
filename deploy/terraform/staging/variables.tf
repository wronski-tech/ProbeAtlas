variable "environment" {
  description = "Name used in resource names and tags."
  type        = string
  default     = "staging"
}

variable "app_domain" {
  description = "Dashboard host name, e.g. app.staging.example.com (DNS A record -> control_plane_ip)."
  type        = string
}

variable "api_domain" {
  description = "API and agent protocol host name, e.g. api.staging.example.com (DNS A record -> control_plane_ip)."
  type        = string
}

variable "acme_email" {
  description = "Contact address for Let's Encrypt certificates."
  type        = string
}

variable "contact_email" {
  description = "Public contact for access requests and abuse reports, shown on the landing and /probes pages (defaults to acme_email)."
  type        = string
  default     = null
}

variable "image_tag" {
  description = "Full commit SHA whose images CI published to GHCR; deployed at first boot."
  type        = string
  validation {
    condition     = can(regex("^[0-9a-f]{40}$", var.image_tag))
    error_message = "image_tag must be a full 40-character commit SHA."
  }
}

variable "image_repo" {
  description = "Registry path of the probeatlas-* images."
  type        = string
  default     = "ghcr.io/wronski-tech"
}

variable "github_repo" {
  description = "GitHub repository the control plane fetches deploy files from (owner/name)."
  type        = string
  default     = "wronski-tech/ProbeAtlas"
}

variable "ssh_public_key" {
  description = "Operator SSH public key installed on every host."
  type        = string
}

variable "operator_cidrs" {
  description = "Source ranges allowed to SSH to the hosts. Empty: no inbound SSH (use AWS Session Manager or the provider console)."
  type        = list(string)
  default     = []
}

variable "agent_enrollment_tokens" {
  description = "Single-use, region-bound enrollment tokens keyed by region label (eu-west, us-east, ap-southeast, au-east). An agent is created only once its token is set."
  type        = map(string)
  default     = {}
  sensitive   = true
  validation {
    condition     = alltrue([for k in keys(var.agent_enrollment_tokens) : contains(["eu-west", "us-east", "ap-southeast", "au-east"], k)])
    error_message = "Keys must be region labels defined in agents.tf: eu-west, us-east, ap-southeast, au-east."
  }
}

variable "agent_image_tag" {
  description = "Commit SHA for the agents' image at first boot (defaults to image_tag)."
  type        = string
  default     = null
}

# --- AWS: control plane and the us-east agent ---

variable "aws_region" {
  description = "Control-plane region."
  type        = string
  default     = "eu-central-1"
}

variable "aws_backup_region" {
  description = "Region of the S3 bucket for database dumps (different from aws_region)."
  type        = string
  default     = "eu-west-1"
}

variable "control_plane_instance_type" {
  description = "EC2 instance type of the control-plane host (x86: the images are amd64)."
  type        = string
  default     = "t3.small"
}

variable "db_instance_class" {
  description = "RDS instance class for PostgreSQL 16."
  type        = string
  default     = "db.t4g.micro"
}

variable "db_deletion_protection" {
  description = "Protect the database from deletion (set false before `terraform destroy`)."
  type        = bool
  default     = true
}

variable "aws_agent_instance_type" {
  description = "EC2 instance type of the us-east agent."
  type        = string
  default     = "t3.micro"
}

# --- Azure: eu-west agent ---

variable "azure_subscription_id" {
  description = "Azure subscription for the eu-west agent."
  type        = string
}

variable "azure_agent_size" {
  description = "Azure VM size of the eu-west agent."
  type        = string
  default     = "Standard_B2ats_v2"
}

# --- Google Cloud: ap-southeast agent ---

variable "gcp_project" {
  description = "Google Cloud project for the ap-southeast agent."
  type        = string
}

variable "gcp_agent_machine_type" {
  description = "Machine type of the ap-southeast agent."
  type        = string
  default     = "e2-small"
}

# --- OVHcloud: au-east agent ---

variable "ovh_region" {
  description = "OVHcloud Public Cloud (OpenStack) region of the au-east agent."
  type        = string
  default     = "SYD1"
}

variable "ovh_agent_flavor" {
  description = "OVHcloud instance flavor of the au-east agent."
  type        = string
  default     = "d2-2"
}
