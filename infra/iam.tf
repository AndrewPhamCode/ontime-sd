# The instance reads its own secret and publishes its own metric. Nothing here
# is broader than that: no wildcard resources beyond the metric namespace, which
# CloudWatch does not scope by resource.

data "aws_caller_identity" "current" {}

resource "aws_iam_role" "collector" {
  name = "ontime-sd-collector"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { Service = "ec2.amazonaws.com" }
    }]
  })
}

data "aws_iam_policy_document" "collector" {
  statement {
    sid    = "ReadTheApiKey"
    effect = "Allow"
    actions = [
      "ssm:GetParameter",
      "ssm:GetParameters",
    ]
    resources = [
      "arn:aws:ssm:${var.region}:${data.aws_caller_identity.current.account_id}:parameter${var.api_key_parameter}",
    ]
  }

  statement {
    sid       = "DecryptTheApiKey"
    effect    = "Allow"
    actions   = ["kms:Decrypt"]
    resources = ["arn:aws:kms:${var.region}:${data.aws_caller_identity.current.account_id}:alias/aws/ssm"]
  }

  statement {
    sid       = "PublishStalenessMetric"
    effect    = "Allow"
    actions   = ["cloudwatch:PutMetricData"]
    resources = ["*"]

    condition {
      test     = "StringEquals"
      variable = "cloudwatch:namespace"
      values   = ["OnTimeSD"]
    }
  }
}

resource "aws_iam_role_policy" "collector" {
  name   = "ontime-sd-collector"
  role   = aws_iam_role.collector.id
  policy = data.aws_iam_policy_document.collector.json
}

# Session Manager, so the instance can be reached without SSH if the key is lost
# or the operator's address changes. This is the managed policy AWS publishes for
# exactly this purpose.
resource "aws_iam_role_policy_attachment" "ssm_core" {
  role       = aws_iam_role.collector.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_instance_profile" "collector" {
  name = "ontime-sd-collector"
  role = aws_iam_role.collector.name
}
