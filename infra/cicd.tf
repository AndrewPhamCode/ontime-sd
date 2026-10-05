# GitHub Actions authenticates by exchanging a short lived OIDC token for a
# role, so there are no AWS access keys in the repository's secrets. A leaked
# workflow log or a compromised secret store yields nothing reusable, and the
# trust policy below is what makes that safe: only this repository, and only
# from its main branch, can assume the role.

resource "aws_iam_openid_connect_provider" "github" {
  url             = "https://token.actions.githubusercontent.com"
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = ["6938fd4d98bab03faadb97b34396831e3780aea1"]
}

data "aws_iam_policy_document" "github_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.github.arn]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }

    # Without this the role could be assumed from any repository on GitHub.
    #
    # The prefix is var.github_sub_prefix rather than "repo:${var.github_repo}"
    # because this repository has GitHub's immutable subject claims enabled, so
    # the token's sub carries the numeric owner and repository ids:
    #
    #   repo:AndrewPhamCode@151807689/ontime-sd@1393841749:ref:refs/heads/main
    #
    # Matching on the human readable name never fired, so every deploy failed at
    # AssumeRoleWithWebIdentity with "Not authorized", while the provider, the
    # audience and this policy all looked correct in the console. See ADR-0048.
    #
    # Pinning the immutable form is stricter than the name, not looser: a
    # repository deleted and recreated under the same name gets a new id and
    # cannot assume this role.
    condition {
      test     = "StringLike"
      variable = "token.actions.githubusercontent.com:sub"
      values   = ["${var.github_sub_prefix}:ref:refs/heads/main"]
    }
  }
}

resource "aws_iam_role" "github_deploy" {
  name               = "ontime-sd-github-deploy"
  assume_role_policy = data.aws_iam_policy_document.github_assume.json
}

data "aws_iam_policy_document" "github_deploy" {
  statement {
    sid       = "PublishTheSite"
    effect    = "Allow"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.web.arn]
  }

  statement {
    effect    = "Allow"
    actions   = ["s3:PutObject", "s3:DeleteObject"]
    resources = ["${aws_s3_bucket.web.arn}/*"]
  }

  # Needed so the workflow can find its own targets instead of carrying them
  # as repository variables that drift when the stack is rebuilt.
  statement {
    sid    = "FindTheTargets"
    effect = "Allow"
    actions = [
      "s3:ListAllMyBuckets",
      "cloudfront:ListDistributions",
      "ec2:DescribeInstances",
    ]
    resources = ["*"]
  }

  statement {
    sid       = "ExpireTheEdgeCache"
    effect    = "Allow"
    actions   = ["cloudfront:CreateInvalidation", "cloudfront:GetInvalidation"]
    resources = [aws_cloudfront_distribution.web.arn]
  }

  # Deploying the API is a command sent to the one instance, not shell access to
  # the account. The document is pinned so this role cannot run an arbitrary SSM
  # document, and the instance is pinned so it cannot reach another host.
  statement {
    sid       = "DeployTheApi"
    effect    = "Allow"
    actions   = ["ssm:SendCommand"]
    resources = ["arn:aws:ssm:${var.region}::document/AWS-RunShellScript"]
  }

  statement {
    effect    = "Allow"
    actions   = ["ssm:SendCommand"]
    resources = [aws_instance.collector.arn]
  }

  statement {
    effect    = "Allow"
    actions   = ["ssm:GetCommandInvocation", "ssm:ListCommandInvocations"]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "github_deploy" {
  name   = "ontime-sd-github-deploy"
  role   = aws_iam_role.github_deploy.id
  policy = data.aws_iam_policy_document.github_deploy.json
}
