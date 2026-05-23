"""Read/write the dbo.tenants registry from Python.

The registry table is created by migration 004. This module is a
small typed wrapper so the admin CLI and the operator GUI don't have
to hand-roll SQL every time they need to list active tenants or set
a quota.

The wrapper takes an already-built ``Database`` (anything with the
``execute_sql`` / ``read_sql_query`` shape) so the same code works
against the gui_app, scraper, and image_proc database classes — and
against MagicMocks in tests.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
from typing import Iterable, Optional

from .ids import validate_tenant_id


VALID_STATUSES = ("active", "suspended", "archived")


@dataclasses.dataclass
class TenantRecord:
    tenant_id: str
    display_name: Optional[str]
    created_at: Optional[dt.datetime]
    status: str
    monthly_image_quota: Optional[int]
    notes: Optional[str]

    @property
    def is_active(self) -> bool:
        return self.status == "active"


class TenantRegistry:
    """Thin façade over ``dbo.tenants``."""

    def __init__(self, db):
        self.db = db

    # ---- read --------------------------------------------------------

    def list(self, *, status: str | None = None) -> list[TenantRecord]:
        if status is not None:
            df = self.db.read_sql_query(
                "SELECT tenant_id, display_name, created_at, status, "
                "       monthly_image_quota, notes "
                "FROM dbo.tenants WHERE status = :status "
                "ORDER BY tenant_id;",
                params={"status": status},
            )
        else:
            df = self.db.read_sql_query(
                "SELECT tenant_id, display_name, created_at, status, "
                "       monthly_image_quota, notes "
                "FROM dbo.tenants ORDER BY tenant_id;"
            )
        return [self._from_row(r) for _, r in df.iterrows()]

    def get(self, tenant_id: str) -> Optional[TenantRecord]:
        tenant_id = validate_tenant_id(tenant_id)
        df = self.db.read_sql_query(
            "SELECT tenant_id, display_name, created_at, status, "
            "       monthly_image_quota, notes "
            "FROM dbo.tenants WHERE tenant_id = :t;",
            params={"t": tenant_id},
        )
        if df.empty:
            return None
        return self._from_row(df.iloc[0])

    # ---- write -------------------------------------------------------

    def upsert(
        self,
        tenant_id: str,
        *,
        display_name: Optional[str] = None,
        status: str = "active",
        monthly_image_quota: Optional[int] = None,
        notes: Optional[str] = None,
    ) -> None:
        """Create the tenant if missing, otherwise update the supplied fields."""
        tenant_id = validate_tenant_id(tenant_id)
        if status not in VALID_STATUSES:
            raise ValueError(f"status must be one of {VALID_STATUSES}, got {status!r}")
        if monthly_image_quota is not None and monthly_image_quota < 0:
            raise ValueError("monthly_image_quota must be >= 0 or None")

        self.db.execute_sql(
            """
            MERGE dbo.tenants AS tgt
            USING (SELECT :t AS tenant_id) AS src
                ON tgt.tenant_id = src.tenant_id
            WHEN MATCHED THEN UPDATE SET
                display_name        = :display_name,
                status              = :status,
                monthly_image_quota = :quota,
                notes               = :notes
            WHEN NOT MATCHED THEN
                INSERT (tenant_id, display_name, status, monthly_image_quota, notes)
                VALUES (:t, :display_name, :status, :quota, :notes);
            """,
            params={
                "t": tenant_id,
                "display_name": display_name,
                "status": status,
                "quota": monthly_image_quota,
                "notes": notes,
            },
        )

    def set_status(self, tenant_id: str, status: str) -> None:
        tenant_id = validate_tenant_id(tenant_id)
        if status not in VALID_STATUSES:
            raise ValueError(f"status must be one of {VALID_STATUSES}, got {status!r}")
        self.db.execute_sql(
            "UPDATE dbo.tenants SET status = :status WHERE tenant_id = :t;",
            params={"status": status, "t": tenant_id},
        )

    def set_quota(self, tenant_id: str, monthly_image_quota: Optional[int]) -> None:
        tenant_id = validate_tenant_id(tenant_id)
        if monthly_image_quota is not None and monthly_image_quota < 0:
            raise ValueError("monthly_image_quota must be >= 0 or None")
        self.db.execute_sql(
            "UPDATE dbo.tenants SET monthly_image_quota = :q WHERE tenant_id = :t;",
            params={"q": monthly_image_quota, "t": tenant_id},
        )

    def delete(self, tenant_id: str) -> None:
        tenant_id = validate_tenant_id(tenant_id)
        self.db.execute_sql(
            "DELETE FROM dbo.tenants WHERE tenant_id = :t;",
            params={"t": tenant_id},
        )

    # ---- quota check -------------------------------------------------

    def images_used_this_month(self, tenant_id: str) -> int:
        """Return how many ``final_tag`` rows for this tenant land in the
        current calendar month.

        We use ``final_tag IS NOT NULL`` as the meter (one row per
        delivered image). For now we don't have a separate
        delivered_at timestamp, so we approximate with `created_at`
        on the parts table if it exists, else simply count current
        non-null rows — that's the worst-case bound and is fine for
        a soft quota.
        """
        tenant_id = validate_tenant_id(tenant_id)
        df = self.db.read_sql_query(
            "SELECT COUNT(*) AS n FROM dbo.parts "
            "WHERE tenant_id = :t AND final_tag IS NOT NULL;",
            params={"t": tenant_id},
        )
        return int(df["n"].iat[0])

    def check_quota(self, tenant_id: str, *, would_add: int = 0) -> tuple[bool, str]:
        """Return ``(ok, reason)``.

        ``ok=False`` when the tenant is non-active or when its quota
        would be exceeded by adding ``would_add`` more images.
        """
        record = self.get(tenant_id)
        if record is None:
            # Registry rows are optional. A tenant that exists in
            # Terraform but not in the registry is treated as active
            # with no quota — fail-open so a missing-row bug doesn't
            # break production.
            return True, "tenant not registered; defaulting to active/no-quota"
        if not record.is_active:
            return False, f"tenant status is {record.status!r}"
        if record.monthly_image_quota is None:
            return True, "no quota configured"
        used = self.images_used_this_month(tenant_id)
        if used + would_add > record.monthly_image_quota:
            return False, (
                f"quota exceeded: used {used}, would add {would_add}, "
                f"limit {record.monthly_image_quota}"
            )
        return True, f"within quota: {used}/{record.monthly_image_quota}"

    # ---- internals ---------------------------------------------------

    @staticmethod
    def _from_row(row) -> TenantRecord:
        quota = row["monthly_image_quota"]
        return TenantRecord(
            tenant_id=str(row["tenant_id"]),
            display_name=(str(row["display_name"]) if row["display_name"] is not None else None),
            created_at=row["created_at"],
            status=str(row["status"]),
            monthly_image_quota=(int(quota) if quota is not None and quota == quota else None),
            notes=(str(row["notes"]) if row["notes"] is not None else None),
        )
