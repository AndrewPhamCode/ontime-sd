# A minimal public VPC rather than the account's default one, so the stack is
# self contained and reviewable. There is no NAT gateway: the instance sits in a
# public subnet with an Elastic IP, which is the right trade at this size. A NAT
# would cost more per month than everything else here combined.

data "aws_availability_zones" "available" {
  state = "available"
}

resource "aws_vpc" "main" {
  cidr_block           = "10.20.0.0/16"
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = { Name = "ontime-sd" }
}

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = "ontime-sd" }
}

resource "aws_subnet" "public" {
  vpc_id                  = aws_vpc.main.id
  cidr_block              = "10.20.1.0/24"
  availability_zone       = data.aws_availability_zones.available.names[0]
  map_public_ip_on_launch = true

  tags = { Name = "ontime-sd-public" }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }

  tags = { Name = "ontime-sd-public" }
}

resource "aws_route_table_association" "public" {
  subnet_id      = aws_subnet.public.id
  route_table_id = aws_route_table.public.id
}

resource "aws_security_group" "collector" {
  name        = "ontime-sd-collector"
  description = "Collector host: SSH in from the operator only, everything out"
  vpc_id      = aws_vpc.main.id

  tags = { Name = "ontime-sd-collector" }
}

# Postgres is deliberately absent from this list. It listens on localhost only,
# because the collector runs on the same box, so there is no reason to expose
# 5432 to anything. Reaching it from a laptop is done over an SSH tunnel.
resource "aws_vpc_security_group_ingress_rule" "ssh" {
  security_group_id = aws_security_group.collector.id
  description       = "SSH from the operator"
  cidr_ipv4         = var.ssh_cidr
  from_port         = 22
  to_port           = 22
  ip_protocol       = "tcp"
}

resource "aws_vpc_security_group_egress_rule" "all" {
  security_group_id = aws_security_group.collector.id
  description       = "MTS feeds, package mirrors, SSM, CloudWatch"
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
}
