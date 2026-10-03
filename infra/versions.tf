terraform {
  required_version = ">= 1.6"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}

# The profile is deliberately required rather than defaulted. This machine has
# credentials for an unrelated project, and a default would make it possible to
# provision this stack into the wrong account by forgetting a flag.
provider "aws" {
  region  = var.region
  profile = var.aws_profile

  default_tags {
    tags = {
      Project   = "ontime-sd"
      ManagedBy = "terraform"
    }
  }
}
