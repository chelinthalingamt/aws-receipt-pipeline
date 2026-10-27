"""Receipt / invoice digitization - EC2 version (Person B).

Lambda is woken up by an S3 event. A server cannot be woken up that way, so
this program runs all the time and checks the ec2/ folder for new files:

    every POLL_SECONDS:
        list the files under s3://<bucket>/ec2/
        for each file not processed yet -> process_receipt()   (shared code)

The processing itself (extract, parse, save, notify) is imported unchanged from
lambda_function.py, so the only difference between the two versions is how the
work arrives and where it runs.
"""

import json
import logging
import os
import signal
import sys
import time

import boto3

import lambda_function as shared  # Person A's code, reused as-is

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
log = logging.getLogger("poller")

BUCKET = os.environ["BUCKET"]
PREFIX = os.environ.get("PREFIX", "ec2/")
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "10"))

s3 = boto3.client("s3")

# Files handled since this program started. Files handled before a restart are
# found through DynamoDB instead (see already_saved), so a reboot never sends
# a second email for the same receipt.
seen = set()
running = True


def stop(signum, frame):
    """systemd sends SIGTERM on stop/restart: finish the current file, then exit."""
    global running
    running = False
    log.info("Stop requested, finishing the current file")


def list_new_files():
    """Yield (key, etag, upload time in epoch ms) for every supported file under PREFIX not handled yet."""
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=BUCKET, Prefix=PREFIX):
        for obj in page.get("Contents", []):
            key, etag = obj["Key"], obj["ETag"].strip('"')
            ext = key.lower().rsplit(".", 1)[-1]
            if key.endswith("/") or ext not in shared.SUPPORTED_TYPES:
                continue
            if (key, etag) not in seen:
                # S3 listings give the upload time to the whole second (on AWS's clock).
                yield key, etag, int(obj["LastModified"].timestamp() * 1000)


def already_saved(key, etag):
    """True if this exact file version is already in the table (e.g. before a reboot)."""
    return shared.already_saved(shared.receipt_id_for(BUCKET, key, etag))


def poll_once():
    """One pass over the folder. Returns how many receipts were processed."""
    processed = 0
    for key, etag, uploaded_ms in list_new_files():
        if not running:
            break
        try:
            if already_saved(key, etag):
                seen.add((key, etag))
                continue
            item = shared.process_receipt(BUCKET, key, etag, uploaded_ms, "s3-listing")
            processed += 1
            log.info(json.dumps({"event": "ec2_processed", "s3_key": key, "status": item["status"],
                                 "processing_ms": item["processing_ms"]}))
        except Exception:
            # One bad file or a passing AWS error must not stop the server loop.
            # The file is not marked as seen, so it is retried on the next pass.
            log.exception("Failed on %s; will retry next pass", key)
            continue
        seen.add((key, etag))
    return processed


def main():
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    log.info("Watching s3://%s/%s every %s s", BUCKET, PREFIX, POLL_SECONDS)
    while running:
        try:
            poll_once()
        except Exception:
            log.exception("Listing the bucket failed; trying again shortly")
        # Sleep in 1-second steps so a stop request is noticed quickly.
        for _ in range(POLL_SECONDS):
            if not running:
                break
            time.sleep(1)
    log.info("Stopped")


if __name__ == "__main__":
    main()
