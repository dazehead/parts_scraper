# Schema migrations

Each file in this directory is a numbered, idempotent SQL Server
migration. Apply them in order to bring a fresh database up to the
shape the application expects, or to roll a production database
forward.

| Step | File | What it does |
| --- | --- | --- |
| 001 | [001_baseline.sql](001_baseline.sql) | Creates `dbo.parts` and `dbo.part_tags` with the pre-tenancy shape. |
| 002 | [002_tenant_id.sql](002_tenant_id.sql) | Adds `tenant_id` to both tables, backfills it, replaces the unique constraint on `parts.number` with `(tenant_id, number)`, adds tenant-scoped indexes, installs a trigger that prevents `part_tags` rows from drifting away from their parent's tenant. |

## Applying

Two options. Either works against the same migrations.

### Option 1: the project's own runner (recommended)

```bash
DB_HOST=sql DB_USER=parts_app DB_PASSWORD="$DB_PASSWORD" \
    python -m db.apply --database parts_db --var LEGACY_TENANT=acme-parts
```

The runner discovers every `NNN_*.sql` file in this directory, splits
each on standalone `GO` lines, honours `:setvar` defaults (overridable
with `--var`), and refuses to substitute an empty string for a missing
variable. `--dry-run` prints the resolved SQL without connecting; useful
in CI.

### Option 2: sqlcmd

```bash
sqlcmd -S <db_host> -U <db_user> -P "$DB_PASSWORD" -d parts_db \
       -i 001_baseline.sql

sqlcmd -S <db_host> -U <db_user> -P "$DB_PASSWORD" -d parts_db \
       -v LEGACY_TENANT="$DEFAULT_TENANT_ID" \
       -i 002_tenant_id.sql
```

`LEGACY_TENANT` is the tenant id that existing rows belong to after
the migration. Pick the same value as the worker fleet's
`$DEFAULT_TENANT_ID`. Both must agree, otherwise the workers will
start writing into a partition the old data doesn't live in.

## Re-running

Each migration is wrapped in `IF NOT EXISTS` / `IF EXISTS` guards.
Running them twice is a no-op (idempotent), so it is safe to wire them
into a deployment pipeline that runs on every release.

## Order of operations during the cutover

1. Pick the legacy tenant id. For an existing single-tenant deployment
   the obvious choice is the Terraform `customer` value, e.g. `acme-parts`.
2. Apply `002_tenant_id.sql` with `LEGACY_TENANT=<value>`.
3. Roll the worker fleet with `DEFAULT_TENANT_ID=<value>` exported in
   the container environment.
4. Confirm the legacy data is reachable: `SELECT COUNT(*) FROM dbo.parts
   WHERE tenant_id = '<value>'` should match the pre-migration count.
5. Onboard new tenants going forward by giving the operator a different
   `tenant_id` at the start of each run (the operator GUI will gain a
   picker for this in phase 3).
