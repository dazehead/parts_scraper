# Security

This file documents how secrets are handled in this repository, where
the known gaps are, and what an operator needs to do when standing up a
new deployment.

## Secret handling

All credentials are loaded from environment variables at process start
(via `python-dotenv`). No secret should be committed to the repository.

The variables consumed by the apps are:

| Variable           | Used by              | Notes                                                  |
| ------------------ | -------------------- | ------------------------------------------------------ |
| `DB_HOST`          | all apps             | SQL Server host                                        |
| `DB_PORT`          | all apps             | SQL Server port                                        |
| `DB_USER`          | all apps             | SQL Server login                                       |
| `DB_PASSWORD`      | all apps             | SQL Server password                                    |
| `BUCKET`           | all apps             | S3 bucket name (single tenant per deployment)          |
| `AWS_REGION`       | all apps             | Defaults to `us-east-1`                                |
| `SEARCH_QUEUE_URL` | gui_app              | SQS queue URL for the scraper workers                  |
| `PROC_QUEUE_URL`   | gui_app              | SQS queue URL for the image-processing workers         |
| `TEST_QUEUE_URL`   | gui_app              | SQS test queue                                         |
| `QUEUE_URL`        | scraper / image_proc | The queue each worker drains                           |
| `SEARCH_KEY`       | gui_app              | S3 prefix for scraper job CSVs                         |
| `PROC_KEY`         | gui_app              | S3 prefix for proc job CSVs                            |
| `TEST_KEY`         | gui_app              | S3 prefix for test job CSVs                            |
| `IMAGE_KEY`        | image_proc_app       | S3 prefix where source images live                     |
| `DECODO_USERNAME`  | scraper_app          | Proxy credentials                                      |
| `DECODO_PASSWORD`  | scraper_app          | Proxy credentials                                      |
| `OPENAI_API_KEY`   | gui_app              | OpenAI API key (Batch tier)                            |
| `HTML_SECRET`      | image_proc_app       | HMAC-SHA256 key used to derive `final/<digest>.png`    |
| `TOR_PATH`         | scraper_app          | Optional path to the Tor binary                        |
| `CUSTOMER`         | all apps (optional)  | Short identifier injected into log lines and metric dimensions; defaults to `unknown` |
| `METRICS_DISABLED` | all apps (optional)  | Set to `1` to skip CloudWatch `PutMetricData` calls (tests, dev) |
| `LOG_LEVEL`        | all apps (optional)  | Defaults to `INFO`                                     |
| `DEFAULT_TENANT_ID`| all apps (optional)  | Tenant id used when an in-flight SQS message has no `tenant_id` field. See [TENANCY.md](TENANCY.md). Required during a single-tenant → multi-tenant cutover; can stay set indefinitely for single-tenant deployments. |

Each app ships a `.env.example` file enumerating its required keys.
The operator copies that to `.env` (which is git-ignored) and fills in
real values.

In production, prefer one of:

- AWS Systems Manager Parameter Store (`SecureString`) with the worker
  instance profile granted `ssm:GetParameter` for its prefix only.
- AWS Secrets Manager, fetched at container start (entrypoint script
  exports the values before the worker runs).

Either of those replaces the `.env` file on the worker host.

## HMAC signing key (`HTML_SECRET`)

`HTML_SECRET` is used to derive the canonical filename for the final
image (`final/<base64-hmac-sha256>.png`). Rotating the key changes the
filenames for any new image; existing rows in `dbo.parts.final_tag`
keep pointing at the old objects.

Operational rules:

- Generate a fresh value per deployment, e.g. `openssl rand -hex 32`.
- Store only in the secret manager. Never write to `notes.txt`,
  `README.md`, or any committed file.
- If a rotation is required because the old key was leaked: rotate the
  key, then re-run the filter step against the catalogue so new
  filenames are produced. The previous final objects in S3 should be
  removed via a one-time cleanup.

A leaked key is what we're remediating now. The previously-used value
`Bruckners1932` (formerly stored in `notes.txt`) MUST be considered
compromised and MUST NOT be reused in any new environment.

## Known gaps

These items are tracked openly; they do not block functional use of
the system but should be closed before a regulated deployment.

1. **TLS to SQL Server uses `TrustServerCertificate=yes`.** This skips
   certificate validation. Acceptable on a private network with the
   instance under your control; not acceptable across the public
   internet. Pinning or a proper CA chain is per-deployment and can be
   enabled in `Database.get_engine`.
2. **No per-tenant data partitioning.** The bucket, the SQL schema,
   and the queues are single-tenant by design. A multi-tenant variant
   would need an explicit `tenant_id` propagated through every shard
   and a per-tenant key derivation.
3. **No CloudWatch alarms shipped with the repo.** A production
   deployment must add at minimum: `ApproximateNumberOfMessages` for
   each DLQ, and a per-job error counter from the operator console.
4. **OpenAI Batch failures (`failed`, `expired`) are logged and
   skipped.** They should be re-queued or surfaced to the operator
   instead.

## Reporting

If you find a vulnerability in this code, contact the maintainer
directly rather than opening a public issue.
