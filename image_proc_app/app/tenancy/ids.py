"""Tenant id validation.

We enforce a tight, allow-listed shape on every tenant identifier
*before* it touches an S3 key, an SQL parameter, or a CloudWatch
dimension. That way the rest of the codebase can interpolate the
value freely without worrying about injection, path traversal, or
invalid characters in metric dimensions.

The shape matches the Terraform ``customer`` variable so they can
be aligned 1:1 if a deployment is single-tenant.
"""
from __future__ import annotations

import os
import re

# Lowercase, alphanumeric + hyphen, must start with a letter.
# 2-32 chars total. Hyphen not allowed at start or end.
_PATTERN = re.compile(r"^[a-z][a-z0-9](?:[a-z0-9-]{0,29}[a-z0-9])?$")


class InvalidTenantError(ValueError):
    """Raised when a tenant identifier fails validation."""


class MissingTenantError(LookupError):
    """Raised when no tenant id can be resolved.

    Workers raise this on a legacy SQS message that lacks ``tenant_id``
    *and* has no ``$DEFAULT_TENANT_ID`` to fall back to.
    """


def validate_tenant_id(tenant_id: str | None) -> str:
    """Return ``tenant_id`` if it passes validation, else raise.

    ``None`` and empty strings are rejected with :class:`MissingTenantError`
    so callers can distinguish "you didn't tell me which tenant" from "you
    told me but the value is malformed".
    """
    if not tenant_id:
        raise MissingTenantError("tenant id is required")
    if not isinstance(tenant_id, str):
        raise InvalidTenantError(f"tenant id must be a string, got {type(tenant_id).__name__}")
    if not _PATTERN.fullmatch(tenant_id):
        raise InvalidTenantError(
            f"invalid tenant id {tenant_id!r}: must be lowercase, "
            "start with a letter, 2-32 alnum/hyphen chars, no leading/trailing hyphen"
        )
    return tenant_id


def resolve_tenant_id(explicit: str | None) -> str:
    """Resolve the tenant id for a unit of work.

    Order of precedence:

    1. ``explicit`` argument (typically taken from the SQS message body).
    2. ``$DEFAULT_TENANT_ID`` environment variable.

    If neither is set, :class:`MissingTenantError` is raised. We do
    *not* fall back to a hardcoded ``"default"`` — that would let
    misconfigured workers silently cross-contaminate tenants.
    """
    if explicit:
        return validate_tenant_id(explicit)
    fallback = os.getenv("DEFAULT_TENANT_ID")
    if fallback:
        return validate_tenant_id(fallback)
    raise MissingTenantError(
        "no tenant_id in message and DEFAULT_TENANT_ID is not set; "
        "refusing to process to avoid cross-tenant writes"
    )
