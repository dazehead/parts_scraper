import io
from math import ceil
import boto3
import json
import os
import sys
from dotenv import load_dotenv
load_dotenv()
from urllib.parse import quote

from s3_service import S3Service
from batch_watermark_detector import (
    OpenAIBatchClient,
    BatchRequestFactory,
    PromptBuilder,
    ResultParser,
    DatabaseURLReader,
)


class Helper:
    def __init__(self, db, s3: S3Service, openai_client: OpenAIBatchClient):
        self.db              = db
        self.s3              = s3
        self.openai_client   = openai_client
        self.sqs             = boto3.client("sqs", region_name='us-east-1')
        self.ec2             = boto3.client("ec2")
        self.request_factory = BatchRequestFactory(PromptBuilder())
        self.parser          = ResultParser()
        self.url_reader      = DatabaseURLReader(db)
        self.max_batch_size  = 40000


    def split_data_and_upload_jobs(self, df, bucket, prefix, chunk_size, testing=False):
        """Function for Image Search"""
        n = len(df)
        num_chunks = ceil(n / chunk_size) if n else 0
        if num_chunks == 0:
            print("Dataframe is empty, nothing to upload.")
            return
        
        base_prefix = prefix.rstrip('/')
        
        for i in range(num_chunks):
            start = i * chunk_size
            stop  = min(start + chunk_size, n)
            chunk = df.iloc[start:stop]
            if testing:
                chunk.to_csv('data/test_data/test_upload.csv', header=False, index=False)
                sys.exit()
            else:
                csv_buf    = io.StringIO()
                chunk.to_csv(csv_buf, index=False, header=False)
                data_bytes = csv_buf.getvalue().encode("utf-8")
                chunk_key  = f"{base_prefix}/chunk_{i+1}.csv"

                self.s3._s3.put_object(
                    Body=data_bytes,
                    Bucket=bucket,
                    Key=chunk_key,
                    ContentType='text/csv'
                )

        return num_chunks
    
    def split_group_upload(self, df, bucket, prefix, chunk_size):
        """Function for Image Proc"""

        def _process_dataframe(df, bucket, prefix, iteration):
            base_prefix = prefix.rstrip('/')
            chunk      = df
            csv_buf    = io.StringIO()
            chunk.to_csv(csv_buf, index=False, header=False)
            data_bytes = csv_buf.getvalue().encode('utf-8')
            chunk_key  = f"{base_prefix}/chunk_{iteration+1}.csv"

            self.s3._s3.put_object(
                Body=data_bytes,
                Bucket=bucket,
                Key=chunk_key,
                ContentType='text/csv'
            )
            return 1
        
        def _split(value):
            return value.split('images/')[-1].split('_')[:-1][0]

        num_df    = df['tag_value'].apply(_split)
        end_idxs  = num_df.index[num_df.ne(num_df.shift(-1))].tolist()
        
        displacement = None
        start        = None
        stop         = None
        num_chunks   = 0

        for i, idx in enumerate(end_idxs):
            if start is None:
                start = i
                continue

            elif stop is None: # first chunk
                if idx >= chunk_size:
                    stop = idx
                    chunk = df.iloc[start:stop+1]
                    start = stop + 1
                    displacement = stop
                    num_chunks += _process_dataframe(chunk, bucket, prefix, num_chunks)
                continue

            else: 
                displaced_idx = idx - displacement
                if end_idxs[-1] == idx: # last chunk
                    chunk = df.iloc[start: idx+1]
                    num_chunks += _process_dataframe(chunk, bucket, prefix, num_chunks)
                    return num_chunks

                elif displaced_idx >= chunk_size: # all middle chunks
                    stop = idx
                    chunk = df.iloc[start:stop+1]
                    start = stop + 1
                    displacement = stop
                    num_chunks += _process_dataframe(chunk, bucket, prefix, num_chunks)


    def send_chunk_messages(self, job_id: str, queue_url: str, num_chunks: int, key: str):
        """
        Send SQS messages for each chunk file.
        """
        for i in range(1, num_chunks + 1):
            s3_key = f"{key}/chunk_{i}.csv"
            message_body = {
                "job_id": job_id,
                "s3_key": s3_key
            }

            print(f"Sending message for {s3_key}")
            self.sqs.send_message(
                QueueUrl=queue_url,
                MessageBody=json.dumps(message_body)
            )


    def determine_instance_state(self):
        """
        Iterate through all EC2 instances and checks if they are all shutdown yet.
        Returns (all_terminated: bool, state_string: str).
        """
        paginator = self.ec2.get_paginator("describe_instances")
        kwargs = {
            "Filters": [{
                "Name": "instance-state-name",
                "Values": ["pending", "running", "stopping", "stopped", "shutting-down", "terminated"]
            }]
        }

        for page in paginator.paginate(**kwargs):
            for res in page.get("Reservations", []):
                for inst in res.get("Instances", []):
                    state = inst["State"]["Name"]
                    if state not in ['terminated', 'shutting-down']:
                        return False, state
        return True, 'Terminated'


    def organize_and_submit_batch(self):
        """
        Read URLs from DB, URL-encode them, chunk into OpenAI batches,
        write JSONL + upload + submit each one.  Returns list of batch_ids.
        """
        all_urls = self.url_reader.read_urls()
        encoded_urls = []

        base = f"https://{self.s3.bucket}.s3.us-east-1.amazonaws.com/images/"
        for url in all_urls:
            key = url.split(base)[-1].split('.png')[0]
            encoded_urls.append(f"{base}{quote(key, safe='/%-_.()~')}.png")

        n = len(encoded_urls)
        num_chunks = ceil(n / self.max_batch_size) if n else 0
        if num_chunks == 0:
            print("No URLs to process.")
            return []

        os.makedirs("data/ai_sent_data", exist_ok=True)

        all_batch_ids = []
        for i in range(num_chunks):
            start = i * self.max_batch_size
            stop  = min(start + self.max_batch_size, n)
            batch_urls = encoded_urls[start:stop]

            # write JSONL via the factory
            jsonl_path = f"data/ai_sent_data/batch_{i}.jsonl"
            self.request_factory.build_and_write(batch_urls, jsonl_path)

            # upload + create batch via the OpenAI client wrapper
            file_id  = self.openai_client.upload_jsonl(jsonl_path)
            batch_id = self.openai_client.create_batch(file_id, description=f"Watermark batch {i}")
            all_batch_ids.append(batch_id)
            print(f"[Helper] submitted batch {i} — {len(batch_urls)} URLs — batch_id={batch_id}")

        return all_batch_ids

    def poll_batch(self, batch_id: str):
        """
        Single poll tick for one batch.
        Returns (is_terminal: bool, status: str)
        """
        status, _ = self.openai_client.get_batch_status(batch_id)
        terminal  = status in {"completed", "failed", "expired", "cancelled", "cancelling"}
        return terminal, status

    def parse_ai_results(self, batch_ids):
        """
        Download + parse completed batch outputs.  Returns the merged
        list of DetectionResult objects across all batches.
        """
        os.makedirs("data/raw_ai_output", exist_ok=True)
        os.makedirs("data/ai_output",     exist_ok=True)

        all_results = []
        for batch_id in batch_ids:
            status, output_file_id = self.openai_client.get_batch_status(batch_id)

            if not output_file_id:
                print(f"[Helper] batch {batch_id} completed but no output_file_id (status={status}). Skipping.")
                continue

            raw_path    = f"data/raw_ai_output/{batch_id}_output.jsonl"
            self.openai_client.download_output(output_file_id, raw_path)

            results = self.parser.parse(raw_path)
            all_results.extend(results)

            # also dump a JSON copy for manual inspection
            with open(f"data/ai_output/{batch_id}_output.json", "w", encoding="utf-8") as f:
                json.dump([{
                    "filename":       r.filename,
                    "has_watermark":  r.has_watermark,
                    "confidence":     r.confidence,
                    "watermark_type": r.watermark_type,
                    "description":    r.description,
                    "error":          r.error,
                } for r in results], f, indent=2)

        return all_results