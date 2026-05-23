#!/bin/bash
set -euo pipefail

# user_data for the scraper worker ASG.
# Expects Docker pre-installed on the AMI (Amazon Linux 2023 ECS-optimised, or
# AL2023 + manual docker install).

REGION="${region}"
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"

# --- Pull image from ECR ---
aws ecr get-login-password --region "$REGION" \
  | docker login --username AWS --password-stdin "$${ACCOUNT_ID}.dkr.ecr.$${REGION}.amazonaws.com"

IMAGE="${image_uri}"
if [[ -z "$IMAGE" ]]; then
  echo "scraper image_uri not provided yet; ASG will keep retrying. Exiting."
  exit 0
fi

# --- Fetch secrets ---
DB_PASSWORD="$(aws secretsmanager get-secret-value --region "$REGION" --secret-id "${db_password_arn}" --query SecretString --output text)"
DECODO_JSON="$(aws secretsmanager get-secret-value --region "$REGION" --secret-id "${decodo_arn}" --query SecretString --output text)"
DECODO_USERNAME="$(echo "$DECODO_JSON" | python3 -c 'import json,sys;print(json.load(sys.stdin)["username"])')"
DECODO_PASSWORD="$(echo "$DECODO_JSON" | python3 -c 'import json,sys;print(json.load(sys.stdin)["password"])')"

# --- Run worker (one-shot) ---
docker run --rm \
  --log-driver=awslogs \
  --log-opt awslogs-region="$REGION" \
  --log-opt awslogs-group="${log_group}" \
  --log-opt awslogs-stream="$(curl -s -H "X-aws-ec2-metadata-token: $(curl -s -X PUT 'http://169.254.169.254/latest/api/token' -H 'X-aws-ec2-metadata-token-ttl-seconds: 60')" http://169.254.169.254/latest/meta-data/instance-id)" \
  -e AWS_REGION="$REGION" \
  -e CUSTOMER="${customer}" \
  -e DEFAULT_TENANT_ID="${default_tenant_id}" \
  -e BUCKET="${bucket}" \
  -e QUEUE_URL="${queue_url}" \
  -e DB_HOST="${db_host}" \
  -e DB_PORT="${db_port}" \
  -e DB_USER="${db_user}" \
  -e DB_PASSWORD="$DB_PASSWORD" \
  -e DECODO_USERNAME="$DECODO_USERNAME" \
  -e DECODO_PASSWORD="$DECODO_PASSWORD" \
  "$IMAGE"

# Container exited (one-and-done worker model). Tell the ASG to terminate us.
TOKEN="$(curl -s -X PUT 'http://169.254.169.254/latest/api/token' -H 'X-aws-ec2-metadata-token-ttl-seconds: 60')"
INSTANCE_ID="$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/instance-id)"
aws autoscaling terminate-instance-in-auto-scaling-group \
  --instance-id "$INSTANCE_ID" \
  --no-should-decrement-desired-capacity \
  --region "$REGION" || true
