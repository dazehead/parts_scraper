resource "aws_s3_bucket" "pipeline" {
  bucket        = local.bucket_name
  force_destroy = false
}

resource "aws_s3_bucket_public_access_block" "pipeline" {
  bucket = aws_s3_bucket.pipeline.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "pipeline" {
  bucket = aws_s3_bucket.pipeline.id
  versioning_configuration {
    status = "Suspended"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "pipeline" {
  bucket = aws_s3_bucket.pipeline.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "pipeline" {
  count  = var.image_lifecycle_days > 0 ? 1 : 0
  bucket = aws_s3_bucket.pipeline.id

  rule {
    id     = "expire-search-jobs"
    status = "Enabled"

    filter {
      prefix = "${local.s3_search_jobs_prefix}/"
    }

    expiration {
      days = var.image_lifecycle_days
    }
  }

  rule {
    id     = "expire-proc-jobs"
    status = "Enabled"

    filter {
      prefix = "${local.s3_proc_jobs_prefix}/"
    }

    expiration {
      days = var.image_lifecycle_days
    }
  }
}
