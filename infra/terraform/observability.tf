# CloudWatch Log Groups for the two worker fleets.
#
# Workers write JSON-per-line to stdout. The Docker daemon on each
# instance is expected to be configured with the awslogs driver
# pointing at these groups (see worker user_data).

resource "aws_cloudwatch_log_group" "scraper" {
  name              = "/parts-pipeline/${local.prefix}/scraper"
  retention_in_days = 30
}

resource "aws_cloudwatch_log_group" "image_proc" {
  name              = "/parts-pipeline/${local.prefix}/image-proc"
  retention_in_days = 30
}

resource "aws_cloudwatch_log_group" "operator" {
  name              = "/parts-pipeline/${local.prefix}/operator"
  retention_in_days = 30
}

# Allow the worker instance profile to ship its own logs.
data "aws_iam_policy_document" "worker_logs" {
  statement {
    sid = "WriteOwnLogs"
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
      "logs:DescribeLogStreams",
    ]
    resources = [
      "${aws_cloudwatch_log_group.scraper.arn}:*",
      "${aws_cloudwatch_log_group.image_proc.arn}:*",
    ]
  }

  statement {
    sid       = "PutCustomMetrics"
    actions   = ["cloudwatch:PutMetricData"]
    resources = ["*"]

    condition {
      test     = "StringEquals"
      variable = "cloudwatch:namespace"
      values   = ["PartsImagePipeline"]
    }
  }
}

resource "aws_iam_role_policy" "worker_logs" {
  name   = "${local.prefix}-parts-worker-logs"
  role   = aws_iam_role.worker.id
  policy = data.aws_iam_policy_document.worker_logs.json
}
