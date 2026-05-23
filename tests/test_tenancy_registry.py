"""Tenant registry behaviour.

These tests run against a MagicMock db. We verify the SQL shapes and
the validation/quota logic — the actual SQL Server execution is
covered by the integration deploy.
"""
import datetime as dt
from unittest.mock import MagicMock

import pandas as pd
import pytest

from tenancy import TenantRegistry, VALID_STATUSES
from tenancy.registry import TenantRecord


def _db_returning(*frames):
    db = MagicMock()
    db.read_sql_query.side_effect = list(frames)
    return db


def _make_row(**overrides):
    base = {
        "tenant_id": "acme",
        "display_name": "Acme Parts",
        "created_at": dt.datetime(2026, 1, 1, 12, 0),
        "status": "active",
        "monthly_image_quota": 5000,
        "notes": None,
    }
    base.update(overrides)
    return pd.DataFrame([base])


def test_list_returns_records():
    db = _db_returning(
        _make_row(tenant_id="acme"),
    )
    reg = TenantRegistry(db)
    out = reg.list()
    assert len(out) == 1
    assert isinstance(out[0], TenantRecord)
    assert out[0].tenant_id == "acme"
    assert out[0].is_active


def test_list_filters_by_status():
    db = _db_returning(pd.DataFrame(columns=["tenant_id", "display_name", "created_at",
                                              "status", "monthly_image_quota", "notes"]))
    reg = TenantRegistry(db)
    reg.list(status="suspended")
    sql = db.read_sql_query.call_args.args[0]
    assert "WHERE status = :status" in sql


def test_get_returns_none_when_missing():
    db = _db_returning(pd.DataFrame(columns=["tenant_id", "display_name", "created_at",
                                              "status", "monthly_image_quota", "notes"]))
    reg = TenantRegistry(db)
    assert reg.get("acme") is None


def test_get_validates_id():
    from tenancy.ids import InvalidTenantError
    reg = TenantRegistry(MagicMock())
    with pytest.raises(InvalidTenantError):
        reg.get("BAD")


def test_upsert_uses_merge():
    db = MagicMock()
    reg = TenantRegistry(db)
    reg.upsert("acme", display_name="Acme", status="active", monthly_image_quota=5000)
    sql = db.execute_sql.call_args.args[0]
    assert "MERGE dbo.tenants" in sql
    assert "WHEN MATCHED THEN UPDATE" in sql
    assert "WHEN NOT MATCHED" in sql
    params = db.execute_sql.call_args.kwargs["params"]
    assert params["t"] == "acme"
    assert params["quota"] == 5000


def test_upsert_rejects_invalid_status():
    reg = TenantRegistry(MagicMock())
    with pytest.raises(ValueError, match="status"):
        reg.upsert("acme", status="bogus")


def test_upsert_rejects_negative_quota():
    reg = TenantRegistry(MagicMock())
    with pytest.raises(ValueError, match="quota"):
        reg.upsert("acme", monthly_image_quota=-1)


def test_set_status_validates():
    reg = TenantRegistry(MagicMock())
    with pytest.raises(ValueError):
        reg.set_status("acme", "frozen")


def test_set_quota_to_none_is_valid():
    db = MagicMock()
    reg = TenantRegistry(db)
    reg.set_quota("acme", None)
    params = db.execute_sql.call_args.kwargs["params"]
    assert params["q"] is None


def test_images_used_this_month_counts_non_null_final_tag():
    db = _db_returning(pd.DataFrame({"n": [42]}))
    reg = TenantRegistry(db)
    assert reg.images_used_this_month("acme") == 42
    sql = db.read_sql_query.call_args.args[0]
    assert "final_tag IS NOT NULL" in sql
    assert "tenant_id = :t" in sql


# ---- check_quota ---------------------------------------------------------

def test_check_quota_passes_when_tenant_not_registered():
    # Registry row missing → fail-open (single-tenant deployments etc).
    db = _db_returning(pd.DataFrame(columns=["tenant_id", "display_name", "created_at",
                                              "status", "monthly_image_quota", "notes"]))
    reg = TenantRegistry(db)
    ok, reason = reg.check_quota("acme", would_add=100)
    assert ok is True
    assert "not registered" in reason


def test_check_quota_blocks_suspended_tenant():
    db = _db_returning(_make_row(status="suspended"))
    reg = TenantRegistry(db)
    ok, reason = reg.check_quota("acme", would_add=1)
    assert ok is False
    assert "suspended" in reason


def test_check_quota_passes_when_no_limit_set():
    db = _db_returning(_make_row(monthly_image_quota=None))
    reg = TenantRegistry(db)
    ok, reason = reg.check_quota("acme", would_add=100000)
    assert ok is True
    assert "no quota" in reason


def test_check_quota_blocks_when_would_exceed():
    db = _db_returning(
        _make_row(monthly_image_quota=1000),
        pd.DataFrame({"n": [900]}),
    )
    reg = TenantRegistry(db)
    ok, reason = reg.check_quota("acme", would_add=200)
    assert ok is False
    assert "quota exceeded" in reason
    assert "used 900" in reason


def test_check_quota_passes_at_exact_boundary():
    db = _db_returning(
        _make_row(monthly_image_quota=1000),
        pd.DataFrame({"n": [900]}),
    )
    reg = TenantRegistry(db)
    ok, reason = reg.check_quota("acme", would_add=100)
    assert ok is True
    assert "within quota" in reason


def test_valid_statuses_constant():
    assert "active" in VALID_STATUSES
    assert "suspended" in VALID_STATUSES
    assert "archived" in VALID_STATUSES
