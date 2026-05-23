"""End-to-end-ish tests for the worker tenant flow.

We don't bring up a real SQS / S3 / SQL — we feed each worker's
``handle_message`` a constructed envelope and assert that:

- the right tenant_id is resolved (explicit wins, env fallback works,
  missing both raises),
- the s3 download key is tenant-scoped (legacy keys are promoted),
- the done-marker lands under the tenant prefix,
- ``process_shard`` receives the tenant_id.

Both workers (scraper_app and image_proc_app) share the same plumbing
with different prefix names, so we parameterise across both.
"""
import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

REPO = Path(__file__).resolve().parents[1]


def _load_worker(app_dir: Path, name_hint: str):
    """Import the ``worker`` module from a specific app dir.

    sys.path is shared across all tests via conftest, but multiple
    ``worker.py`` files exist (one per app). We load each explicitly
    by path so the test is unambiguous.
    """
    spec = importlib.util.spec_from_file_location(name_hint, app_dir / "worker.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name_hint] = mod
    spec.loader.exec_module(mod)
    return mod


def _envelope(tenant_id=None, s3_key="ignored/x.csv", v=1):
    body = {"v": v, "s3_key": s3_key}
    if tenant_id is not None:
        body["tenant_id"] = tenant_id
    return json.dumps(body)


@pytest.fixture(
    params=[
        ("scraper", REPO / "scraper_app" / "app", "search_jobs"),
        ("image_proc", REPO / "image_proc_app" / "app", "proc_jobs"),
    ],
    ids=["scraper", "image_proc"],
)
def worker_under_test(request, monkeypatch, tmp_path):
    name, app_dir, prefix = request.param
    monkeypatch.setenv("BUCKET", "test-bucket")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("LOCAL_TMP_DIR", str(tmp_path))

    worker = _load_worker(app_dir, f"_test_{name}_worker")
    worker.s3 = MagicMock()
    worker.sqs = MagicMock()

    class _NotFound(Exception):
        pass

    worker.s3.exceptions = MagicMock(ClientError=_NotFound)
    worker.s3.head_object.side_effect = _NotFound("missing")
    return worker, prefix


def test_explicit_tenant_id_in_envelope_wins(worker_under_test, monkeypatch):
    worker, prefix = worker_under_test
    monkeypatch.setenv("DEFAULT_TENANT_ID", "fallback")
    seen = {}

    def fake_process(local_in, tenant_id):
        seen["tenant_id"] = tenant_id

    monkeypatch.setattr(worker, "process_shard", fake_process)

    worker.handle_message(
        {"Body": _envelope(tenant_id="acme", s3_key=f"{prefix}/x.csv")}
    )

    assert seen["tenant_id"] == "acme"
    args, _ = worker.s3.download_file.call_args
    assert args[1] == f"tenants/acme/{prefix}/x.csv"
    put_kwargs = worker.s3.put_object.call_args.kwargs
    assert put_kwargs["Key"] == f"tenants/acme/{prefix}/x.csv.done"


def test_legacy_envelope_falls_back_to_default_tenant_id(worker_under_test, monkeypatch):
    worker, prefix = worker_under_test
    monkeypatch.setenv("DEFAULT_TENANT_ID", "legacy-tenant")
    seen = {}

    def fake_process(local_in, tenant_id):
        seen["tenant_id"] = tenant_id

    monkeypatch.setattr(worker, "process_shard", fake_process)

    legacy = json.dumps({"s3_key": f"{prefix}/old.csv"})
    worker.handle_message({"Body": legacy})

    assert seen["tenant_id"] == "legacy-tenant"
    args, _ = worker.s3.download_file.call_args
    assert args[1] == f"tenants/legacy-tenant/{prefix}/old.csv"


def test_missing_tenant_id_and_no_default_raises(worker_under_test, monkeypatch):
    worker, prefix = worker_under_test
    monkeypatch.delenv("DEFAULT_TENANT_ID", raising=False)
    from tenancy.ids import MissingTenantError

    legacy = json.dumps({"s3_key": f"{prefix}/x.csv"})
    with pytest.raises(MissingTenantError):
        worker.handle_message({"Body": legacy})


def test_invalid_tenant_id_raises(worker_under_test):
    worker, prefix = worker_under_test
    from tenancy.ids import InvalidTenantError

    bad = _envelope(tenant_id="BAD value", s3_key=f"{prefix}/x.csv")
    with pytest.raises(InvalidTenantError):
        worker.handle_message({"Body": bad})


def test_already_done_shard_is_skipped(worker_under_test, monkeypatch):
    worker, prefix = worker_under_test
    monkeypatch.setenv("DEFAULT_TENANT_ID", "acme")
    worker.s3.head_object.side_effect = None
    worker.s3.head_object.return_value = {}

    called = []
    monkeypatch.setattr(
        worker, "process_shard", lambda *a, **kw: called.append((a, kw))
    )

    worker.handle_message({"Body": _envelope(tenant_id="acme", s3_key=f"{prefix}/x.csv")})
    assert called == []
    worker.s3.download_file.assert_not_called()
    worker.s3.put_object.assert_not_called()


def test_future_envelope_version_rejected(worker_under_test):
    worker, prefix = worker_under_test
    from tenancy.envelope import EnvelopeError

    future = json.dumps({"v": 99, "tenant_id": "acme", "s3_key": f"{prefix}/x.csv"})
    with pytest.raises(EnvelopeError, match="newer than this worker"):
        worker.handle_message({"Body": future})
