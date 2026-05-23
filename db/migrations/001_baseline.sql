-- 001_baseline.sql
-- The pre-tenancy schema. Captured here so a fresh deployment can be
-- bootstrapped from migrations alone rather than depending on whatever
-- shape the production database happens to be in.
--
-- All statements are idempotent: re-running this script against an
-- already-baseline'd database is a no-op.

SET ANSI_NULLS ON;
SET QUOTED_IDENTIFIER ON;
GO

IF NOT EXISTS (SELECT 1 FROM sys.schemas WHERE name = 'dbo')
BEGIN
    EXEC('CREATE SCHEMA dbo;');
END
GO

-- parts: one row per SKU.
IF OBJECT_ID('dbo.parts', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.parts (
        part_id     BIGINT IDENTITY(1,1) NOT NULL PRIMARY KEY,
        [number]    NVARCHAR(128)  NOT NULL,
        description NVARCHAR(1024) NULL,
        final_tag   NVARCHAR(2048) NULL,
        CONSTRAINT UQ_parts_number UNIQUE ([number])
    );
END
GO

-- part_tags: candidate image URL(s) per part. One part to many tags.
IF OBJECT_ID('dbo.part_tags', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.part_tags (
        tag_id    BIGINT IDENTITY(1,1) NOT NULL PRIMARY KEY,
        part_id   BIGINT         NOT NULL,
        tag_value NVARCHAR(2048) NOT NULL,
        CONSTRAINT FK_part_tags_parts FOREIGN KEY (part_id) REFERENCES dbo.parts(part_id)
    );

    CREATE INDEX IX_part_tags_part_id ON dbo.part_tags(part_id);
END
GO
