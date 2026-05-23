"""Per-run report builder.

At the end of an operator run we generate two artefacts under
``s3://<bucket>/reports/<job_id>/``:

- ``report.json``  — machine-readable summary (counts, durations,
  per-stage status, the list of batches that were unusable, etc.).
  Designed to be consumed by downstream tooling without any HTML
  parsing.
- ``index.html``  — single-file human report. Includes the same counts
  plus a sample of N before/after pairs so the operator can drop the
  link into an email to the customer.

We deliberately don't introduce a Jinja dependency. The HTML is
generated with ``str.format`` over a single template constant; the
amount of formatting we do here doesn't justify another package on the
worker.

The report is built ``post-hoc`` rather than from a propagated job_id:
it queries the existing ``dbo.parts`` and ``dbo.part_tags`` tables for
the rows that were touched during the run window, and combines that
with the metrics buffered by the operator console. This avoids changing
the worker contract; the trade-off is that we can't attribute outcomes
across overlapping runs. For a single-operator deployment this is
acceptable. For real multi-tenant audit trails, the next step is
threading a ``job_id`` through every SQS message and shard CSV.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import html
import json
import os
import uuid
from typing import Any

import boto3

from obs import get_logger
from tenancy import TenantPaths

_log = get_logger("operator.report")


@dataclasses.dataclass
class RunSummary:
    """Counters and timings the operator console accumulates during a run."""

    job_id: str
    customer: str
    tenant_id: str
    started_at: dt.datetime
    finished_at: dt.datetime | None = None
    csv_rows: int = 0
    parts_with_existing_image: int = 0
    parts_searched: int = 0
    candidates_downloaded: int = 0
    candidates_flagged: int = 0
    candidates_accepted: int = 0
    final_images_written: int = 0
    batches_total: int = 0
    batches_unusable: list[dict[str, str]] = dataclasses.field(default_factory=list)
    notes: list[str] = dataclasses.field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        d = dataclasses.asdict(self)
        d["started_at"] = self.started_at.isoformat()
        d["finished_at"] = self.finished_at.isoformat() if self.finished_at else None
        d["duration_seconds"] = (
            int((self.finished_at - self.started_at).total_seconds())
            if self.finished_at
            else None
        )
        return d


def new_job_id() -> str:
    """Stable, sortable job id: ``YYYYMMDDTHHMMSS-<shortuuid>``."""
    return f"{dt.datetime.now(dt.timezone.utc).replace(tzinfo=None).strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"


class ReportBuilder:
    """Render a run summary to S3 as JSON + HTML."""

    def __init__(self, bucket: str, region: str | None = None, s3=None):
        self.bucket = bucket
        self.region = region or os.getenv("AWS_REGION", "us-east-1")
        self.s3 = s3 or boto3.client("s3", region_name=self.region)

    def write(
        self,
        summary: RunSummary,
        samples: list[dict[str, str]] | None = None,
        include_thumbnails: bool = True,
    ) -> dict[str, str]:
        """Upload ``report.json`` and ``index.html`` for the run.

        The report lands under the tenant prefix (``tenants/<id>/reports/<job_id>/``)
        so a customer-facing pre-signed URL never leaks the existence
        of other tenants' artefacts.
        """
        if summary.finished_at is None:
            summary.finished_at = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)

        paths = TenantPaths(summary.tenant_id)
        json_key = paths.report_json_key(summary.job_id)
        html_key = paths.report_html_key(summary.job_id)

        json_body = json.dumps(summary.as_dict(), indent=2, ensure_ascii=False).encode("utf-8")
        self.s3.put_object(
            Bucket=self.bucket,
            Key=json_key,
            Body=json_body,
            ContentType="application/json",
        )

        html_body = self._render_html(summary, samples or [], include_thumbnails)
        self.s3.put_object(
            Bucket=self.bucket,
            Key=html_key,
            Body=html_body.encode("utf-8"),
            ContentType="text/html; charset=utf-8",
        )

        json_url = self._presign(json_key)
        html_url = self._presign(html_key)
        _log.info(
            "report written",
            job_id=summary.job_id,
            bucket=self.bucket,
            json_key=json_key,
            html_key=html_key,
        )
        return {
            "json_key": json_key,
            "html_key": html_key,
            "json_url": json_url,
            "html_url": html_url,
        }

    # ---- internals -----------------------------------------------------

    def _presign(self, key: str, expires: int = 3600 * 24 * 5) -> str:
        return self.s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": key},
            ExpiresIn=expires,
        )

    def _render_html(
        self,
        summary: RunSummary,
        samples: list[dict[str, str]],
        include_thumbnails: bool,
    ) -> str:
        s = summary
        duration = (
            int((s.finished_at - s.started_at).total_seconds())
            if s.finished_at
            else 0
        )
        h_minutes, h_seconds = divmod(duration, 60)
        h_hours, h_minutes = divmod(h_minutes, 60)
        duration_str = f"{h_hours:d}h {h_minutes:02d}m {h_seconds:02d}s"

        flagged_pct = (
            100 * s.candidates_flagged / s.candidates_downloaded
            if s.candidates_downloaded
            else 0
        )

        unusable_block = ""
        if s.batches_unusable:
            rows = "".join(
                "<tr><td><code>{}</code></td><td>{}</td></tr>".format(
                    html.escape(item.get("batch_id", "")),
                    html.escape(item.get("status", "")),
                )
                for item in s.batches_unusable
            )
            unusable_block = (
                "<section><h2>Unusable batches</h2>"
                "<p>These OpenAI batches finished in a non-OK terminal state and "
                "their items were <strong>not</strong> classified. Re-submit them "
                "before treating the run as complete.</p>"
                f"<table><thead><tr><th>Batch ID</th><th>Status</th></tr></thead>"
                f"<tbody>{rows}</tbody></table></section>"
            )

        notes_block = ""
        if s.notes:
            items = "".join(f"<li>{html.escape(n)}</li>" for n in s.notes)
            notes_block = f"<section><h2>Notes</h2><ul>{items}</ul></section>"

        sample_block = ""
        if samples and include_thumbnails:
            cards = "".join(
                (
                    "<figure>"
                    f"<img src=\"{html.escape(item['final_url'])}\" alt=\"\" loading=\"lazy\">"
                    f"<figcaption><strong>{html.escape(item.get('part_number',''))}</strong>"
                    f"<br>{html.escape(item.get('description',''))}</figcaption>"
                    "</figure>"
                )
                for item in samples
            )
            sample_block = f"<section><h2>Sample results ({len(samples)})</h2><div class=\"grid\">{cards}</div></section>"
        elif samples:
            rows = "".join(
                "<tr><td><code>{}</code></td><td>{}</td><td><a href=\"{}\">link</a></td></tr>".format(
                    html.escape(item.get("part_number", "")),
                    html.escape(item.get("description", "")),
                    html.escape(item["final_url"]),
                )
                for item in samples
            )
            sample_block = (
                "<section><h2>Sample results</h2>"
                "<table><thead><tr><th>Part</th><th>Description</th><th>Image</th></tr></thead>"
                f"<tbody>{rows}</tbody></table></section>"
            )

        return _HTML_TEMPLATE.format(
            customer=html.escape(s.customer),
            tenant_id=html.escape(s.tenant_id),
            job_id=html.escape(s.job_id),
            started_at=html.escape(s.started_at.strftime("%Y-%m-%d %H:%M UTC")),
            finished_at=html.escape(
                s.finished_at.strftime("%Y-%m-%d %H:%M UTC") if s.finished_at else "in progress"
            ),
            duration=duration_str,
            csv_rows=s.csv_rows,
            parts_searched=s.parts_searched,
            parts_with_existing_image=s.parts_with_existing_image,
            candidates_downloaded=s.candidates_downloaded,
            candidates_flagged=s.candidates_flagged,
            candidates_accepted=s.candidates_accepted,
            flagged_pct=f"{flagged_pct:.1f}",
            final_images_written=s.final_images_written,
            unusable_block=unusable_block,
            notes_block=notes_block,
            sample_block=sample_block,
        )


_HTML_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Parts pipeline run · {job_id}</title>
<style>
  :root {{
    --fg: #1c1f24;
    --muted: #5d6571;
    --line: #e4e7eb;
    --accent: #1f3aa6;
    --bg: #fafbfc;
  }}
  body {{ font: 15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
         color: var(--fg); background: var(--bg); margin: 0; padding: 32px; }}
  main {{ max-width: 960px; margin: 0 auto; background: #fff;
          border: 1px solid var(--line); border-radius: 6px;
          padding: 36px 40px; }}
  h1 {{ font-size: 22px; margin: 0 0 4px; }}
  header p {{ color: var(--muted); margin: 0; }}
  section {{ margin-top: 32px; }}
  h2 {{ font-size: 15px; text-transform: uppercase; letter-spacing: 0.05em;
        color: var(--muted); margin: 0 0 12px; font-weight: 600; }}
  table {{ border-collapse: collapse; width: 100%; font-size: 14px; }}
  th, td {{ text-align: left; padding: 8px 10px; border-bottom: 1px solid var(--line); }}
  th {{ font-weight: 600; color: var(--muted); }}
  dl.kv {{ display: grid; grid-template-columns: max-content 1fr; gap: 6px 24px; margin: 0; }}
  dl.kv dt {{ color: var(--muted); }}
  dl.kv dd {{ margin: 0; font-variant-numeric: tabular-nums; }}
  .grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); gap: 16px; }}
  figure {{ margin: 0; border: 1px solid var(--line); border-radius: 4px; padding: 8px; background: #fff; }}
  figure img {{ width: 100%; height: 140px; object-fit: contain; background: #f4f5f7; }}
  figcaption {{ font-size: 12px; color: var(--muted); margin-top: 6px; line-height: 1.4; }}
  code {{ font: 13px/1 ui-monospace, Menlo, Consolas, monospace; }}
  footer {{ margin-top: 36px; color: var(--muted); font-size: 12px; }}
</style>
</head>
<body>
<main>
  <header>
    <h1>Parts pipeline · run report</h1>
    <p>Customer <strong>{customer}</strong> · tenant <code>{tenant_id}</code> · job <code>{job_id}</code></p>
  </header>

  <section>
    <h2>Run window</h2>
    <dl class="kv">
      <dt>Started</dt><dd>{started_at}</dd>
      <dt>Finished</dt><dd>{finished_at}</dd>
      <dt>Duration</dt><dd>{duration}</dd>
    </dl>
  </section>

  <section>
    <h2>Throughput</h2>
    <dl class="kv">
      <dt>Rows in input CSV</dt><dd>{csv_rows}</dd>
      <dt>Parts already had an image</dt><dd>{parts_with_existing_image}</dd>
      <dt>Parts sent to search</dt><dd>{parts_searched}</dd>
      <dt>Candidate images downloaded</dt><dd>{candidates_downloaded}</dd>
      <dt>Flagged by classifier</dt><dd>{candidates_flagged} ({flagged_pct}%)</dd>
      <dt>Accepted by classifier</dt><dd>{candidates_accepted}</dd>
      <dt>Final images written</dt><dd>{final_images_written}</dd>
    </dl>
  </section>

  {unusable_block}
  {notes_block}
  {sample_block}

  <footer>
    Generated by parts-image-pipeline. Numbers reflect catalogue rows touched during the
    run window above; rows from earlier runs are not counted.
  </footer>
</main>
</body>
</html>
"""
