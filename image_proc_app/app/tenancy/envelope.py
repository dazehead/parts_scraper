"""SQS message envelope.

Every message body the operator console pushes onto an SQS queue
follows this shape::

    {
      "v": 1,
      "tenant_id": "acme-parts",
      "s3_key": "tenants/acme-parts/search_jobs/chunk_3.csv",
      "job_id": "20260522T143107-deadbeef",
      "submitted_at": "2026-05-22T14:31:07Z"
    }

- ``v`` is the envelope version. Bumping it lets us evolve the wire
  format while in-flight messages drain. Workers refuse messages with
  a higher ``v`` than they know.
- ``tenant_id`` is required for new messages. Legacy messages without
  this key fall back to ``$DEFAULT_TENANT_ID`` via
  :func:`tenancy.resolve_tenant_id`.
- ``s3_key`` may be tenant-scoped or legacy. The worker normalises it
  through :meth:`TenantPaths.normalise`.
- ``job_id`` ties a shard back to the operator run summary used by the
  report builder. Optional today, will be required when we wire
  per-shard counters into reports.
- ``submitted_at`` is informational; useful when reading DLQs months
  later.
"""
from __future__ import annotations

import datetime as dt
import json
from typing import Any

from .ids import validate_tenant_id

ENVELOPE_VERSION = 1


class EnvelopeError(ValueError):
    """The message body did not parse as a known envelope shape."""


def envelope(
    *,
    tenant_id: str,
    s3_key: str,
    job_id: str | None = None,
    submitted_at: dt.datetime | None = None,
) -> str:
    """Build a JSON-encoded SQS body for one shard."""
    validate_tenant_id(tenant_id)
    if not s3_key:
        raise ValueError("s3_key is required")
    body: dict[str, Any] = {
        "v": ENVELOPE_VERSION,
        "tenant_id": tenant_id,
        "s3_key": s3_key,
    }
    if job_id:
        body["job_id"] = job_id
    body["submitted_at"] = (submitted_at or dt.datetime.now(dt.timezone.utc)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    return json.dumps(body, ensure_ascii=False)


def parse_envelope(body: str | bytes) -> dict[str, Any]:
    """Parse an SQS body and return a dict with at least ``s3_key``.

    Legacy bodies (no ``v``, no ``tenant_id``) are returned unchanged so
    the caller can supply a fallback tenant via
    :func:`tenancy.resolve_tenant_id`.
    """
    if isinstance(body, bytes):
        body = body.decode("utf-8")
    try:
        data = json.loads(body)
    except json.JSONDecodeError as e:
        raise EnvelopeError(f"message body is not valid JSON: {e}") from e
    if not isinstance(data, dict):
        raise EnvelopeError("message body must be a JSON object")
    if "s3_key" not in data:
        raise EnvelopeError("message body missing required field s3_key")

    v = data.get("v")
    if v is not None and v > ENVELOPE_VERSION:
        raise EnvelopeError(
            f"message envelope v={v} is newer than this worker (max v={ENVELOPE_VERSION})"
        )
    return data
