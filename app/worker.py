# worker.py
import os, sys, json, time, threading, signal, logging
import boto3
from botocore.exceptions import BotoCoreError, ClientError

# Reuse your region/env style
from app.config import AWS_REGION  # e.g. "ap-southeast-2"
from app.aws import ddb_update_job_status, ddb_get_job  # your DDB helpers
from app.routes_videos import _do_split  # reuse your splitting implementation

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
log = logging.getLogger("video-worker")

SQS_QUEUE_URL = os.getenv("SQS_QUEUE_URL")
if not SQS_QUEUE_URL:
    log.error("SQS_QUEUE_URL is not set. Exiting.")
    sys.exit(1)

# Visibility heartbeat settings (seconds)
VISIBILITY_EXTENSION = int(os.getenv("VIS_EXT_SECONDS", "60"))
HEARTBEAT_PERIOD = int(os.getenv("HEARTBEAT_PERIOD", "30"))

sqs = boto3.client("sqs", region_name=AWS_REGION)

_shutdown = False
def _handle_sigterm(signum, frame):
    global _shutdown
    log.info("Received SIGTERM — finishing current work then exiting.")
    _shutdown = True

signal.signal(signal.SIGTERM, _handle_sigterm)
signal.signal(signal.SIGINT, _handle_sigterm)

def _extend_visibility_loop(receipt_handle: str, stop_event: threading.Event):
    """While long job runs, keep the message invisible to other workers."""
    while not stop_event.wait(HEARTBEAT_PERIOD):
        try:
            sqs.change_message_visibility(
                QueueUrl=SQS_QUEUE_URL,
                ReceiptHandle=receipt_handle,
                VisibilityTimeout=VISIBILITY_EXTENSION,
            )
            log.debug("Extended visibility by %ss", VISIBILITY_EXTENSION)
        except Exception as e:
            log.warning("Visibility extension failed: %s", e)

def _coerce_int(maybe_number):
    try:
        return int(maybe_number)
    except Exception:
        try:
            return int(float(maybe_number))
        except Exception:
            return maybe_number

def _process_message(m):
    body = json.loads(m["Body"])
    # Attributes may come as strings; normalize
    job_id = body["job_id"]
    video_id = body["video_id"]
    mode = body.get("mode", "heavy")
    parts = _coerce_int(body.get("parts", 10))
    email = body["email"]

    # Idempotency: if job already terminal, skip
    try:
        job = ddb_get_job(email, job_id)
        if job:
            status = list(job["status"].values())[0] if "status" in job else ""
            if status in ("done", "error"):
                log.info("Job %s already %s — skipping", job_id, status)
                return "skip"
    except Exception as e:
        log.warning("Could not read job %s for idempotency check: %s", job_id, e)

    # Mark running and start heartbeat
    ddb_update_job_status(email, job_id, "running")

    stop_hb = threading.Event()
    hb_thread = threading.Thread(
        target=_extend_visibility_loop,
        args=(m["ReceiptHandle"], stop_hb),
        daemon=True
    )
    hb_thread.start()

    try:
        _do_split(video_id, mode, parts, email, async_=True, job_id=job_id)
        ddb_update_job_status(email, job_id, "done")
        return "done"
    except Exception as e:
        log.exception("Job %s failed: %s", job_id, e)
        # Do NOT delete the message on failure → SQS redrive (DLQ) after max receives
        ddb_update_job_status(email, job_id, "error")
        return "error"
    finally:
        stop_hb.set()
        hb_thread.join(timeout=2)

def main():
    log.info("Worker started. Queue: %s  Region: %s", SQS_QUEUE_URL, AWS_REGION)

    while not _shutdown:
        try:
            resp = sqs.receive_message(
                QueueUrl=SQS_QUEUE_URL,
                MaxNumberOfMessages=5,
                WaitTimeSeconds=20,  # long polling
                MessageAttributeNames=["All"],
                AttributeNames=["All"],
            )
            msgs = resp.get("Messages", [])
            if not msgs:
                continue

            for m in msgs:
                if _shutdown:
                    break

                outcome = _process_message(m)

                # Only delete on success or safe skip; failures are retried/then DLQ
                if outcome in ("done", "skip"):
                    try:
                        sqs.delete_message(
                            QueueUrl=SQS_QUEUE_URL,
                            ReceiptHandle=m["ReceiptHandle"]
                        )
                        log.info("Deleted message (job %s)", json.loads(m["Body"]).get("job_id"))
                    except Exception as e:
                        log.warning("Delete message failed: %s", e)

        except (BotoCoreError, ClientError) as e:
            log.error("SQS error: %s", e)
            time.sleep(2)
        except Exception as e:
            log.exception("Worker loop error: %s", e)
            time.sleep(2)

    log.info("Worker exiting.")

if __name__ == "__main__":
    main()
