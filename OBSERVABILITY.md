# Observability

This document covers the three observability surfaces shipped with the
pipeline: structured logs, custom metrics, and the per-run report.

## Structured logs

All three apps (`gui_app`, `scraper_app`, `image_proc_app`) write JSON
lines to stdout via `obs.log`. One line per log event, encoded as a
flat JSON object. Example:

```json
{"ts":"2026-05-22T14:31:07Z","level":"info","logger":"scraper.worker",
 "msg":"shard processing","customer":"acme-parts","shard":"chunk_3.csv",
 "s3_key":"search_jobs/chunk_3.csv"}
```

Fields:

- `ts`, `level`, `logger`, `msg` are always present.
- `customer` is set automatically if the `CUSTOMER` env var is exported
  (the Terraform user-data scripts set it from the customer prefix).
- Arbitrary key-value pairs from `log.info("event", **fields)` are
  emitted at the top level of the record.

The Docker `awslogs` driver is configured in the Terraform user-data
to ship every line straight to the CloudWatch log groups
`/parts-pipeline/<customer>/scraper` and
`/parts-pipeline/<customer>/image-proc`. No parsing happens on the
worker.

Tune verbosity with `LOG_LEVEL=DEBUG` per process.

## Custom metrics

Namespace: `PartsImagePipeline`. Dimensions on every metric: `Customer`,
`Stage` (`scraper` | `image_proc` | `operator`). Some metrics also
carry `Status` or `shard`.

| Metric                     | Emitted by   | Meaning                                                       |
| -------------------------- | ------------ | ------------------------------------------------------------- |
| `ShardStarted`             | both workers | A shard began processing.                                     |
| `ShardDone`                | both workers | A shard completed successfully.                               |
| `ShardFailed`              | both workers | A shard raised; will be retried via SQS.                      |
| `ShardSeconds`             | both workers | Wall-clock seconds for the shard (`Unit=Seconds`).            |
| `ShardsSkipped`            | both workers | The `.done` marker existed; the shard was a no-op.            |
| `ImagesDownloaded`         | scraper      | Candidate image written to S3.                                |
| `ImageFetchErrors`         | scraper      | Per-image download error (does not fail the shard).           |
| `ImagesFlagged`            | operator     | Classifier marked an image for deletion.                      |
| `ImagesAccepted`           | operator     | Classifier accepted an image.                                 |
| `ClassifierItemErrors`     | operator     | OpenAI returned an item-level error inside an otherwise OK batch. |
| `BatchesProcessed`         | operator     | OpenAI batch reached a terminal state (status as dimension).  |
| `BatchesUnusable`          | operator     | Batch finished in `failed`/`expired`/`cancelled`; classifier did not run for that batch. |
| `ImagesKept`               | image_proc   | One per part: the survivor after dedup.                       |
| `ImagesDiscardedByDedup`   | image_proc   | The `n-1` candidates dropped when collapsing a part's group.  |

Two of these have alarms wired in Terraform out of the box:

- `BatchesUnusable >= 1 / minute` → SNS alert. The classifier silently
  skipping a batch used to be the system's worst-case failure mode;
  now it pages.
- DLQ depth on either work queue (covered by the existing SQS alarms).

## Per-run report

At the end of `perform_filter`, the operator console writes two files
to S3:

- `s3://<bucket>/reports/<job_id>/report.json` — machine-readable run
  summary. Stable schema; feel free to consume from downstream tooling.
- `s3://<bucket>/reports/<job_id>/index.html` — single-file report
  with throughput numbers, unusable batches, optional thumbnails of
  representative results.

`job_id` is generated when the operator clicks "Search Images" and
takes the form `YYYYMMDDTHHMMSS-<8-char hex>`. It's persisted to
`data/state.json` so a GUI restart mid-run doesn't lose the trail.

The operator console returns a 5-day pre-signed link to the HTML; that
link is what gets sent to the customer.

### Regenerating a report

```
python -m run_report \
    --state data/state.json \
    --bucket <pipeline-bucket> \
    --samples 24
```

Pass `--no-thumbnails` for the link-only variant (some customers
prefer it for legal review).

### Limitations

The current implementation is post-hoc: it queries `dbo.parts` and
`dbo.part_tags` at report-build time, so the numbers reflect "rows
touched during this run window," not strict per-job attribution. For a
deployment that needs true per-job audit (cross-run overlap, multiple
operators, regulatory compliance), the next iteration is propagating
`job_id` through every SQS message and shard CSV. That refactor is
intentionally deferred until a customer asks for it.
