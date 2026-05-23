#!/bin/bash
set -euo pipefail

REGION="${region}"
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"

aws ecr get-login-password --region "$REGION" \
  | docker login --username AWS --password-stdin "$${ACCOUNT_ID}.dkr.ecr.$${REGION}.amazonaws.com"

IMAGE="${image_uri}"
if [[ -z "$IMAGE" ]]; then
  echo "image_proc image_uri not provided yet; ASG will keep retrying. Exiting."
  exit 0
fi

DB_PASSWORD="$(aws secretsmanager get-secret-value --region "$REGION" --secret-id "${db_password_arn}" --query SecretString --output text)"
HTML_SECRET="$(aws secretsmanager get-secret-value --region "$REGION" --secret-id "${html_secret_arn}" --query SecretString --output text)"

docker run --rm \
  --log-driver=awslogs \
  --log-opt awslogs-region="$REGION" \
  --log-opt awslogs-group="${log_group}" \
  --log-opt awslogs-stream="$(curl -s -H "X-aws-ec2-metadata-token: $(curl -s -X PUT 'http://169.254.169.254/latest/api/token' -H 'X-aws-ec2-metadata-token-ttl-seconds: 60')" http://169.254.169.254/latest/meta-data/instance-id)" \
  -e AWS_REGION="$REGION" \
  -e CUSTOMER="${customer}" \
  -e DEFAULT_TENANT_ID="${default_tenant_id}" \
  -e BUCKET="${bucket}" \
  -e IMAGE_KEY="${image_key}" \
  -e QUEUE_URL="${queue_url}" \
  -e DB_HOST="${db_host}" \
  -e DB_PORT="${db_port}" \
  -e DB_USER="${db_user}" \
  -e DB_PASSWORD="$DB_PASSWORD" \
  -e HTML_SECRET="$HTML_SECRET" \
  -e TENANT_HTML_SECRET_ARNS='${tenant_html_secret_arns}' \
  "$IMAGE"

TOKEN="$(curl -s -X PUT 'http://169.254.169.254/latest/api/token' -H 'X-aws-ec2-metadata-token-ttl-seconds: 60')"
INSTANCE_ID="$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/instance-id)"
aws autoscaling terminate-instance-in-auto-scaling-group \
  --instance-id "$INSTANCE_ID" \
  --no-should-decrement-desired-capacity \
  --region "$REGION" || true
