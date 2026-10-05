variable "aws_profile" {
  description = "Named AWS profile to deploy with. No default on purpose: see versions.tf."
  type        = string
}

variable "region" {
  description = "Region to run in. us-west-2 is the closest cheap region to San Diego."
  type        = string
  default     = "us-west-2"
}

variable "instance_type" {
  description = <<-EOT
    Graviton instance. t4g.small gives 2 vCPU and 2 GB, which comfortably holds
    Postgres plus the collector at this write volume. t4g.micro halves the RAM
    and the price, and is tight once prediction_errors is being rebuilt.
  EOT
  type        = string
  default     = "t4g.small"
}

variable "data_volume_gb" {
  description = <<-EOT
    EBS volume for the database. Irreplaceable capture runs about 0.55 GB per
    day at full coverage, so 100 GB is roughly four to five months of runway
    before a retention policy is needed.
  EOT
  type        = number
  default     = 100
}

variable "ssh_cidr" {
  description = <<-EOT
    CIDR allowed to reach SSH. Set this to your own address with a /32, not
    0.0.0.0/0: the instance holds the only copy of data that cannot be
    recollected. Find it with: curl -s https://checkip.amazonaws.com
  EOT
  type        = string
}

variable "ssh_public_key" {
  description = "Contents of the public key to install for the admin user."
  type        = string
}

variable "api_key_parameter" {
  description = <<-EOT
    Name of the SSM SecureString holding MTS_API_KEY. The parameter is created
    outside Terraform so the secret never enters the state file; see ADR-0041.
  EOT
  type        = string
  default     = "/ontime-sd/mts-api-key"
}

variable "alarm_email" {
  description = "Address to notify when collection goes stale. Empty disables the subscription."
  type        = string
  default     = ""
}

variable "staleness_alarm_seconds" {
  description = <<-EOT
    Seconds without a successful poll before the alarm fires. The feed publishes
    roughly every 30s and the collector polls every 30s, so 600 is about twenty
    missed polls: comfortably past a transient failure, well short of a lost hour.
  EOT
  type        = number
  default     = 600
}

variable "repo_url" {
  description = "Git remote the instance clones the collector from."
  type        = string
  default     = "https://github.com/AndrewPhamCode/ontime-sd.git"
}

variable "repo_ref" {
  description = "Branch or tag to deploy."
  type        = string
  default     = "main"
}

variable "monthly_budget_usd" {
  description = <<-EOT
    Spend threshold for the budget alarm. The stack costs roughly $25, so the
    default leaves room for a snapshot or two without crying wolf, while still
    catching anything genuinely unintended.
  EOT
  type        = string
  default     = "45"
}

variable "github_sub_prefix" {
  description = <<-DESC
    Prefix of the OIDC subject claim GitHub actually sends, which is NOT
    "repo:owner/name" when immutable subject claims are enabled on the
    repository. Read it from:

      gh api /repos/<owner>/<name>/actions/oidc/customization/sub

    and use the sub_claim_prefix it returns verbatim. The numeric ids are the
    owner id and the repository id.
  DESC
  type        = string
  default     = "repo:AndrewPhamCode@151807689/ontime-sd@1393841749"
}
