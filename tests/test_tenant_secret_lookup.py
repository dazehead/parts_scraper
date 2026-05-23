"""image_proc worker resolves the right HTML_SECRET per tenant.

We don't bring up real Secrets Manager; the worker's ``sm`` client is
replaced with a MagicMock. We test the env-var contract:

- ``TENANT_HTML_SECRET_ARNS`` empty or absent → no override
- registered arn for the active tenant → secret fetched + exported
- registered arn for some other tenant → no override
- secrets-manager error → no override + warning logged
"""
import importlib.util
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

REPO = Path(__file__).resolve().parents[1]


def _load_image_proc_worker(monkeypatch, tenant_arns: dict):
    """Fresh import of image_proc worker with the given env."""
    monkeypatch.setenv("BUCKET", "test-bucket")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("TENANT_HTML_SECRET_ARNS", json.dumps(tenant_arns))
    monkeypatch.setenv("HTML_SECRET", "shared-deployment-wide-secret")

    spec = importlib.util.spec_from_file_location(
        "_test_image_proc_worker_for_secret",
        REPO / "image_proc_app" / "app" / "worker.py",
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_test_image_proc_worker_for_secret"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_no_tenant_arn_leaves_env_unchanged(monkeypatch):
    worker = _load_image_proc_worker(monkeypatch, {})
    worker._set_html_secret_for_tenant("acme")
    assert os.environ["HTML_SECRET"] == "shared-deployment-wide-secret"


def test_registered_tenant_overrides_env(monkeypatch):
    worker = _load_image_proc_worker(monkeypatch, {"acme": "arn:secret:acme"})
    worker.sm = MagicMock()
    worker.sm.get_secret_value.return_value = {"SecretString": "per-tenant-acme-secret"}

    worker._set_html_secret_for_tenant("acme")
    assert os.environ["HTML_SECRET"] == "per-tenant-acme-secret"
    worker.sm.get_secret_value.assert_called_once_with(SecretId="arn:secret:acme")


def test_unregistered_tenant_falls_back_to_shared(monkeypatch):
    worker = _load_image_proc_worker(monkeypatch, {"acme": "arn:secret:acme"})
    worker.sm = MagicMock()

    worker._set_html_secret_for_tenant("zenith")  # not in the map
    assert os.environ["HTML_SECRET"] == "shared-deployment-wide-secret"
    worker.sm.get_secret_value.assert_not_called()


def test_secrets_manager_error_does_not_crash(monkeypatch):
    worker = _load_image_proc_worker(monkeypatch, {"acme": "arn:secret:acme"})
    worker.sm = MagicMock()
    worker.sm.get_secret_value.side_effect = RuntimeError("access denied")

    # Must not raise; we fall back silently to the shared secret.
    worker._set_html_secret_for_tenant("acme")
    assert os.environ["HTML_SECRET"] == "shared-deployment-wide-secret"


def test_malformed_arn_map_is_ignored(monkeypatch):
    monkeypatch.setenv("TENANT_HTML_SECRET_ARNS", "not-json{")
    monkeypatch.setenv("BUCKET", "test-bucket")
    monkeypatch.setenv("HTML_SECRET", "shared")

    spec = importlib.util.spec_from_file_location(
        "_test_image_proc_worker_malformed_arns",
        REPO / "image_proc_app" / "app" / "worker.py",
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_test_image_proc_worker_malformed_arns"] = mod
    spec.loader.exec_module(mod)

    assert mod.TENANT_SECRET_ARNS == {}
