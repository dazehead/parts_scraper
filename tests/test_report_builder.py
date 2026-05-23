"""Tests for the per-run report builder.

We mock S3 and assert on (a) the keys the report lands at, (b) the JSON
content shape, and (c) a handful of HTML invariants that we don't want
to silently regress (the customer-facing wording lives there).
"""
import datetime as dt
import json
from unittest.mock import MagicMock

import pytest

from report_builder import ReportBuilder, RunSummary, new_job_id


@pytest.fixture
def summary():
    return RunSummary(
        job_id="20260101T120000-deadbeef",
        customer="acme-parts",
        tenant_id="acme-parts",
        started_at=dt.datetime(2026, 1, 1, 12, 0, 0),
        finished_at=dt.datetime(2026, 1, 1, 12, 42, 17),
        csv_rows=1000,
        parts_with_existing_image=120,
        parts_searched=880,
        candidates_downloaded=4321,
        candidates_flagged=1240,
        candidates_accepted=3081,
        final_images_written=842,
        batches_total=4,
        batches_unusable=[{"batch_id": "batch_abc", "status": "expired"}],
        notes=["Re-submit the expired batch before treating the run as final."],
    )


@pytest.fixture
def s3():
    client = MagicMock()
    client.generate_presigned_url.side_effect = (
        lambda op, Params, ExpiresIn: f"https://signed/{Params['Key']}"
    )
    return client


def test_new_job_id_is_sortable_and_unique():
    a = new_job_id()
    b = new_job_id()
    assert a != b
    assert a[:8].isdigit()  # YYYYMMDD prefix
    assert "T" in a


def test_writes_json_and_html_under_tenant_prefix(summary, s3):
    rb = ReportBuilder(bucket="acme-bucket", s3=s3)
    refs = rb.write(summary, samples=[])

    # Report lives under the tenant prefix, never at the bucket root.
    assert refs["json_key"] == f"tenants/{summary.tenant_id}/reports/{summary.job_id}/report.json"
    assert refs["html_key"] == f"tenants/{summary.tenant_id}/reports/{summary.job_id}/index.html"
    assert refs["json_url"].endswith(refs["json_key"])
    assert refs["html_url"].endswith(refs["html_key"])

    calls = s3.put_object.call_args_list
    assert len(calls) == 2
    kinds = {c.kwargs["ContentType"] for c in calls}
    assert "application/json" in kinds
    assert any(c.startswith("text/html") for c in kinds)


def test_json_body_round_trips(summary, s3):
    rb = ReportBuilder(bucket="acme-bucket", s3=s3)
    rb.write(summary, samples=[])
    body = next(
        c.kwargs["Body"] for c in s3.put_object.call_args_list
        if c.kwargs["ContentType"] == "application/json"
    )
    parsed = json.loads(body)
    assert parsed["job_id"] == summary.job_id
    assert parsed["customer"] == "acme-parts"
    assert parsed["candidates_downloaded"] == 4321
    assert parsed["batches_unusable"][0]["status"] == "expired"
    assert parsed["duration_seconds"] == 42 * 60 + 17


def test_html_contains_key_numbers_and_warnings(summary, s3):
    rb = ReportBuilder(bucket="acme-bucket", s3=s3)
    rb.write(summary, samples=[])
    html_body = next(
        c.kwargs["Body"] for c in s3.put_object.call_args_list
        if c.kwargs["ContentType"].startswith("text/html")
    ).decode("utf-8")

    assert "acme-parts" in html_body
    assert summary.job_id in html_body
    assert "4321" in html_body  # candidates downloaded
    assert "842" in html_body   # final images
    assert "Unusable batches" in html_body
    assert "batch_abc" in html_body


def test_thumbnails_render_in_grid_by_default(summary, s3):
    samples = [
        {"part_number": "AB123", "description": "brake disc", "final_url": "https://acme/final/x.png"},
        {"part_number": "AB124", "description": "rotor", "final_url": "https://acme/final/y.png"},
    ]
    rb = ReportBuilder(bucket="acme-bucket", s3=s3)
    rb.write(summary, samples=samples)
    html_body = next(
        c.kwargs["Body"] for c in s3.put_object.call_args_list
        if c.kwargs["ContentType"].startswith("text/html")
    ).decode("utf-8")

    assert "<img " in html_body
    assert "https://acme/final/x.png" in html_body
    assert "https://acme/final/y.png" in html_body


def test_thumbnails_can_be_disabled(summary, s3):
    samples = [
        {"part_number": "AB123", "description": "brake disc", "final_url": "https://acme/final/x.png"},
    ]
    rb = ReportBuilder(bucket="acme-bucket", s3=s3)
    rb.write(summary, samples=samples, include_thumbnails=False)
    html_body = next(
        c.kwargs["Body"] for c in s3.put_object.call_args_list
        if c.kwargs["ContentType"].startswith("text/html")
    ).decode("utf-8")

    # Falls back to a link table, no <img> tags.
    assert "<img " not in html_body
    assert "AB123" in html_body
    assert "https://acme/final/x.png" in html_body


def test_html_escapes_user_input(s3):
    summary = RunSummary(
        job_id="20260101T120000-deadbeef",
        customer="<script>alert(1)</script>",
        tenant_id="acme-parts",
        started_at=dt.datetime(2026, 1, 1, 12, 0, 0),
        finished_at=dt.datetime(2026, 1, 1, 12, 1, 0),
    )
    rb = ReportBuilder(bucket="acme-bucket", s3=s3)
    rb.write(summary, samples=[])
    html_body = next(
        c.kwargs["Body"] for c in s3.put_object.call_args_list
        if c.kwargs["ContentType"].startswith("text/html")
    ).decode("utf-8")

    assert "<script>alert(1)</script>" not in html_body
    assert "&lt;script&gt;" in html_body
