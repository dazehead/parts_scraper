data "aws_caller_identity" "current" {}

# ---------- Worker instance role ----------
data "aws_iam_policy_document" "ec2_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "worker" {
  name               = "${local.prefix}-parts-worker-role"
  assume_role_policy = data.aws_iam_policy_document.ec2_trust.json
}

# Minimum CloudWatch Agent permissions for log shipping.
resource "aws_iam_role_policy_attachment" "worker_cloudwatch" {
  role       = aws_iam_role.worker.name
  policy_arn = "arn:aws:iam::aws:policy/CloudWatchAgentServerPolicy"
}

# So workers can `docker pull` from the customer's own ECR.
resource "aws_iam_role_policy_attachment" "worker_ecr_readonly" {
  role       = aws_iam_role.worker.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly"
}

# Useful for `aws ssm start-session` debugging on the workers, without
# opening SSH ingress.
resource "aws_iam_role_policy_attachment" "worker_ssm" {
  role       = aws_iam_role.worker.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

data "aws_iam_policy_document" "worker_inline" {
  # SQS: per worker, narrow to its own queue.
  statement {
    sid = "SqsReceive"
    actions = [
      "sqs:ReceiveMessage",
      "sqs:DeleteMessage",
      "sqs:ChangeMessageVisibility",
      "sqs:GetQueueAttributes",
      "sqs:GetQueueUrl",
    ]
    resources = [
      aws_sqs_queue.scraper.arn,
      aws_sqs_queue.proc.arn,
    ]
  }

  # S3: read/write under the pipeline bucket only.
  statement {
    sid = "S3Object"
    actions = [
      "s3:GetObject",
      "s3:PutObject",
      "s3:DeleteObject",
    ]
    resources = ["${aws_s3_bucket.pipeline.arn}/*"]
  }

  statement {
    sid       = "S3List"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.pipeline.arn]
  }

  # Secrets: read-only, only the secrets this stack creates.
  statement {
    sid     = "ReadDeploymentSecrets"
    actions = ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"]
    resources = concat(
      [
        aws_secretsmanager_secret.html_secret.arn,
        aws_secretsmanager_secret.db_password.arn,
        aws_secretsmanager_secret.openai_api_key.arn,
        aws_secretsmanager_secret.decodo_credentials.arn,
      ],
      [for s in aws_secretsmanager_secret.tenant_html_secret : s.arn],
    )
  }
}

resource "aws_iam_role_policy" "worker_inline" {
  name   = "${local.prefix}-parts-worker-inline"
  role   = aws_iam_role.worker.id
  policy = data.aws_iam_policy_document.worker_inline.json
}

resource "aws_iam_instance_profile" "worker" {
  name = "${local.prefix}-parts-worker-profile"
  role = aws_iam_role.worker.name
}

# ---------- Operator identity (for the desktop GUI) ----------
# A managed policy meant to be attached to whichever IAM user/role the
# operator's workstation assumes. Not attached here; the customer
# attaches it to the human they choose.
data "aws_iam_policy_document" "operator" {
  statement {
    sid = "SqsSend"
    actions = [
      "sqs:SendMessage",
      "sqs:GetQueueAttributes",
      "sqs:GetQueueUrl",
    ]
    resources = [
      aws_sqs_queue.scraper.arn,
      aws_sqs_queue.proc.arn,
    ]
  }

  statement {
    sid = "S3FullForPipelineBucket"
    actions = [
      "s3:GetObject",
      "s3:PutObject",
      "s3:DeleteObject",
      "s3:ListBucket",
    ]
    resources = [
      aws_s3_bucket.pipeline.arn,
      "${aws_s3_bucket.pipeline.arn}/*",
    ]
  }

  statement {
    sid = "DescribeWorkers"
    actions = [
      "ec2:DescribeInstances",
      "autoscaling:DescribeAutoScalingGroups",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_policy" "operator" {
  name        = "${local.prefix}-parts-operator"
  description = "Permissions required by the operator desktop console."
  policy      = data.aws_iam_policy_document.operator.json
}
