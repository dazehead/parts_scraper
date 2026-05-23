resource "aws_ecr_repository" "scraper" {
  name                 = local.scraper_ecr_repo
  image_tag_mutability = "MUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "AES256"
  }
}

resource "aws_ecr_repository" "image_proc" {
  name                 = local.proc_ecr_repo
  image_tag_mutability = "MUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "AES256"
  }
}

# Keep only the last 10 untagged images, to control storage cost.
resource "aws_ecr_lifecycle_policy" "scraper" {
  repository = aws_ecr_repository.scraper.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Expire untagged images older than 10"
      selection = {
        tagStatus   = "untagged"
        countType   = "imageCountMoreThan"
        countNumber = 10
      }
      action = { type = "expire" }
    }]
  })
}

resource "aws_ecr_lifecycle_policy" "image_proc" {
  repository = aws_ecr_repository.image_proc.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Expire untagged images older than 10"
      selection = {
        tagStatus   = "untagged"
        countType   = "imageCountMoreThan"
        countNumber = 10
      }
      action = { type = "expire" }
    }]
  })
}
