"""Receipt pipeline - website backend (one small Lambda function behind a function URL).

Routes:
  GET  /             the upload page (index.html)
  POST /api/upload   gives the browser a short-lived, size-limited pass to upload
                     ONE file straight to s3://<bucket>/serverless/web/...
  GET  /api/result   the saved result for that file, or "PROCESSING" until it exists
  GET  /api/recent   the latest receipts uploaded through the website

The upload lands in serverless/, so the existing pipeline (S3 event -> Lambda ->
DynamoDB + SNS) processes it exactly as before. This function never reads the
receipt itself.
"""

import base64
import hashlib
import json
import os
import re
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

REGION = os.environ.get("AWS_REGION", "ap-southeast-2")
BUCKET = os.environ["BUCKET"]
TABLE_NAME = os.environ["TABLE_NAME"]
PREFIX = "serverless/web/"
MAX_BYTES = 5 * 1024 * 1024  # 5 MB per receipt
TYPES = {"pdf": "application/pdf", "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png"}

# Regional endpoint + virtual-host style, so the browser posts straight to
# https://<bucket>.s3.<region>.amazonaws.com/ with no redirects.
s3 = boto3.client("s3", region_name=REGION, endpoint_url=f"https://s3.{REGION}.amazonaws.com",
                  config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}))
table = boto3.resource("dynamodb", region_name=REGION).Table(TABLE_NAME)

with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html"), encoding="utf-8") as fh:
    PAGE = fh.read()

RESULT_FIELDS = ["receipt_id", "s3_key", "status", "vendor", "amount", "currency", "receipt_date",
                 "extraction_method", "processing_ms", "processed_at", "note", "amount_corrected_from", "source"]


def receipt_id_for(bucket, key, etag):
    """Must match lambda_function.receipt_id_for: same file version -> same ID."""
    return hashlib.sha256(f"{bucket}/{key}/{etag.strip(chr(34))}".encode()).hexdigest()[:16]


def respond(status, body, content_type="application/json"):
    if content_type == "application/json":
        body = json.dumps(body, default=lambda o: str(o) if isinstance(o, Decimal) else o)
    return {"statusCode": status, "headers": {"content-type": content_type, "cache-control": "no-store"}, "body": body}


def clean_item(item):
    return {k: item[k] for k in RESULT_FIELDS if k in item}


def valid_key(key):
    return bool(key) and key.startswith(PREFIX) and ".." not in key and len(key) < 300


def upload_pass(body):
    name = str(body.get("filename", ""))
    size = int(body.get("size", 0) or 0)
    ext = name.lower().rsplit(".", 1)[-1] if "." in name else ""
    if ext not in TYPES:
        return respond(400, {"error": "Please choose a PDF, JPG or PNG file."})
    if not 0 < size <= MAX_BYTES:
        return respond(400, {"error": "The file must be smaller than 5 MB."})
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", name.rsplit(".", 1)[0]).strip("-")[:50] or "receipt"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    key = f"{PREFIX}{stamp}-{uuid.uuid4().hex[:8]}-{safe}.{ext}"
    content_type = TYPES[ext]
    post = s3.generate_presigned_post(
        BUCKET, key,
        Fields={"Content-Type": content_type},
        Conditions=[{"Content-Type": content_type}, ["content-length-range", 1, MAX_BYTES]],
        ExpiresIn=300,  # the pass is useless after 5 minutes
    )
    return respond(200, {"key": key, "url": post["url"], "fields": post["fields"]})


def result(key):
    if not valid_key(key):
        return respond(400, {"error": "Unknown receipt."})
    try:
        etag = s3.head_object(Bucket=BUCKET, Key=key)["ETag"]
    except ClientError as exc:
        if exc.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
            return respond(200, {"status": "WAITING_FOR_UPLOAD"})
        raise
    item = table.get_item(Key={"receipt_id": receipt_id_for(BUCKET, key, etag)}).get("Item")
    if not item:
        return respond(200, {"status": "PROCESSING"})
    return respond(200, clean_item(item))


def recent():
    items, kwargs = [], {"FilterExpression": "begins_with(s3_key, :p)", "ExpressionAttributeValues": {":p": PREFIX}}
    while True:
        page = table.scan(**kwargs)
        items.extend(page.get("Items", []))
        if "LastEvaluatedKey" not in page:
            break
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    items.sort(key=lambda i: i.get("processed_at", ""), reverse=True)
    return respond(200, {"receipts": [clean_item(i) for i in items[:10]]})


def handler(event, context):
    method = event.get("requestContext", {}).get("http", {}).get("method", "GET")
    path = event.get("rawPath", "/")
    query = event.get("queryStringParameters") or {}
    try:
        if method == "GET" and path in ("/", "/index.html"):
            return respond(200, PAGE, "text/html; charset=utf-8")
        if method == "POST" and path == "/api/upload":
            raw = event.get("body") or "{}"
            if event.get("isBase64Encoded"):
                raw = base64.b64decode(raw).decode()
            return upload_pass(json.loads(raw))
        if method == "GET" and path == "/api/result":
            return result(query.get("key", ""))
        if method == "GET" and path == "/api/recent":
            return recent()
        return respond(404, {"error": "Not found"})
    except (ValueError, TypeError):
        return respond(400, {"error": "Bad request"})
    except Exception as exc:  # keep the page usable, but leave the details in CloudWatch
        print(json.dumps({"event": "web_error", "path": path, "error": repr(exc)}))
        return respond(500, {"error": "Something went wrong on the server. Please try again."})
