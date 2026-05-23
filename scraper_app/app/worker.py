# app/worker.py
import os, boto3, tempfile, pathlib, traceback
from scraper.run import process_shard
from obs import get_logger
from obs.metrics import build_emitter
from tenancy import TenantPaths, parse_envelope
from tenancy.envelope import EnvelopeError
from tenancy.ids import resolve_tenant_id, MissingTenantError, InvalidTenantError


REGION  = os.getenv("AWS_REGION")
QUEUE_URL = os.getenv("QUEUE_URL")
BUCKET  = os.getenv("BUCKET")

# choose a temp dir that works everywhere (Windows/Mac/Linux)
LOCAL_TMP_DIR = os.getenv("LOCAL_TMP_DIR", tempfile.gettempdir())
pathlib.Path(LOCAL_TMP_DIR).mkdir(parents=True, exist_ok=True)


sqs = boto3.client("sqs", region_name=REGION)
s3  = boto3.client("s3",  region_name=REGION)

log = get_logger("scraper.worker")
metrics = build_emitter(stage="scraper")


def _output_exists(paths: TenantPaths, basename: str) -> bool:
    try:
        s3.head_object(Bucket=BUCKET, Key=paths.search_done_key(basename))
        return True
    except s3.exceptions.ClientError:
        return False


def _mark_done(paths: TenantPaths, basename: str):
    s3.put_object(Bucket=BUCKET, Key=paths.search_done_key(basename), Body=b"ok")


def handle_message(m):
    # Parse envelope (legacy bodies with no tenant_id still work).
    body = parse_envelope(m["Body"])
    tenant_id = resolve_tenant_id(body.get("tenant_id"))
    paths = TenantPaths(tenant_id)

    # The envelope's s3_key may be tenant-scoped or legacy; normalise.
    key = paths.normalise(body["s3_key"])
    basename = os.path.basename(key)
    local_in = os.path.join(LOCAL_TMP_DIR, basename)

    bound = log.bind(shard=basename, s3_key=key, tenant_id=tenant_id)

    if _output_exists(paths, basename):
        bound.info("shard already complete, skipping")
        metrics.count("ShardsSkipped", Tenant=tenant_id)
        return

    bound.info("shard download starting", local_path=local_in)
    s3.download_file(BUCKET, key, local_in)

    bound.info("shard processing")
    with metrics.timer("Shard", shard=basename, Tenant=tenant_id):
        process_shard(local_in, tenant_id=tenant_id)
    _mark_done(paths, basename)
    bound.info("shard complete")


def main():
    log.info(
        "worker starting",
        region=REGION,
        queue=QUEUE_URL,
        bucket=BUCKET,
    )

    resp = sqs.receive_message(
        QueueUrl=QUEUE_URL,
        MaxNumberOfMessages=1,
        WaitTimeSeconds=20,
        VisibilityTimeout=4000,
        AttributeNames=["ApproximateReceiveCount"],
    )
    msgs = resp.get("Messages", [])
    if not msgs:
        log.info("no work; exiting")
        metrics.flush()
        return 0

    m = msgs[0]
    tries = int(m.get("Attributes", {}).get("ApproximateReceiveCount", "1"))

    try:
        handle_message(m)
        sqs.delete_message(QueueUrl=QUEUE_URL, ReceiptHandle=m["ReceiptHandle"])
        log.info("message processed & deleted; exiting one-and-done", tries=tries)
        return 0
    except (MissingTenantError, InvalidTenantError, EnvelopeError) as e:
        # Bad/missing tenant data is a poison message — re-delivery
        # won't change the body. Skip the retry-with-short-visibility
        # path and send it to the DLQ at the next attempt.
        log.error("rejecting unprocessable message", error=str(e), tries=tries)
        try:
            sqs.change_message_visibility(
                QueueUrl=QUEUE_URL,
                ReceiptHandle=m["ReceiptHandle"],
                VisibilityTimeout=0,  # let DLQ logic count this as a receive
            )
        except Exception as e2:
            log.warning("change_message_visibility failed", error=str(e2))
        return 3
    except Exception as e:
        log.exception("error processing message", tries=tries, error=str(e))
        traceback.print_exc()
        try:
            sqs.change_message_visibility(
                QueueUrl=QUEUE_URL,
                ReceiptHandle=m["ReceiptHandle"],
                VisibilityTimeout=60 if tries < 3 else 0,
            )
        except Exception as e2:
            log.warning("change_message_visibility failed", error=str(e2))
        return 2
    finally:
        metrics.flush()


if __name__ == "__main__":
    main()
