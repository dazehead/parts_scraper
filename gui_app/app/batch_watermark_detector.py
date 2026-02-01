"""
batch_watermark_detector.py
────────────────────────────
Detects watermarks / overlays / mismatches in product images via
OpenAI's Batch API.  All batches are fired in parallel and polled
together, so 200 k images finish in ~24 h instead of ~100 days.

SOLID breakdown
───────────────
SRP  → each class owns exactly one job (see docstrings).
OCP  → prompt & schema live in PromptBuilder; swap detection logic
       without touching anything else.
LSP  → URLReader / ResultHandler are thin protocols; any conforming
       implementation is a drop-in replacement.
ISP  → consumers depend only on the interface they need.
DIP  → high-level Pipeline receives collaborators through __init__;
       nothing is hard-wired.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Dict, List, Protocol, runtime_checkable

from openai import OpenAI


# ──────────────────────────────────────────────────────────────
# 1.  Data types  (plain, shared across the pipeline)
# ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class DetectionResult:
    """Immutable value object — one result per image."""

    filename: str
    has_watermark: bool
    confidence: str = "medium"          # high | medium | low
    watermark_type: str = "none"        # logo | text | pattern | overlay | none
    description: str = ""
    error: str | None = None            # non-None  → something went wrong

    @property
    def is_error(self) -> bool:
        return self.error is not None


@dataclass
class DetectionSummary:
    """Aggregate stats produced by SummaryReporter."""

    total: int = 0
    flagged: int = 0
    clean: int = 0
    errors: int = 0
    confidence_distribution: Dict[str, int] = field(default_factory=dict)
    type_distribution: Dict[str, int] = field(default_factory=dict)

    @property
    def flagged_pct(self) -> float:
        return (self.flagged / self.total * 100) if self.total else 0.0


# ──────────────────────────────────────────────────────────────
# 2.  Protocols  (the "interfaces" Python uses)
# ──────────────────────────────────────────────────────────────


@runtime_checkable
class URLReader(Protocol):
    """Anything that can hand us a list of image URLs."""

    def read_urls(self) -> List[str]: ...


@runtime_checkable
class ResultHandler(Protocol):
    """Anything that can consume a finished DetectionResult."""

    def handle(self, result: DetectionResult) -> None: ...


# ──────────────────────────────────────────────────────────────
# 3.  PromptBuilder  (SRP: owns the prompt & schema)
# ──────────────────────────────────────────────────────────────


class PromptBuilder:
    """
    Single place for the detection prompt and the JSON-schema
    that constrains the model's reply.  Change detection rules
    here; nothing else in the pipeline needs to know.
    """

    SYSTEM_TEXT = (
        "You are a strict quality-control classifier for product images. "
        "Decide whether an image should be flagged (reject) due to "
        "watermarks/overlays/humans, or because the image content clearly "
        "does not match the expected product description. "
        "Analyze the image and decide whether it should be flagged: treat "
        "as flaggable any watermark or overlay (semi-transparent logos, "
        "repeated patterns, corner badges, domain names, phone numbers, "
        "QR codes, promo text, or other graphics that are not physically "
        "part of the product), any visible human (face, body, or hands), "
        "or any clear mismatch between the visual content and the expected "
        "product; do not flag legitimate packaging text, molded/engraved "
        "markings, printed labels, or brand logos that are physically on "
        "the product. "
        "Use the second path segment after image/<partnumber>_<description>.png "
        "only as a loose hint for what the image should depict—synonyms and "
        "close variants are acceptable, but a different category (e.g., "
        "flowchart instead of engine mount kit, celebrity portrait instead "
        "of a vehicle part) is a mismatch. "
        "Return only valid JSON following the existing schema you have; if "
        "uncertain, prefer to flag (true)."
    )

    RESPONSE_FORMAT: Dict[str, Any] = {
        "type": "json_schema",
        "json_schema": {
            "name": "watermark_detection",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "has_watermark":   {"type": "boolean"},
                    "confidence":      {"type": "string", "enum": ["high", "medium", "low"]},
                    "watermark_type":  {"type": "string", "enum": ["logo", "text", "pattern", "overlay", "none"]},
                    "description":     {"type": "string"},
                },
                "required": ["has_watermark", "confidence", "watermark_type", "description"],
                "additionalProperties": False,
            },
        },
    }

    def build_messages(self, image_url: str) -> List[Dict[str, Any]]:
        """Return the messages list ready to drop into a chat request."""
        return [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": self.SYSTEM_TEXT},
                    {"type": "image_url", "image_url": {"url": image_url}},
                ],
            }
        ]


# ──────────────────────────────────────────────────────────────
# 4.  OpenAIBatchClient  (SRP: one job — talk to OpenAI Batch API)
# ──────────────────────────────────────────────────────────────


class OpenAIBatchClient:
    """
    Thin wrapper around the OpenAI SDK calls used by the pipeline.
    Swap this class (or mock it in tests) without changing anything else.
    """

    def __init__(self, client: OpenAI) -> None:
        self._client = client

    # ── upload ──────────────────────────────────────────────

    def upload_jsonl(self, path: str) -> str:
        """Upload a local JSONL file; return the file-id."""
        with open(path, "rb") as fh:
            resp = self._client.files.create(file=fh, purpose="batch")
        return resp.id

    # ── create batch ────────────────────────────────────────

    def create_batch(self, input_file_id: str, description: str) -> str:
        """Submit a batch job; return the batch-id."""
        batch = self._client.batches.create(
            input_file_id=input_file_id,
            endpoint="/v1/chat/completions",
            completion_window="24h",
            metadata={"description": description},
        )
        return batch.id

    # ── poll ────────────────────────────────────────────────

    def get_batch_status(self, batch_id: str) -> tuple[str, str | None]:
        """
        Return (status, output_file_id).
        output_file_id is None until the batch completes.
        """
        batch = self._client.batches.retrieve(batch_id)
        return batch.status, getattr(batch, "output_file_id", None)

    # ── download ────────────────────────────────────────────

    def download_output(self, output_file_id: str, dest_path: str) -> None:
        """Download the completed output JSONL to disk."""
        resp = self._client.files.content(output_file_id)
        data = getattr(resp, "text", None) or getattr(resp, "content", None)
        if data is None and hasattr(resp, "read"):
            data = resp.read()
        if isinstance(data, bytes):
            data = data.decode("utf-8")
        with open(dest_path, "w", encoding="utf-8") as fh:
            fh.write(data)


# ──────────────────────────────────────────────────────────────
# 5.  BatchRequestFactory  (SRP: builds the JSONL request list)
# ──────────────────────────────────────────────────────────────


class BatchRequestFactory:
    """
    Turns a list of S3 URLs into the list-of-dicts that
    OpenAI's Batch API expects, then writes them to a JSONL file.
    """

    def __init__(self, prompt_builder: PromptBuilder) -> None:
        self._prompt = prompt_builder

    # ── public ──────────────────────────────────────────────

    def build_and_write(self, urls: List[str], jsonl_path: str) -> int:
        """
        Write one JSONL file for *urls*.  Return the number of
        requests written.
        """
        with open(jsonl_path, "w", encoding="utf-8") as fh:
            for url in urls:
                req = self._make_request(url)
                fh.write(json.dumps(req) + "\n")
        return len(urls)

    # ── private ─────────────────────────────────────────────

    @staticmethod
    def _filename_from_url(url: str) -> str:
        return os.path.basename(urllib.parse.urlparse(url).path)

    def _make_request(self, url: str) -> Dict[str, Any]:
        return {
            "custom_id": self._filename_from_url(url),
            "method":    "POST",
            "url":       "/v1/chat/completions",
            "body": {
                "model":           "gpt-4o-mini",
                "service_tier":    "priority",
                "messages":        self._prompt.build_messages(url),
                "response_format": self._prompt.RESPONSE_FORMAT,
                "temperature":     0.1,
                "max_tokens":      200,
            },
        }


# ──────────────────────────────────────────────────────────────
# 6.  ResultParser  (SRP: parses one JSONL output file)
# ──────────────────────────────────────────────────────────────


class ResultParser:
    """
    Reads a completed-batch JSONL and yields DetectionResult objects.
    No side-effects; the caller decides what to do with each result.
    """

    @staticmethod
    def parse(jsonl_path: str) -> List[DetectionResult]:
        results: List[DetectionResult] = []
        with open(jsonl_path, "r", encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                try:
                    results.append(ResultParser._parse_line(line))
                except Exception as exc:                          # noqa: BLE001
                    print(f"[ResultParser] skipped malformed line: {exc}")
        return results

    # ── private ─────────────────────────────────────────────

    @staticmethod
    def _parse_line(line: str) -> DetectionResult:
        obj      = json.loads(line)
        filename = obj["custom_id"]
        resp     = obj.get("response", {})

        if resp.get("status_code") != 200:
            msg = resp.get("body", {}).get("error", {}).get("message", "Unknown error")
            return DetectionResult(filename=filename, has_watermark=False, error=msg)

        content = resp["body"]["choices"][0]["message"]["content"]
        data    = json.loads(content)
        return DetectionResult(
            filename=data.get("filename", filename),
            has_watermark=data["has_watermark"],
            confidence=data.get("confidence", "medium"),
            watermark_type=data.get("watermark_type", "none"),
            description=data.get("description", ""),
        )


# ──────────────────────────────────────────────────────────────
# 7.  SummaryReporter  (SRP: computes stats from results)
# ──────────────────────────────────────────────────────────────


class SummaryReporter:
    """
    Pure function wrapped in a class for consistency.
    Feed it results, get back a DetectionSummary.
    """

    @staticmethod
    def summarise(results: List[DetectionResult]) -> DetectionSummary:
        summary = DetectionSummary(total=len(results))
        for r in results:
            if r.is_error:
                summary.errors += 1
            elif r.has_watermark:
                summary.flagged += 1
                summary.confidence_distribution[r.confidence] = (
                    summary.confidence_distribution.get(r.confidence, 0) + 1
                )
                summary.type_distribution[r.watermark_type] = (
                    summary.type_distribution.get(r.watermark_type, 0) + 1
                )
            else:
                summary.clean += 1
        return summary


# ──────────────────────────────────────────────────────────────
# 8.  S3DeleteHandler  (SRP + ResultHandler protocol)
# ──────────────────────────────────────────────────────────────


class S3DeleteHandler:
    """
    Accumulates S3 delete-keys for every flagged image.
    Implements the ResultHandler protocol so the pipeline
    can call .handle() without knowing about S3.
    """

    def __init__(self) -> None:
        self.delete_keys: List[Dict[str, str]] = []

    def handle(self, result: DetectionResult) -> None:
        if result.has_watermark and not result.is_error:
            self.delete_keys.append({"Key": f"images/{result.filename}"})


# ──────────────────────────────────────────────────────────────
# 9.  DatabaseURLReader  (SRP + URLReader protocol)
# ──────────────────────────────────────────────────────────────


class DatabaseURLReader:
    """
    Reads image URLs from the database.
    Implements URLReader so the pipeline never imports a DB driver.
    """

    def __init__(self, db) -> None:
        self._db = db

    def read_urls(self) -> List[str]:
        return list(
            self._db.read_sql_query("SELECT tag_value FROM part_tags;")["tag_value"]
        )


# ──────────────────────────────────────────────────────────────
# 10. BatchOrchestrator  (THE bottleneck fix)
#     Submits ALL chunks in parallel, then polls them together
#     in a single async loop until every one is done.
# ──────────────────────────────────────────────────────────────

# Terminal states — stop polling once a batch reaches one of these.
_TERMINAL_STATUSES = {"completed", "failed", "expired", "cancelled", "cancelling"}

# How many images per OpenAI batch (stay under the 20 k-line limit).
DEFAULT_CHUNK_SIZE = 5_000


class BatchOrchestrator:
    """
    1. Splits URLs into chunks.
    2. Writes each chunk to its own JSONL via BatchRequestFactory.
    3. Uploads + submits every chunk (sequentially — the upload is
       fast; it's the *waiting* that was slow).
    4. Polls ALL batches together in one tight loop until every one
       reaches a terminal state.
    5. Downloads every completed output and parses it via ResultParser.

    Everything runs in a single thread; the "parallel" part is that
    OpenAI processes all submitted batches concurrently on their side,
    and we never block waiting for one before submitting the next.
    """

    def __init__(
        self,
        openai_client: OpenAIBatchClient,
        request_factory: BatchRequestFactory,
        parser: ResultParser,
        *,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        poll_interval: int = 30,       # seconds between status checks
        output_dir: str = "data/ai_sent_data",
    ) -> None:
        self._api            = openai_client
        self._factory        = request_factory
        self._parser         = parser
        self._chunk_size     = chunk_size
        self._poll_interval  = poll_interval
        self._output_dir     = output_dir
        os.makedirs(output_dir, exist_ok=True)

    # ── public entry-point ──────────────────────────────────

    def run(self, urls: List[str]) -> List[DetectionResult]:
        """
        Full pipeline: chunk → upload → submit → poll → download → parse.
        Returns the flat list of every DetectionResult.
        """
        chunks       = self._split(urls)
        batch_ids    = self._submit_all(chunks)
        output_files = self._poll_all(batch_ids)
        return self._download_and_parse(output_files)

    # ── private ─────────────────────────────────────────────

    @staticmethod
    def _split(urls: List[str]) -> List[List[str]]:
        """Cut urls into chunks no larger than chunk_size."""
        return [
            urls[i : i + DEFAULT_CHUNK_SIZE]
            for i in range(0, len(urls), DEFAULT_CHUNK_SIZE)
        ]

    def _submit_all(self, chunks: List[List[str]]) -> List[str]:
        """
        Write + upload + create every chunk.  Returns batch-ids in
        the same order as the chunks so we can match them later.
        """
        batch_ids: List[str] = []
        for idx, chunk in enumerate(chunks):
            jsonl_path = os.path.join(self._output_dir, f"batch_{idx}.jsonl")
            self._factory.build_and_write(chunk, jsonl_path)
            file_id  = self._api.upload_jsonl(jsonl_path)
            batch_id = self._api.create_batch(file_id, description=f"Watermark batch {idx}")
            batch_ids.append(batch_id)
            print(f"[Orchestrator] submitted chunk {idx}/{len(chunks)-1} "
                  f"— {len(chunk)} images — batch_id={batch_id}")
        return batch_ids

    def _poll_all(self, batch_ids: List[str]) -> Dict[str, str]:
        """
        Block until every batch_id is in a terminal state.
        Returns {batch_id: output_file_id} for completed batches.
        Failed / expired batches are logged and skipped.
        """
        pending   = set(batch_ids)
        outputs: Dict[str, str] = {}

        while pending:
            still_pending: set = set()
            for bid in pending:
                status, output_file_id = self._api.get_batch_status(bid)

                if status == "completed" and output_file_id:
                    outputs[bid] = output_file_id
                    print(f"[Orchestrator] batch {bid} → completed")
                elif status in _TERMINAL_STATUSES:
                    # failed / expired / cancelled — nothing to download
                    print(f"[Orchestrator] batch {bid} → {status} (skipped)")
                else:
                    still_pending.add(bid)

            pending = still_pending
            if pending:
                print(f"[Orchestrator] {len(pending)} batch(es) still running … "
                      f"sleeping {self._poll_interval}s")
                time.sleep(self._poll_interval)

        return outputs

    def _download_and_parse(self, outputs: Dict[str, str]) -> List[DetectionResult]:
        """Download every completed output file and parse it."""
        all_results: List[DetectionResult] = []
        for batch_id, output_file_id in outputs.items():
            dest = os.path.join(self._output_dir, f"output_{batch_id}.jsonl")
            self._api.download_output(output_file_id, dest)
            all_results.extend(self._parser.parse(dest))
            print(f"[Orchestrator] parsed {dest} → {len(all_results)} results so far")
        return all_results


# ──────────────────────────────────────────────────────────────
# 11. BatchWatermarkPipeline  (thin top-level wiring)
# ──────────────────────────────────────────────────────────────


class BatchWatermarkPipeline:
    """
    Composes every piece together.  This is the only place that
    knows about all the collaborators; everything else is decoupled.
    """

    def __init__(
        self,
        url_reader: URLReader,
        orchestrator: BatchOrchestrator,
        result_handlers: List[ResultHandler] | None = None,
    ) -> None:
        self._url_reader      = url_reader
        self._orchestrator    = orchestrator
        self._result_handlers = result_handlers or []

    def execute(self) -> DetectionSummary:
        """Run the full pipeline end-to-end; return the summary."""
        urls    = self._url_reader.read_urls()
        print(f"[Pipeline] loaded {len(urls)} URLs")

        results = self._orchestrator.run(urls)

        # Let every registered handler see every result
        for result in results:
            for handler in self._result_handlers:
                handler.handle(result)

        summary = SummaryReporter.summarise(results)
        self._print_summary(summary)
        return summary

    # ── private ─────────────────────────────────────────────

    @staticmethod
    def _print_summary(s: DetectionSummary) -> None:
        print("\n═══ Detection Summary ═══")
        print(f"  Total images   : {s.total}")
        print(f"  Flagged        : {s.flagged}  ({s.flagged_pct:.1f} %)")
        print(f"  Clean          : {s.clean}")
        print(f"  Errors         : {s.errors}")
        print(f"  Confidence dist: {s.confidence_distribution}")
        print(f"  Type dist      : {s.type_distribution}")


# ──────────────────────────────────────────────────────────────
# 12. Factory helper  (wires everything with sensible defaults)
# ──────────────────────────────────────────────────────────────


def build_pipeline(db, openai_api_key: str | None = None) -> BatchWatermarkPipeline:
    """
    One-liner to get a fully wired pipeline.
    Pass *db* (anything with .read_sql_query) and an optional API key.
    """
    openai_client   = OpenAI(api_key=openai_api_key)
    api             = OpenAIBatchClient(openai_client)
    prompt          = PromptBuilder()
    factory         = BatchRequestFactory(prompt)
    parser          = ResultParser()
    orchestrator    = BatchOrchestrator(api, factory, parser)
    url_reader      = DatabaseURLReader(db)
    s3_handler      = S3DeleteHandler()

    return BatchWatermarkPipeline(
        url_reader=url_reader,
        orchestrator=orchestrator,
        result_handlers=[s3_handler],
    )


# ──────────────────────────────────────────────────────────────
# Usage
# ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # `db` is whatever object your project already passes around.
    # Replace with your real DB connection.
    # db = ...

    # pipeline = build_pipeline(db)
    # summary  = pipeline.execute()
    print("Import build_pipeline and call pipeline.execute() to run.")