"""``attach_tenant_to_engine`` registers a sp_set_session_context hook.

We don't bring up a real SQL Server. We use an in-memory SQLite
engine purely so SQLAlchemy's event machinery is real, then assert
that our hook is registered (and on rebinding, the previous hook
is replaced rather than stacked).
"""
from sqlalchemy import create_engine, event

import pytest

from tenancy import attach_tenant_to_engine
from tenancy.session import _LISTENER_ATTR


@pytest.fixture
def engine():
    # SQLite is enough — we only check listener registration shape.
    return create_engine("sqlite:///:memory:")


def test_none_tenant_is_a_noop(engine):
    attach_tenant_to_engine(engine, None)
    assert getattr(engine, _LISTENER_ATTR, None) is None


def test_empty_string_tenant_is_a_noop(engine):
    attach_tenant_to_engine(engine, "")
    assert getattr(engine, _LISTENER_ATTR, None) is None


def test_registers_a_listener(engine):
    attach_tenant_to_engine(engine, "acme")
    listener = getattr(engine, _LISTENER_ATTR)
    assert listener is not None
    assert event.contains(engine, "connect", listener)


def test_rebinding_replaces_listener_not_stacks(engine):
    attach_tenant_to_engine(engine, "acme")
    first = getattr(engine, _LISTENER_ATTR)

    attach_tenant_to_engine(engine, "zenith")
    second = getattr(engine, _LISTENER_ATTR)

    assert first is not second
    assert not event.contains(engine, "connect", first)
    assert event.contains(engine, "connect", second)


def test_rebinding_to_none_clears_listener(engine):
    attach_tenant_to_engine(engine, "acme")
    attach_tenant_to_engine(engine, None)
    assert getattr(engine, _LISTENER_ATTR, None) is None


def test_invalid_tenant_raises(engine):
    from tenancy.ids import InvalidTenantError
    with pytest.raises(InvalidTenantError):
        attach_tenant_to_engine(engine, "BAD TENANT")


def test_listener_executes_sp_set_session_context(engine, monkeypatch):
    """When a connection is checked out, the listener should call the
    set-session-context proc with the tenant id. We don't want a real
    SQL Server, so we fire the event manually with a fake DBAPI conn."""
    attach_tenant_to_engine(engine, "acme")
    listener = getattr(engine, _LISTENER_ATTR)

    cursor_calls = []

    class FakeCursor:
        def execute(self, sql, *params):
            cursor_calls.append((sql, params))

        def close(self):
            pass

    class FakeConn:
        def cursor(self):
            return FakeCursor()

        def commit(self):
            cursor_calls.append(("COMMIT", ()))

    listener(FakeConn(), object())

    assert cursor_calls, "listener did not invoke cursor.execute"
    sql, params = cursor_calls[0]
    assert "sp_set_session_context" in sql
    assert "@read_only=1" in sql
    assert params == ("acme",)
    assert cursor_calls[-1][0] == "COMMIT"
