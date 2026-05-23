# Infrastructure (Terraform)

This module provisions one complete deployment of the parts image
pipeline into an AWS account. One deployment serves one customer; run
the module again with a different `customer` value to stand up a second
isolated environment in the same account.

## What this creates

| Resource group | Purpose |
| --- | --- |
| S3 bucket `<customer>-parts-pipeline` | All inputs (job CSVs), candidate images, final images, and per-stage `.done` markers. |
| SQS `<customer>-search-queue` + DLQ | Work queue for the scraper. |
| SQS `<customer>-proc-queue` + DLQ | Work queue for the image-processing worker. |
| ECR `<customer>-parts-scraper`, `<customer>-parts-image-proc` | Container registries for the two workers. |
| Launch template + ASG (one per worker) | Workers scale on `ApproximateNumberOfMessagesVisible`. Each instance pulls the image, runs one shard, exits, and asks the ASG to terminate it. |
| Secrets Manager entries | `HTML_SECRET`, DB password, OpenAI API key, Decodo credentials. The Terraform run seeds `HTML_SECRET` with a freshly generated 64-byte random value; the other three are created empty and must be populated by the operator. |
| Per-tenant Secrets Manager entries | When `var.tenants` is non-empty, each tenant gets its own `html-secret` under `${customer}/parts-pipeline/tenants/${tenant}/`. The image-proc worker honours `TENANT_HTML_SECRET_ARNS` injected via user_data to pick the right key per shard. |
| Per-tenant CloudWatch dashboard + `BatchesUnusable` alarm | One dashboard and one alarm per declared tenant. Use the per-tenant dashboard link as the artefact you share with that customer. |
| CloudWatch alarms + dashboard | DLQ depth, queue stuck, basic worker fleet visibility, and pipeline-specific dashboards on `PartsImagePipeline/*` (shards, images, p50/p95 durations). |
| CloudWatch log groups `/parts-pipeline/<customer>/{scraper,image-proc,operator}` | Worker containers ship JSON logs here via the Docker `awslogs` driver. |
| CloudWatch alarm `<customer>-openai-batches-unusable` | Pages when a `BatchesUnusable` metric tick > 0; replaces the previous silent-skip on failed/expired OpenAI batches. |
| SNS topic | Sink for alarms; optionally subscribed to `alerts_email`. |
| IAM | A least-privilege instance profile for the workers, plus a managed policy you can attach to whatever identity the operator uses on their workstation. |

What this does **not** create:

- The SQL Server instance. Pass `db_host`, `db_port`, `db_user`.
- A VPC. Pass `vpc_id` and `subnet_ids`. The subnets must have outbound
  internet (NAT or IGW) so workers can reach AWS APIs, OpenAI, the
  Decodo proxy, and Tor.
- A web UI. The operator console still runs on a workstation.

## Apply

```bash
cd infra/terraform/envs/<customer>
terraform init
terraform plan
terraform apply
```

First apply leaves `scraper_image_uri` and `image_proc_image_uri`
empty. After the apply, push the worker images to the new ECR repos
(URLs in the outputs), update `scraper_image_uri` /
`image_proc_image_uri` in the env wrapper, and re-apply.

```bash
# Build and push (run once per release)
aws ecr get-login-password --region <region> \
  | docker login --username AWS --password-stdin <account>.dkr.ecr.<region>.amazonaws.com

docker build -t <account>.dkr.ecr.<region>.amazonaws.com/<customer>-parts-scraper:latest scraper_app
docker push      <account>.dkr.ecr.<region>.amazonaws.com/<customer>-parts-scraper:latest

docker build -t <account>.dkr.ecr.<region>.amazonaws.com/<customer>-parts-image-proc:latest image_proc_app
docker push      <account>.dkr.ecr.<region>.amazonaws.com/<customer>-parts-image-proc:latest
```

## Post-apply checklist

1. Populate the secrets that Terraform created empty:
   - `aws secretsmanager put-secret-value --secret-id <db_password_secret_arn> --secret-string '<DB password>'`
   - `aws secretsmanager put-secret-value --secret-id <openai_api_key_secret_arn> --secret-string '<sk-...>'`
   - `aws secretsmanager put-secret-value --secret-id <decodo_credentials_secret_arn> --secret-string '{"username":"...","password":"..."}'`
2. Confirm the alarm-email subscription (AWS sends a confirmation link).
3. Attach `operator_policy_arn` to the IAM identity the operator's
   workstation uses (a dedicated IAM user, or an SSO role).
4. Configure the operator's `.env` (see `gui_app/.env.example`) with
   the Terraform outputs: `BUCKET`, `SEARCH_QUEUE_URL`,
   `PROC_QUEUE_URL`.
5. Apply the SQL schema (`dbo.parts`, `dbo.part_tags`) against the
   customer's SQL Server. This is not Terraform's job.

## Updates

To change a setting (queue depth target, max workers, AMI), edit
`envs/<customer>/main.tf` and re-apply. The ASGs' `desired_capacity`
is `ignore_changes`'d so a re-apply won't reset a scaled-out fleet
mid-run.

To rotate `HTML_SECRET`, change the value in Secrets Manager directly
and trigger a managed re-run of the filter step (see
`SECURITY.md`). Do not delete the secret resource through Terraform
unless you intend to lose history.

## Adding a tenant

Add the new id to `tenants` in `envs/<customer>/main.tf` and re-apply.
That single step:

- creates a 64-byte random HMAC secret in Secrets Manager at
  `${customer}/parts-pipeline/tenants/${tenant}/html-secret`,
- grants the worker instance profile read access on it,
- updates the image-proc launch template so freshly-launched workers
  pick up the new entry in `TENANT_HTML_SECRET_ARNS`,
- creates a CloudWatch dashboard `${customer}-tenant-${tenant}`,
- creates a `${customer}-${tenant}-openai-batches-unusable` alarm.

In-flight workers keep using the snapshot they were launched with,
which is fine because they were launched against a different tenant.
New shards for the new tenant get a fresh worker per the existing
one-and-done model.

## Destroy

```bash
terraform destroy
```

Note: the S3 bucket has `force_destroy = false`. Empty it manually
first if you really want it gone.
