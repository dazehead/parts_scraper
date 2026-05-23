-- 004_tenant_registry.sql
-- A small tenant registry, owned by the application rather than by
-- Terraform. Each row tracks:
--
--   tenant_id              the canonical id (matches the regex in
--                          app/tenancy/ids.py).
--   display_name           free-form, what we'd show in a UI.
--   created_at             utc timestamp.
--   status                 'active' | 'suspended' | 'archived'. The
--                          worker / operator check status before
--                          accepting work; only 'active' is allowed.
--   monthly_image_quota    optional cap. NULL = no quota.
--                          enforced in app/database.py at upsert
--                          time, not in SQL — SQL only stores it.
--   notes                  free-form.
--
-- Quotas are enforced at the application layer because doing it in
-- SQL requires a per-tenant counter table and a trigger; that's worth
-- the complexity once a customer asks for it, not before.
--
-- This table is not subject to row-level security: the admin CLI and
-- the migration runner need to read every row regardless of which
-- tenant they're impersonating.

SET ANSI_NULLS ON;
SET QUOTED_IDENTIFIER ON;
GO

IF OBJECT_ID('dbo.tenants', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.tenants (
        tenant_id           NVARCHAR(32)  NOT NULL PRIMARY KEY,
        display_name        NVARCHAR(128) NULL,
        created_at          DATETIME2     NOT NULL CONSTRAINT DF_tenants_created DEFAULT (SYSUTCDATETIME()),
        status              NVARCHAR(16)  NOT NULL CONSTRAINT DF_tenants_status DEFAULT (N'active'),
        monthly_image_quota INT           NULL,
        notes               NVARCHAR(MAX) NULL,
        CONSTRAINT CK_tenants_status CHECK (status IN (N'active', N'suspended', N'archived')),
        CONSTRAINT CK_tenants_tenant_id_shape CHECK (
            tenant_id NOT LIKE '%[^a-z0-9-]%'
            AND tenant_id NOT LIKE '-%'
            AND tenant_id NOT LIKE '%-'
            AND LEN(tenant_id) BETWEEN 2 AND 32
        )
    );

    CREATE INDEX IX_tenants_status ON dbo.tenants (status);
END
GO
