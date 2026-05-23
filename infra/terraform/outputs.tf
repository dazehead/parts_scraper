output "bucket" {
  description = "Name of the pipeline S3 bucket. Use this for BUCKET in the operator console and worker .env."
  value       = aws_s3_bucket.pipeline.id
}

output "search_queue_url" {
  description = "URL of the scraper SQS queue."
  value       = aws_sqs_queue.scraper.url
}

output "proc_queue_url" {
  description = "URL of the image-processing SQS queue."
  value       = aws_sqs_queue.proc.url
}

output "scraper_ecr_repo_url" {
  description = "Push the scraper image to this ECR repo."
  value       = aws_ecr_repository.scraper.repository_url
}

output "image_proc_ecr_repo_url" {
  description = "Push the image-processing image to this ECR repo."
  value       = aws_ecr_repository.image_proc.repository_url
}

output "scraper_asg_name" {
  value = aws_autoscaling_group.scraper.name
}

output "image_proc_asg_name" {
  value = aws_autoscaling_group.image_proc.name
}

output "html_secret_arn" {
  description = "ARN of the Secrets Manager secret holding HTML_SECRET. Workers read this at boot."
  value       = aws_secretsmanager_secret.html_secret.arn
}

output "db_password_secret_arn" {
  description = "ARN of the Secrets Manager secret holding the SQL Server password. Operator must populate this before the system is functional."
  value       = aws_secretsmanager_secret.db_password.arn
}

output "openai_api_key_secret_arn" {
  description = "ARN of the Secrets Manager secret holding the OpenAI API key."
  value       = aws_secretsmanager_secret.openai_api_key.arn
}

output "decodo_credentials_secret_arn" {
  description = "ARN of the Secrets Manager secret holding Decodo proxy credentials (JSON-encoded)."
  value       = aws_secretsmanager_secret.decodo_credentials.arn
}

output "operator_policy_arn" {
  description = "Managed policy for the operator workstation IAM identity."
  value       = aws_iam_policy.operator.arn
}

output "alerts_topic_arn" {
  description = "SNS topic that receives CloudWatch alarms for this deployment."
  value       = aws_sns_topic.alerts.arn
}

output "tenant_html_secret_arns" {
  description = "Map of tenant_id -> Secrets Manager ARN of that tenant's HMAC signing key. Empty when var.tenants is empty (single-tenant deployment uses the shared html_secret)."
  value = {
    for k, s in aws_secretsmanager_secret.tenant_html_secret : k => s.arn
  }
}

output "tenant_dashboards" {
  description = "Names of the per-tenant CloudWatch dashboards. Hand each link to the corresponding customer."
  value = {
    for k, d in aws_cloudwatch_dashboard.per_tenant : k => d.dashboard_name
  }
}
