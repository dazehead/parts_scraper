variable "customer" {
  description = "Short customer identifier (lowercase, hyphen-separated). Used as a prefix on every resource so multiple deployments can live in the same AWS account."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,30}$", var.customer))
    error_message = "customer must be lowercase alphanumeric/hyphen, start with a letter, max 31 chars."
  }
}

variable "region" {
  description = "AWS region to deploy into."
  type        = string
  default     = "eu-central-1"
}

variable "vpc_id" {
  description = "VPC where the worker EC2 instances run. Must have outbound internet (NAT or IGW) for AWS API calls, OpenAI, the proxy and Tor."
  type        = string
}

variable "subnet_ids" {
  description = "Private subnet IDs for the worker Auto Scaling Groups."
  type        = list(string)

  validation {
    condition     = length(var.subnet_ids) >= 1
    error_message = "Provide at least one subnet id."
  }
}

variable "worker_instance_type" {
  description = "Instance type used by both worker ASGs."
  type        = string
  default     = "t3.medium"
}

variable "worker_ami_id" {
  description = "AMI id to launch workers from. Should have Docker installed (Amazon Linux 2023 ECS-optimised works)."
  type        = string
}

variable "scraper_image_uri" {
  description = "Full ECR image URI for the scraper worker, e.g. 123456789012.dkr.ecr.eu-central-1.amazonaws.com/<customer>-parts-scraper:latest. If empty, the ECR repo is still created and the ASG runs no image until you push one."
  type        = string
  default     = ""
}

variable "image_proc_image_uri" {
  description = "Full ECR image URI for the image-processing worker."
  type        = string
  default     = ""
}

variable "max_workers_per_queue" {
  description = "Hard upper bound on each worker ASG. Tune to your OpenAI/Decodo capacity."
  type        = number
  default     = 20
}

variable "messages_per_worker_target" {
  description = "Target ratio used by the queue-depth scaling policy. With a value of 1, one extra instance is launched per visible message above the current desired count."
  type        = number
  default     = 1
}

variable "sqs_visibility_timeout_seconds" {
  description = "Visibility timeout for both work queues. Must exceed the longest expected shard runtime."
  type        = number
  default     = 4000
}

variable "sqs_max_receive_count" {
  description = "Number of receives before a message goes to the DLQ."
  type        = number
  default     = 5
}

variable "image_lifecycle_days" {
  description = "S3 lifecycle: delete intermediate job CSVs (under search_jobs/ and proc_jobs/) after this many days. 0 disables the rule."
  type        = number
  default     = 7
}

variable "db_host" {
  description = "SQL Server hostname. The DB is provisioned outside this module."
  type        = string
}

variable "db_port" {
  description = "SQL Server port."
  type        = number
  default     = 1433
}

variable "db_user" {
  description = "SQL Server login name."
  type        = string
}

variable "alerts_email" {
  description = "Email to receive CloudWatch alarm notifications (DLQ depth, worker errors). Leave empty to skip the SNS subscription."
  type        = string
  default     = ""
}

variable "tenants" {
  description = "List of tenant ids served by this deployment. When non-empty, each tenant gets its own HMAC signing key in Secrets Manager and its own CloudWatch alarms. When empty, the deployment runs single-tenant against the shared HTML_SECRET (legacy behaviour). Tenant ids must match the validation rule in app/tenancy/ids.py: lowercase, 2-32 chars, [a-z][a-z0-9-]*."
  type        = list(string)
  default     = []

  validation {
    condition = alltrue([
      for t in var.tenants :
      can(regex("^[a-z][a-z0-9](?:[a-z0-9-]{0,29}[a-z0-9])?$", t))
    ])
    error_message = "Each tenant id must be lowercase, 2-32 chars, [a-z][a-z0-9-]* with no leading/trailing hyphen."
  }
}

variable "default_tenant_id" {
  description = "Fallback tenant id workers use when an in-flight SQS message has no tenant_id field. Required during a single-tenant → multi-tenant cutover. Leave empty to refuse legacy messages entirely."
  type        = string
  default     = ""
}
