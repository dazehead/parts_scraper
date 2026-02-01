"""
s3_service.py
─────────────
Everything that touches S3 (and the local filesystem helpers that
go hand-in-hand with uploads/downloads).

Two classes, two jobs:

    S3Service          – raw S3 operations (upload, download, bulk-delete, list)
    S3DeleteHandler    – accumulates flagged-image keys during the detection
                         pipeline, then flushes them to S3 *and* cleans up
                         the matching rows in the database in one call.
"""

from __future__ import annotations

import os
import shutil
import stat
from typing import Dict, List

import boto3
import pandas as pd
from botocore.exceptions import NoCredentialsError


# ──────────────────────────────────────────────────────────────
# 1.  S3Service  (SRP: all S3 + local-fs I/O)
# ──────────────────────────────────────────────────────────────

# S3 delete_objects accepts at most 1 000 keys per call; stay under
# that with a comfortable margin.
_DELETE_CHUNK_LIMIT = 900


class S3Service:
    """
    Stateless wrapper around boto3 S3 operations.

    Responsibilities
    ────────────────
    • Upload a single file
    • Download a group of files
    • Bulk-delete an entire S3 prefix
    • Empty a local directory (used after uploads to reclaim disk)
    """

    def __init__(self, bucket: str, client=None) -> None:
        self._bucket = bucket
        self._s3     = client or boto3.client("s3")

    @property
    def bucket(self) -> str:
        return self._bucket

    # ── upload ──────────────────────────────────────────────

    def upload_file(
        self,
        local_path: str,
        s3_key: str,
        *,
        content_type: str = "image/png",
        delete_after: bool = True,
    ) -> None:
        """
        Upload *local_path* to *s3_key* inside self.bucket.
        Optionally remove the local file afterwards.
        """
        try:
            self._s3.upload_file(
                local_path,
                self._bucket,
                s3_key,
                ExtraArgs={"ContentType": content_type},
            )
            print(f"[S3Service] uploaded {local_path} → {s3_key}")

            if delete_after:
                os.remove(local_path)
                print(f"[S3Service] deleted local file {local_path}")

        except NoCredentialsError:
            print("[S3Service] AWS credentials not found. Did you run 'aws configure'?")
        except Exception as exc:
            print(f"[S3Service] upload failed: {exc}")

    # ── download ────────────────────────────────────────────

    def download_group(self, keys: List[str], local_base: str = "images") -> None:
        """
        Download every *key* in *keys* from the bucket.
        Each file lands at <local_base>/<key>.
        """
        for key in keys:
            dest = os.path.join(local_base, key)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            self._s3.download_file(self._bucket, f"images/{key}", dest)

    # ── bulk delete (prefix) ────────────────────────────────

    def empty_prefix(self, prefix: str) -> int:
        """
        Delete every object under *prefix* in the bucket.
        Returns the total number of objects deleted.
        """
        paginator  = self._s3.get_paginator("list_objects_v2")
        total_deleted = 0

        for page in paginator.paginate(Bucket=self._bucket, Prefix=prefix):
            contents = page.get("Contents")
            if not contents:
                continue

            objects = [{"Key": obj["Key"]} for obj in contents]

            for chunk in _chunk_list(objects, _DELETE_CHUNK_LIMIT):
                self._s3.delete_objects(
                    Bucket=self._bucket,
                    Delete={"Objects": chunk, "Quiet": True},  # ← was `objects` (bug)
                )
                total_deleted += len(chunk)

        return total_deleted

    # ── local filesystem ────────────────────────────────────

    @staticmethod
    def empty_dir(folder: str) -> None:
        """
        Remove every file and subdirectory inside *folder*,
        but leave *folder* itself intact.
        """
        if not (os.path.isdir(folder) and folder not in ("", "/", "\\")):
            raise ValueError(f"Refusing to operate on suspicious folder: {folder}")

        for entry in os.scandir(folder):
            try:
                if entry.is_file() or entry.is_symlink():
                    os.unlink(entry.path)
                else:
                    shutil.rmtree(entry.path, onerror=_on_rm_error)
            except FileNotFoundError:
                pass  # already gone


# ──────────────────────────────────────────────────────────────
# 2.  S3DeleteHandler  (SRP: accumulate → flush flagged images)
# ──────────────────────────────────────────────────────────────


class S3DeleteHandler:
    """
    Implements the ResultHandler protocol used by BatchWatermarkPipeline.

    Phase 1 – during the pipeline loop:
        pipeline calls handler.handle(result) for every DetectionResult.
        If the image was flagged, its S3 key is appended to delete_keys.

    Phase 2 – after the loop:
        pipeline calls handler.execute(db, s3) which:
            1. Sends the accumulated keys to S3 for deletion (chunked).
            2. Writes the corresponding URLs into a temp DB table.
            3. Deletes matching rows from dbo.part_tags.
            4. Drops the temp table.
    """

    def __init__(self) -> None:
        self.delete_keys: List[Dict[str, str]] = []

    # ── phase 1: accumulate ─────────────────────────────────

    def handle(self, result) -> None:
        """ResultHandler protocol — called once per DetectionResult."""
        if getattr(result, "has_watermark", False) and not getattr(result, "is_error", True):
            self.delete_keys.append({"Key": f"images/{result.filename}"})

    # ── phase 2: flush ──────────────────────────────────────

    def execute(self, db, s3: S3Service) -> None:
        """
        Push all accumulated deletes to S3, then reconcile the database.
        Safe to call even if delete_keys is empty (no-op).
        """
        if not self.delete_keys:
            print("[S3DeleteHandler] nothing to delete.")
            return

        base_url   = f"https://{s3.bucket}.s3.us-east-1.amazonaws.com/"
        all_urls: List[str] = []

        # 1. Delete from S3 in chunks
        for chunk in _chunk_list(self.delete_keys, _DELETE_CHUNK_LIMIT):
            s3._s3.delete_objects(
                Bucket=s3.bucket,
                Delete={"Objects": chunk, "Quiet": True},
            )
            all_urls.extend(f"{base_url}{obj['Key']}" for obj in chunk)
            print(f"[S3DeleteHandler] deleted {len(chunk)} objects from S3")

        # 2. Remove matching rows from dbo.part_tags via a temp table
        df_urls = pd.DataFrame({"tag_value": all_urls})
        db.create_table_if_not_exists("to_delete", df_urls)
        db.to_sql(df_urls, "to_delete", schema="dbo")

        db.execute_sql("""
            DELETE pt
            FROM   dbo.part_tags AS pt
            INNER JOIN dbo.to_delete AS td
                ON td.tag_value = pt.tag_value;
        """)
        db.execute_sql("DROP TABLE dbo.to_delete;")
        print(f"[S3DeleteHandler] removed {len(all_urls)} rows from part_tags")

        # 3. Reset so the handler can be reused
        self.delete_keys.clear()


# ──────────────────────────────────────────────────────────────
# Private helpers
# ──────────────────────────────────────────────────────────────


def _chunk_list(data: list, limit: int):
    """Yield successive *limit*-sized slices of *data*."""
    for i in range(0, len(data), limit):
        yield data[i : i + limit]


def _on_rm_error(func, path, exc_info):
    """shutil.rmtree error handler — makes read-only files writable (Windows)."""
    os.chmod(path, stat.S_IWRITE)
    func(path)