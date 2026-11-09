# us-east agent on AWS (N. Virginia), in the region's default VPC.

data "aws_vpc" "us_east" {
  count    = length(local.aws_agents) > 0 ? 1 : 0
  provider = aws.us_east
  default  = true
}

data "aws_subnets" "us_east" {
  count    = length(local.aws_agents) > 0 ? 1 : 0
  provider = aws.us_east
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.us_east[0].id]
  }
  filter {
    name   = "default-for-az"
    values = ["true"]
  }
}

data "aws_ami" "ubuntu_us_east" {
  count       = length(local.aws_agents) > 0 ? 1 : 0
  provider    = aws.us_east
  most_recent = true
  owners      = ["099720109477"] # Canonical
  filter {
    name   = "name"
    values = ["ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-*"]
  }
}

resource "aws_security_group" "agent_us_east" {
  count       = length(local.aws_agents) > 0 ? 1 : 0
  provider    = aws.us_east
  name_prefix = "probeatlas-${var.environment}-agent-"
  description = "ProbeAtlas agent: outbound only, SSH from operators"
  vpc_id      = data.aws_vpc.us_east[0].id

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
}

resource "aws_key_pair" "operator_us_east" {
  count           = length(local.aws_agents) > 0 ? 1 : 0
  provider        = aws.us_east
  key_name_prefix = "probeatlas-${var.environment}-"
  public_key      = var.ssh_public_key
}

resource "aws_instance" "agent" {
  for_each                    = local.aws_agents
  provider                    = aws.us_east
  ami                         = data.aws_ami.ubuntu_us_east[0].id
  instance_type               = var.aws_agent_instance_type
  subnet_id                   = sort(data.aws_subnets.us_east[0].ids)[0]
  vpc_security_group_ids      = [aws_security_group.agent_us_east[0].id]
  key_name                    = aws_key_pair.operator_us_east[0].key_name
  associate_public_ip_address = true
  user_data                   = local.agent_user_data[each.key]

  # The agent needs no AWS API access: no instance role, and containers cannot reach the metadata service.
  metadata_options {
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }

  root_block_device {
    volume_size = 20
    volume_type = "gp3"
    encrypted   = true
  }

  tags = { Name = "probeatlas-${var.environment}-${each.value.name}", region_label = each.key }

  # Enrollment happens once at first boot; later token or image changes must not recreate the agent.
  lifecycle {
    ignore_changes = [ami, user_data]
  }
}
