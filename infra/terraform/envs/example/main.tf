# Per-customer wrapper. Copy this directory to envs/<customer>/ and
# fill in the values for that deployment. Keep one of these per customer
# in a private repo (or the customer's own infra repo).

module "pipeline" {
  source = "../.."

  customer = "acme-parts"
  region   = "eu-central-1"

  # Networking
  vpc_id     = "vpc-0123456789abcdef0"
  subnet_ids = ["subnet-0123456789abcdef0", "subnet-0fedcba9876543210"]

  # Worker fleet
  worker_ami_id        = "ami-0123456789abcdef0" # Amazon Linux 2023 ECS-optimised in your region
  worker_instance_type = "t3.medium"

  # Image URIs are blank on first apply; push images to the ECR repos
  # produced by `terraform output` then update these and re-apply.
  scraper_image_uri    = ""
  image_proc_image_uri = ""

  max_workers_per_queue          = 20
  messages_per_worker_target     = 1
  sqs_visibility_timeout_seconds = 4000

  # SQL Server is provisioned outside this module.
  db_host = "sql.internal.example.com"
  db_port = 1433
  db_user = "parts_app"

  alerts_email = "ops@example.com"

  # Multi-tenancy. Leave the list empty for a single-tenant deployment;
  # add ids to onboard new tenants in a re-apply. ``default_tenant_id``
  # is the fallback workers use when a legacy in-flight SQS message
  # has no tenant_id field — set it to the value you used as
  # LEGACY_TENANT when running db/migrations/002_tenant_id.sql.
  tenants           = [] # e.g. ["acme-parts", "zenith-industrial"]
  default_tenant_id = "acme-parts"
}

output "bucket" { value = module.pipeline.bucket }
output "search_queue_url" { value = module.pipeline.search_queue_url }
output "proc_queue_url" { value = module.pipeline.proc_queue_url }
output "scraper_ecr_repo_url" { value = module.pipeline.scraper_ecr_repo_url }
output "image_proc_ecr_repo_url" { value = module.pipeline.image_proc_ecr_repo_url }
output "operator_policy_arn" { value = module.pipeline.operator_policy_arn }
output "tenant_html_secret_arns" { value = module.pipeline.tenant_html_secret_arns }
output "tenant_dashboards" { value = module.pipeline.tenant_dashboards }

terraform {
  required_version = ">= 1.6.0"

  # Recommended: real customers use an S3+DynamoDB backend.
  # backend "s3" {
  #   bucket         = "acme-tfstate"
  #   key            = "parts-pipeline/terraform.tfstate"
  #   region         = "eu-central-1"
  #   dynamodb_table = "acme-tfstate-locks"
  #   encrypt        = true
  # }
}
