"""Receipt / invoice digitization - serverless processor.

Flow for each file uploaded to s3://<bucket>/serverless/ :
  1. Extract   - get the text out of the file
                   .pdf            -> read the PDF's own text layer (no OCR needed)
                   .jpg/.jpeg/.png -> OCR with Amazon Rekognition DetectText
  2. Parse     - pick out vendor, amount and date from the text
  3. Save      - write one item to DynamoDB
  4. Notify    - publish a message to SNS (arrives as an email)

process_receipt() holds all the logic and does not depend on Lambda, so the
EC2 version can import this file and call the same function.
"""

import hashlib
import io
import json
import logging
import os
import re
import time
import urllib.parse
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

import boto3
from botocore.exceptions import ClientError
from pypdf import PdfReader
from pypdf.errors import PyPdfError

log = logging.getLogger()
log.setLevel(logging.INFO)

TABLE_NAME = os.environ["TABLE_NAME"]
TOPIC_ARN = os.environ["TOPIC_ARN"]
SOURCE = os.environ.get("PIPELINE_SOURCE", "lambda")  # "lambda" or "ec2"

s3 = boto3.client("s3")
table = boto3.resource("dynamodb").Table(TABLE_NAME)
sns = boto3.client("sns")
rekognition = boto3.client("rekognition")

IMAGE_TYPES = {"jpg", "jpeg", "png"}
SUPPORTED_TYPES = IMAGE_TYPES | {"pdf"}

# Rekognition errors that mean "this file is bad", not "the pipeline is broken".
BAD_IMAGE_ERRORS = {
    "InvalidImageFormatException",
    "ImageTooLargeException",
    "InvalidParameterException",
}


# --------------------------------------------------------------------------
# 1. Extract
# --------------------------------------------------------------------------

def extract_text(bucket, key):
    """Return (text, method) for the object at s3://bucket/key."""
    ext = key.lower().rsplit(".", 1)[-1]

    if ext == "pdf":
        body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
        reader = PdfReader(io.BytesIO(body))
        pages = [(page.extract_text() or "") for page in reader.pages]
        return "\n".join(pages), "pdf-text"

    if ext in IMAGE_TYPES:
        response = rekognition.detect_text(
            Image={"S3Object": {"Bucket": bucket, "Name": key}}
        )
        return _rows_from_rekognition(response), "rekognition-ocr"

    raise ValueError(f"Unsupported file type: .{ext}")


def _rows_from_rekognition(response):
    """Join OCR fragments that sit on the same visual row.

    Rekognition returns "TOTAL" and "450.00" as two separate pieces when they
    are far apart on a receipt. Grouping by vertical position puts them back
    on one line, which the amount parser relies on.
    """
    pieces = [d for d in response.get("TextDetections", []) if d.get("Type") == "LINE"]
    pieces.sort(key=lambda d: d["Geometry"]["BoundingBox"]["Top"])

    rows = []
    for piece in pieces:
        box = piece["Geometry"]["BoundingBox"]
        if rows and box["Top"] - rows[-1]["top"] < 0.6 * rows[-1]["height"]:
            rows[-1]["pieces"].append(piece)
        else:
            rows.append({"top": box["Top"], "height": box["Height"], "pieces": [piece]})

    lines = []
    for row in rows:
        row["pieces"].sort(key=lambda d: d["Geometry"]["BoundingBox"]["Left"])
        lines.append(" ".join(d["DetectedText"] for d in row["pieces"]))
    return "\n".join(lines)


# --------------------------------------------------------------------------
# 2. Parse
# --------------------------------------------------------------------------

MONEY_RE = re.compile(
    r"(?<![\w.,/-])"
    r"(\d{1,3}(?:,\d{2,3})+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)"
    r"(?![\w/-]|\s*%|[.,]\d)"
)

# Checked in order: the first group that matches a line wins.
STRONG_TOTAL = re.compile(
    r"grand\s*total|total\s*amount|amount\s*(?:due|payable|paid)|net\s*(?:amount|payable)"
    r"|balance\s*due|total\s*(?:due|payable|paid)|invoice\s*(?:total|value|amount)",
    re.I,
)
PLAIN_TOTAL = re.compile(r"\btotal\b", re.I)
NOT_A_TOTAL = re.compile(
    r"sub\s*-?\s*total|total\s*(?:items?|qty|quantity|tax|gst|savings?|discount|pages?)",
    re.I,
)

# OCR often splits big printed prices: "25. 00" or "25 .00" -> "25.00".
SPACED_DECIMAL = re.compile(r"(\d)\s*\.\s+(\d{2})(?!\d)|(\d)\s+\.(\d{2})(?!\d)")
# Lines that are not item prices when adding up the items to check the total.
NOT_AN_ITEM = re.compile(
    r"total|tax|gst|vat|discount|service|round|change|cash|paid|tender|balance|savings?|tip",
    re.I,
)

CURRENCIES = [
    ("INR", re.compile(r"₹|\brs\b\.?|\binr\b", re.I)),
    ("USD", re.compile(r"\$|\busd\b", re.I)),
    ("EUR", re.compile(r"€|\beur\b", re.I)),
    ("GBP", re.compile(r"£|\bgbp\b", re.I)),
]

MONTHS = {
    m: i
    for i, names in enumerate(
        ["jan january", "feb february", "mar march", "apr april", "may", "jun june",
         "jul july", "aug august", "sep sept september", "oct october",
         "nov november", "dec december"],
        start=1,
    )
    for m in names.split()
}

DATE_PATTERNS = [
    # 2026-09-12
    (re.compile(r"\b(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})\b"), "ymd"),
    # 12/09/2026, 12-09-26   (read day-first, the Indian convention)
    (re.compile(r"\b(\d{1,2})[-/.](\d{1,2})[-/.](\d{4}|\d{2})\b"), "dmy"),
    # 12 Sep 2026, 12-Sep-2026, 12th September 2026
    (re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?[\s-]+([A-Za-z]{3,9})\.?,?[\s-]+(\d{4}|\d{2})\b"), "dMy"),
    # Sep 12, 2026
    (re.compile(r"\b([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b"), "Mdy"),
]

VENDOR_LABEL = re.compile(
    r"^\s*(?:sold\s*by|seller|vendor|merchant|supplier|billed\s*by|store(?:\s*name)?)\s*[:\-]\s*(.+)$",
    re.I,
)
# Lines that are never the vendor name when falling back to "first line of the page".
NOT_A_VENDOR = re.compile(
    r"^\s*(?:tax\s*invoice|invoice|receipt|cash\s*memo|bill(?:\s*of\s*supply)?|original|duplicate"
    r"|page\b|date\b|invoice\s*(?:no|number|date|#)|bill\s*(?:no|to)|ship\s*to|order\b|gstin|gst\b"
    r"|phone|tel\b|mobile|email|www\.|http)",
    re.I,
)


def parse_receipt(text):
    """Return a dict with vendor, amount, currency, receipt_date (any may be None)."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    amount, total_line, total_index = _find_amount(lines)
    amount, corrected_from = _repair_misread_symbol(amount, lines, total_index)
    if amount is not None and amount == 0:
        amount = None  # a zero total is a misread, not a free meal: flag it for review
    result = {
        "vendor": _find_vendor(lines),
        "amount": amount,
        "currency": _find_currency(lines, total_line) if amount is not None else None,
        "receipt_date": _find_date(lines),
    }
    if corrected_from is not None:
        result["amount_corrected_from"] = corrected_from
    return result


def _money_values(line):
    line = _fix_spaced_decimals(_strip_dates(line))
    values = []
    for match in MONEY_RE.finditer(line):
        try:
            values.append(Decimal(match.group(1).replace(",", "")))
        except InvalidOperation:
            pass
    return values


def _fix_spaced_decimals(line):
    return SPACED_DECIMAL.sub(lambda m: f"{m.group(1) or m.group(3)}.{m.group(2) or m.group(4)}", line)


def _strip_dates(line):
    """Blank out real dates so "12.09.2026" is never read as an amount."""
    for pattern, kind in DATE_PATTERNS:
        line = pattern.sub(lambda m, kind=kind: " " if _to_date(kind, m.groups()) else m.group(0), line)
    return line


def _find_amount(lines):
    """Return (amount, the text it was read from, its line number).

    The text is used to pick the currency; the line number to check the total
    against the item prices above it.
    """
    def from_total_lines(keyword):
        found, found_text, found_index = None, None, None
        for i, line in enumerate(lines):
            if not keyword.search(line) or NOT_A_TOTAL.search(line):
                continue
            values, text = _money_values(line), line
            # Label and value on separate lines: look at the next line.
            if not values and i + 1 < len(lines):
                values, text = _money_values(lines[i + 1]), line + " " + lines[i + 1]
            if values:
                # Prefer a number written like a price (two decimals) over, say,
                # a phone number on the same line. Keep going through the page:
                # the last total wins.
                prices = [v for v in values if v.as_tuple().exponent == -2]
                found, found_text, found_index = (prices or values)[-1], text, i
        return found, found_text, found_index

    amount, text, index = from_total_lines(STRONG_TOTAL)
    if amount is None:
        amount, text, index = from_total_lines(PLAIN_TOTAL)
    if amount is None:
        # No total line at all: fall back to the largest price-looking number.
        candidates = [(v, line, i) for i, line in enumerate(lines)
                      for v in _money_values(line) if v.as_tuple().exponent == -2]
        if candidates:
            amount, text, index = max(candidates, key=lambda c: c[0])
    return amount, text, index


def _repair_misread_symbol(amount, lines, total_index):
    """Fix a currency sign that OCR read as a digit, but only when the receipt proves it.

    Photographed receipts often lose the symbol in front of the total: "£25.00"
    comes back as "225.00". If dropping the first digit makes the total exactly
    equal the sum of the item prices above it, the digit was the symbol.
    Returns (amount, the original amount if it was corrected, else None).
    """
    if amount is None or total_index is None:
        return amount, None
    whole = str(amount).split(".")[0]
    if len(whole) < 2:
        return amount, None
    items = [v for line in lines[:total_index] if not NOT_AN_ITEM.search(line)
             for v in _money_values(line) if v.as_tuple().exponent == -2]
    if len(items) < 2:
        return amount, None
    candidate = Decimal(str(amount)[1:])
    if sum(items) == candidate and candidate != amount:
        return candidate, amount
    return amount, None


def _find_currency(lines, total_line=None):
    """The currency printed on the total line; otherwise the one used most on the receipt.

    Receipts often print a conversion too ("TOTAL £25.00 (€37.50)"), so the
    first symbol on the page is not reliable.
    """
    if total_line:
        for code, pattern in CURRENCIES:
            if pattern.search(total_line):
                return code
    text = "\n".join(lines)
    counts = [(len(pattern.findall(text)), -order, code) for order, (code, pattern) in enumerate(CURRENCIES)]
    best = max(counts)
    return best[2] if best[0] else None


def _to_date(kind, groups):
    try:
        if kind == "ymd":
            year, month, day = int(groups[0]), int(groups[1]), int(groups[2])
        elif kind == "dmy":
            day, month, year = int(groups[0]), int(groups[1]), int(groups[2])
            if month > 12 and day <= 12:  # clearly written month-first
                day, month = month, day
        elif kind == "dMy":
            day, month, year = int(groups[0]), MONTHS[groups[1].lower()], int(groups[2])
        else:  # "Mdy"
            month, day, year = MONTHS[groups[0].lower()], int(groups[1]), int(groups[2])
        if year < 100:
            year += 2000
        parsed = datetime(year, month, day)
    except (KeyError, ValueError):
        return None
    if not 2000 <= parsed.year <= datetime.now().year + 1:
        return None
    return parsed.strftime("%Y-%m-%d")


def _find_date(lines):
    best = None  # (priority, line number, iso date) - lowest wins
    for number, line in enumerate(lines):
        lowered = line.lower()
        if re.search(r"due|deliver|expir|valid", lowered):
            priority = 3  # a due or delivery date is not the receipt date
        elif re.search(r"(?:invoice|bill|receipt)\s*date|dated", lowered):
            priority = 0  # the date the document itself was issued
        elif "date" in lowered:
            priority = 1  # e.g. "Order Date"
        else:
            priority = 2
        for pattern, kind in DATE_PATTERNS:
            for match in pattern.finditer(line):
                iso = _to_date(kind, match.groups())
                if iso and (best is None or (priority, number) < best[:2]):
                    best = (priority, number, iso)
    return best[2] if best else None


def _find_vendor(lines):
    for line in lines:
        match = VENDOR_LABEL.match(line)
        if match and re.search(r"[A-Za-z]{2}", match.group(1)):
            return match.group(1).strip()[:120]
    for line in lines[:15]:
        letters = sum(ch.isalpha() for ch in line)
        if letters < 3 or letters < len(line) / 2:
            continue
        if NOT_A_VENDOR.match(line) or any(p.search(line) for p, _ in DATE_PATTERNS):
            continue
        return line[:120]
    return None


# --------------------------------------------------------------------------
# 3 + 4. Save and notify
# --------------------------------------------------------------------------

def receipt_id_for(bucket, key, etag=""):
    """Same file (same bucket, name and version) always gives the same ID.

    S3 events and S3 listings both carry the file's ETag, so the Lambda
    version and the EC2 poller compute identical IDs for the same upload.
    """
    etag = etag.strip('"')  # listings quote the ETag, events do not
    return hashlib.sha256(f"{bucket}/{key}/{etag}".encode()).hexdigest()[:16]


def process_receipt(bucket, key, etag=""):
    """Run the whole pipeline for one S3 object and return the saved item."""
    started = time.perf_counter()
    fields = {"vendor": None, "amount": None, "currency": None, "receipt_date": None}
    method, note, text = "none", None, ""

    try:
        text, method = extract_text(bucket, key)
        fields = parse_receipt(text)
    except (PyPdfError, ValueError) as exc:
        note = f"Could not read file: {exc}"
    except ClientError as exc:
        if exc.response["Error"]["Code"] not in BAD_IMAGE_ERRORS:
            raise  # permissions, throttling etc. are real failures: let Lambda report them
        note = f"Could not read image: {exc.response['Error']['Message']}"

    missing = [name for name in ("vendor", "amount", "receipt_date") if fields[name] is None]
    if note:
        status = "FAILED"
    elif not text.strip():
        status, note = "NEEDS_REVIEW", "No text found. If this is a scanned PDF, upload it as a JPG or PNG."
    elif missing:
        status, note = "NEEDS_REVIEW", "Could not find: " + ", ".join(missing)
    else:
        status = "PROCESSED"

    item = {
        # Same file uploaded again gives the same id, so retries never create duplicates.
        "receipt_id": receipt_id_for(bucket, key, etag),
        "s3_key": key,
        "source": SOURCE,
        "status": status,
        "extraction_method": method,
        "text_chars": len(text),
        "processed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "processing_ms": int((time.perf_counter() - started) * 1000),
    }
    item.update({name: value for name, value in fields.items() if value is not None})
    if note:
        item["note"] = note[:500]

    table.put_item(Item=item)
    sns.publish(TopicArn=TOPIC_ARN, Subject=_subject(item), Message=_message(item))

    # One JSON line per receipt: easy to search and to build CloudWatch metrics from.
    log.info(json.dumps({"event": "receipt_processed", **item}, default=str))
    return item


def _subject(item):
    name = item["s3_key"].rsplit("/", 1)[-1]
    name = name.encode("ascii", "replace").decode()  # SNS subjects are ASCII only
    return f"Receipt {item['status'].lower().replace('_', ' ')}: {name}"[:100]


def _message(item):
    amount = item.get("amount")
    amount_text = f"{item.get('currency', '')} {amount:,.2f}".strip() if amount is not None else "not found"
    rows = [
        "Your receipt has been processed.",
        "",
        f"File:    {item['s3_key']}",
        f"Vendor:  {item.get('vendor', 'not found')}",
        f"Amount:  {amount_text}",
        f"Date:    {item.get('receipt_date', 'not found')}",
        f"Status:  {item['status']}",
    ]
    if item.get("amount_corrected_from") is not None:
        rows.append(f"Check:   amount corrected from {item['amount_corrected_from']} "
                    "(OCR misread the currency sign; matches the item prices)")
    if item.get("note"):
        rows.append(f"Note:    {item['note']}")
    rows += [
        "",
        f"Receipt ID: {item['receipt_id']}",
        f"Processed by {item['source']} in {item['processing_ms']} ms ({item['extraction_method']}).",
    ]
    return "\n".join(rows)


# --------------------------------------------------------------------------
# Lambda entry point: called by S3 for every new object under serverless/
# --------------------------------------------------------------------------

def lambda_handler(event, context):
    results = []
    for record in event.get("Records", []):
        bucket = record["s3"]["bucket"]["name"]
        key = urllib.parse.unquote_plus(record["s3"]["object"]["key"])  # S3 URL-encodes keys
        ext = key.lower().rsplit(".", 1)[-1]

        if key.endswith("/") or ext not in SUPPORTED_TYPES:
            log.info(json.dumps({"event": "skipped", "s3_key": key, "reason": "unsupported type"}))
            continue

        item = process_receipt(bucket, key, record["s3"]["object"].get("eTag", ""))
        results.append({"receipt_id": item["receipt_id"], "s3_key": key, "status": item["status"]})

    return {"processed": len(results), "receipts": results}
