# Workers run as one-and-done containers in Auto Scaling Groups that
# scale on SQS queue depth (target tracking on
# ApproximateNumberOfMessagesVisible).
#
# When an SQS message arrives, the scaling policy raises desired
# capacity; the new instance pulls the worker image, runs it once, the
# container exits, and the user_data script calls
# autoscaling:TerminateInstanceInAutoScalingGroup so the ASG hands us a
# fresh instance for the next message. This matches the existing
# worker.py contract (single shard per process).

# ---------- Security group ----------
resource "aws_security_group" "worker" {
  name        = "${local.prefix}-parts-worker-sg"
  description = "Outbound only — workers reach AWS APIs, the proxy, and the customer's SQL Server."
  vpc_id      = var.vpc_id

  egress {
    description = "Allow all outbound. Workers need AWS, OpenAI, Decodo, Tor."
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

# ---------- Scraper ASG ----------
locals {
  # JSON map of tenant_id -> per-tenant html_secret ARN. The worker
  # entrypoint exports this as TENANT_HTML_SECRET_ARNS so the image
  # processing app can look up the right key per message.
  tenant_html_secret_arns_json = jsonencode({
    for k, s in aws_secretsmanager_secret.tenant_html_secret : k => s.arn
  })

  scraper_user_data = templatefile("${path.module}/templates/scraper_user_data.sh.tpl", {
    region            = var.region
    customer          = var.customer
    default_tenant_id = var.default_tenant_id
    bucket            = aws_s3_bucket.pipeline.id
    queue_url         = aws_sqs_queue.scraper.url
    log_group         = aws_cloudwatch_log_group.scraper.name
    db_host           = var.db_host
    db_port           = var.db_port
    db_user           = var.db_user
    db_password_arn   = aws_secretsmanager_secret.db_password.arn
    decodo_arn        = aws_secretsmanager_secret.decodo_credentials.arn
    image_uri         = var.scraper_image_uri
  })

  image_proc_user_data = templatefile("${path.module}/templates/image_proc_user_data.sh.tpl", {
    region                  = var.region
    customer                = var.customer
    default_tenant_id       = var.default_tenant_id
    bucket                  = aws_s3_bucket.pipeline.id
    image_key               = local.s3_images_prefix
    queue_url               = aws_sqs_queue.proc.url
    log_group               = aws_cloudwatch_log_group.image_proc.name
    db_host                 = var.db_host
    db_port                 = var.db_port
    db_user                 = var.db_user
    db_password_arn         = aws_secretsmanager_secret.db_password.arn
    html_secret_arn         = aws_secretsmanager_secret.html_secret.arn
    tenant_html_secret_arns = local.tenant_html_secret_arns_json
    image_uri               = var.image_proc_image_uri
  })
}

resource "aws_launch_template" "scraper" {
  name_prefix   = "${local.prefix}-scraper-"
  image_id      = var.worker_ami_id
  instance_type = var.worker_instance_type

  iam_instance_profile {
    arn = aws_iam_instance_profile.worker.arn
  }

  vpc_security_group_ids = [aws_security_group.worker.id]
  user_data              = base64encode(local.scraper_user_data)

  metadata_options {
    http_tokens                 = "required"
    http_endpoint               = "enabled"
    http_put_response_hop_limit = 2
  }

  tag_specifications {
    resource_type = "instance"
    tags = {
      Name = "${local.prefix}-scraper-worker"
      Role = "scraper"
    }
  }

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_launch_template" "image_proc" {
  name_prefix   = "${local.prefix}-image-proc-"
  image_id      = var.worker_ami_id
  instance_type = var.worker_instance_type

  iam_instance_profile {
    arn = aws_iam_instance_profile.worker.arn
  }

  vpc_security_group_ids = [aws_security_group.worker.id]
  user_data              = base64encode(local.image_proc_user_data)

  metadata_options {
    http_tokens                 = "required"
    http_endpoint               = "enabled"
    http_put_response_hop_limit = 2
  }

  tag_specifications {
    resource_type = "instance"
    tags = {
      Name = "${local.prefix}-image-proc-worker"
      Role = "image-proc"
    }
  }

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_autoscaling_group" "scraper" {
  name                  = local.scraper_asg_name
  min_size              = 0
  max_size              = var.max_workers_per_queue
  desired_capacity      = 0
  vpc_zone_identifier   = var.subnet_ids
  health_check_type     = "EC2"
  default_cooldown      = 60
  capacity_rebalance    = false
  termination_policies  = ["OldestInstance"]
  protect_from_scale_in = false

  launch_template {
    id      = aws_launch_template.scraper.id
    version = "$Latest"
  }

  tag {
    key                 = "Project"
    value               = "parts-image-pipeline"
    propagate_at_launch = true
  }

  tag {
    key                 = "Customer"
    value               = var.customer
    propagate_at_launch = true
  }

  lifecycle {
    ignore_changes = [desired_capacity]
  }
}

resource "aws_autoscaling_group" "image_proc" {
  name                  = local.proc_asg_name
  min_size              = 0
  max_size              = var.max_workers_per_queue
  desired_capacity      = 0
  vpc_zone_identifier   = var.subnet_ids
  health_check_type     = "EC2"
  default_cooldown      = 60
  capacity_rebalance    = false
  termination_policies  = ["OldestInstance"]
  protect_from_scale_in = false

  launch_template {
    id      = aws_launch_template.image_proc.id
    version = "$Latest"
  }

  tag {
    key                 = "Project"
    value               = "parts-image-pipeline"
    propagate_at_launch = true
  }

  tag {
    key                 = "Customer"
    value               = var.customer
    propagate_at_launch = true
  }

  lifecycle {
    ignore_changes = [desired_capacity]
  }
}

# ---------- Scaling policies (target tracking on queue depth) ----------
resource "aws_autoscaling_policy" "scraper_scale" {
  name                   = "${local.prefix}-scraper-scale-on-queue"
  autoscaling_group_name = aws_autoscaling_group.scraper.name
  policy_type            = "TargetTrackingScaling"

  target_tracking_configuration {
    customized_metric_specification {
      metric_name = "ApproximateNumberOfMessagesVisible"
      namespace   = "AWS/SQS"
      statistic   = "Average"

      metric_dimension {
        name  = "QueueName"
        value = aws_sqs_queue.scraper.name
      }
    }

    target_value     = var.messages_per_worker_target
    disable_scale_in = false
  }
}

resource "aws_autoscaling_policy" "image_proc_scale" {
  name                   = "${local.prefix}-image-proc-scale-on-queue"
  autoscaling_group_name = aws_autoscaling_group.image_proc.name
  policy_type            = "TargetTrackingScaling"

  target_tracking_configuration {
    customized_metric_specification {
      metric_name = "ApproximateNumberOfMessagesVisible"
      namespace   = "AWS/SQS"
      statistic   = "Average"

      metric_dimension {
        name  = "QueueName"
        value = aws_sqs_queue.proc.name
      }
    }

    target_value     = var.messages_per_worker_target
    disable_scale_in = false
  }
}
