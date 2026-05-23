"""Offboarding CLI behaviour.

We don't bring up real SQL or S3. The Database and S3 client are
MagicMocks; we verify the *order* of operations (S3 listed/deleted
before SQL DELETE so a half-failed run still leaves the SQL audit
trail) and that dry-run never invokes a destructive call.
"""
from unittest.mock import MagicMock

import pandas as pd
import pytest

from offboard_tenant import offboard, OffboardReport


def _make_db(parts_count: int = 5, tags_count: int = 17):
    db = MagicMock()
    db.read_sql_query.side_effect = [
        pd.DataFrame({"n": [parts_count]}),
        pd.DataFrame({"n": [tags_count]}),
    ]
    return db


def _make_s3(keys):
    s3 = MagicMock()
    page_a = {"Contents": [{"Key": k} for k in keys[:1000]]}
    rest = keys[1000:]
    pages = [page_a] + ([{"Contents": [{"Key": k} for k in rest]}] if rest else [])
    s3.get_paginator.return_value.paginate.return_value = iter(pages)
    return s3


def test_dry_run_counts_but_does_not_delete():
    db = _make_db()
    s3 = _make_s3([f"tenants/acme/images/{i}.png" for i in range(3)])

    report = offboard("acme", bucket="b", apply=False, db=db, s3=s3)

    assert report.dry_run is True
    assert report.parts_rows == 5
    assert report.part_tags_rows == 17
    assert report.s3_objects_listed == 3
    assert report.s3_objects_deleted == 0
    db.execute_sql.assert_not_called()
    s3.delete_objects.assert_not_called()


def test_apply_deletes_sql_and_s3():
    db = _make_db()
    s3 = _make_s3([f"tenants/acme/images/{i}.png" for i in range(5)])

    report = offboard("acme", bucket="b", apply=True, db=db, s3=s3)

    assert report.dry_run is False
    assert report.s3_objects_deleted == 5
    # SQL: part_tags THEN parts. FK + trigger would catch the reverse,
    # but being explicit makes the failure mode predictable.
    sql_executed = [c.args[0] for c in db.execute_sql.call_args_list]
    assert "DELETE FROM dbo.part_tags" in sql_executed[0]
    assert "DELETE FROM dbo.parts" in sql_executed[1]
    # Tenant param threaded through every statement.
    for call in db.execute_sql.call_args_list:
        assert call.kwargs["params"] == {"t": "acme"}


def test_s3_batches_at_aws_limit():
    # 1850 keys → flushes whenever batch hits 900: [900, 900, 50].
    keys = [f"tenants/acme/images/{i}.png" for i in range(1850)]
    db = _make_db()
    s3 = _make_s3(keys)

    offboard("acme", bucket="b", apply=True, db=db, s3=s3)

    sizes = [
        len(call.kwargs["Delete"]["Objects"])
        for call in s3.delete_objects.call_args_list
    ]
    # Every batch must be under AWS's 1000-object limit per call.
    assert all(s <= 1000 for s in sizes)
    assert sum(sizes) == 1850
    assert sizes == [900, 900, 50]


def test_invalid_tenant_id_raises():
    from tenancy.ids import InvalidTenantError
    with pytest.raises(InvalidTenantError):
        offboard("BAD TENANT", bucket="b", apply=False, db=MagicMock(), s3=MagicMock())


def test_sql_failure_is_captured_in_report():
    db = _make_db()
    db.execute_sql.side_effect = RuntimeError("connection reset")
    s3 = _make_s3([])

    report = offboard("acme", bucket="b", apply=True, db=db, s3=s3)
    assert any("sql delete failed" in e for e in report.errors)


def test_s3_failure_skips_sql_delete():
    # If we can't enumerate S3, we shouldn't proceed to delete the
    # audit trail in SQL. (We delete SQL only when errors is empty.)
    db = _make_db()
    s3 = MagicMock()
    s3.get_paginator.return_value.paginate.side_effect = RuntimeError("S3 down")

    report = offboard("acme", bucket="b", apply=True, db=db, s3=s3)

    assert any("s3 traversal failed" in e for e in report.errors)
    db.execute_sql.assert_not_called()


def test_report_serialises_to_json():
    report = OffboardReport(tenant_id="acme", dry_run=True, parts_rows=1, part_tags_rows=2)
    d = report.as_dict()
    assert d["tenant_id"] == "acme"
    assert d["errors"] == []
