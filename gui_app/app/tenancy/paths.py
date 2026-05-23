"""Tenant-scoped S3 path derivation.

Every S3 key in the system now sits under ``tenants/<tenant_id>/``.
Nothing else should know about that prefix shape: callers ask
:class:`TenantPaths` for the key they need.

Backward-compat:

- Legacy single-tenant deployments lived under un-prefixed keys
  (``search_jobs/foo.csv``, ``images/...``). Calling
  ``TenantPaths(...).key("search_jobs/foo.csv")`` will always produce
  the tenant-scoped form. A one-off S3 copy/migration job is what
  moves the old keys; the application never reads from both shapes at
  the same time.
"""
from __future__ import annotations

from dataclasses import dataclass

from .ids import validate_tenant_id

ROOT = "tenants"

# Logical prefixes used across the pipeline. Kept centralised so a
# rename later is a one-line change.
SEARCH_JOBS = "search_jobs"
PROC_JOBS = "proc_jobs"
IMAGES = "images"
FINAL = "final"
REPORTS = "reports"


@dataclass(frozen=True)
class TenantPaths:
    """Helpers to build S3 keys and prefixes scoped to a single tenant."""

    tenant_id: str

    def __post_init__(self):
        # Re-validate on construction. Frozen dataclass means we can't
        # reassign, so this is a tripwire only.
        validate_tenant_id(self.tenant_id)

    # ---- prefix helpers -----------------------------------------------

    @property
    def root(self) -> str:
        return f"{ROOT}/{self.tenant_id}"

    def prefix(self, logical: str) -> str:
        """Return the tenant-scoped prefix for a logical area.

        Example::

            TenantPaths("acme").prefix(IMAGES) == "tenants/acme/images"
        """
        logical = logical.strip("/")
        return f"{self.root}/{logical}"

    # ---- specific key helpers -----------------------------------------

    def search_job_key(self, basename: str) -> str:
        return f"{self.prefix(SEARCH_JOBS)}/{basename}"

    def proc_job_key(self, basename: str) -> str:
        return f"{self.prefix(PROC_JOBS)}/{basename}"

    def search_done_key(self, basename: str) -> str:
        return f"{self.prefix(SEARCH_JOBS)}/{basename}.done"

    def proc_done_key(self, basename: str) -> str:
        return f"{self.prefix(PROC_JOBS)}/{basename}.done"

    def image_key(self, filename: str) -> str:
        return f"{self.prefix(IMAGES)}/{filename}"

    def final_key(self, filename: str) -> str:
        return f"{self.prefix(FINAL)}/{filename}"

    def report_prefix(self, job_id: str) -> str:
        return f"{self.prefix(REPORTS)}/{job_id}"

    def report_json_key(self, job_id: str) -> str:
        return f"{self.report_prefix(job_id)}/report.json"

    def report_html_key(self, job_id: str) -> str:
        return f"{self.report_prefix(job_id)}/index.html"

    # ---- accept tenant-scoped *or* legacy keys ------------------------

    def normalise(self, key: str) -> str:
        """Return a tenant-scoped key whether ``key`` is already scoped or not.

        Useful at the boundary where we read a legacy SQS message that
        still says ``search_jobs/chunk_0.csv``.
        """
        key = key.lstrip("/")
        if key.startswith(f"{self.root}/"):
            return key
        return f"{self.root}/{key}"
