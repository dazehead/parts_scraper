import io
from math import ceil
import boto3
import json
import os
import sys
from dotenv import load_dotenv
load_dotenv()
from urllib.parse import quote

class Helper:
    def __init__(self, db, detector):
        self.db = db
        self.sqs = boto3.client("sqs", region_name='us-east-1')
        self.ec2 = boto3.client("ec2")
        self.detector = detector
        self.max_batch_size = 40000


    def split_data_and_upload_jobs(self,df, bucket, prefix, chunk_size, testing=False):
        """Function for Image Search"""
        n = len(df)
        num_chunks = ceil(n /chunk_size) if n else 0
        if num_chunks ==0:
            print("Dataframe is empty, nothing to upload.")
            return
        
        base_prefix = prefix.rstrip('/')
        
        for i in range(num_chunks):
            start = i * chunk_size
            stop = min(start + chunk_size, n)
            chunk = df.iloc[start:stop]
            if testing:
                chunk.to_csv('data/test_data/test_upload.csv', header=False, index=False)
                sys.exit()
                
            else:
                csv_buf = io.StringIO()
                chunk.to_csv(csv_buf, index=False, header=False)
                data_bytes = csv_buf.getvalue().encode("utf-8")

                chunk_key = f"{base_prefix}/chunk_{i+1}.csv"
                

                self.db.s3.put_object(
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

            chunk = df
            csv_buf = io.StringIO()
            chunk.to_csv(csv_buf, index=False, header=False)
            data_bytes = csv_buf.getvalue().encode('utf-8')

            chunk_key = f"{base_prefix}/chunk_{iteration+1}.csv"

            self.db.s3.put_object(
                Body=data_bytes,
                Bucket=bucket,
                Key=chunk_key,
                ContentType='text/csv'
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

            elif stop is None: # first chunk
                if idx >= chunk_size:
                    stop = idx
                    chunk = df.iloc[start:stop+1]
                    start = stop +1
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
                    start = stop +1
                    displacement = stop
                    num_chunks +=_process_dataframe(chunk, bucket, prefix, num_chunks)
    


    def send_chunk_messages(self, job_id: str, queue_url: str, num_chunks: int, key: str):
        """
        Send SQS messages for each chunk file.

        :param job_id: Unique job ID (e.g., "dazetest-run-001")
        :param queue_url: SQS queue URL
        :param num_chunks: Number of chunk files to send
        :param prefix: S3 key prefix (default "jobs")
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
                MessageBody=json.dumps(message_body)  # must be a string
            )



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
        all_urls = self.detector.get_urls_from_db()
        encoded_urls = []

        base = "https://partsbucket0000.s3.us-east-1.amazonaws.com/images/"
        for url in all_urls:
            splits = url.split(base)[-1]
            key = splits.split('.png')[0]
            encoded_urls.append(f"{base}{quote(key, safe='/%-_.()~')}.png")


        n = len(encoded_urls)
        num_chunks = ceil(n /self.max_batch_size) if n else 0
        if num_chunks ==0:
            print("Dataframe is empty, nothing to upload.")
            return
        all_batch_ids = []
        for i in range(num_chunks):
            start = i * self.max_batch_size
            stop = min(start + self.max_batch_size, n)
            batch = encoded_urls[start:stop]

            request = self.detector.create_batch_requests(batch)
            batch_id = self.detector.submit_batch(requests=request, batch_num=i)
            all_batch_ids.append(batch_id)

        return all_batch_ids
    
    def parse_ai_results(self, batch_ids):
        for batch_id in batch_ids:
            batch = self.detector.client.batches.retrieve(batch_id)
            if not getattr(batch, "output_file_id", None):
                print("Batch completed but no output_file_id present.")
                if getattr(batch, "error_file_id", None):
                    print(f"Errors were generated. Downloading error file: {batch.error_file_id}")
                    err_path = "data/test_batch_errors.jsonl"
                    self.detector.download_results(batch.error_file_id, err_path)
                    print(f"Saved errors to {err_path}")
                raise RuntimeError("No output_file_id on completed batch—check error file and individual request statuses.")   
            
            output_path = f'data/raw_ai_output/{batch_id}_output.jsonl'

            self.detector.download_results(
                output_file_id = batch.output_file_id,
                output_path = output_path)
            
            results = self.detector.parse_results(output_path) # appends key to delete in the database

            with open(f"data/ai_output/{batch_id}_output.json", "w", encoding="utf-8") as f:
                json.dump(results, f, indent=2)






