"""TenantPaths derives S3 keys without anyone string-concatenating prefixes."""
import pytest

from tenancy import TenantPaths


def test_root_prefix():
    p = TenantPaths("acme-parts")
    assert p.root == "tenants/acme-parts"


def test_prefix_strips_leading_and_trailing_slashes():
    p = TenantPaths("acme")
    assert p.prefix("images") == "tenants/acme/images"
    assert p.prefix("/images/") == "tenants/acme/images"


def test_specific_keys():
    p = TenantPaths("acme")
    assert p.search_job_key("chunk_0.csv") == "tenants/acme/search_jobs/chunk_0.csv"
    assert p.proc_job_key("chunk_0.csv") == "tenants/acme/proc_jobs/chunk_0.csv"
    assert p.search_done_key("chunk_0.csv") == "tenants/acme/search_jobs/chunk_0.csv.done"
    assert p.proc_done_key("chunk_0.csv") == "tenants/acme/proc_jobs/chunk_0.csv.done"
    assert p.image_key("ab123_disc_0.png") == "tenants/acme/images/ab123_disc_0.png"
    assert p.final_key("abc.png") == "tenants/acme/final/abc.png"


def test_report_keys():
    p = TenantPaths("acme")
    job = "20260522T143107-deadbeef"
    assert p.report_prefix(job) == f"tenants/acme/reports/{job}"
    assert p.report_json_key(job) == f"tenants/acme/reports/{job}/report.json"
    assert p.report_html_key(job) == f"tenants/acme/reports/{job}/index.html"


def test_normalise_passes_through_already_scoped_key():
    p = TenantPaths("acme")
    key = "tenants/acme/search_jobs/x.csv"
    assert p.normalise(key) == key


def test_normalise_promotes_legacy_key():
    p = TenantPaths("acme")
    assert p.normalise("search_jobs/x.csv") == "tenants/acme/search_jobs/x.csv"
    assert p.normalise("/search_jobs/x.csv") == "tenants/acme/search_jobs/x.csv"


def test_normalise_does_not_double_prefix():
    p = TenantPaths("acme")
    nested = p.normalise(p.normalise("search_jobs/x.csv"))
    assert nested == "tenants/acme/search_jobs/x.csv"


def test_normalise_for_other_tenants_key_still_scopes_to_self():
    # If someone hands us a key already scoped to a different tenant,
    # the helper doesn't try to rewrite it — that would be silent
    # cross-tenant data movement. The normalise() contract is "make
    # this look tenant-scoped"; the call site is responsible for not
    # confusing tenants.
    p = TenantPaths("acme")
    other = "tenants/zenith/images/x.png"
    # Already starts with "tenants/", so prefix check fails → re-wraps.
    # This is intentional: it converts legacy un-scoped keys, but if
    # you hand it another tenant's key it produces a clearly-wrong
    # double-scope path that will 404 in S3 rather than silently
    # collide. Belt and braces: also assert it doesn't *steal* the key.
    out = p.normalise(other)
    assert out.startswith("tenants/acme/")
    assert "zenith" in out  # the other tenant's segment is preserved as data, not honoured


def test_construction_rejects_bad_tenant_id():
    from tenancy import InvalidTenantError

    with pytest.raises(InvalidTenantError):
        TenantPaths("BAD")
