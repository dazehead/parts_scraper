resource "aws_sns_topic" "alerts" {
  name = "${local.prefix}-parts-pipeline-alerts"
}

resource "aws_sns_topic_subscription" "alerts_email" {
  count     = var.alerts_email == "" ? 0 : 1
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alerts_email
}

# ---------- DLQ depth alarms ----------
resource "aws_cloudwatch_metric_alarm" "scraper_dlq" {
  alarm_name          = "${local.prefix}-scraper-dlq-not-empty"
  alarm_description   = "Scraper DLQ has messages; investigate failed shards."
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = 1
  threshold           = 1
  metric_name         = "ApproximateNumberOfMessagesVisible"
  namespace           = "AWS/SQS"
  period              = 60
  statistic           = "Maximum"
  treat_missing_data  = "notBreaching"
  dimensions          = { QueueName = aws_sqs_queue.scraper_dlq.name }
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "proc_dlq" {
  alarm_name          = "${local.prefix}-image-proc-dlq-not-empty"
  alarm_description   = "Image-processing DLQ has messages; investigate failed shards."
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = 1
  threshold           = 1
  metric_name         = "ApproximateNumberOfMessagesVisible"
  namespace           = "AWS/SQS"
  period              = 60
  statistic           = "Maximum"
  treat_missing_data  = "notBreaching"
  dimensions          = { QueueName = aws_sqs_queue.proc_dlq.name }
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]
}

# ---------- Queue stuck (no consumers) alarm ----------
resource "aws_cloudwatch_metric_alarm" "scraper_queue_stuck" {
  alarm_name          = "${local.prefix}-scraper-queue-stuck"
  alarm_description   = "Messages have been visible in the scraper queue for more than 30 minutes; workers may not be draining it."
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 3
  threshold           = 1800
  metric_name         = "ApproximateAgeOfOldestMessage"
  namespace           = "AWS/SQS"
  period              = 300
  statistic           = "Maximum"
  treat_missing_data  = "notBreaching"
  dimensions          = { QueueName = aws_sqs_queue.scraper.name }
  alarm_actions       = [aws_sns_topic.alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "proc_queue_stuck" {
  alarm_name          = "${local.prefix}-image-proc-queue-stuck"
  alarm_description   = "Messages have been visible in the image-proc queue for more than 30 minutes; workers may not be draining it."
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 3
  threshold           = 1800
  metric_name         = "ApproximateAgeOfOldestMessage"
  namespace           = "AWS/SQS"
  period              = 300
  statistic           = "Maximum"
  treat_missing_data  = "notBreaching"
  dimensions          = { QueueName = aws_sqs_queue.proc.name }
  alarm_actions       = [aws_sns_topic.alerts.arn]
}

# ---------- Dashboard ----------
resource "aws_cloudwatch_dashboard" "pipeline" {
  dashboard_name = "${local.prefix}-parts-pipeline"

  dashboard_body = jsonencode({
    widgets = [
      {
        type   = "metric"
        x      = 0
        y      = 0
        width  = 12
        height = 6
        properties = {
          title  = "SQS queue depth (visible messages)"
          region = var.region
          view   = "timeSeries"
          metrics = [
            ["AWS/SQS", "ApproximateNumberOfMessagesVisible", "QueueName", aws_sqs_queue.scraper.name],
            ["AWS/SQS", "ApproximateNumberOfMessagesVisible", "QueueName", aws_sqs_queue.proc.name],
            ["AWS/SQS", "ApproximateNumberOfMessagesVisible", "QueueName", aws_sqs_queue.scraper_dlq.name],
            ["AWS/SQS", "ApproximateNumberOfMessagesVisible", "QueueName", aws_sqs_queue.proc_dlq.name],
          ]
        }
      },
      {
        type   = "metric"
        x      = 12
        y      = 0
        width  = 12
        height = 6
        properties = {
          title  = "Worker fleet size"
          region = var.region
          view   = "timeSeries"
          metrics = [
            ["AWS/AutoScaling", "GroupInServiceInstances", "AutoScalingGroupName", aws_autoscaling_group.scraper.name],
            ["AWS/AutoScaling", "GroupInServiceInstances", "AutoScalingGroupName", aws_autoscaling_group.image_proc.name],
          ]
        }
      },
      {
        type   = "metric"
        x      = 0
        y      = 6
        width  = 12
        height = 6
        properties = {
          title  = "Message age in main queues"
          region = var.region
          view   = "timeSeries"
          metrics = [
            ["AWS/SQS", "ApproximateAgeOfOldestMessage", "QueueName", aws_sqs_queue.scraper.name],
            ["AWS/SQS", "ApproximateAgeOfOldestMessage", "QueueName", aws_sqs_queue.proc.name],
          ]
        }
      },
      {
        type   = "metric"
        x      = 12
        y      = 6
        width  = 12
        height = 6
        properties = {
          title  = "Shard outcomes"
          region = var.region
          view   = "timeSeries"
          stat   = "Sum"
          period = 300
          metrics = [
            ["PartsImagePipeline", "ShardDone", "Customer", var.customer, "Stage", "scraper"],
            [".", "ShardFailed", ".", ".", ".", "."],
            [".", "ShardsSkipped", ".", ".", ".", "."],
            [".", "ShardDone", ".", ".", "Stage", "image_proc"],
            [".", "ShardFailed", ".", ".", ".", "."],
            [".", "ShardsSkipped", ".", ".", ".", "."],
          ]
        }
      },
      {
        type   = "metric"
        x      = 0
        y      = 12
        width  = 12
        height = 6
        properties = {
          title  = "Images flowing through the pipeline"
          region = var.region
          view   = "timeSeries"
          stat   = "Sum"
          period = 300
          metrics = [
            ["PartsImagePipeline", "ImagesDownloaded", "Customer", var.customer, "Stage", "scraper"],
            ["PartsImagePipeline", "ImagesFlagged", "Customer", var.customer, "Stage", "operator"],
            ["PartsImagePipeline", "ImagesAccepted", "Customer", var.customer, "Stage", "operator"],
            ["PartsImagePipeline", "ImagesKept", "Customer", var.customer, "Stage", "image_proc"],
            ["PartsImagePipeline", "ImagesDiscardedByDedup", "Customer", var.customer, "Stage", "image_proc"],
          ]
        }
      },
      {
        type   = "metric"
        x      = 12
        y      = 12
        width  = 12
        height = 6
        properties = {
          title  = "Shard wall-clock seconds (p50/p95)"
          region = var.region
          view   = "timeSeries"
          period = 300
          metrics = [
            ["PartsImagePipeline", "ShardSeconds", "Customer", var.customer, "Stage", "scraper", { "stat" : "p50" }],
            ["...", { "stat" : "p95" }],
            ["PartsImagePipeline", "ShardSeconds", "Customer", var.customer, "Stage", "image_proc", { "stat" : "p50" }],
            ["...", { "stat" : "p95" }],
          ]
        }
      },
    ]
  })
}

# Alert if the classifier ever returns a non-OK terminal batch.
resource "aws_cloudwatch_metric_alarm" "unusable_batches" {
  alarm_name          = "${local.prefix}-openai-batches-unusable"
  alarm_description   = "An OpenAI batch finished in failed/expired/cancelled state. The classifier did not run for those candidates."
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = 1
  threshold           = 1
  metric_name         = "BatchesUnusable"
  namespace           = "PartsImagePipeline"
  period              = 60
  statistic           = "Sum"
  treat_missing_data  = "notBreaching"

  dimensions = {
    Customer = var.customer
    Stage    = "operator"
  }

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]
}

# ---------- Per-tenant alarms ----------
# When ``var.tenants`` is set, every tenant gets its own
# BatchesUnusable alarm. The deployment-wide alarm above stays in
# place so the operator still pages on traffic that arrives without
# a tenant (legacy messages).
resource "aws_cloudwatch_metric_alarm" "unusable_batches_per_tenant" {
  for_each = toset(var.tenants)

  alarm_name          = "${local.prefix}-${each.key}-openai-batches-unusable"
  alarm_description   = "Tenant ${each.key}: an OpenAI batch finished in failed/expired/cancelled state."
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = 1
  threshold           = 1
  metric_name         = "BatchesUnusable"
  namespace           = "PartsImagePipeline"
  period              = 60
  statistic           = "Sum"
  treat_missing_data  = "notBreaching"

  dimensions = {
    Customer = var.customer
    Stage    = "operator"
    Tenant   = each.key
  }

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]
}

# Per-tenant dashboard. Created when var.tenants is non-empty; mirrors
# the deployment-wide dashboard but filters every metric by the Tenant
# dimension. One dashboard per tenant keeps the per-customer view
# uncluttered, which matters when you're sharing a link with that
# customer.
resource "aws_cloudwatch_dashboard" "per_tenant" {
  for_each = toset(var.tenants)

  dashboard_name = "${local.prefix}-tenant-${each.key}"

  dashboard_body = jsonencode({
    widgets = [
      {
        type   = "metric"
        x      = 0
        y      = 0
        width  = 12
        height = 6
        properties = {
          title  = "Shard outcomes (tenant ${each.key})"
          region = var.region
          view   = "timeSeries"
          stat   = "Sum"
          period = 300
          metrics = [
            ["PartsImagePipeline", "ShardDone", "Customer", var.customer, "Stage", "scraper", "Tenant", each.key],
            [".", "ShardFailed", ".", ".", ".", ".", ".", "."],
            [".", "ShardDone", ".", ".", "Stage", "image_proc", ".", "."],
            [".", "ShardFailed", ".", ".", ".", ".", ".", "."],
          ]
        }
      },
      {
        type   = "metric"
        x      = 12
        y      = 0
        width  = 12
        height = 6
        properties = {
          title  = "Image flow (tenant ${each.key})"
          region = var.region
          view   = "timeSeries"
          stat   = "Sum"
          period = 300
          metrics = [
            ["PartsImagePipeline", "ImagesDownloaded", "Customer", var.customer, "Stage", "scraper", "Tenant", each.key],
            ["PartsImagePipeline", "ImagesFlagged", "Customer", var.customer, "Stage", "operator", "Tenant", each.key],
            ["PartsImagePipeline", "ImagesAccepted", "Customer", var.customer, "Stage", "operator", "Tenant", each.key],
            ["PartsImagePipeline", "ImagesKept", "Customer", var.customer, "Stage", "image_proc", "Tenant", each.key],
            ["PartsImagePipeline", "ImagesDiscardedByDedup", "Customer", var.customer, "Stage", "image_proc", "Tenant", each.key],
          ]
        }
      },
    ]
  })
}
