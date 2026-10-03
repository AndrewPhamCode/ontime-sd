# Amazon Linux 2023 on ARM, resolved from the public SSM parameter so the AMI id
# is never pinned to something that goes stale or is region specific.
data "aws_ssm_parameter" "al2023" {
  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64"
}

resource "aws_key_pair" "admin" {
  key_name   = "ontime-sd-admin"
  public_key = var.ssh_public_key
}

# The database lives on its own volume, not the root volume, so the instance can
# be replaced, resized or rebuilt from a newer AMI without touching data that
# cannot be recollected.
resource "aws_ebs_volume" "data" {
  availability_zone = aws_subnet.public.availability_zone
  size              = var.data_volume_gb
  type              = "gp3"
  encrypted         = true

  tags = { Name = "ontime-sd-data" }

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_instance" "collector" {
  ami                    = data.aws_ssm_parameter.al2023.value
  instance_type          = var.instance_type
  subnet_id              = aws_subnet.public.id
  vpc_security_group_ids = [aws_security_group.collector.id]
  iam_instance_profile   = aws_iam_instance_profile.collector.name
  key_name               = aws_key_pair.admin.key_name

  # 30 GB rather than the default 8. The migration stages each table as CSV
  # inside the container before merging, and predictions alone is over 1 GB, so
  # the root volume needs headroom the data volume does not provide.
  root_block_device {
    volume_size = 30
    volume_type = "gp3"
    encrypted   = true
  }

  user_data = templatefile("${path.module}/user_data.sh", {
    api_key_parameter = var.api_key_parameter
    region            = var.region
    repo_url          = var.repo_url
    repo_ref          = var.repo_ref
  })

  # Replacing the instance when user_data changes is the point: the box is
  # disposable and the data volume is not.
  user_data_replace_on_change = true

  tags = { Name = "ontime-sd-collector" }
}

resource "aws_volume_attachment" "data" {
  device_name = "/dev/sdf"
  volume_id   = aws_ebs_volume.data.id
  instance_id = aws_instance.collector.id

  # Let Terraform detach on replacement rather than blocking; the unit that
  # writes to it is stopped by the shutdown the detach follows.
  stop_instance_before_detaching = true
}

# A stable address, so a rebuilt instance keeps the same SSH target and anything
# pointed at it does not have to be updated.
resource "aws_eip" "collector" {
  domain   = "vpc"
  instance = aws_instance.collector.id

  tags = { Name = "ontime-sd" }
}

# Daily snapshots of the data volume. This is the backup story for running
# Postgres on the box instead of RDS, and it is the honest answer to "what
# happens when the volume dies".
resource "aws_dlm_lifecycle_policy" "data_snapshots" {
  description        = "ontime-sd data volume daily snapshots"
  execution_role_arn = aws_iam_role.dlm.arn
  state              = "ENABLED"

  policy_details {
    resource_types = ["VOLUME"]
    target_tags    = { Name = "ontime-sd-data" }

    schedule {
      name = "daily"

      create_rule {
        interval      = 24
        interval_unit = "HOURS"
        times         = ["09:00"]
      }

      retain_rule {
        count = 7
      }

      copy_tags = true
    }
  }
}

resource "aws_iam_role" "dlm" {
  name = "ontime-sd-dlm"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { Service = "dlm.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy_attachment" "dlm" {
  role       = aws_iam_role.dlm.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSDataLifecycleManagerServiceRole"
}
