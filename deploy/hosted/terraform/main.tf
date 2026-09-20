terraform {
  required_version = ">= 1.9, < 2.0"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 6.0" }

  }
  # Use an encrypted, access-controlled remote backend before applying. See README.
}
provider "aws" {
  region = var.region
  default_tags {
    tags = { Project = var.name, ManagedBy = "Terraform" }
  }
}
variable "region" {
  type    = string
  default = "us-east-1"
}
variable "name" {
  type    = string
  default = "private-books"
}
variable "instance_type" {
  type    = string
  default = "t3.large"
}
variable "database_class" {
  type    = string
  default = "db.t4g.small"
}
variable "database_backup_retention_days" {
  description = "RDS retention; AWS Free plans may require one day."
  type        = number
  default     = 7
}
variable "k3s_version" {
  type    = string
  default = "v1.35.8+k3s1"
}
variable "monthly_budget_usd" {
  type    = number
  default = 100
}
variable "create_budget" {
  description = "Create the deployment budget; disable when an account budget already covers this stack."
  type        = bool
  default     = true
}
variable "budget_email" {
  type = string
}
data "aws_availability_zones" "available" {
  state = "available"
}
data "aws_ssm_parameter" "ami" {
  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"
}
resource "aws_vpc" "book" {
  cidr_block           = "10.72.0.0/16"
  enable_dns_hostnames = true
  enable_dns_support   = true
}
resource "aws_subnet" "node" {
  vpc_id            = aws_vpc.book.id
  cidr_block        = "10.72.1.0/24"
  availability_zone = data.aws_availability_zones.available.names[0]
}
resource "aws_subnet" "database" {
  count             = 2
  vpc_id            = aws_vpc.book.id
  cidr_block        = "10.72.${count.index + 10}.0/24"
  availability_zone = data.aws_availability_zones.available.names[count.index]
}
resource "aws_internet_gateway" "book" {
  vpc_id = aws_vpc.book.id
}
resource "aws_route_table" "public" {
  vpc_id = aws_vpc.book.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.book.id
  }
}
resource "aws_route_table_association" "node" {
  subnet_id      = aws_subnet.node.id
  route_table_id = aws_route_table.public.id
}
resource "aws_security_group" "node" {
  name_prefix = "${var.name}-node-"
  vpc_id      = aws_vpc.book.id
  ingress {
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
  ingress {
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}
resource "aws_security_group" "database" {
  name_prefix = "${var.name}-database-"
  vpc_id      = aws_vpc.book.id
  ingress {
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [aws_security_group.node.id]
  }
}
resource "aws_db_subnet_group" "book" {
  subnet_ids = aws_subnet.database[*].id
}
resource "aws_db_parameter_group" "book" {
  family = "postgres16"
  parameter {
    name  = "rds.force_ssl"
    value = "1"
  }
}
resource "aws_db_instance" "book" {
  identifier                  = var.name
  engine                      = "postgres"
  engine_version              = "16"
  instance_class              = var.database_class
  db_name                     = "novel_engine"
  username                    = "book_operator"
  manage_master_user_password = true
  allocated_storage           = 20
  max_allocated_storage       = 100
  storage_type                = "gp3"
  storage_encrypted           = true
  multi_az                    = false
  publicly_accessible         = false
  db_subnet_group_name        = aws_db_subnet_group.book.name
  vpc_security_group_ids      = [aws_security_group.database.id]
  parameter_group_name        = aws_db_parameter_group.book.name
  backup_retention_period     = var.database_backup_retention_days
  backup_window               = "09:00-10:00"
  maintenance_window          = "sun:10:00-sun:11:00"
  deletion_protection         = true
  skip_final_snapshot         = false
  final_snapshot_identifier   = "${var.name}-final"
  auto_minor_version_upgrade  = true
}
resource "aws_s3_bucket" "data" {
  bucket_prefix = "${var.name}-data-"
  force_destroy = false
}
resource "aws_s3_bucket" "backup" {
  bucket_prefix = "${var.name}-backup-"
  force_destroy = false
}
locals {
  buckets = { data = aws_s3_bucket.data.id, backup = aws_s3_bucket.backup.id }
}
resource "aws_s3_bucket_public_access_block" "private" {
  for_each                = local.buckets
  bucket                  = each.value
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}
resource "aws_s3_bucket_server_side_encryption_configuration" "encrypted" {
  for_each = local.buckets
  bucket   = each.value
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}
resource "aws_s3_bucket_versioning" "versioned" {
  for_each = local.buckets
  bucket   = each.value
  versioning_configuration {
    status = "Enabled"
  }
}
resource "aws_s3_bucket_policy" "tls" {
  for_each = local.buckets
  bucket   = each.value
  policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Sid       = "RequireTLS", Effect = "Deny", Principal = "*", Action = "s3:*",
    Resource  = ["arn:aws:s3:::${each.value}", "arn:aws:s3:::${each.value}/*"],
    Condition = { Bool = { "aws:SecureTransport" = "false" } }
  }] })
}
resource "aws_s3_bucket_lifecycle_configuration" "backup" {
  bucket = aws_s3_bucket.backup.id
  rule {
    id     = "portable-backup-retention"
    status = "Enabled"
    filter {
      prefix = "snapshots/"
    }
    expiration {
      days = 30
    }
    noncurrent_version_expiration {
      noncurrent_days = 30
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }

  }
}
resource "aws_s3_bucket_lifecycle_configuration" "data" {
  bucket = aws_s3_bucket.data.id
  rule {
    id     = "retire-old-versions"
    status = "Enabled"
    filter {
      prefix = "novels/"
    }
    noncurrent_version_expiration {
      noncurrent_days = 7
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }

  }
}
resource "aws_ecr_repository" "services" {
  for_each             = toset(["reader-api", "ingest-api", "scraper", "python", "web", "textproc"])
  name                 = "${var.name}/${each.value}"
  image_tag_mutability = "IMMUTABLE"
  image_scanning_configuration {
    scan_on_push = true
  }
}
resource "aws_secretsmanager_secret" "services" {
  for_each                = toset(["reader", "auth", "ingest", "pipeline", "scraper", "askai", "cleanup", "backup", "redis"])
  name                    = "${var.name}/${each.value}"
  recovery_window_in_days = 7
}
resource "aws_iam_role" "node" {
  name_prefix        = "${var.name}-node-"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{ Effect = "Allow", Principal = { Service = "ec2.amazonaws.com" }, Action = "sts:AssumeRole" }] })
}
resource "aws_iam_role_policy_attachment" "ssm" {
  role       = aws_iam_role.node.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}
resource "aws_iam_role_policy_attachment" "registry" {
  role       = aws_iam_role.node.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly"
}
resource "aws_iam_role_policy" "data" {
  role = aws_iam_role.node.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["s3:ListBucket", "s3:ListBucketVersions", "s3:GetBucketLocation"], Resource = [aws_s3_bucket.data.arn, aws_s3_bucket.backup.arn] },
    { Effect = "Allow", Action = ["s3:GetObject", "s3:GetObjectVersion", "s3:PutObject", "s3:DeleteObject", "s3:DeleteObjectVersion", "s3:AbortMultipartUpload"], Resource = ["${aws_s3_bucket.data.arn}/*", "${aws_s3_bucket.backup.arn}/*"] },
    { Effect = "Allow", Action = ["secretsmanager:GetSecretValue"], Resource = [for s in aws_secretsmanager_secret.services : s.arn] }
  ] })
}
resource "aws_iam_instance_profile" "node" {
  role = aws_iam_role.node.name
}
resource "aws_ebs_volume" "redis" {
  availability_zone = data.aws_availability_zones.available.names[0]
  size              = 8
  type              = "gp3"
  encrypted         = true
  lifecycle { prevent_destroy = true }
}
resource "aws_volume_attachment" "redis" {
  device_name = "/dev/sdf"
  volume_id   = aws_ebs_volume.redis.id
  instance_id = aws_instance.node.id
}
resource "aws_instance" "node" {
  ami                         = data.aws_ssm_parameter.ami.value
  instance_type               = var.instance_type
  subnet_id                   = aws_subnet.node.id
  associate_public_ip_address = true
  vpc_security_group_ids      = [aws_security_group.node.id]
  iam_instance_profile        = aws_iam_instance_profile.node.name
  metadata_options {
    http_tokens                 = "required"
    http_put_response_hop_limit = 2
  }
  root_block_device {
    volume_size = 40
    volume_type = "gp3"
    encrypted   = true
  }
  user_data = templatefile("${path.module}/node.sh.tftpl", { k3s_version = var.k3s_version, redis_volume = replace(aws_ebs_volume.redis.id, "-", "") })
  lifecycle {
    ignore_changes = [ami]
  }
}
resource "aws_eip" "node" {
  domain   = "vpc"
  instance = aws_instance.node.id
}
resource "aws_budgets_budget" "monthly" {
  count        = var.create_budget ? 1 : 0
  name         = var.name
  budget_type  = "COST"
  limit_amount = tostring(var.monthly_budget_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"
  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 80
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = [var.budget_email]
  }
}
moved {
  from = aws_budgets_budget.monthly
  to   = aws_budgets_budget.monthly[0]
}
resource "aws_cloudwatch_metric_alarm" "node_health" {
  alarm_name          = "${var.name}-node-health"
  namespace           = "AWS/EC2"
  metric_name         = "StatusCheckFailed"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  period              = 60
  statistic           = "Maximum"
  threshold           = 0
  dimensions          = { InstanceId = aws_instance.node.id }
}
output "public_ip" {
  value = aws_eip.node.public_ip
}
output "instance_id" {
  value = aws_instance.node.id
}
output "database_host" {
  value = aws_db_instance.book.address
}
output "operator_secret_arn" {
  value     = aws_db_instance.book.master_user_secret[0].secret_arn
  sensitive = true
}
output "data_bucket" {
  value = aws_s3_bucket.data.id
}
output "backup_bucket" {
  value = aws_s3_bucket.backup.id
}
output "registries" {
  value = { for k, v in aws_ecr_repository.services : k => v.repository_url }
}
