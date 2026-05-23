locals {
  prefix = var.customer

  bucket_name = "${local.prefix}-parts-pipeline"

  scraper_queue_name = "${local.prefix}-search-queue"
  proc_queue_name    = "${local.prefix}-proc-queue"
  scraper_dlq_name   = "${local.prefix}-search-dlq"
  proc_dlq_name      = "${local.prefix}-proc-dlq"

  scraper_ecr_repo = "${local.prefix}-parts-scraper"
  proc_ecr_repo    = "${local.prefix}-parts-image-proc"

  scraper_asg_name = "${local.prefix}-scraper-asg"
  proc_asg_name    = "${local.prefix}-proc-asg"

  # S3 prefixes consumed by the operator console and the workers.
  s3_search_jobs_prefix = "search_jobs"
  s3_proc_jobs_prefix   = "proc_jobs"
  s3_images_prefix      = "images"
  s3_final_prefix       = "final"
}
