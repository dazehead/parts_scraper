"""Admin CLI smoke tests. We replace the registry with a MagicMock and
verify the right method is called with the right arguments."""
import sys
from unittest.mock import MagicMock, patch

import pytest

import admin_cli


@pytest.fixture
def fake_registry(monkeypatch):
    """Patch admin_cli._registry to return a controllable mock."""
    reg = MagicMock()
    monkeypatch.setattr(admin_cli, "_registry", lambda: reg)
    return reg


def test_list_invokes_registry_list(fake_registry, capsys):
    fake_registry.list.return_value = []
    rc = admin_cli.main(["list"])
    assert rc == 0
    fake_registry.list.assert_called_once_with(status=None)


def test_list_passes_status_filter(fake_registry):
    fake_registry.list.return_value = []
    admin_cli.main(["list", "--status", "suspended"])
    fake_registry.list.assert_called_once_with(status="suspended")


def test_show_missing_tenant_returns_nonzero(fake_registry):
    fake_registry.get.return_value = None
    rc = admin_cli.main(["show", "acme"])
    assert rc == 1


def test_add_forwards_all_fields(fake_registry):
    fake_registry.get.return_value = _stub_record()
    admin_cli.main([
        "add", "acme",
        "--display-name", "Acme",
        "--quota", "5000",
        "--notes", "n/a",
    ])
    fake_registry.upsert.assert_called_once_with(
        "acme",
        display_name="Acme",
        status="active",
        monthly_image_quota=5000,
        notes="n/a",
    )


def test_set_status(fake_registry):
    fake_registry.get.return_value = _stub_record()
    admin_cli.main(["set-status", "acme", "--status", "suspended"])
    fake_registry.set_status.assert_called_once_with("acme", "suspended")


def test_set_quota(fake_registry):
    fake_registry.get.return_value = _stub_record()
    admin_cli.main(["set-quota", "acme", "--quota", "2500"])
    fake_registry.set_quota.assert_called_once_with("acme", 2500)


def test_clear_quota(fake_registry):
    fake_registry.get.return_value = _stub_record()
    admin_cli.main(["clear-quota", "acme"])
    fake_registry.set_quota.assert_called_once_with("acme", None)


def test_usage_reports_remaining(fake_registry, capsys):
    fake_registry.images_used_this_month.return_value = 750
    fake_registry.get.return_value = _stub_record(quota=1000)
    admin_cli.main(["usage", "acme"])
    out = capsys.readouterr().out
    assert "\"images_used\": 750" in out
    assert "\"remaining\": 250" in out


def test_check_exit_code_reflects_quota_ok(fake_registry):
    fake_registry.check_quota.return_value = (True, "fine")
    assert admin_cli.main(["check", "acme", "--would-add", "10"]) == 0


def test_check_exit_code_reflects_quota_block(fake_registry):
    fake_registry.check_quota.return_value = (False, "blown")
    assert admin_cli.main(["check", "acme", "--would-add", "10"]) == 1


def _stub_record(quota=None):
    """Build a real TenantRecord — admin_cli serialises it to JSON."""
    from tenancy.registry import TenantRecord
    return TenantRecord(
        tenant_id="acme",
        display_name="Acme",
        created_at=None,
        status="active",
        monthly_image_quota=quota,
        notes=None,
    )
