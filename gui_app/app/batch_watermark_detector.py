# batch_watermark_detector.py
import json
import os
import urllib.parse
from openai import OpenAI
import boto3
from typing import Dict, List

from obs import get_logger
from obs.metrics import build_emitter


_log = get_logger("operator.watermark")
_metrics = build_emitter(stage="operator")


# Terminal OpenAI batch statuses that mean "no usable output for this batch".
_NON_OK_TERMINAL = {"failed", "expired", "cancelled", "cancelling"}


class BatchUnusableError(RuntimeError):
    """Raised when an OpenAI batch finished in a state that produced no output.

    Previously the operator console logged this and moved on, silently
    dropping every candidate image in the batch. That meant a watermarked
    image could ship to the customer without the classifier ever running.
    Surface it instead so the operator can re-submit.
    """


class BatchWatermarkDetector:
    def __init__(self, db, openai_api_key=None):
        self.client = OpenAI(api_key=openai_api_key)
        self.s3 = boto3.client("s3")
        self.poll_interval = 30
        self.backoff_max = 300
        self.db = db

    def get_urls_from_db(self, tenant_id=None):
        """Return every candidate URL for ``tenant_id``.

        ``tenant_id`` is required in the multi-tenant pipeline; an
        unscoped call returns every tenant's URLs, which we never want
        to feed into one OpenAI batch. We keep the unscoped form
        available for legacy single-tenant scripts but log loudly.
        """
        if tenant_id:
            df = self.db.read_sql_query(
                "SELECT tag_value FROM dbo.part_tags WHERE tenant_id = :tenant_id;",
                params={"tenant_id": tenant_id},
            )
        else:
            _log.warning("get_urls_from_db called without tenant_id; returning all rows")
            df = self.db.read_sql_query("SELECT tag_value FROM dbo.part_tags;")
        return df['tag_value']

    
    def basename_from_url(self, url: str) -> str:
        """Extract filename from S3 URL"""
        return os.path.basename(urllib.parse.urlparse(url).path)
    
    def create_batch_requests(self, urls: List[str]) -> List[dict]:
        """Create OpenAI batch API requests"""
        requests = []
        for url in urls:
            filename = self.basename_from_url(url)
            request = {
                "custom_id": filename,
                "method": "POST",
                "url": "/v1/chat/completions",  # Fixed: was "/v1/responses"
                "body": {
                    "model": "gpt-4o-mini",
                    "service_tier": "priority",
                    "messages": [{
                        "role": "user",
                        "content": [
                            {
                                "type": "text", 
                                "text": (
                                        "You are a strict quality-control classifier for product images."
                                        "Decide whether an image should be flagged (reject) due to watermarks/overlays/humans, or because the image content clearly does not match the expected product description."
                                        "Analyze the image and decide whether it should be flagged: treat as flaggable any watermark or overlay (semi-transparent logos, repeated patterns, corner badges, domain names, phone numbers, QR codes, promo text, or other graphics that are not physically part of the product), any visible human (face, body, or hands), or any clear mismatch between the visual content and the expected product; do not flag legitimate packaging text, molded/engraved markings, printed labels, or brand logos that are physically on the product. "
                                        "Use the second path segment after image/<partnumber>_<description>.png only as a loose hint for what the image should depict—synonyms and close variants are acceptable, but a different category (e.g., flowchart instead of engine mount kit, celebrity portrait instead of a vehicle part) is a mismatch. "
                                        "Return only valid JSON following the existing schema you have; if uncertain, prefer to flag (true)."
                                )
                            },
                            {
                                "type": "image_url",
                                "image_url": {"url": url}
                            }
                        ]
                    }],
                    "response_format": {
                        "type": "json_schema",
                        "json_schema": {
                            "name": "watermark_detection",
                            "strict": True,
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "has_watermark": {"type": "boolean"},
                                    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                                    "watermark_type": {"type": "string", "enum": ["logo", "text", "pattern", "overlay", "none"]},
                                    "description": {"type": "string"}
                                },
                                "required": ["has_watermark", "confidence", "watermark_type", "description"],
                                "additionalProperties": False
                            }
                        }
                    },
                    "temperature": 0.1,
                    "max_tokens": 200
                }
            }
            requests.append(request)
        
        return requests
    
    def submit_batch(self, requests: List[dict], batch_num: str, tenant_id: str = None) -> str:
        """Submit batch to OpenAI API.

        ``tenant_id`` is stamped into the OpenAI batch metadata so a
        later operator (or a post-mortem on a leaked batch id) can
        attribute the batch to a customer.
        """
        jsonl_dir = "data/ai_sent_data"
        os.makedirs(jsonl_dir, exist_ok=True)
        jsonl_path = f"{jsonl_dir}/batch_{batch_num}.jsonl"
        with open(jsonl_path, "w", encoding="utf-8") as f:
            for request in requests:
                f.write(json.dumps(request) + "\n")

        with open(jsonl_path, "rb") as f:
            upload_file = self.client.files.create(file=f, purpose="batch")

        metadata = {"description": f"Watermark detection batch {batch_num}"}
        if tenant_id:
            metadata["tenant_id"] = tenant_id

        batch = self.client.batches.create(
            input_file_id=upload_file.id,
            endpoint="/v1/chat/completions",
            completion_window="24h",
            metadata=metadata,
        )

        _log.info(
            "openai batch submitted",
            batch_id=batch.id,
            tenant_id=tenant_id,
            requests=len(requests),
        )
        return batch.id
    

    def poll_multiple_batch_completion(self, batch_id):
        
        batch = self.client.batches.retrieve(batch_id)
        if batch.status not in ["completed", "failed", "expired", "cancelling", "cancelled"]:
            return False, batch.status
        return True, batch.status

        
    
    def download_results(self, output_file_id: str, output_path: str):
        """Download batch results file_id -> write JSONL to disk"""
        resp = self.client.files.content(output_file_id)
        # SDK returns a response-like object; some versions expose .text, others .content/read()
        data = getattr(resp, "text", None)
        if data is None:
            # fall back to bytes
            data = getattr(resp, "content", None)
        if data is None and hasattr(resp, "read"):
            data = resp.read()
        # ensure str
        if isinstance(data, bytes):
            data = data.decode("utf-8")
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(data)
    
    def parse_results(self, jsonl_path: str, tenant_id: str = None) -> Dict[str, dict]:
        """Parse batch results into dictionary.

        ``tenant_id`` is required when the calling pipeline is
        multi-tenant: the flagged-image S3 keys are stamped with the
        tenant prefix so the deletion at the end of the watermark stage
        cannot accidentally delete another tenant's image.
        """
        from tenancy import TenantPaths
        paths = TenantPaths(tenant_id) if tenant_id else None
        results = {}

        with open(jsonl_path, "r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue

                try:
                    obj = json.loads(line)
                    filename = obj["custom_id"]

                    # Handle successful responses
                    if obj.get("response", {}).get("status_code") == 200:
                        content = obj["response"]["body"]["choices"][0]["message"]["content"]
                        watermark_data = json.loads(content)
                        results[filename] = {
                            "status": "success",
                            "has_watermark": watermark_data["has_watermark"],
                            "confidence": watermark_data.get("confidence", "medium"),
                            "watermark_type": watermark_data.get("watermark_type", "none"),
                            "description": watermark_data.get("description", "")
                        }

                        if watermark_data.get('has_watermark'):
                            if paths is not None:
                                delete_key = paths.image_key(filename)
                            else:
                                delete_key = f"images/{filename}"
                            self.db.delete_keys.append({'Key': delete_key})
                            _metrics.count("ImagesFlagged", Tenant=tenant_id or "unknown")
                        else:
                            _metrics.count("ImagesAccepted")

                    else:
                        # Per-item error inside an otherwise OK batch.
                        results[filename] = {
                            "status": "error",
                            "has_watermark": False,  # Default to no watermark on error
                            "error": obj.get("response", {}).get("body", {}).get("error", {}).get("message", "Unknown error")
                        }
                        _metrics.count("ClassifierItemErrors")
                        _log.warning(
                            "classifier item error",
                            filename=filename,
                            error=results[filename]["error"],
                        )

                except Exception as e:
                    _log.warning("could not parse classifier result line", error=str(e))
                    continue

        return results
    
    def get_watermark_summary(self, results: Dict[str, dict]) -> dict:
        """Get summary statistics of watermark detection"""
        total = len(results)
        with_watermarks = sum(1 for r in results.values() if r.get("has_watermark", False))
        errors = sum(1 for r in results.values() if r.get("status") == "error")
        
        confidence_counts = {}
        type_counts = {}
        
        for result in results.values():
            if result.get("status") == "success":
                conf = result.get("confidence", "unknown")
                wm_type = result.get("watermark_type", "unknown")
                confidence_counts[conf] = confidence_counts.get(conf, 0) + 1
                type_counts[wm_type] = type_counts.get(wm_type, 0) + 1
        
        return {
            "total_images": total,
            "images_with_watermarks": with_watermarks,
            "images_without_watermarks": total - with_watermarks - errors,
            "errors": errors,
            "watermark_percentage": (with_watermarks / total * 100) if total > 0 else 0,
            "confidence_distribution": confidence_counts,
            "watermark_type_distribution": type_counts
        }


# Usage example
if __name__ == "__main__":
    # Initialize detector
    detector = BatchWatermarkDetector()
    
    # Process all images in S3 bucket
    results = detector.process_images_batch(os.environ["BUCKET"], "images/")
    
    # Get summary
    summary = detector.get_watermark_summary(results)
    print("\nSummary:")
    print(f"Total images: {summary['total_images']}")
    print(f"With watermarks: {summary['images_with_watermarks']} ({summary['watermark_percentage']:.1f}%)")
    print(f"Without watermarks: {summary['images_without_watermarks']}")
    print(f"Errors: {summary['errors']}")
    print(f"Confidence distribution: {summary['confidence_distribution']}")
    print(f"Watermark types: {summary['watermark_type_distribution']}")