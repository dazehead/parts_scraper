"""Tenant id validation and resolution."""
import pytest

from tenancy import validate_tenant_id, MissingTenantError, InvalidTenantError
from tenancy.ids import resolve_tenant_id


@pytest.mark.parametrize(
    "value",
    [
        "acme",
        "a1",
        "acme-parts",
        "a" * 32,
        "ab",
        "acme-parts-eu",
        "tenant1",
    ],
)
def test_valid_ids_round_trip(value):
    assert validate_tenant_id(value) == value


@pytest.mark.parametrize(
    "value",
    [
        "",
        None,
        "ACME",            # uppercase
        "1acme",           # starts with digit
        "-acme",           # leading hyphen
        "acme-",           # trailing hyphen
        "ac_me",           # underscore
        "ac me",           # space
        "a" * 33,          # too long
        "a",               # too short (must be ≥2 chars after the initial letter requirement)
        "acme/parts",      # path separator
        "../etc/passwd",   # path traversal attempt
        "acme;DROP TABLE", # sql-ish
    ],
)
def test_invalid_ids_raise(value):
    if value in (None, ""):
        with pytest.raises(MissingTenantError):
            validate_tenant_id(value)
    else:
        with pytest.raises(InvalidTenantError):
            validate_tenant_id(value)


def test_resolve_prefers_explicit(monkeypatch):
    monkeypatch.setenv("DEFAULT_TENANT_ID", "fallback")
    assert resolve_tenant_id("explicit-one") == "explicit-one"


def test_resolve_uses_default_when_explicit_missing(monkeypatch):
    monkeypatch.setenv("DEFAULT_TENANT_ID", "fallback")
    assert resolve_tenant_id(None) == "fallback"
    assert resolve_tenant_id("") == "fallback"


def test_resolve_raises_when_nothing_set(monkeypatch):
    monkeypatch.delenv("DEFAULT_TENANT_ID", raising=False)
    with pytest.raises(MissingTenantError):
        resolve_tenant_id(None)


def test_resolve_validates_fallback(monkeypatch):
    # A misconfigured fallback should be just as loud as a bad explicit id.
    monkeypatch.setenv("DEFAULT_TENANT_ID", "BAD VALUE")
    with pytest.raises(InvalidTenantError):
        resolve_tenant_id(None)
