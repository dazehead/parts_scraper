import io
from math import ceil
import boto3
import json
import os
import sys
from dotenv import load_dotenv
load_dotenv()
from urllib.parse import quote

from obs import get_logger
from obs.metrics import build_emitter
from tenancy import TenantPaths, envelope
from tenancy.ids import validate_tenant_id
from batch_watermark_detector import BatchUnusableError, _NON_OK_TERMINAL


_log = get_logger("operator.helper")
_metrics = build_emitter(stage="operator")


class Helper:
    def __init__(self, db, detector, tenant_id=None):
        self.db = db
        self.region = os.getenv("AWS_REGION", "us-east-1")
        self.bucket = os.getenv("BUCKET")
        self.sqs = boto3.client("sqs", region_name=self.region)
        self.ec2 = boto3.client("ec2", region_name=self.region)
        self.detector = detector
        self.max_batch_size = 40000
        self.tenant_id = validate_tenant_id(tenant_id) if tenant_id else None
        self.paths = TenantPaths(self.tenant_id) if self.tenant_id else None

    def set_tenant(self, tenant_id):
        """Switch the operator's active tenant. GUI calls this when the
        user picks a different value from the tenant dropdown."""
        self.tenant_id = validate_tenant_id(tenant_id)
        self.paths = TenantPaths(self.tenant_id)
        return self.tenant_id

    def _require_tenant(self):
        if not self.tenant_id:
            raise RuntimeError(
                "Helper has no active tenant_id; call set_tenant() before submitting work"
            )


    def _chunk_key(self, logical_prefix, i):
        """Build the tenant-scoped S3 key for chunk ``i`` under ``logical_prefix``.

        ``logical_prefix`` is the bare prefix used historically
        (``search_jobs`` / ``proc_jobs``); we route it through
        :class:`TenantPaths` so the key sits under tenants/<id>/.
        """
        self._require_tenant()
        base = self.paths.prefix(logical_prefix.strip('/'))
        return f"{base}/chunk_{i}.csv"

    def split_data_and_upload_jobs(self, df, bucket, prefix, chunk_size, testing=False):
        """Function for Image Search"""
        self._require_tenant()
        n = len(df)
        num_chunks = ceil(n / chunk_size) if n else 0
        if num_chunks == 0:
            _log.info("dataframe empty, nothing to upload")
            return

        for i in range(num_chunks):
            start = i * chunk_size
            stop = min(start + chunk_size, n)
            chunk = df.iloc[start:stop]
            if testing:
                chunk.to_csv('data/test_data/test_upload.csv', header=False, index=False)
                sys.exit()

            csv_buf = io.StringIO()
            chunk.to_csv(csv_buf, index=False, header=False)
            data_bytes = csv_buf.getvalue().encode("utf-8")

            chunk_key = self._chunk_key(prefix, i + 1)
            self.db.s3.put_object(
                Body=data_bytes,
                Bucket=bucket,
                Key=chunk_key,
                ContentType='text/csv',
            )

        return num_chunks

    def split_group_upload(self, df, bucket, prefix, chunk_size):
        """Function for Image Proc"""
        self._require_tenant()

        def _process_dataframe(df, iteration):
            csv_buf = io.StringIO()
            df.to_csv(csv_buf, index=False, header=False)
            data_bytes = csv_buf.getvalue().encode('utf-8')

            chunk_key = self._chunk_key(prefix, iteration + 1)
            self.db.s3.put_object(
                Body=data_bytes,
                Bucket=bucket,
                Key=chunk_key,
                ContentType='text/csv',
            )
            return 1
        
        def _split(value):
            return value.split('images/')[-1].split('_')[:-1][0]

        num_df = df['tag_value'].apply(_split)
        end_idxs = num_df.index[num_df.ne(num_df.shift(-1))].tolist()
        
        displacement = None
        start = None
        stop = None
        num_chunks = 0

        for i, idx in enumerate(end_idxs):
            if start is None:
                start = i
                continue

            elif stop is None:  # first chunk
                if idx >= chunk_size:
                    stop = idx
                    chunk = df.iloc[start:stop+1]
                    start = stop + 1
                    displacement = stop
                    num_chunks += _process_dataframe(chunk, num_chunks)
                continue

            else:
                displaced_idx = idx - displacement
                if end_idxs[-1] == idx:  # last chunk
                    chunk = df.iloc[start: idx+1]
                    num_chunks += _process_dataframe(chunk, num_chunks)
                    return num_chunks

                elif displaced_idx >= chunk_size:  # all middle chunks
                    stop = idx
                    chunk = df.iloc[start:stop+1]
                    start = stop + 1
                    displacement = stop
                    num_chunks += _process_dataframe(chunk, num_chunks)
    


    def send_chunk_messages(self, job_id: str, queue_url: str, num_chunks: int, key: str):
        """Send SQS messages for each chunk file as a tenancy envelope.

        ``key`` is the logical prefix (``search_jobs`` / ``proc_jobs``);
        the helper resolves it to the tenant-scoped S3 path so we never
        send a worker a non-scoped key.
        """
        self._require_tenant()
        for i in range(1, num_chunks + 1):
            s3_key = self._chunk_key(key, i)
            body = envelope(
                tenant_id=self.tenant_id,
                s3_key=s3_key,
                job_id=job_id,
            )
            _log.info(
                "enqueuing shard",
                tenant_id=self.tenant_id,
                job_id=job_id,
                s3_key=s3_key,
                queue_url=queue_url,
            )
            self.sqs.send_message(QueueUrl=queue_url, MessageBody=body)



    def determine_instance_state(self):
        """
        Iterate through all EC2 instances and checks if they are all shutdown yet
        returns state as well to show on GUI.
        """

        paginator = self.ec2.get_paginator("describe_instances")
        kwargs = {}
        kwargs["Filters"] = [{
            "Name": "instance-state-name",
            "Values": ["pending", "running", "stopping", "stopped", "shutting-down", "terminated"]
        }]

        for page in paginator.paginate(**kwargs):
            for res in page.get("Reservations", []):
                for inst in res.get("Instances", []):
                    iid = inst["InstanceId"]
                    state = inst["State"]["Name"]   # <- this is the plain string
                    if state not in ['terminated', 'shutting-down']:
                        return False,state
        return True, 'Terminated'


    def organize_and_submit_batch(self):
        self._require_tenant()
        all_urls = self.detector.get_urls_from_db(tenant_id=self.tenant_id)
        encoded_urls = []

        base = (
            f"https://{self.bucket}.s3.{self.region}.amazonaws.com/"
            f"{self.paths.prefix('images')}/"
        )
        for url in all_urls:
            splits = url.split(base)[-1]
            key = splits.split('.png')[0]
            encoded_urls.append(f"{base}{quote(key, safe='/%-_.()~')}.png")

        n = len(encoded_urls)
        num_chunks = ceil(n / self.max_batch_size) if n else 0
        if num_chunks == 0:
            _log.info("no candidate images for tenant", tenant_id=self.tenant_id)
            return
        all_batch_ids = []
        for i in range(num_chunks):
            start = i * self.max_batch_size
            stop = min(start + self.max_batch_size, n)
            batch = encoded_urls[start:stop]

            request = self.detector.create_batch_requests(batch)
            batch_id = self.detector.submit_batch(
                requests=request,
                batch_num=i,
                tenant_id=self.tenant_id,
            )
            all_batch_ids.append(batch_id)

        return all_batch_ids
    
    def parse_ai_results(self, batch_ids):
        """Pull results from each completed OpenAI batch.

        A batch can finish in a non-OK terminal status (``failed``,
        ``expired``, ``cancelled``). The previous version logged that and
        moved on, silently dropping every image in that batch — watermarked
        candidates could ship to the customer without ever being classified.

        We now refuse to silently skip those batches. The operator gets a
        ``BatchUnusableError`` per affected batch with the batch_id and
        terminal status; the caller can choose to re-submit only those
        batches without redoing the whole watermark stage.
        """
        unusable: list[tuple[str, str]] = []  # (batch_id, status)

        for batch_id in batch_ids:
            batch = self.detector.client.batches.retrieve(batch_id)
            status = getattr(batch, "status", "unknown")
            _metrics.count("BatchesProcessed", Status=status)

            if status in _NON_OK_TERMINAL:
                _log.error(
                    "openai batch ended in non-ok terminal state",
                    batch_id=batch_id,
                    status=status,
                )
                # Pull the error file if there is one so we can include it
                # in the report and so the operator can post-mortem.
                if getattr(batch, "error_file_id", None):
                    err_path = f"data/raw_ai_output/{batch_id}_errors.jsonl"
                    os.makedirs(os.path.dirname(err_path), exist_ok=True)
                    try:
                        self.detector.download_results(batch.error_file_id, err_path)
                        _log.info(
                            "saved batch error file",
                            batch_id=batch_id,
                            path=err_path,
                        )
                    except Exception as e:
                        _log.warning(
                            "could not download batch error file",
                            batch_id=batch_id,
                            error=str(e),
                        )
                unusable.append((batch_id, status))
                continue

            if not getattr(batch, "output_file_id", None):
                # Completed but somehow no output. Treat as unusable too.
                _log.error(
                    "completed batch has no output_file_id",
                    batch_id=batch_id,
                )
                unusable.append((batch_id, "no_output"))
                continue

            output_path = f"data/raw_ai_output/{batch_id}_output.jsonl"
            os.makedirs(os.path.dirname(output_path), exist_ok=True)
            self.detector.download_results(
                output_file_id=batch.output_file_id,
                output_path=output_path,
            )

            results = self.detector.parse_results(output_path, tenant_id=self.tenant_id)

            json_dir = "data/ai_output"
            os.makedirs(json_dir, exist_ok=True)
            with open(f"{json_dir}/{batch_id}_output.json", "w", encoding="utf-8") as f:
                json.dump(results, f, indent=2)

            _log.info(
                "openai batch parsed",
                batch_id=batch_id,
                items=len(results),
            )

        if unusable:
            for batch_id, status in unusable:
                _metrics.count("BatchesUnusable", Status=status)
            raise BatchUnusableError(
                "OpenAI batches finished unusable; re-submit before proceeding: "
                + ", ".join(f"{bid}({status})" for bid, status in unusable)
            )






