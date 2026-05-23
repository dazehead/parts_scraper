"""SQS message envelope: round-trip, schema, version negotiation."""
import datetime as dt
import json

import pytest

from tenancy import envelope, parse_envelope, ENVELOPE_VERSION
from tenancy.envelope import EnvelopeError


def test_round_trip_basic():
    body = envelope(
        tenant_id="acme",
        s3_key="tenants/acme/search_jobs/chunk_0.csv",
        job_id="20260101T000000-deadbeef",
        submitted_at=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc),
    )
    parsed = parse_envelope(body)
    assert parsed["v"] == ENVELOPE_VERSION
    assert parsed["tenant_id"] == "acme"
    assert parsed["s3_key"] == "tenants/acme/search_jobs/chunk_0.csv"
    assert parsed["job_id"] == "20260101T000000-deadbeef"
    assert parsed["submitted_at"] == "2026-01-01T00:00:00Z"


def test_envelope_omits_job_id_when_none():
    body = envelope(tenant_id="acme", s3_key="x")
    parsed = parse_envelope(body)
    assert "job_id" not in parsed
    assert parsed["s3_key"] == "x"


def test_envelope_rejects_bad_tenant_id():
    from tenancy import InvalidTenantError

    with pytest.raises(InvalidTenantError):
        envelope(tenant_id="BAD", s3_key="x")


def test_envelope_requires_s3_key():
    with pytest.raises(ValueError):
        envelope(tenant_id="acme", s3_key="")


def test_parse_envelope_accepts_legacy_body():
    # Legacy body has no v, no tenant_id — just s3_key.
    body = json.dumps({"s3_key": "search_jobs/chunk_0.csv"})
    parsed = parse_envelope(body)
    assert "tenant_id" not in parsed
    assert parsed["s3_key"] == "search_jobs/chunk_0.csv"


def test_parse_envelope_rejects_unknown_future_version():
    body = json.dumps({"v": ENVELOPE_VERSION + 1, "s3_key": "x", "tenant_id": "acme"})
    with pytest.raises(EnvelopeError, match="newer than this worker"):
        parse_envelope(body)


def test_parse_envelope_rejects_non_json():
    with pytest.raises(EnvelopeError):
        parse_envelope("not-json{")


def test_parse_envelope_rejects_non_object():
    with pytest.raises(EnvelopeError):
        parse_envelope(json.dumps([1, 2, 3]))


def test_parse_envelope_requires_s3_key():
    body = json.dumps({"v": 1, "tenant_id": "acme"})
    with pytest.raises(EnvelopeError, match="s3_key"):
        parse_envelope(body)


def test_parse_envelope_accepts_bytes():
    body = envelope(tenant_id="acme", s3_key="x").encode("utf-8")
    parsed = parse_envelope(body)
    assert parsed["s3_key"] == "x"
