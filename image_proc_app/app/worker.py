# app/worker.py
import json
import os, boto3, tempfile, pathlib, traceback
from image_proc.run import process_shard
from obs import get_logger
from obs.metrics import build_emitter
from tenancy import TenantPaths, parse_envelope
from tenancy.envelope import EnvelopeError
from tenancy.ids import resolve_tenant_id, MissingTenantError, InvalidTenantError


REGION    = os.getenv("AWS_REGION", "us-east-1")
QUEUE_URL = os.getenv("QUEUE_URL")
BUCKET    = os.getenv("BUCKET")

LOCAL_TMP_DIR = os.getenv("LOCAL_TMP_DIR", tempfile.gettempdir())
pathlib.Path(LOCAL_TMP_DIR).mkdir(parents=True, exist_ok=True)


sqs = boto3.client("sqs", region_name=REGION)
s3  = boto3.client("s3",  region_name=REGION)
sm  = boto3.client("secretsmanager", region_name=REGION)

log = get_logger("image_proc.worker")
metrics = build_emitter(stage="image_proc")


# Per-tenant HMAC secret arns are passed in as a JSON env var by the
# Terraform user_data. Empty (or missing) means single-tenant: every
# tenant shares the deployment-wide HTML_SECRET.
def _load_tenant_secret_arns():
    raw = os.getenv("TENANT_HTML_SECRET_ARNS", "").strip()
    if not raw or raw == "{}":
        return {}
    try:
        m = json.loads(raw)
        return m if isinstance(m, dict) else {}
    except json.JSONDecodeError:
        log.warning("TENANT_HTML_SECRET_ARNS is not valid JSON; ignoring")
        return {}


TENANT_SECRET_ARNS = _load_tenant_secret_arns()


def _set_html_secret_for_tenant(tenant_id):
    """Override $HTML_SECRET for the lifetime of this shard.

    Looks up the per-tenant secret arn from TENANT_HTML_SECRET_ARNS. If
    no per-tenant arn is registered, leaves the env unchanged so the
    deployment-wide HTML_SECRET still works (single-tenant / cutover).
    """
    arn = TENANT_SECRET_ARNS.get(tenant_id)
    if not arn:
        return
    try:
        resp = sm.get_secret_value(SecretId=arn)
        os.environ["HTML_SECRET"] = resp["SecretString"]
        log.info("html_secret overridden for tenant", tenant_id=tenant_id)
    except Exception as e:
        log.warning(
            "could not fetch per-tenant html_secret; falling back to deployment-wide",
            tenant_id=tenant_id,
            error=str(e),
        )


def _output_exists(paths: TenantPaths, basename: str) -> bool:
    try:
        s3.head_object(Bucket=BUCKET, Key=paths.proc_done_key(basename))
        return True
    except s3.exceptions.ClientError:
        return False


def _mark_done(paths: TenantPaths, basename: str):
    s3.put_object(Bucket=BUCKET, Key=paths.proc_done_key(basename), Body=b"ok")


def handle_message(m):
    body = parse_envelope(m["Body"])
    tenant_id = resolve_tenant_id(body.get("tenant_id"))
    paths = TenantPaths(tenant_id)

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

    _set_html_secret_for_tenant(tenant_id)

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
        # Bad tenant data → poison message. Send to DLQ next attempt.
        log.error("rejecting unprocessable message", error=str(e), tries=tries)
        try:
            sqs.change_message_visibility(
                QueueUrl=QUEUE_URL,
                ReceiptHandle=m["ReceiptHandle"],
                VisibilityTimeout=0,
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
