"""Admin CLI for the tenant registry.

A small operations tool. List tenants, change a tenant's status,
set or clear a monthly image quota, check current usage. Wraps
:class:`tenancy.TenantRegistry` so the rest of the project can use
the same methods programmatically.

Examples::

    python -m admin_cli list
    python -m admin_cli show acme-parts
    python -m admin_cli add acme-parts --display-name "Acme Parts s.r.o."
    python -m admin_cli set-status acme-parts --status suspended
    python -m admin_cli set-quota acme-parts --quota 5000
    python -m admin_cli clear-quota acme-parts
    python -m admin_cli usage acme-parts
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict
from typing import Any

from dotenv import load_dotenv

load_dotenv()


def _registry():
    """Lazy import so --help is cheap."""
    from database import Database
    from tenancy import TenantRegistry
    db = Database()
    return TenantRegistry(db)


def _record_json(rec) -> dict[str, Any]:
    d = asdict(rec)
    if d.get("created_at") is not None:
        d["created_at"] = str(d["created_at"])
    return d


def cmd_list(args) -> int:
    reg = _registry()
    rows = reg.list(status=args.status)
    print(json.dumps([_record_json(r) for r in rows], indent=2))
    return 0


def cmd_show(args) -> int:
    reg = _registry()
    rec = reg.get(args.tenant)
    if rec is None:
        print(f"tenant {args.tenant!r} not found in registry", file=sys.stderr)
        return 1
    print(json.dumps(_record_json(rec), indent=2))
    return 0


def cmd_add(args) -> int:
    reg = _registry()
    reg.upsert(
        args.tenant,
        display_name=args.display_name,
        status=args.status,
        monthly_image_quota=args.quota,
        notes=args.notes,
    )
    print(json.dumps(_record_json(reg.get(args.tenant)), indent=2))
    return 0


def cmd_set_status(args) -> int:
    reg = _registry()
    reg.set_status(args.tenant, args.status)
    print(json.dumps(_record_json(reg.get(args.tenant)), indent=2))
    return 0


def cmd_set_quota(args) -> int:
    reg = _registry()
    reg.set_quota(args.tenant, args.quota)
    print(json.dumps(_record_json(reg.get(args.tenant)), indent=2))
    return 0


def cmd_clear_quota(args) -> int:
    reg = _registry()
    reg.set_quota(args.tenant, None)
    print(json.dumps(_record_json(reg.get(args.tenant)), indent=2))
    return 0


def cmd_usage(args) -> int:
    reg = _registry()
    used = reg.images_used_this_month(args.tenant)
    record = reg.get(args.tenant)
    quota = record.monthly_image_quota if record else None
    print(json.dumps({
        "tenant_id": args.tenant,
        "images_used": used,
        "monthly_image_quota": quota,
        "remaining": (quota - used) if quota is not None else None,
    }, indent=2))
    return 0


def cmd_check(args) -> int:
    reg = _registry()
    ok, reason = reg.check_quota(args.tenant, would_add=args.would_add)
    print(json.dumps({"ok": ok, "reason": reason}, indent=2))
    return 0 if ok else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Admin CLI for the tenant registry.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="List every tenant in the registry.")
    p_list.add_argument("--status", choices=("active", "suspended", "archived"))
    p_list.set_defaults(func=cmd_list)

    p_show = sub.add_parser("show", help="Show one tenant.")
    p_show.add_argument("tenant")
    p_show.set_defaults(func=cmd_show)

    p_add = sub.add_parser("add", help="Create or update a tenant row.")
    p_add.add_argument("tenant")
    p_add.add_argument("--display-name", default=None)
    p_add.add_argument("--status", default="active",
                       choices=("active", "suspended", "archived"))
    p_add.add_argument("--quota", type=int, default=None,
                       help="Monthly image quota; omit for no quota.")
    p_add.add_argument("--notes", default=None)
    p_add.set_defaults(func=cmd_add)

    p_status = sub.add_parser("set-status", help="Change a tenant's status.")
    p_status.add_argument("tenant")
    p_status.add_argument("--status", required=True,
                          choices=("active", "suspended", "archived"))
    p_status.set_defaults(func=cmd_set_status)

    p_quota = sub.add_parser("set-quota", help="Set a tenant's monthly image quota.")
    p_quota.add_argument("tenant")
    p_quota.add_argument("--quota", type=int, required=True)
    p_quota.set_defaults(func=cmd_set_quota)

    p_clear = sub.add_parser("clear-quota", help="Remove a tenant's monthly image quota.")
    p_clear.add_argument("tenant")
    p_clear.set_defaults(func=cmd_clear_quota)

    p_usage = sub.add_parser("usage", help="Show this month's usage for one tenant.")
    p_usage.add_argument("tenant")
    p_usage.set_defaults(func=cmd_usage)

    p_check = sub.add_parser("check", help="Check whether a tenant could accept N more images.")
    p_check.add_argument("tenant")
    p_check.add_argument("--would-add", type=int, default=0)
    p_check.set_defaults(func=cmd_check)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
