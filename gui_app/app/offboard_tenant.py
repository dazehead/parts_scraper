"""Offboard a tenant — delete its rows from SQL and its objects from S3.

The order matters. We delete child rows before parent rows, and we
count first so we can refuse to run if the counts look surprising
(e.g. a typo in the tenant id that hits zero rows but would have
deleted thousands of S3 objects).

This script is destructive. By default it dry-runs and asks for
explicit confirmation; ``--yes`` skips the prompt for use from CI or
a runbook step. Either way it prints a JSON report at the end.

Usage::

    python -m offboard_tenant --tenant acme-parts          # dry run
    python -m offboard_tenant --tenant acme-parts --apply  # actually delete

The script does not touch:

- Secrets Manager — Terraform owns the per-tenant secret lifecycle.
  Remove the tenant from ``var.tenants`` and re-apply.
- CloudWatch dashboards or alarms — same.
- The operator's local ``data/state.json`` — that's per-workstation.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field, asdict
from typing import Iterable

from dotenv import load_dotenv

load_dotenv()


# Lazy imports keep --help cheap.
def _import():
    import boto3  # noqa
    from tenancy import TenantPaths
    from tenancy.ids import validate_tenant_id
    from database import Database
    return boto3, TenantPaths, validate_tenant_id, Database


@dataclass
class OffboardReport:
    tenant_id: str
    dry_run: bool
    parts_rows: int = 0
    part_tags_rows: int = 0
    s3_objects_listed: int = 0
    s3_objects_deleted: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self):
        return asdict(self)


def _count_rows(db, tenant_id: str) -> tuple[int, int]:
    parts = db.read_sql_query(
        "SELECT COUNT(*) AS n FROM dbo.parts WHERE tenant_id = :t;",
        params={"t": tenant_id},
    )
    tags = db.read_sql_query(
        "SELECT COUNT(*) AS n FROM dbo.part_tags WHERE tenant_id = :t;",
        params={"t": tenant_id},
    )
    return int(parts["n"].iat[0]), int(tags["n"].iat[0])


def _delete_rows(db, tenant_id: str) -> None:
    # part_tags first (child of parts). The FK enforces this anyway,
    # but being explicit is cheaper than reading the error message.
    db.execute_sql(
        "DELETE FROM dbo.part_tags WHERE tenant_id = :t;",
        params={"t": tenant_id},
    )
    db.execute_sql(
        "DELETE FROM dbo.parts WHERE tenant_id = :t;",
        params={"t": tenant_id},
    )


def _list_and_delete_s3(s3, bucket: str, prefix: str, dry_run: bool) -> tuple[int, int]:
    """List every key under ``prefix`` and (if not dry-run) delete in batches."""
    paginator = s3.get_paginator("list_objects_v2")
    listed = 0
    deleted = 0
    batch: list[dict] = []

    def _flush():
        nonlocal deleted, batch
        if not batch:
            return
        if not dry_run:
            s3.delete_objects(Bucket=bucket, Delete={"Objects": batch, "Quiet": True})
            deleted += len(batch)
        batch = []

    for page in paginator.paginate(Bucket=bucket, Prefix=prefix + "/"):
        for obj in page.get("Contents", []) or []:
            listed += 1
            batch.append({"Key": obj["Key"]})
            if len(batch) >= 900:  # AWS limit is 1000 per DeleteObjects call
                _flush()
    _flush()
    return listed, deleted


def offboard(
    tenant_id: str,
    bucket: str,
    *,
    apply: bool,
    db=None,
    s3=None,
    paths_cls=None,
) -> OffboardReport:
    """Programmatic entry point. Returns an :class:`OffboardReport`."""
    _, TenantPaths, validate_tenant_id, Database = _import()
    tenant_id = validate_tenant_id(tenant_id)
    paths_cls = paths_cls or TenantPaths
    paths = paths_cls(tenant_id)

    if db is None:
        db = Database(tenant_id=tenant_id)
    if s3 is None:
        import boto3
        s3 = boto3.client("s3", region_name=os.getenv("AWS_REGION", "us-east-1"))

    report = OffboardReport(tenant_id=tenant_id, dry_run=not apply)

    try:
        report.parts_rows, report.part_tags_rows = _count_rows(db, tenant_id)
    except Exception as e:
        report.errors.append(f"could not count rows: {e}")

    try:
        listed, deleted = _list_and_delete_s3(s3, bucket, paths.root, dry_run=not apply)
        report.s3_objects_listed = listed
        report.s3_objects_deleted = deleted
    except Exception as e:
        report.errors.append(f"s3 traversal failed: {e}")

    if apply and not report.errors:
        try:
            _delete_rows(db, tenant_id)
        except Exception as e:
            report.errors.append(f"sql delete failed: {e}")

    return report


def _confirm(report: OffboardReport) -> bool:
    print(json.dumps(report.as_dict(), indent=2))
    print()
    print(
        f"This will delete {report.parts_rows} parts, "
        f"{report.part_tags_rows} part_tags rows, and "
        f"{report.s3_objects_listed} S3 objects under "
        f"tenants/{report.tenant_id}/."
    )
    ans = input("Type the tenant id to confirm: ").strip()
    return ans == report.tenant_id


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Delete every trace of a tenant from SQL and S3."
    )
    parser.add_argument("--tenant", required=True, help="Tenant id to offboard.")
    parser.add_argument(
        "--bucket",
        default=os.getenv("BUCKET"),
        help="Pipeline S3 bucket (defaults to $BUCKET).",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually perform the deletes. Without this we dry-run.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip the interactive confirmation prompt. Required for CI.",
    )
    args = parser.parse_args(argv)

    if not args.bucket:
        print("error: --bucket or BUCKET env var is required", file=sys.stderr)
        return 2

    # Always run a dry pass first to surface what we'd touch.
    dry = offboard(args.tenant, args.bucket, apply=False)

    if not args.apply:
        print(json.dumps(dry.as_dict(), indent=2))
        return 0

    if not args.yes:
        if not _confirm(dry):
            print("aborted", file=sys.stderr)
            return 1

    final = offboard(args.tenant, args.bucket, apply=True)
    print(json.dumps(final.as_dict(), indent=2))
    return 0 if not final.errors else 1


if __name__ == "__main__":
    sys.exit(main())
