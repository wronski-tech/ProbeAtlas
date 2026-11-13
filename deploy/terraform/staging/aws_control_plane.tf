# Control plane on AWS: one EC2 host (Caddy, web, API, worker via Docker Compose), RDS PostgreSQL 16 in
# subnets without internet routes, the database URL in SSM, and dumps in S3 in another region.

data "aws_availability_zones" "available" {
  state = "available"
}

data "aws_ami" "ubuntu" {
  most_recent = true
  owners      = ["099720109477"] # Canonical
  filter {
    name   = "name"
    values = ["ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-*"]
  }
}

resource "aws_vpc" "main" {
  cidr_block           = "10.40.0.0/16"
  enable_dns_hostnames = true
  tags                 = { Name = "probeatlas-${var.environment}" }
}

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id
}

resource "aws_subnet" "public" {
  vpc_id            = aws_vpc.main.id
  cidr_block        = "10.40.0.0/24"
  availability_zone = data.aws_availability_zones.available.names[0]
  tags              = { Name = "probeatlas-${var.environment}-public" }
}

# RDS needs subnets in two zones. They have no route to the internet.
resource "aws_subnet" "db" {
  count             = 2
  vpc_id            = aws_vpc.main.id
  cidr_block        = "10.40.${10 + count.index}.0/24"
  availability_zone = data.aws_availability_zones.available.names[count.index]
  tags              = { Name = "probeatlas-${var.environment}-db-${count.index}" }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }
}

resource "aws_route_table_association" "public" {
  subnet_id      = aws_subnet.public.id
  route_table_id = aws_route_table.public.id
}

resource "aws_security_group" "control_plane" {
  name_prefix = "probeatlas-${var.environment}-cp-"
  description = "ProbeAtlas control plane: HTTPS in (Caddy), SSH from operators only"
  vpc_id      = aws_vpc.main.id

  ingress {
    description = "HTTP (ACME challenges and redirect to HTTPS)"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
  ingress {
    description = "HTTPS"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
  ingress {
    description = "HTTP/3"
    from_port   = 443
    to_port     = 443
    protocol    = "udp"
    cidr_blocks = ["0.0.0.0/0"]
  }
  dynamic "ingress" {
    for_each = length(var.operator_cidrs) > 0 ? [1] : []
    content {
      description = "SSH from operators"
      from_port   = 22
      to_port     = 22
      protocol    = "tcp"
      cidr_blocks = var.operator_cidrs
    }
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_security_group" "db" {
  name_prefix = "probeatlas-${var.environment}-db-"
  description = "ProbeAtlas PostgreSQL: only from the control plane"
  vpc_id      = aws_vpc.main.id

  ingress {
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [aws_security_group.control_plane.id]
  }
  lifecycle {
    create_before_destroy = true
  }
}

resource "random_password" "db" {
  length  = 40
  special = false
}

resource "aws_db_subnet_group" "main" {
  name_prefix = "probeatlas-${var.environment}-"
  subnet_ids  = aws_subnet.db[*].id
}

resource "aws_db_instance" "main" {
  identifier_prefix       = "probeatlas-${var.environment}-"
  engine                  = "postgres"
  engine_version          = "16"
  instance_class          = var.db_instance_class
  allocated_storage       = 20
  max_allocated_storage   = 100
  storage_type            = "gp3"
  storage_encrypted       = true
  db_name                 = "probeatlas"
  username                = "probeatlas"
  password                = random_password.db.result
  db_subnet_group_name    = aws_db_subnet_group.main.name
  vpc_security_group_ids  = [aws_security_group.db.id]
  publicly_accessible     = false
  multi_az                = false
  backup_retention_period = 7 # point-in-time recovery window
  backup_window           = "02:00-02:30"
  maintenance_window      = "sun:04:00-sun:05:00"
  # PostgreSQL 15+ on RDS requires TLS by default (rds.force_ssl=1); the URL also sets sslmode=require.
  auto_minor_version_upgrade = true
  copy_tags_to_snapshot      = true
  deletion_protection        = var.db_deletion_protection
  skip_final_snapshot        = false
  final_snapshot_identifier  = "probeatlas-${var.environment}-final"
}

resource "aws_ssm_parameter" "database_url" {
  name  = "/probeatlas/${var.environment}/database-url"
  type  = "SecureString"
  value = "postgresql+psycopg://probeatlas:${random_password.db.result}@${aws_db_instance.main.endpoint}/probeatlas?sslmode=require"
}

# --- backups ---

resource "aws_s3_bucket" "backups" {
  provider      = aws.backup
  bucket_prefix = "probeatlas-${var.environment}-backups-"
}

resource "aws_s3_bucket_public_access_block" "backups" {
  provider                = aws.backup
  bucket                  = aws_s3_bucket.backups.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "backups" {
  provider = aws.backup
  bucket   = aws_s3_bucket.backups.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "backups" {
  provider = aws.backup
  bucket   = aws_s3_bucket.backups.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "backups" {
  provider = aws.backup
  bucket   = aws_s3_bucket.backups.id
  rule {
    id     = "expire-daily-dumps"
    status = "Enabled"
    filter {
      prefix = "daily/"
    }
    expiration {
      days = 35
    }
    noncurrent_version_expiration {
      noncurrent_days = 7
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }
}

# --- host ---

data "aws_iam_policy_document" "ec2_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "control_plane" {
  name_prefix        = "probeatlas-${var.environment}-cp-"
  assume_role_policy = data.aws_iam_policy_document.ec2_assume.json
}

# Least privilege: read one parameter, write dumps. Nothing else.
data "aws_iam_policy_document" "control_plane" {
  statement {
    actions   = ["ssm:GetParameter"]
    resources = [aws_ssm_parameter.database_url.arn]
  }
  statement {
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.backups.arn}/daily/*"]
  }
}

resource "aws_iam_role_policy" "control_plane" {
  role   = aws_iam_role.control_plane.id
  policy = data.aws_iam_policy_document.control_plane.json
}

# Shell access through AWS Systems Manager Session Manager, so SSH can stay closed.
resource "aws_iam_role_policy_attachment" "control_plane_ssm" {
  role       = aws_iam_role.control_plane.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_instance_profile" "control_plane" {
  name_prefix = "probeatlas-${var.environment}-cp-"
  role        = aws_iam_role.control_plane.name
}

resource "aws_key_pair" "operator" {
  key_name_prefix = "probeatlas-${var.environment}-"
  public_key      = var.ssh_public_key
}

resource "aws_instance" "control_plane" {
  ami                    = data.aws_ami.ubuntu.id
  instance_type          = var.control_plane_instance_type
  subnet_id              = aws_subnet.public.id
  vpc_security_group_ids = [aws_security_group.control_plane.id]
  iam_instance_profile   = aws_iam_instance_profile.control_plane.name
  key_name               = aws_key_pair.operator.key_name
  # The first boot needs the internet (packages, images) before the Elastic IP is attached.
  associate_public_ip_address = true

  user_data = templatefile("${path.module}/../../staging/cloud-init.yaml.tftpl", {
    aws_region         = var.aws_region
    ssm_database_url   = aws_ssm_parameter.database_url.name
    backup_bucket      = aws_s3_bucket.backups.id
    backup_region      = var.aws_backup_region
    github_repo        = var.github_repo
    app_domain         = var.app_domain
    api_domain         = var.api_domain
    acme_email         = var.acme_email
    contact_email      = coalesce(var.contact_email, var.acme_email)
    image_repo         = var.image_repo
    image_tag          = var.image_tag
    deploy_b64         = filebase64("${path.module}/../../staging/host/probeatlas-deploy")
    render_env_b64     = filebase64("${path.module}/../../staging/host/probeatlas-render-env")
    backup_b64         = filebase64("${path.module}/../../staging/host/probeatlas-backup")
    backup_service_b64 = filebase64("${path.module}/../../staging/host/probeatlas-backup.service")
    backup_timer_b64   = filebase64("${path.module}/../../staging/host/probeatlas-backup.timer")
  })

  # Hop limit 1: containers (one network hop further) cannot reach the instance metadata service and
  # borrow the host's role.
  metadata_options {
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }

  root_block_device {
    volume_size = 30
    volume_type = "gp3"
    encrypted   = true
  }

  tags = { Name = "probeatlas-${var.environment}-control-plane" }

  # The first boot deploys image_tag; later deploys use probeatlas-deploy on the host, not Terraform.
  lifecycle {
    ignore_changes = [ami, user_data]
  }

  depends_on = [aws_db_instance.main, aws_iam_role_policy.control_plane, aws_route_table_association.public]
}

resource "aws_eip" "control_plane" {
  domain   = "vpc"
  instance = aws_instance.control_plane.id
  tags     = { Name = "probeatlas-${var.environment}-control-plane" }
}
