# app/worker.py
import os, time, json, boto3, tempfile, pathlib, traceback
from scraper.run import process_shard


REGION        = os.getenv("AWS_REGION")
QUEUE_URL     = os.getenv("QUEUE_URL")
BUCKET  = os.getenv("BUCKET")

# choose a temp dir that works everywhere (Windows/Mac/Linux)
LOCAL_TMP_DIR = os.getenv("LOCAL_TMP_DIR", tempfile.gettempdir())
pathlib.Path(LOCAL_TMP_DIR).mkdir(parents=True, exist_ok=True)


sqs = boto3.client("sqs", region_name=REGION)
s3  = boto3.client("s3",  region_name=REGION)

def output_exists(basename: str) -> bool:
    try:
        s3.head_object(Bucket=BUCKET, Key=f"search_jobs/{basename}.done")
        return True
    except s3.exceptions.ClientError:
        return False

def mark_done(basename: str):
    s3.put_object(Bucket=BUCKET, Key=f"search_jobs/{basename}.done", Body=b"ok")

def handle_message(m):
    body = json.loads(m["Body"])
    key  = body["s3_key"]
    basename = os.path.basename(key)
    local_in = f"/tmp/{basename}"

    if output_exists(basename):
        print(f"[worker] already done for {basename}; skipping")
        return

    print(f"[worker] downloading s3://{BUCKET}/{key} -> {local_in}")
    s3.download_file(BUCKET, key, local_in)

    print(f"[worker] processing shard {local_in}")
    process_shard(local_in)          # raise on failure
    mark_done(basename)  

def main():

    print(f"[worker] starting in {REGION}")
    print(f"[worker] queue={QUEUE_URL}")
    print(f"[worker] input_bucket={BUCKET}")

    resp = sqs.receive_message(
        QueueUrl=QUEUE_URL,
        MaxNumberOfMessages=1,
        WaitTimeSeconds=20,
        VisibilityTimeout=4000,
        AttributeNames=["ApproximateReceiveCount"],
    )
    msgs = resp.get("Messages", [])
    if not msgs:
        print("[worker] no work; exiting")
        return 0

    m = msgs[0]
    tries = int(m.get("Attributes", {}).get("ApproximateReceiveCount", "1"))

    try:
        handle_message(m)  # must raise if anything fails
        sqs.delete_message(QueueUrl=QUEUE_URL, ReceiptHandle=m["ReceiptHandle"])
        print("[worker] message processed & deleted; exiting one-and-done")
        return 0
    except Exception as e:
        print(f"[worker] ERROR processing message: {e}")
        traceback.print_exc()

        # make the next retry happen sooner; DLQ handles maxReceiveCount
        try:
            sqs.change_message_visibility(
                QueueUrl=QUEUE_URL,
                ReceiptHandle=m["ReceiptHandle"],
                VisibilityTimeout=60 if tries < 3 else 0  # next retry quickly; let DLQ take over after N tries
            )
        except Exception as e2:
            print(f"[worker] change_message_visibility failed: {e2}")
        return 2

if __name__ == "__main__":
    main()
