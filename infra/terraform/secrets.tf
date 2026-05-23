# Secrets Manager entries.
#
# The Terraform run creates the secret containers and seeds an initial
# value; the operator then rotates each one (via the console, CLI, or a
# rotation Lambda) before the system goes live. The values stored here
# at creation are deliberately placeholders so a missing rotation is
# loud rather than silent.

resource "random_password" "html_secret" {
  length  = 64
  special = false
}

resource "aws_secretsmanager_secret" "html_secret" {
  name        = "${local.prefix}/parts-pipeline/html-secret"
  description = "HMAC-SHA256 signing key used to derive final/<digest>.png filenames."

  recovery_window_in_days = 7
}

resource "aws_secretsmanager_secret_version" "html_secret" {
  secret_id     = aws_secretsmanager_secret.html_secret.id
  secret_string = random_password.html_secret.result
}

resource "aws_secretsmanager_secret" "db_password" {
  name        = "${local.prefix}/parts-pipeline/db-password"
  description = "SQL Server password for the parts_app login."

  recovery_window_in_days = 7
}

# DB password is intentionally not seeded by Terraform: the operator
# sets it via the console once. Workers will fail loudly until then.

resource "aws_secretsmanager_secret" "openai_api_key" {
  name        = "${local.prefix}/parts-pipeline/openai-api-key"
  description = "OpenAI API key (Batch tier) used by the operator console."

  recovery_window_in_days = 7
}

resource "aws_secretsmanager_secret" "decodo_credentials" {
  name        = "${local.prefix}/parts-pipeline/decodo-credentials"
  description = "Decodo proxy username/password as a JSON object: {\"username\":\"...\",\"password\":\"...\"}."

  recovery_window_in_days = 7
}

# ---------- Per-tenant HMAC signing keys ----------
# When var.tenants is non-empty, each tenant gets its own HMAC signing
# key. A leaked key is bounded to that tenant's final-image filenames.
# The deployment-wide ``html_secret`` above stays in place so single-
# tenant deployments and the cutover window keep working.
resource "random_password" "tenant_html_secret" {
  for_each = toset(var.tenants)
  length   = 64
  special  = false
}

resource "aws_secretsmanager_secret" "tenant_html_secret" {
  for_each = toset(var.tenants)

  name        = "${local.prefix}/parts-pipeline/tenants/${each.key}/html-secret"
  description = "HMAC-SHA256 signing key for tenant ${each.key}'s final-image filenames."

  recovery_window_in_days = 7
}

resource "aws_secretsmanager_secret_version" "tenant_html_secret" {
  for_each = toset(var.tenants)

  secret_id     = aws_secretsmanager_secret.tenant_html_secret[each.key].id
  secret_string = random_password.tenant_html_secret[each.key].result
}
