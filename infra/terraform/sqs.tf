resource "aws_sqs_queue" "scraper_dlq" {
  name                      = local.scraper_dlq_name
  message_retention_seconds = 1209600 # 14 days, the max
  sqs_managed_sse_enabled   = true
}

resource "aws_sqs_queue" "proc_dlq" {
  name                      = local.proc_dlq_name
  message_retention_seconds = 1209600
  sqs_managed_sse_enabled   = true
}

resource "aws_sqs_queue" "scraper" {
  name                       = local.scraper_queue_name
  visibility_timeout_seconds = var.sqs_visibility_timeout_seconds
  message_retention_seconds  = 345600 # 4 days
  sqs_managed_sse_enabled    = true

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.scraper_dlq.arn
    maxReceiveCount     = var.sqs_max_receive_count
  })
}

resource "aws_sqs_queue" "proc" {
  name                       = local.proc_queue_name
  visibility_timeout_seconds = var.sqs_visibility_timeout_seconds
  message_retention_seconds  = 345600
  sqs_managed_sse_enabled    = true

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.proc_dlq.arn
    maxReceiveCount     = var.sqs_max_receive_count
  })
}
