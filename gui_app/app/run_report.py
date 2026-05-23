"""CLI: regenerate a run report from state.json + the SQL Server.

Useful when the operator wants to re-render a report (e.g. with a
different sample size, or because the original upload failed and they
need a link to send to the customer).

Example:

    python -m run_report --state data/state.json --samples 24
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys

from dotenv import load_dotenv

load_dotenv()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Regenerate a parts-pipeline run report.")
    parser.add_argument(
        "--state",
        default="data/state.json",
        help="Path to the operator state.json file with the run summary.",
    )
    parser.add_argument(
        "--bucket",
        default=os.getenv("BUCKET"),
        help="S3 bucket to write the report into (defaults to BUCKET).",
    )
    parser.add_argument(
        "--tenant",
        default=os.getenv("DEFAULT_TENANT_ID"),
        help="Tenant id to attribute the run to. Defaults to DEFAULT_TENANT_ID.",
    )
    parser.add_argument(
        "--samples",
        type=int,
        default=12,
        help="Maximum sample images to include in the HTML.",
    )
    parser.add_argument(
        "--no-thumbnails",
        action="store_true",
        help="Render the HTML report without inline thumbnails (link table only).",
    )
    args = parser.parse_args(argv)

    if not args.bucket:
        print("error: --bucket or BUCKET env var is required", file=sys.stderr)
        return 2

    if not args.tenant:
        print(
            "error: --tenant or DEFAULT_TENANT_ID env var is required",
            file=sys.stderr,
        )
        return 2

    if not os.path.exists(args.state):
        print(f"error: state file not found: {args.state}", file=sys.stderr)
        return 2

    with open(args.state, "r", encoding="utf-8") as f:
        state = json.load(f)

    raw = state.get("run_summary") or {}
    if not raw.get("job_id"):
        print("error: state.json has no active run_summary", file=sys.stderr)
        return 2

    # Lazy imports so the script is cheap when the user just runs --help.
    from report_builder import ReportBuilder, RunSummary
    from database import Database

    summary = RunSummary(
        job_id=raw["job_id"],
        customer=raw.get("customer") or os.getenv("CUSTOMER", "unknown"),
        tenant_id=args.tenant,
        started_at=dt.datetime.fromisoformat(raw["started_at"]),
        finished_at=(
            dt.datetime.fromisoformat(raw["finished_at"])
            if raw.get("finished_at") else dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
        ),
        csv_rows=raw.get("csv_rows", 0),
        parts_with_existing_image=raw.get("parts_with_existing_image", 0),
        parts_searched=raw.get("parts_searched", 0),
        candidates_downloaded=raw.get("candidates_downloaded", 0),
        candidates_flagged=raw.get("candidates_flagged", 0),
        candidates_accepted=raw.get("candidates_accepted", 0),
        final_images_written=raw.get("final_images_written", 0),
        batches_total=raw.get("batches_total", 0),
        batches_unusable=list(raw.get("batches_unusable", [])),
        notes=list(raw.get("notes", [])),
    )

    samples = []
    try:
        db = Database(tenant_id=args.tenant)
        rows = db.read_sql_query(
            "SELECT TOP " + str(int(args.samples)) +
            " number, description, final_tag FROM dbo.parts "
            "WHERE tenant_id = :tenant_id AND final_tag IS NOT NULL "
            "ORDER BY part_id DESC;",
            params={"tenant_id": args.tenant},
        )
        samples = [
            {
                "part_number": str(r["number"]),
                "description": str(r.get("description", "") or ""),
                "final_url": str(r["final_tag"]),
            }
            for _, r in rows.iterrows()
        ]
    except Exception as e:
        print(f"warning: could not pull samples from db: {e}", file=sys.stderr)

    reporter = ReportBuilder(bucket=args.bucket)
    refs = reporter.write(summary, samples=samples, include_thumbnails=not args.no_thumbnails)
    print(json.dumps(refs, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
