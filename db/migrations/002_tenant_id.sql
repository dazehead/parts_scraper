-- 002_tenant_id.sql
-- Introduce tenant_id on dbo.parts and dbo.part_tags.
--
-- This migration is designed to be run against a single-tenant
-- production database without downtime. The strategy:
--
--   1. Add tenant_id as NULL, with a default of the legacy tenant.
--   2. Backfill any existing rows to the legacy tenant.
--   3. Promote the column to NOT NULL.
--   4. Replace the unique constraint on (number) with (tenant_id, number).
--   5. Add filtered indexes that match the workers' read patterns.
--
-- The legacy tenant id is supplied via the SQLCMD variable
-- :setvar LEGACY_TENANT "default". Pick the value that matches
-- $DEFAULT_TENANT_ID on the worker fleet during the cutover.
--
-- Re-running this script is a no-op as long as :LEGACY_TENANT is set
-- to the same value.

SET ANSI_NULLS ON;
SET QUOTED_IDENTIFIER ON;
GO

:setvar LEGACY_TENANT "default"

-- ---------- dbo.parts ----------------------------------------------------

IF NOT EXISTS (
    SELECT 1 FROM sys.columns
    WHERE object_id = OBJECT_ID('dbo.parts') AND name = 'tenant_id'
)
BEGIN
    ALTER TABLE dbo.parts
        ADD tenant_id NVARCHAR(32) NULL;
END
GO

UPDATE dbo.parts SET tenant_id = '$(LEGACY_TENANT)' WHERE tenant_id IS NULL;
GO

IF EXISTS (
    SELECT 1 FROM sys.columns
    WHERE object_id = OBJECT_ID('dbo.parts') AND name = 'tenant_id' AND is_nullable = 1
)
BEGIN
    ALTER TABLE dbo.parts ALTER COLUMN tenant_id NVARCHAR(32) NOT NULL;
END
GO

-- Replace the unique constraint on (number) with (tenant_id, number).
IF EXISTS (SELECT 1 FROM sys.objects WHERE name = 'UQ_parts_number' AND type = 'UQ')
BEGIN
    ALTER TABLE dbo.parts DROP CONSTRAINT UQ_parts_number;
END
GO

IF NOT EXISTS (SELECT 1 FROM sys.objects WHERE name = 'UQ_parts_tenant_number' AND type = 'UQ')
BEGIN
    ALTER TABLE dbo.parts
        ADD CONSTRAINT UQ_parts_tenant_number UNIQUE (tenant_id, [number]);
END
GO

-- Filtered index that backs the operator console's "parts still missing
-- a final image" query, scoped per tenant.
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_parts_tenant_pending')
BEGIN
    CREATE INDEX IX_parts_tenant_pending
        ON dbo.parts (tenant_id)
        WHERE final_tag IS NULL;
END
GO

-- ---------- dbo.part_tags ------------------------------------------------

IF NOT EXISTS (
    SELECT 1 FROM sys.columns
    WHERE object_id = OBJECT_ID('dbo.part_tags') AND name = 'tenant_id'
)
BEGIN
    ALTER TABLE dbo.part_tags
        ADD tenant_id NVARCHAR(32) NULL;
END
GO

-- Backfill part_tags.tenant_id from the parent parts row.
UPDATE pt
    SET pt.tenant_id = p.tenant_id
FROM dbo.part_tags AS pt
INNER JOIN dbo.parts AS p ON p.part_id = pt.part_id
WHERE pt.tenant_id IS NULL;
GO

IF EXISTS (
    SELECT 1 FROM sys.columns
    WHERE object_id = OBJECT_ID('dbo.part_tags')
      AND name = 'tenant_id' AND is_nullable = 1
)
BEGIN
    ALTER TABLE dbo.part_tags ALTER COLUMN tenant_id NVARCHAR(32) NOT NULL;
END
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_part_tags_tenant')
BEGIN
    CREATE INDEX IX_part_tags_tenant ON dbo.part_tags (tenant_id, tag_value);
END
GO

-- ---------- Optional: row-level guard ------------------------------------
-- An application bug that forgets the tenant_id predicate is the worst
-- failure mode in a multi-tenant system. The check below adds a CHECK
-- constraint that part_tags rows always share their parent part's
-- tenant_id; SQL Server enforces it by trigger because CHECK can't
-- reference another table.

IF OBJECT_ID('dbo.trg_part_tags_tenant_match', 'TR') IS NOT NULL
BEGIN
    DROP TRIGGER dbo.trg_part_tags_tenant_match;
END
GO

CREATE TRIGGER dbo.trg_part_tags_tenant_match
ON dbo.part_tags
AFTER INSERT, UPDATE
AS
BEGIN
    SET NOCOUNT ON;
    IF EXISTS (
        SELECT 1
        FROM inserted i
        INNER JOIN dbo.parts p ON p.part_id = i.part_id
        WHERE i.tenant_id <> p.tenant_id
    )
    BEGIN
        ;THROW 50001, 'part_tags.tenant_id must match parts.tenant_id', 1;
    END
END
GO
