# Multi-tenancy

This document describes how a single deployment of the parts pipeline
serves multiple end customers (tenants) from the same workers, S3
bucket, SQL database, and CloudWatch namespace.

The rollout is staged across four phases. Phase 1 is in place; the
later phases land in their own commits.

| Phase | What | Status |
| --- | --- | --- |
| 1 | Foundation: `tenancy/` package (ids, paths, envelope), SQL migration, tests, docs. No production code path moves yet. | done |
| 2 | Workers read `tenant_id` from the SQS envelope, use `TenantPaths` for every S3 key, query SQL with `tenant_id` in the predicate. | pending |
| 3 | Operator GUI picks a tenant per run; helpers stamp `tenant_id` on every SQS message; the run report lives under the tenant prefix; OpenAI batches carry the tenant id in metadata. | pending |
| 4 | Terraform: per-tenant Secrets Manager entries (so a leaked HMAC key is bounded to one tenant); per-tenant CloudWatch dashboards / alarms; an "onboard a tenant" workflow. | done |

## Tenant identifier

A tenant id is a lowercase string, 2–32 characters, starting with a
letter, made of `[a-z0-9-]` with no leading or trailing hyphen. The
shape matches the existing Terraform `customer` variable so they can
be set to the same value when a deployment is single-tenant.

The allow-list is enforced at the boundary (`tenancy.validate_tenant_id`),
which means downstream callers can interpolate the value into S3 keys,
CloudWatch dimensions, and SQL `tenant_id` parameters without worrying
about escaping.

## S3 path scheme

Every key in the pipeline bucket now sits under `tenants/<tenant_id>/`:

```
tenants/<tenant_id>/search_jobs/chunk_N.csv
tenants/<tenant_id>/search_jobs/chunk_N.csv.done
tenants/<tenant_id>/proc_jobs/chunk_N.csv
tenants/<tenant_id>/images/<part>_<n>.png
tenants/<tenant_id>/final/<hmac-digest>.png
tenants/<tenant_id>/reports/<job_id>/index.html
```

Callers go through `tenancy.TenantPaths` rather than building these
strings by hand. The helper also has a `normalise()` method that
promotes a legacy un-scoped key (`search_jobs/foo.csv`) to its
tenant-scoped form, which is what phase-2 workers use when they
process an old in-flight message during the cutover.

## SQL schema

Both `dbo.parts` and `dbo.part_tags` gain a `tenant_id NVARCHAR(32)
NOT NULL` column. The previous unique constraint on
`dbo.parts.number` becomes `(tenant_id, number)`, so two different
tenants can legitimately use the same part number.

A trigger on `dbo.part_tags` rejects inserts/updates where
`part_tags.tenant_id` doesn't match the parent `parts.tenant_id`.
This is a defence-in-depth against an application bug that forgets the
tenant predicate.

See [`db/migrations/`](db/migrations) for the idempotent SQL.

## SQS message envelope

Operator-side code never builds raw JSON bodies — it goes through
`tenancy.envelope()`:

```json
{
  "v": 1,
  "tenant_id": "acme-parts",
  "s3_key": "tenants/acme-parts/search_jobs/chunk_3.csv",
  "job_id": "20260522T143107-deadbeef",
  "submitted_at": "2026-05-22T14:31:07Z"
}
```

- `v` lets us evolve the wire format later. Workers refuse a body with
  `v` higher than they know.
- `tenant_id` is required for new messages. A legacy body without it
  is accepted and the worker falls back to `$DEFAULT_TENANT_ID`.
- `submitted_at` is informational; useful when reading DLQs months
  later.

## Backward compatibility

Existing single-tenant deployments keep working without code changes
on day one of the rollout:

1. Apply migration `002_tenant_id.sql` with `LEGACY_TENANT=<id>`. All
   existing rows are stamped with that id.
2. Export `DEFAULT_TENANT_ID=<id>` on every worker and on the operator
   console. The phase-2 worker code uses this as the fallback when an
   in-flight SQS message lacks the new `tenant_id` field.
3. The old un-scoped S3 keys keep working until the operator runs a
   one-shot copy job that rewrites `search_jobs/...` →
   `tenants/<id>/search_jobs/...`. Until that's done, the workers
   call `TenantPaths.normalise()` on incoming keys.

Once the cutover is complete, the legacy `DEFAULT_TENANT_ID`
mechanism can stay in place indefinitely; it just becomes
"single-tenant deployments get to skip the tenant picker".

## Isolation guarantees today

With phases 1–4 in place:

- **S3:** each tenant's keys live under `tenants/<tenant_id>/`. The
  worker IAM policy still grants `GetObject`/`PutObject` on the whole
  pipeline bucket because a single fleet serves all tenants;
  tightening to per-tenant resource ARNs is straightforward when you
  want one workload per tenant (run the module twice with different
  `customer` values).
- **SQL:** every read and write filters by `tenant_id`. The trigger
  on `dbo.part_tags` rejects any row whose tenant_id doesn't match
  its parent `parts` row. The `gui_app` `Database.upsert_append_new_only`
  also refuses to insert rows whose `tenant_id` differs from the
  active Database tenant. **Row-level security** (migration 003)
  attaches a `SECURITY POLICY` to both tables that hides rows whose
  `tenant_id` doesn't match `SESSION_CONTEXT('tenant_id')`. The
  application sets that session context once per connection via
  `attach_tenant_to_engine` in `tenancy/session.py`; an ad-hoc SQL
  query that forgets the predicate now returns zero rows instead of
  the wrong tenant's data. The `parts_admin` role bypasses RLS for
  offboarding and migrations.
- **Secrets:** when `var.tenants` is non-empty, every tenant gets its
  own `html_secret` in Secrets Manager at
  `${customer}/parts-pipeline/tenants/${tenant}/html-secret`. The
  image-processing worker looks up the right secret per shard via
  the `TENANT_HTML_SECRET_ARNS` env var (a JSON map injected by
  Terraform user_data). A leaked HMAC signing key now compromises
  one tenant's final-image filenames, not all of them.
- **CloudWatch:** every custom metric carries `Customer` and
  (after phase 3) `Tenant` dimensions. The Terraform stack ships
  one CloudWatch dashboard per declared tenant
  (`<customer>-tenant-<tenant_id>`) plus the deployment-wide one,
  and a per-tenant `BatchesUnusable` alarm so an OpenAI batch
  failure pages with the affected tenant clearly named.

## Onboarding a new tenant

The end-to-end checklist for adding a new customer (tenant) to a
running deployment:

1. **Validate the id.** Lowercase, 2–32 chars, `[a-z][a-z0-9-]*`,
   no leading/trailing hyphen. Same shape Terraform enforces.
2. **Terraform:** add the id to the `tenants` list in the
   per-customer env wrapper (`infra/terraform/envs/<customer>/main.tf`)
   and re-apply. This creates the per-tenant HMAC secret, the per-tenant
   CloudWatch dashboard, and the per-tenant `BatchesUnusable` alarm.
3. **SQL:** no schema change. New rows just carry the new
   `tenant_id` value.
4. **Operator:** type the tenant id into the GUI's Tenant field
   and click Apply. The GUI rebuilds its Database / Helper /
   BatchWatermarkDetector for the new tenant and you can load that
   tenant's CSV.

Offboarding is the reverse: remove the id from `tenants`,
`terraform apply` (which drops the per-tenant secret after a 7-day
recovery window), then run a one-off SQL `DELETE FROM dbo.parts
WHERE tenant_id = '<id>'` (cascade via `dbo.part_tags`) and an S3
`aws s3 rm s3://<bucket>/tenants/<id>/ --recursive` to remove the
data.

## Tenant registry

The `dbo.tenants` table (migration 004) is a small application-owned
registry of who's currently provisioned. Each row carries:

| Column | Purpose |
| --- | --- |
| `tenant_id` | Canonical id; matches the regex used by `validate_tenant_id`. |
| `display_name` | Free-form. What we'd show in a UI. |
| `created_at` | UTC timestamp set by the default. |
| `status` | `active` / `suspended` / `archived`. Only `active` accepts new work. |
| `monthly_image_quota` | Optional cap. NULL means no quota. |
| `notes` | Free-form. |

Reads and writes go through `tenancy.TenantRegistry`, which the
operator GUI consults at the start of `search_images`: if the tenant
is suspended/archived, or if the run would push the monthly image
count past `monthly_image_quota`, the GUI refuses to start and logs
the reason. Tenants that don't have a registry row are treated as
active with no quota — fail-open so single-tenant deployments that
haven't run migration 004 keep working.

The `admin_cli` module is a small operations tool:

```
python -m admin_cli list
python -m admin_cli add acme-parts --display-name "Acme Parts s.r.o." --quota 5000
python -m admin_cli set-status acme-parts --status suspended
python -m admin_cli usage acme-parts
python -m admin_cli check acme-parts --would-add 800
```

The CLI returns exit code 1 when `check` reports the tenant blocked,
so it can be wired into a pre-run hook in a CI pipeline.

Quotas are enforced at the application layer, not in SQL. A
production-grade quota would need a per-month counter table and a
trigger; we'll do that once a customer actually asks. For now,
`monthly_image_quota` is a soft cap measured against the count of
`parts.final_tag IS NOT NULL` for this tenant.

## Offboarding

`offboard_tenant.py` is the destructive counterpart. By default it
dry-runs and prints a JSON summary; with `--apply` it deletes
`dbo.part_tags` then `dbo.parts` rows for the tenant and removes the
S3 prefix in batches of 900 keys (under AWS's DeleteObjects limit).

```
python -m offboard_tenant --tenant acme-parts             # dry run
python -m offboard_tenant --tenant acme-parts --apply     # interactive prompt
python -m offboard_tenant --tenant acme-parts --apply --yes  # CI / runbook
```

The script does not touch Secrets Manager or Terraform-managed
resources; remove the tenant from `var.tenants` and re-apply for that.

## What you don't get yet

This isn't a SaaS control plane. There is no admin UI (the CLI is the
admin surface), no per-tenant billing meter, no rate limiting. The
expectation is that the operator (you, or the agency reseller)
maintains the tenant registry by hand and feeds tenant ids into the
GUI at run-start.
