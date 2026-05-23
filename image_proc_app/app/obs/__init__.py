"""Cross-cutting observability helpers.

Two responsibilities live here:

- ``log``  — JSON-per-line structured logger written to stdout, so the
  Docker ``awslogs`` driver can ship lines straight to CloudWatch Logs
  without re-parsing.
- ``metrics`` — a thin CloudWatch ``PutMetricData`` wrapper. Workers
  call it during a shard to emit a small set of business-level counters
  under namespace ``PartsImagePipeline``.

Both modules are import-safe: missing AWS credentials degrade to
no-ops (with a stderr warning) rather than crashing the worker.
"""
from .log import get_logger, bind  # noqa: F401
from .metrics import MetricsEmitter, NullMetricsEmitter  # noqa: F401
