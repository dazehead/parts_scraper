"""Attach a tenant id to every SQL Server connection.

SQL Server's row-level-security policy (migration 003) reads the
active tenant from ``SESSION_CONTEXT('tenant_id')``. We set it once
per connection checkout via a SQLAlchemy ``connect`` event listener,
which means every query run through the engine is automatically
scoped — including ad-hoc reads from pandas ``read_sql_query`` or
``df.to_sql``.

Usage::

    engine = create_engine(...)
    attach_tenant_to_engine(engine, tenant_id="acme-parts")

Calling ``attach_tenant_to_engine`` with ``tenant_id=None`` is a
no-op, which matches the rest of the codebase: components that
support both single- and multi-tenant operation can pass through
their ``self.tenant_id`` without branching.

We stash the registered listener on the engine itself
(``engine._parts_tenancy_listener``) so subsequent calls — for
example, the operator switching tenants in the GUI — can replace it
cleanly rather than stacking up unbounded listeners.
"""
from __future__ import annotations

from typing import Optional

try:
    from sqlalchemy import event  # type: ignore
except ImportError:  # pragma: no cover - sqlalchemy is a runtime dep
    event = None  # type: ignore

from .ids import validate_tenant_id

_LISTENER_ATTR = "_parts_tenancy_listener"


def attach_tenant_to_engine(engine, tenant_id: Optional[str]) -> None:
    """Register a connect-time hook on ``engine`` that sets SESSION_CONTEXT.

    No-op when ``tenant_id`` is falsy. Idempotent: any previously
    registered listener on this engine is removed first.
    """
    if event is None:  # pragma: no cover
        return

    # Remove any prior listener so set_tenant() can rebind safely.
    previous = getattr(engine, _LISTENER_ATTR, None)
    if previous is not None:
        try:
            event.remove(engine, "connect", previous)
        except Exception:
            pass
        setattr(engine, _LISTENER_ATTR, None)

    if not tenant_id:
        return

    tenant_id = validate_tenant_id(tenant_id)

    def _on_connect(dbapi_conn, _conn_record):  # pragma: no cover - integration
        cursor = dbapi_conn.cursor()
        try:
            cursor.execute(
                "EXEC sp_set_session_context @key=N'tenant_id', @value=?, @read_only=1;",
                tenant_id,
            )
            dbapi_conn.commit()
        finally:
            cursor.close()

    event.listen(engine, "connect", _on_connect)
    setattr(engine, _LISTENER_ATTR, _on_connect)
