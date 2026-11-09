output "control_plane_ip" {
  description = "Point the A records of app_domain and api_domain here."
  value       = aws_eip.control_plane.public_ip
}

output "control_plane_instance_id" {
  description = "For `aws ssm start-session --target <id>` (shell without SSH)."
  value       = aws_instance.control_plane.id
}

output "database_endpoint" {
  description = "RDS endpoint (reachable only from the control-plane host)."
  value       = aws_db_instance.main.endpoint
}

output "database_url_parameter" {
  description = "SSM SecureString holding PROBEATLAS_DATABASE_URL."
  value       = aws_ssm_parameter.database_url.name
}

output "backup_bucket" {
  description = "S3 bucket with daily database dumps (daily/ prefix)."
  value       = aws_s3_bucket.backups.id
}

output "agent_public_ips" {
  description = "Public IPv4 of each agent, by region label."
  value = merge(
    { for k, v in aws_instance.agent : k => v.public_ip },
    { for k, v in azurerm_public_ip.agent : k => v.ip_address },
    { for k, v in google_compute_instance.agent : k => v.network_interface[0].access_config[0].nat_ip },
    { for k, v in openstack_compute_instance_v2.agent : k => v.access_ip_v4 },
  )
}
