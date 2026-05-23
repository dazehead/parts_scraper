-- 003_row_level_security.sql
-- Row-Level Security on dbo.parts and dbo.part_tags.
--
-- The application already filters every query by tenant_id; the
-- migration-002 trigger backs that up for part_tags. RLS is the next
-- step of defence-in-depth: even a hand-rolled ad-hoc query (a
-- support engineer in SSMS, a misbehaving BI tool) only sees rows
-- whose tenant_id matches a session-context value, unless the
-- connection is using the bypass role.
--
-- How it works:
--
--   1. SESSION_CONTEXT('tenant_id') holds the active tenant. The
--      application sets it once after connecting:
--          EXEC sp_set_session_context @key=N'tenant_id', @value=N'acme-parts';
--   2. The predicate function fn_rls_tenant_predicate returns 1 when
--      the row's tenant_id matches SESSION_CONTEXT, OR when the
--      session is running as the bypass role (so the offboard /
--      migration-runner tooling still works).
--   3. The security policy attaches the predicate as a FILTER (rows
--      not matching are silently invisible to SELECT/UPDATE/DELETE)
--      and as a BLOCK on AFTER INSERT/UPDATE (rows that would not be
--      visible are rejected on write).
--
-- The bypass role:
--
--   CREATE ROLE parts_admin;
--   GRANT IMPERSONATE ON USER::<admin_login> TO parts_admin;
--   -- members of parts_admin see/write every tenant's rows.
--
-- The application login should NOT be a member of parts_admin.
--
-- Setting the session context per request:
--
--   With SQLAlchemy on the operator side, do it in a connection
--   event handler. See app/database.py for the wire-up in the
--   gui_app and worker apps.
--
-- This migration is idempotent.

SET ANSI_NULLS ON;
SET QUOTED_IDENTIFIER ON;
GO

-- ---------- bypass role ------------------------------------------------

IF DATABASE_PRINCIPAL_ID('parts_admin') IS NULL
BEGIN
    CREATE ROLE parts_admin;
END
GO

-- ---------- predicate function -----------------------------------------

IF OBJECT_ID('dbo.fn_rls_tenant_predicate', 'IF') IS NOT NULL
BEGIN
    DROP FUNCTION dbo.fn_rls_tenant_predicate;
END
GO

CREATE FUNCTION dbo.fn_rls_tenant_predicate(@row_tenant_id NVARCHAR(32))
RETURNS TABLE
WITH SCHEMABINDING
AS
RETURN
    SELECT 1 AS allowed
    WHERE
        IS_ROLEMEMBER('parts_admin') = 1
        OR @row_tenant_id = CAST(SESSION_CONTEXT(N'tenant_id') AS NVARCHAR(32));
GO

-- ---------- security policy --------------------------------------------

IF EXISTS (
    SELECT 1 FROM sys.security_policies WHERE name = 'parts_tenant_isolation'
)
BEGIN
    DROP SECURITY POLICY dbo.parts_tenant_isolation;
END
GO

CREATE SECURITY POLICY dbo.parts_tenant_isolation
    ADD FILTER PREDICATE dbo.fn_rls_tenant_predicate(tenant_id) ON dbo.parts,
    ADD BLOCK PREDICATE  dbo.fn_rls_tenant_predicate(tenant_id) ON dbo.parts AFTER INSERT,
    ADD BLOCK PREDICATE  dbo.fn_rls_tenant_predicate(tenant_id) ON dbo.parts AFTER UPDATE,
    ADD FILTER PREDICATE dbo.fn_rls_tenant_predicate(tenant_id) ON dbo.part_tags,
    ADD BLOCK PREDICATE  dbo.fn_rls_tenant_predicate(tenant_id) ON dbo.part_tags AFTER INSERT,
    ADD BLOCK PREDICATE  dbo.fn_rls_tenant_predicate(tenant_id) ON dbo.part_tags AFTER UPDATE
    WITH (STATE = ON);
GO
