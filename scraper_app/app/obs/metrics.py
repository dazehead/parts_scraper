"""CloudWatch custom-metrics emitter.

Workers and the operator console use this to publish business-level
counters under namespace ``PartsImagePipeline``. Dimensions:

- ``Customer`` (taken from ``$CUSTOMER`` at construction, or passed in)
- ``Stage`` (e.g. ``scraper``, ``image_proc``, ``operator``)

Metric names are deliberately small and stable so downstream alarms
don't need to be rebuilt on every refactor:

============== =============================================================
Name           Meaning
============== =============================================================
ShardsStarted  A worker began processing a shard.
ShardsDone     A worker successfully completed a shard.
ShardsFailed   A worker raised during a shard (will be retried via SQS).
ShardSeconds   Wall-clock seconds taken to process one shard (Unit=Seconds).
ImagesDownloaded   Number of candidate images written to S3 during a shard.
ImagesFlagged  Number of images dropped by the watermark/mismatch classifier.
ImagesKept     Number of images kept after dedup (one per part).
============== =============================================================

The emitter batches calls (up to 20 metrics per PutMetricData, AWS
limit) and flushes on close/exit. On any AWS failure it logs and
continues — observability must never crash the actual work.
"""
from __future__ import annotations

import atexit
import os
import sys
import time
from contextlib import contextmanager
from typing import Iterable, Sequence

try:
    import boto3
    from botocore.exceptions import BotoCoreError, ClientError
    _HAS_BOTO = True
except Exception:  # pragma: no cover - boto3 should be present but be safe
    _HAS_BOTO = False

from .log import get_logger

_log = get_logger(__name__)

NAMESPACE = "PartsImagePipeline"
_BATCH_LIMIT = 20  # AWS PutMetricData hard limit per call.


class NullMetricsEmitter:
    """Drop-in replacement when CloudWatch isn't reachable or wanted (tests)."""

    def __init__(self, *args, **kwargs):
        pass

    def count(self, name: str, value: float = 1, **dims) -> None: ...
    def value(self, name: str, value: float, unit: str = "None", **dims) -> None: ...
    def flush(self) -> None: ...

    @contextmanager
    def timer(self, name: str, **dims):
        yield


class MetricsEmitter:
    """Buffered CloudWatch PutMetricData client."""

    def __init__(
        self,
        stage: str,
        customer: str | None = None,
        region: str | None = None,
        namespace: str = NAMESPACE,
        client=None,
    ):
        self._namespace = namespace
        self._customer = customer or os.getenv("CUSTOMER") or "unknown"
        self._stage = stage
        self._region = region or os.getenv("AWS_REGION", "us-east-1")
        self._buffer: list[dict] = []
        if client is not None:
            self._client = client
        elif _HAS_BOTO:
            try:
                self._client = boto3.client("cloudwatch", region_name=self._region)
            except Exception as e:  # pragma: no cover
                print(f"[metrics] could not build cloudwatch client: {e}", file=sys.stderr)
                self._client = None
        else:
            self._client = None
        atexit.register(self.flush)

    # ---- public API ----------------------------------------------------

    def count(self, name: str, value: float = 1, **dims) -> None:
        self._enqueue(name, value, unit="Count", extra_dims=dims)

    def value(self, name: str, value: float, unit: str = "None", **dims) -> None:
        self._enqueue(name, value, unit=unit, extra_dims=dims)

    @contextmanager
    def timer(self, name: str, **dims):
        """Time a block and emit ``name`` (seconds) and ``name``Started/Done/Failed counts."""
        start = time.monotonic()
        self.count(f"{name}Started", 1, **dims)
        try:
            yield
        except Exception:
            self.count(f"{name}Failed", 1, **dims)
            raise
        else:
            self.count(f"{name}Done", 1, **dims)
        finally:
            elapsed = time.monotonic() - start
            self.value(f"{name}Seconds", elapsed, unit="Seconds", **dims)

    def flush(self) -> None:
        if not self._buffer or self._client is None:
            self._buffer.clear()
            return
        # Drain in batches of up to 20 (AWS limit).
        pending, self._buffer = self._buffer, []
        for i in range(0, len(pending), _BATCH_LIMIT):
            chunk = pending[i : i + _BATCH_LIMIT]
            try:
                self._client.put_metric_data(
                    Namespace=self._namespace,
                    MetricData=chunk,
                )
            except Exception as e:
                # Observability must never crash production work. Catch
                # broadly: BotoCore/Client errors are expected, but the
                # AWS SDK has been known to raise unrelated exceptions
                # under credential rotation, IMDS hiccups, etc.
                _log.warning("metrics put_metric_data failed", error=str(e), batch_size=len(chunk))
                # Don't re-buffer — losing one batch is acceptable.

    # ---- internals -----------------------------------------------------

    def _enqueue(self, name: str, value: float, unit: str, extra_dims: dict) -> None:
        dims = [
            {"Name": "Customer", "Value": self._customer},
            {"Name": "Stage", "Value": self._stage},
        ]
        for k, v in extra_dims.items():
            if v is None:
                continue
            dims.append({"Name": str(k), "Value": str(v)})
        self._buffer.append(
            {
                "MetricName": name,
                "Dimensions": dims,
                "Value": float(value),
                "Unit": unit,
                "Timestamp": _now(),
            }
        )
        if len(self._buffer) >= _BATCH_LIMIT:
            self.flush()


def _now():
    # boto3 will accept any aware datetime or an iso string; epoch float is simpler.
    from datetime import datetime, timezone
    return datetime.now(tz=timezone.utc)


def build_emitter(stage: str) -> MetricsEmitter | NullMetricsEmitter:
    """Factory used by workers — returns a no-op emitter if explicitly disabled."""
    if os.getenv("METRICS_DISABLED", "").lower() in ("1", "true", "yes"):
        return NullMetricsEmitter()
    return MetricsEmitter(stage=stage)
