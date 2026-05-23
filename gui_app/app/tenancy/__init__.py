"""Multi-tenant helpers.

This package owns three things:

- :func:`validate_tenant_id`   — strict allow-list on the tenant
  identifier shape, so we never have to worry about escaping it in S3
  keys or SQL queries downstream.
- :class:`TenantPaths`         — derives S3 keys and prefixes from a
  tenant id. All callers go through this; nobody string-concatenates
  ``"tenants/" + tid`` on their own.
- :func:`envelope`/:func:`parse_envelope` — the message-body schema
  that gets pushed to SQS. Versioned so we can evolve the wire format
  without breaking in-flight messages.

A worker that reads a legacy message (no ``tenant_id`` in the body)
falls back to ``$DEFAULT_TENANT_ID``. If that's also unset, the helper
raises ``MissingTenantError`` — refusing to process beats silently
writing to the wrong tenant's S3 prefix or SQL row.
"""
from .ids import validate_tenant_id, MissingTenantError, InvalidTenantError  # noqa: F401
from .paths import TenantPaths  # noqa: F401
from .envelope import envelope, parse_envelope, ENVELOPE_VERSION  # noqa: F401
from .session import attach_tenant_to_engine  # noqa: F401
from .registry import TenantRegistry, TenantRecord, VALID_STATUSES  # noqa: F401
