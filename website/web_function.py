"""Receipt pipeline - website backend (one small Lambda function behind a function URL).

Routes:
  GET  /             the upload page (index.html)
  POST /api/upload   gives the browser a short-lived, size-limited pass to upload
                     ONE file straight to s3://<bucket>/serverless/web/...
  GET  /api/result   the saved result for that file, or "PROCESSING" until it exists
  GET  /api/recent   the latest receipts uploaded through the website
  GET  /api/summary  spending dashboard: totals by category, month and vendor
                     (?scope=web for website uploads only, default: all receipts)
  GET  /api/export.csv   every receipt as a spreadsheet

The upload lands in serverless/, so the existing pipeline (S3 event -> Lambda ->
DynamoDB + SNS) processes it exactly as before. This function never reads the
receipt itself.
"""

import base64
import csv
import hashlib
import io
import json
import os
import re
import uuid
from collections import Counter, defaultdict
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
                 "extraction_method", "processing_ms", "processed_at", "note", "amount_corrected_from", "source",
                 "category", "gstin", "gstin_valid", "gst_state", "cgst", "sgst", "igst", "gst_total",
                 "taxable_value", "gst_check", "duplicate_of", "ocr_calls"]
EXPORT_FIELDS = ["processed_at", "vendor", "amount", "currency", "receipt_date", "category", "status",
                 "gstin", "gstin_valid", "gst_total", "gst_check", "duplicate_of", "note", "receipt_id", "s3_key"]


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


def scan_receipts(prefix):
    """Receipts under prefix: no duplicate-check markers, no experiment files."""
    items, kwargs = [], {"FilterExpression": "begins_with(s3_key, :p)", "ExpressionAttributeValues": {":p": prefix}}
    while True:
        page = table.scan(**kwargs)
        items.extend(i for i in page.get("Items", []) if "/experiments/" not in i.get("s3_key", ""))
        if "LastEvaluatedKey" not in page:
            break
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    items.sort(key=lambda i: i.get("processed_at", ""), reverse=True)
    return items


def recent():
    return respond(200, {"receipts": [clean_item(i) for i in scan_receipts(PREFIX)[:10]]})


def summary(scope):
    """Spending dashboard. Amounts are only added up within one currency (the most used one)."""
    items = scan_receipts(PREFIX if scope == "web" else "serverless/")
    priced = [i for i in items if i.get("amount") is not None and i.get("currency")]
    by_currency = defaultdict(Decimal)
    for i in priced:
        by_currency[i["currency"]] += i["amount"]
    main = Counter(i["currency"] for i in priced).most_common(1)[0][0] if priced else None
    mine = [i for i in priced if i["currency"] == main]
    by_category, by_month, by_vendor = defaultdict(Decimal), defaultdict(Decimal), defaultdict(Decimal)
    for i in mine:
        by_category[i.get("category") or "Other"] += i["amount"]
        by_month[(i.get("receipt_date") or i.get("processed_at", ""))[:7] or "unknown"] += i["amount"]
        by_vendor[(i.get("vendor") or "unknown")[:40]] += i["amount"]
    ranked = lambda d, n=None: [{"name": k, "amount": v} for k, v in sorted(d.items(), key=lambda kv: -kv[1])][:n]
    gst = [i for i in items if i.get("gst_check")]
    return respond(200, {
        "scope": scope, "receipts": len(items), "currency": main,
        "no_currency": sum(1 for i in items if i.get("amount") is not None and not i.get("currency")),
        "total": sum((i["amount"] for i in mine), Decimal("0")),
        "other_currencies": {k: v for k, v in by_currency.items() if k != main},
        "statuses": dict(Counter(i.get("status", "?") for i in items)),
        "gst_checked": len(gst), "gst_problems": sum(1 for i in gst if str(i["gst_check"]).startswith("PROBLEM")),
        "duplicates": sum(1 for i in items if i.get("duplicate_of")),
        "by_category": ranked(by_category),
        "by_month": [{"name": k, "amount": by_month[k]} for k in sorted(by_month)][-12:],
        "top_vendors": ranked(by_vendor, 8),
    })


def _cell(value):
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@") else text  # stop spreadsheets running it as a formula


def export_csv(scope):
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(EXPORT_FIELDS)
    for i in scan_receipts(PREFIX if scope == "web" else "serverless/"):
        writer.writerow([_cell(i.get(f)) for f in EXPORT_FIELDS])
    response = respond(200, out.getvalue(), "text/csv; charset=utf-8")
    response["headers"]["content-disposition"] = 'attachment; filename="receipts.csv"'
    return response


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
        scope = "web" if query.get("scope") == "web" else "all"
        if method == "GET" and path == "/api/summary":
            return summary(scope)
        if method == "GET" and path == "/api/export.csv":
            return export_csv(scope)
        return respond(404, {"error": "Not found"})
    except (ValueError, TypeError):
        return respond(400, {"error": "Bad request"})
    except Exception as exc:  # keep the page usable, but leave the details in CloudWatch
        print(json.dumps({"event": "web_error", "path": path, "error": repr(exc)}))
        return respond(500, {"error": "Something went wrong on the server. Please try again."})
