"""Receipt / invoice digitization - serverless processor.

Flow for each file uploaded to s3://<bucket>/serverless/ :
  1. Extract   - get the text out of the file
                   .pdf            -> read the PDF's own text layer (no OCR needed)
                   .jpg/.jpeg/.png -> OCR with Amazon Rekognition DetectText
                                      (read in bands when a receipt has more than 100 words)
  2. Parse     - pick out vendor, amount, currency and date
  3. Check     - Indian GST invoices: GSTIN check digit, CGST/SGST/IGST arithmetic;
                 spending category; possible duplicate of an earlier receipt
  4. Save      - write one item to DynamoDB
  5. Notify    - publish a message to SNS (arrives as an email)

process_receipt() holds all the logic and does not depend on Lambda, so the
EC2 version can import this file and call the same function.

Experiments: files under serverless/experiments/ or ec2/experiments/ go through
exactly the same steps but send no email (so load tests do not flood the inbox),
and the duplicate check only compares files inside the same test folder (so one
test run never flags the files of an earlier run).
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
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal, InvalidOperation

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

log = logging.getLogger()
log.setLevel(logging.INFO)

TABLE_NAME = os.environ["TABLE_NAME"]
TOPIC_ARN = os.environ["TOPIC_ARN"]
SOURCE = os.environ.get("PIPELINE_SOURCE", "lambda")  # "lambda" or "ec2"

IMAGE_TYPES = {"jpg", "jpeg", "png"}
SUPPORTED_TYPES = IMAGE_TYPES | {"pdf"}
EXPERIMENT_MARK = "/experiments/"

# Rekognition errors that mean "this file is bad", not "the pipeline is broken".
BAD_IMAGE_ERRORS = {
    "InvalidImageFormatException",
    "ImageTooLargeException",
    "InvalidParameterException",
}


# --------------------------------------------------------------------------
# AWS clients and libraries, loaded while the function starts up
#
# Measured on this function (comparison/results, cold-start experiment): Lambda
# runs the start-up phase with more CPU than a request gets at 256 MB, so the
# same loading work took ~7x longer when it was moved into the first request
# (first PDF: ~1.2 s when loaded at start-up, ~5 s when loaded on first use).
# So everything is loaded at start-up. LAZY_INIT=1 switches to loading on first
# use; it exists only so the cold-start experiment can measure the difference.
# --------------------------------------------------------------------------

_clients = {}


# Parallel OCR reads can meet Rekognition's request-rate limit when many receipts arrive
# together; boto3's "standard" retry mode backs off and tries again instead of failing.
CLIENT_CONFIG = Config(retries={"mode": "standard", "max_attempts": 6}, max_pool_connections=16)


def aws(name):
    if name not in _clients:
        _clients[name] = boto3.client(name, config=CLIENT_CONFIG)
    return _clients[name]


def _pdf_reader():
    from pypdf import PdfReader
    return PdfReader


def _pdf_errors():
    from pypdf.errors import PyPdfError
    return PyPdfError


if os.environ.get("LAZY_INIT") != "1":
    for _name in ("s3", "dynamodb", "sns", "rekognition"):
        aws(_name)
    _pdf_reader()
    import boto3.dynamodb.types  # noqa: F401  (used by save_item / load_item)


def _serializer():
    from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
    return TypeSerializer(), TypeDeserializer()


def save_item(item, only_if_new=False):
    ser, _ = _serializer()
    kwargs = {"TableName": TABLE_NAME, "Item": {k: ser.serialize(v) for k, v in item.items()}}
    if only_if_new:
        kwargs["ConditionExpression"] = "attribute_not_exists(receipt_id)"
    aws("dynamodb").put_item(**kwargs)


def load_item(receipt_id):
    _, de = _serializer()
    found = aws("dynamodb").get_item(TableName=TABLE_NAME, Key={"receipt_id": {"S": receipt_id}}).get("Item")
    return {k: de.deserialize(v) for k, v in found.items()} if found else None


# --------------------------------------------------------------------------
# 1. Extract
# --------------------------------------------------------------------------

REKOGNITION_WORD_LIMIT = 100  # DetectText returns at most 100 words per call
OCR_BANDS = 4                 # bands used when a receipt hits that limit
OCR_MAX_CALLS = 12            # safety cap on calls for one image


def extract_text(bucket, key):
    """Return (text, method, ocr_calls) for the object at s3://bucket/key."""
    ext = key.lower().rsplit(".", 1)[-1]

    if ext == "pdf":
        body = aws("s3").get_object(Bucket=bucket, Key=key)["Body"].read()
        reader = _pdf_reader()(io.BytesIO(body))
        pages = [(page.extract_text() or "") for page in reader.pages]
        return "\n".join(pages), "pdf-text", 0

    if ext in IMAGE_TYPES:
        response, calls = detect_text_all(lambda **kw: aws("rekognition").detect_text(
            Image={"S3Object": {"Bucket": bucket, "Name": key}}, **kw))
        return _rows_from_rekognition(response), "rekognition-ocr", calls

    raise ValueError(f"Unsupported file type: .{ext}")


def _words(response):
    return sum(1 for d in response.get("TextDetections", []) if d.get("Type") == "WORD")


def _band(top, height):
    return {"RegionsOfInterest": [{"BoundingBox": {"Left": 0.0, "Top": top, "Width": 1.0, "Height": height}}]}


def detect_text_all(detect):
    """Read every word on the image, even past Rekognition's 100-word limit.

    A long shop receipt easily has 150-300 words, and one DetectText call stops
    at 100: usually before it reaches the total at the bottom. When the first
    call comes back full, the image is read again as horizontal bands, all at
    the same time. A word belongs to a band when more than half of it is inside,
    so no word is read twice. A band that is itself full is split in two again.
    Returns (combined response, number of calls).
    """
    first = detect()
    if _words(first) < REKOGNITION_WORD_LIMIT:
        return first, 1

    # The bands are independent, so they are read at the same time: a long receipt
    # then takes about as long as one or two reads instead of four or five in a row.
    calls, lines = 1, []
    step = 1.0 / OCR_BANDS
    pending = [(i * step, step) for i in range(OCR_BANDS)]
    with ThreadPoolExecutor(max_workers=OCR_BANDS * 2) as pool:
        while pending:
            batch = pending[:max(0, OCR_MAX_CALLS - calls)]
            if not batch:
                break
            results = list(pool.map(lambda band: detect(Filters=_band(*band)), batch))
            calls += len(batch)
            pending = []
            for (top, height), part in zip(batch, results):
                # A band that is itself full is split in two, if the call budget allows.
                if _words(part) >= REKOGNITION_WORD_LIMIT and height > 0.05 and calls + len(pending) + 2 <= OCR_MAX_CALLS:
                    pending += [(top, height / 2), (top + height / 2, height / 2)]
                else:
                    lines += [d for d in part.get("TextDetections", []) if d.get("Type") == "LINE"]
    if not lines:  # banding found nothing (should not happen): keep the first read
        return first, calls
    return {"TextDetections": lines}, calls


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

# How strongly a line's wording says "this is the amount actually paid".
FINAL_TOTAL = re.compile(
    r"grand\s*total|total\s*amount|amount\s*(?:due|payable|paid)|net\s*(?:amount|payable|total)|nett\s*total"
    r"|balance\s*due|total\s*(?:due|payable|paid)|invoice\s*(?:total|value|amount)|bill\s*amount"
    r"|round(?:ed|ing)?\s*total|total\s*round(?:ed|ing)?|total\s*after\s*(?:adj|round)"
    r"|total\s*(?:sales\s*)?\(?\s*incl|total\s*sales"
    r"|^\s*total\s*(?:\(\s*\w{1,3}\s*\))?\s*[:=]?\s*(?:[^\w\s]{0,2}\s*\w{0,3}\.?\s*)?[\d,]+\.\d{2}\s*$",
    re.I,
)
PLAIN_TOTAL = re.compile(r"\btotal\b|\bamount\b", re.I)
NOT_A_TOTAL = re.compile(
    r"sub\s*-?\s*total|total\s*(?:items?|qty|quantity|tax|gst|vat|savings?|discount|pages?|no\b|number|weight|count)"
    r"|(?:tax|gst|vat)\s*total|total\s*includes?|includ(?:es|ed|ing)\s*(?:\d+%?\s*)?(?:gst|tax|vat)"
    r"|excl|exclusive|\(\s*ex\s*\)|before\s*tax|taxable|\bqty\b|item\s*count|disc(?:ount)?\s*amount|amount\s*saved"
    r"|\bchange\b|tender|\bcash\b(?!\s*(?:bill|memo))",
    re.I,
)
# Where the tax summary table starts: totals below it are tax bases, not the bill.
SUMMARY_START = re.compile(r"(?:gst|tax|vat)\s*summary|tax\s*code|tax\s*break\s*-?up|hsn\s*/?\s*sac"
                           r"|^\s*code\s+(?:amt|amount)", re.I)
# Lines that can make a total smaller than the subtotal.
SUBTOTAL_LINE = re.compile(r"sub\s*-?\s*total", re.I)
REDUCES_TOTAL = re.compile(r"disc|less\b|voucher|coupon|redeem|deposit|advance|paid\s*earlier|member|points", re.I)
# A currency glued to the number ("RM8.60", "Rs.231"): split it so the number is read.
GLUED_CURRENCY = re.compile(r"\b(RM|RS|INR|MYR|SGD|USD)\.?(?=\d)", re.I)
CASH_LINE = re.compile(r"\b(?:cash|tendered|amount\s*tendered|paid)\b", re.I)
CHANGE_LINE = re.compile(r"\bchange\b", re.I)

# OCR often splits big printed prices: "25. 00" or "25 .00" -> "25.00".
SPACED_DECIMAL = re.compile(r"(\d)\s*\.\s+(\d{2})(?!\d)|(\d)\s+\.(\d{2})(?!\d)")
# Lines that are not item prices when adding up the items to check the total.
NOT_AN_ITEM = re.compile(
    r"total|tax|gst|vat|discount|service|round|change|cash|paid|tender|balance|savings?|tip",
    re.I,
)

# Checked in order on the total line, so "S$" is tried before "$".
CURRENCIES = [
    ("INR", re.compile(r"₹|\brs\b\.?|\binr\b", re.I)),
    ("MYR", re.compile(r"\bRM\b|\bmyr\b")),
    ("SGD", re.compile(r"S\$|\bsgd\b", re.I)),
    ("USD", re.compile(r"(?<!S)\$|\busd\b", re.I)),
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
    # 20180304 (only next to the word "date")
    (re.compile(r"(?i)date\D{0,6}\b(20\d{2})(\d{2})(\d{2})\b"), "ymd"),
]

VENDOR_LABEL = re.compile(
    r"^\s*(?:sold\s*by|seller|vendor|merchant|supplier|billed\s*by|own(?:ed)?\s*by|store(?:\s*name)?)\s*[:\-]\s*(.+)$",
    re.I,
)
# Lines that are never the vendor name when falling back to "first line of the page".
NOT_A_VENDOR = re.compile(
    r"^\s*(?:tax\s*invoice|simplified\s*tax|invoice|receipt|cash\s*(?:memo|sale|bill)|bill(?:\s*of\s*supply)?|original|duplicate"
    r"|page\b|date\b|invoice\s*(?:no|number|date|#)|bill\s*(?:no|to)|ship\s*to|order\b|gstin|gst\b"
    r"|phone|tel\b|fax\b|mobile|email|www\.|http|thank|welcome|your\s*order|company\s*(?:no|reg)|co\.?\s*(?:no|reg)"
    r"|reg(?:istration)?\s*no|goods\s*sold|formerly|cashier|terminal|table\b|for\s+feedback|please)",
    re.I,
)
# Words that mark a registered business name ("... SDN BHD", "... Pvt Ltd").
COMPANY_WORDS = re.compile(
    r"\b(?:sdn\.?\s*bhd|s/b|bhd|pvt\.?\s*ltd|private\s+limited|limited|ltd|llp|llc|inc|plc|gmbh"
    r"|enterprises?|trading|traders|corporation|corp|& sons|hardware|restaurant|supermarket|pharmacy)\b\.?",
    re.I,
)
# Company registration numbers printed next to the name: "(519537-X)", "(CO. NO:20154-T)", "#000002".
REG_NUMBER = re.compile(r"\s*\(\s*(?:co(?:mpany)?\.?\s*(?:no|reg)\.?\s*:?\s*)?[A-Z]{0,3}\d{4,}[-\s]?[A-Z]?\s*\)|\s+\d{5,}-[A-Z]\b|^#?\d{3,}\s+", re.I)
# A line that continues a wrapped company name: "POPULAR BOOK" + "CO. (M) SDN BHD".
NAME_CONTINUES = re.compile(r"^\s*(?:co\.(?!\s*(?:no|reg))|company\b(?!\s*(?:no|reg))|\(m\)|sdn|bhd|pvt|private\s+limited|ltd|limited|llp|inc\b|enterprises?\b|trading\b|traders\b|& sons)", re.I)
ADDRESS_LIKE = re.compile(
    r"^\s*(?:lot|no\b\.?|unit|blk|block|plot|shop|flat|door|g/f|\d+\s*[,/-]|\d+(?:st|nd|rd|th)\s*(?:fl|floor))"
    r"|\b(?:jalan|jln|road|rd\b|street|st\b\.|floor|flr|taman|lane|nagar|marg|avenue|ave\b|sector|phase|bandar)\b"
    r"|\b\d{5,6}\b",  # a postcode or PIN code
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
    warning = _amount_warning(lines, amount, total_index)
    if warning:
        result["amount_warning"] = warning
    return result


def _amount_warning(lines, amount, total_index):
    """A reason to doubt the total, or None. Never changes the total: it only asks a person to check.

    Measured on 626 real receipts: when the total is wrong, it is usually a tax amount or a
    figure from the tax summary taken instead, and those are smaller than what was bought.
    Two checks earned their place there (8 wrong totals caught, 3 correct ones flagged); a third,
    "total equals a tax amount", was dropped because it flagged 17 correct receipts to catch 2.
    """
    if amount is None or total_index is None:
        return None
    above = lines[:total_index]
    if any(REDUCES_TOTAL.search(ln) and any(v > 0 for v in _money_values(ln)) for ln in above):
        return None  # a discount or a deposit can legitimately make the total smaller
    subtotal = _last_value(above, SUBTOTAL_LINE) if above else None
    if subtotal is not None and amount < subtotal * Decimal("0.9"):
        return f"total {amount} is less than the subtotal {subtotal}"
    items = [v for ln in above if not NOT_AN_ITEM.search(ln) for v in _money_values(ln) if _is_price(v)]
    if len(items) >= 2 and amount < max(items) * Decimal("0.5"):
        return f"total {amount} is less than half of an item price ({max(items)})"
    return None


def _money_values(line):
    line = GLUED_CURRENCY.sub(r"\1 ", _fix_spaced_decimals(_strip_dates(line)))
    values = []
    for match in MONEY_RE.finditer(line):
        try:
            values.append(Decimal(match.group(1).replace(",", "")))
        except InvalidOperation:
            pass
    return values


def _is_price(value):
    return value.as_tuple().exponent == -2


def _fix_spaced_decimals(line):
    return SPACED_DECIMAL.sub(lambda m: f"{m.group(1) or m.group(3)}.{m.group(2) or m.group(4)}", line)


def _strip_dates(line):
    """Blank out real dates so "12.09.2026" is never read as an amount."""
    for pattern, kind in DATE_PATTERNS:
        line = pattern.sub(lambda m, kind=kind: " " if _to_date(kind, m.groups()) else m.group(0), line)
    return line


def _value_on_or_after(lines, i):
    """Money values on line i; if it has none (label and value on separate lines), on the next line."""
    values, text = _money_values(lines[i]), lines[i]
    if not values and i + 1 < len(lines) and not re.search(r"[A-Za-z]{4}", lines[i + 1]):
        values, text = _money_values(lines[i + 1]), lines[i] + " " + lines[i + 1]
    return values, text


def _last_value(lines, pattern, before=None, unless=None):
    for i in range(len(lines) - 1 if before is None else before, -1, -1):
        if pattern.search(lines[i]) and not (unless and unless.search(lines[i])):
            values, _ = _value_on_or_after(lines, i)
            prices = [v for v in values if _is_price(v)]
            if prices:
                return prices[-1]
    return None


def _find_amount(lines):
    """Return (amount, the text it was read from, its line number).

    Every line that looks like a total becomes a candidate with a score:
      +4  wording that means "the amount paid" (grand total, rounded total, total incl. tax)
      +2  just the word "total"
      +3  it equals cash given minus change returned (the receipt proves it)
      -5  it sits inside the tax summary table at the bottom
    Any other line printing exactly "cash given minus change" also scores 6: that
    is the amount the customer really paid, even when its label is cut off ("TO : 73.00").
    The best score wins; on a tie, the line further down the page wins.
    The text is used to pick the currency; the line number to check the total
    against the item prices above it.
    """
    summary_from = next((i for i, ln in enumerate(lines) if SUMMARY_START.search(ln)), len(lines))
    cash = _last_value(lines, CASH_LINE, before=summary_from - 1, unless=CHANGE_LINE)
    change = _last_value(lines, CHANGE_LINE, before=summary_from - 1)
    proven = cash - change if cash is not None and change is not None and cash > change else None

    candidates = []
    for i, line in enumerate(lines):
        if NOT_A_TOTAL.search(line):
            continue
        if FINAL_TOTAL.search(line):
            score = 4
        elif PLAIN_TOTAL.search(line):
            score = 2
        else:
            continue
        values, text = _value_on_or_after(lines, i)
        if not values:
            continue
        # Prefer a number written like a price (two decimals) over, say, a phone
        # number on the same line.
        prices = [v for v in values if _is_price(v)]
        value = (prices or values)[-1]
        if proven is not None and value == proven:
            score += 3
        if i >= summary_from:  # the summary's own heading row ("GST SUMMARY AMOUNT ...") included
            score -= 5
        candidates.append((score, i, value, text))
    if proven is not None:
        for i, line in enumerate(lines[:summary_from]):
            if CASH_LINE.search(line) or CHANGE_LINE.search(line):
                continue
            if proven in [v for v in _money_values(line) if _is_price(v)]:
                candidates.append((6, i, proven, line))

    if candidates:
        score, index, amount, text = max(candidates, key=lambda c: (c[0], c[1]))
        return amount, text, index
    if proven is not None:
        return proven, None, None
    # No total line at all: fall back to the largest price-looking number.
    fallback = [(v, line, i) for i, line in enumerate(lines)
                for v in _money_values(line) if _is_price(v)]
    if fallback:
        amount, text, index = max(fallback, key=lambda c: c[0])
        return amount, text, index
    return None, None, None


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
             for v in _money_values(line) if _is_price(v)]
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


def _vendor_candidates(lines, limit=15):
    """The first lines of the page, with names that wrap onto a second line joined back up."""
    head = [REG_NUMBER.sub("", ln).strip(" ,.-") for ln in lines[:limit]]
    out, i = [], 0
    while i < len(head):
        line = head[i]
        nxt = head[i + 1] if i + 1 < len(head) else ""
        wraps = re.search(r"(?:&|\band)\s*$", line, re.I) or (nxt and NAME_CONTINUES.match(nxt))
        if wraps and nxt and not ADDRESS_LIKE.search(nxt):
            out.append((line + " " + nxt).strip())
            i += 2
        else:
            out.append(line)
            i += 1
    return out


def _plausible_name(line):
    letters = sum(ch.isalpha() for ch in line)
    if letters < 3 or letters < len(line) / 2:
        return False
    if NOT_A_VENDOR.match(line) or any(p.search(line) for p, _ in DATE_PATTERNS):
        return False
    return True


def _find_vendor(lines):
    for line in lines:
        match = VENDOR_LABEL.match(line)
        if match and re.search(r"[A-Za-z]{2}", match.group(1)):
            return match.group(1).strip()[:120]
    candidates = _vendor_candidates(lines)
    # A registered business name ("... Pvt Ltd", "... SDN BHD") near the top is the best guess.
    for line in candidates[:5]:
        if COMPANY_WORDS.search(line) and _plausible_name(line) and not ADDRESS_LIKE.search(line):
            return line[:120]
    for line in candidates:
        if _plausible_name(line) and not ADDRESS_LIKE.search(line):
            return line[:120]
    return None


# --------------------------------------------------------------------------
# 3. Checks: Indian GST, spending category, duplicates
# --------------------------------------------------------------------------

GSTIN_CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
# 15 characters: state (2 digits) + PAN (5 letters, 4 digits, 1 letter) + entity + 'Z' + check character.
GSTIN_SHAPE = re.compile(r"(?<![A-Z0-9])([0-9OIL]{2}[A-Z0-9]{5}[0-9OILSB]{4}[A-Z0-9][1-9A-Z][Z2][0-9A-Z])(?![A-Z0-9])")
GST_STATES = {
    "01": "Jammu and Kashmir", "02": "Himachal Pradesh", "03": "Punjab", "04": "Chandigarh",
    "05": "Uttarakhand", "06": "Haryana", "07": "Delhi", "08": "Rajasthan", "09": "Uttar Pradesh",
    "10": "Bihar", "11": "Sikkim", "12": "Arunachal Pradesh", "13": "Nagaland", "14": "Manipur",
    "15": "Mizoram", "16": "Tripura", "17": "Meghalaya", "18": "Assam", "19": "West Bengal",
    "20": "Jharkhand", "21": "Odisha", "22": "Chhattisgarh", "23": "Madhya Pradesh", "24": "Gujarat",
    "25": "Daman and Diu", "26": "Dadra and Nagar Haveli and Daman and Diu", "27": "Maharashtra",
    "28": "Andhra Pradesh (old)", "29": "Karnataka", "30": "Goa", "31": "Lakshadweep", "32": "Kerala",
    "33": "Tamil Nadu", "34": "Puducherry", "35": "Andaman and Nicobar Islands", "36": "Telangana",
    "37": "Andhra Pradesh", "38": "Ladakh", "97": "Other Territory", "99": "Centre Jurisdiction",
}
CGST_LINE = re.compile(r"\bC\s*GST\b", re.I)
SGST_LINE = re.compile(r"\b(?:S|UT)\s*GST\b", re.I)
IGST_LINE = re.compile(r"\bI\s*GST\b", re.I)
TAXABLE_LINE = re.compile(r"taxable\s*(?:value|amount|amt)|sub\s*-?\s*total|amount\s*before\s*tax", re.I)
# The tax can be off by a few paise from rounding each line; the bill total by up to a rupee.
TAX_TOLERANCE = Decimal("0.05")
TOTAL_TOLERANCE = Decimal("1.00")


def gstin_check_char(first14):
    """The 15th character of a GSTIN, computed from the first 14 (a base-36 Luhn-style checksum)."""
    total = 0
    for position, char in enumerate(first14):
        product = GSTIN_CHARS.index(char) * (2 if position % 2 else 1)
        total += product // 36 + product % 36
    return GSTIN_CHARS[(36 - total % 36) % 36]


def gstin_is_valid(gstin):
    return (len(gstin) == 15 and all(c in GSTIN_CHARS for c in gstin)
            and gstin[:2] in GST_STATES and gstin_check_char(gstin[:14]) == gstin[14])


def _fix_gstin_ocr(raw):
    """Undo the usual OCR mix-ups (O/0, I/1, S/5, B/8) where the GSTIN layout fixes the type."""
    to_digit = {"O": "0", "I": "1", "L": "1", "S": "5", "B": "8", "Z": "2"}
    to_letter = {"0": "O", "1": "I", "5": "S", "8": "B", "2": "Z"}
    chars = list(raw)
    for p in (0, 1, 7, 8, 9, 10):
        chars[p] = to_digit.get(chars[p], chars[p])
    for p in (2, 3, 4, 5, 6, 11):
        chars[p] = to_letter.get(chars[p], chars[p])
    chars[13] = "Z" if chars[13] == "2" else chars[13]
    return "".join(chars)


BUYER_LINE = re.compile(r"buyer|bill(?:ed)?\s*to|customer|recipient|consignee|ship(?:ped)?\s*to|client", re.I)


def _try_ambiguous(gstin):
    """The 13th character (almost always 1) and the check character can be letters or digits,
    so OCR mix-ups there cannot be undone by position alone. Try the usual swaps and keep a
    version only if it then passes the check digit; otherwise return the code as read."""
    swaps = {"I": "1", "L": "1", "1": "I", "O": "0", "0": "O", "S": "5", "5": "S", "B": "8", "8": "B", "Z": "2", "2": "Z"}
    for position in (12, 14):
        if gstin[position] in swaps:
            candidate = gstin[:position] + swaps[gstin[position]] + gstin[position + 1:]
            if gstin_is_valid(candidate):
                return candidate
    return gstin


def find_gstins(text):
    """Every GSTIN-shaped code on the page, the seller's first: [(gstin, valid, fixed_by_ocr_rules)].

    An invoice between two businesses prints both GSTINs; the buyer's is on a
    line that says so ("Buyer GSTIN", "Bill to"), and the seller's is the one to check.
    """
    found, seen = [], set()
    upper = text.upper().splitlines()
    seller = [ln for ln in upper if not BUYER_LINE.search(ln)]
    buyer = [ln for ln in upper if BUYER_LINE.search(ln)]
    # OCR sometimes splits the code ("33 AAACN 1234 K1Z9"): rejoin it, but only on lines that say GST.
    rejoin = lambda lines: [re.sub(r"(?<=[A-Z0-9]) (?=[A-Z0-9])", "", ln) for ln in lines if "GST" in ln]
    for source in ("\n".join(seller), "\n".join(rejoin(seller)), "\n".join(buyer), "\n".join(rejoin(buyer))):
        for match in GSTIN_SHAPE.finditer(source):
            raw = match.group(1)
            if not re.search(r"[A-Z]{3}", raw):
                continue
            fixed = _fix_gstin_ocr(raw)
            if not gstin_is_valid(fixed):
                fixed = _try_ambiguous(fixed)
            if fixed in seen:
                continue
            seen.add(fixed)
            found.append((fixed, gstin_is_valid(fixed), fixed != raw))
    return found


def _tax_value(lines, pattern):
    """Sum of the tax on lines matching pattern; None when it is not charged (absent or 0.00)."""
    total, found = Decimal("0"), False
    for i, line in enumerate(lines):
        if pattern.search(line) and not re.search(r"gstin|summary|no\b|number|reg", line, re.I):
            values, _ = _value_on_or_after(lines, i)
            prices = [v for v in values if _is_price(v)]
            if prices:
                total += prices[-1]
                found = True
    return total if found and total != 0 else None


def gst_check(text, amount):
    """GST fields for an Indian invoice, or {} when the receipt is not one.

    Checks three things a fake or mistyped bill often gets wrong:
      1. the GSTIN's check character (and that its state code exists),
      2. CGST equals SGST, and IGST is not charged together with them,
      3. taxable value + taxes = the bill total (within a rupee of rounding).
    """
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    gstins = find_gstins(text)
    cgst, sgst, igst = (_tax_value(lines, p) for p in (CGST_LINE, SGST_LINE, IGST_LINE))
    if not gstins and cgst is None and sgst is None and igst is None:
        return {}

    result, problems = {}, []
    if gstins:
        gstin, valid, fixed = gstins[0]  # the seller's
        result.update({"gstin": gstin, "gstin_valid": valid, "gst_state": GST_STATES.get(gstin[:2], "unknown")})
        if fixed and valid:
            result["gstin_ocr_fixed"] = True
        if not valid:
            problems.append(f"GSTIN {gstin} fails its check digit")
    for name, value in (("cgst", cgst), ("sgst", sgst), ("igst", igst)):
        if value is not None:
            result[name] = value
    if cgst is not None and sgst is not None and abs(cgst - sgst) > TAX_TOLERANCE:
        problems.append(f"CGST {cgst} and SGST {sgst} should be equal")
    if (cgst is not None) != (sgst is not None):
        problems.append("CGST and SGST must be charged together")
    if igst is not None and (cgst is not None or sgst is not None):
        problems.append("IGST charged together with CGST/SGST")

    tax = sum(v for v in (cgst, sgst, igst) if v is not None)
    if cgst is not None or sgst is not None or igst is not None:
        result["gst_total"] = tax
        first_tax_line = next(i for i, ln in enumerate(lines)
                              if CGST_LINE.search(ln) or SGST_LINE.search(ln) or IGST_LINE.search(ln))
        taxable = _last_value(lines, TAXABLE_LINE, before=first_tax_line)
        if taxable is None:  # no "Subtotal": use the item prices above the tax lines
            items = [v for ln in lines[:first_tax_line] if not NOT_AN_ITEM.search(ln)
                     for v in _money_values(ln) if _is_price(v)]
            taxable = sum(items) if len(items) >= 1 else None
        if taxable is not None:
            result["taxable_value"] = taxable
            if amount is not None and abs(taxable + tax - amount) > TOTAL_TOLERANCE:
                problems.append(f"taxable {taxable} + GST {tax} = {taxable + tax}, but the total is {amount}")

    result["gst_check"] = "OK" if not problems else "PROBLEM: " + "; ".join(problems)
    return result


CATEGORIES = [
    ("Food & dining", r"restaurant|cafe|caf[eé]|coffee|kitchen|bakery|bakers|food|pizza|burger|sushi|chicken|dosa"
                      r"|biryani|\btea\b|juice|\bdine\b|dining|bistro|\bbar\b|grill|kfc|mcdonald|domino|subway|starbucks|canteen|\bmess\b|hotel"),
    ("Groceries", r"mart\b|grocer|supermarket|hypermarket|provision|kirana|fresh|vegetable|dairy|speed\s*mart|cash\s*&\s*carry|bigbasket|dmart"),
    ("Travel & transport", r"\bcabs?\b|taxi|\buber\b|\bola\b|rapido|petrol|fuel|diesel|\bshell\b|parking|\btoll\b|airline|airways|railway|irctc|metro\s*rail|\bbus\b|travel"),
    ("Utilities & bills", r"electricity|electric\s*(?:supply|board)|power\s*(?:distribution|supply|corporation)|\bpower\b(?!\s*tools)|water\s*(?:supply|board)|broadband|internet|telecom|airtel|jio|bsnl|recharge|gas\s*(?:agency|bill)|utility|distribution"),
    ("Health", r"pharma|medical|medic|clinic|hospital|chemist|drug|health|diagnostic|\blab\b|watson|guardian|apollo"),
    ("Office & stationery", r"stationery|stationers|office|book|printing|print|copier|xerox|computer|laptop|electronics?"),
    ("Home & hardware", r"hardware|electrical|machine|tools|deco|furniture|home|paint|plumb|d\.?i\.?y|sanitary"),
    ("Shopping", r"retail|fashion|apparel|garment|textile|store|shop|mall|boutique|amazon|flipkart"),
]


def categorize(vendor, text):
    """A spending category from the vendor name first, then from the receipt's wording."""
    for haystack in (vendor or "", text[:600]):
        for name, pattern in CATEGORIES:
            if re.search(pattern, haystack, re.I):
                return name
    return "Other"


def _fingerprint(fields, s3_key=""):
    vendor = re.sub(r"[^a-z0-9]", "", (fields.get("vendor") or "").lower())
    # Real receipts are compared with everything this version has seen; test files only with their own folder.
    scope = s3_key.rsplit("/", 1)[0] if EXPERIMENT_MARK in s3_key else SOURCE
    key = f"{scope}|{vendor}|{fields.get('amount')}|{fields.get('receipt_date')}"
    return "fingerprint#" + hashlib.sha256(key.encode()).hexdigest()[:20]


def find_duplicate(receipt_id, key, fields):
    """Return the earlier receipt with the same vendor, total and date, or None.

    A small marker row ("fingerprint#...") remembers the first receipt seen with
    each vendor + total + date. Saving it only works if it does not exist yet,
    so two copies processed at the same moment still cannot both be "first".
    A different photo or a re-scan of the same bill has a different file and
    receipt ID but the same fingerprint, so it is caught too.
    """
    if not (fields.get("vendor") and fields.get("amount") is not None and fields.get("receipt_date")):
        return None
    marker = {"receipt_id": _fingerprint(fields, key), "item_type": "fingerprint",
              "first_receipt_id": receipt_id, "first_s3_key": key}
    try:
        save_item(marker, only_if_new=True)
        return None
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
    first = load_item(marker["receipt_id"]) or {}
    if first.get("first_receipt_id") in (None, receipt_id):
        return None  # the same file being processed again is not a duplicate
    return {"receipt_id": first["first_receipt_id"], "s3_key": first.get("first_s3_key", "")}


# --------------------------------------------------------------------------
# 4 + 5. Save and notify
# --------------------------------------------------------------------------

def receipt_id_for(bucket, key, etag=""):
    """Same file (same bucket, name and version) always gives the same ID.

    S3 events and S3 listings both carry the file's ETag, so the Lambda
    version and the EC2 poller compute identical IDs for the same upload.
    """
    etag = etag.strip('"')  # listings quote the ETag, events do not
    return hashlib.sha256(f"{bucket}/{key}/{etag}".encode()).hexdigest()[:16]


def already_saved(receipt_id):
    return load_item(receipt_id) is not None


def process_receipt(bucket, key, etag="", uploaded_at_ms=None, upload_time_source=None):
    """Run the whole pipeline for one S3 object and return the saved item.

    uploaded_at_ms is when S3 finished storing the file, on AWS's clock, so the
    delay from upload to result can be measured without trusting any laptop clock.
    """
    started_ms = int(time.time() * 1000)
    started = time.perf_counter()
    experiment = EXPERIMENT_MARK in key
    receipt_id = receipt_id_for(bucket, key, etag)
    fields = {"vendor": None, "amount": None, "currency": None, "receipt_date": None}
    method, note, text, ocr_calls = "none", None, "", 0

    try:
        text, method, ocr_calls = extract_text(bucket, key)
        fields = parse_receipt(text)
    except ValueError as exc:
        note = f"Could not read file: {exc}"
    except ClientError as exc:
        if exc.response["Error"]["Code"] not in BAD_IMAGE_ERRORS:
            raise  # permissions, throttling etc. are real failures: let Lambda report them
        note = f"Could not read image: {exc.response['Error']['Message']}"
    except Exception as exc:  # a damaged PDF
        if not isinstance(exc, _pdf_errors()):
            raise
        note = f"Could not read file: {exc}"

    checks = {}
    if not note and text.strip():
        checks = gst_check(text, fields.get("amount"))
        if fields.get("amount") is not None and not fields.get("currency") and (
                checks.get("gstin") or checks.get("gst_total") is not None):
            fields["currency"] = "INR"  # GST is charged only in India, so a GST invoice is in rupees
        checks["category"] = categorize(fields.get("vendor"), text)
        duplicate = find_duplicate(receipt_id, key, fields)
        if duplicate:
            checks["duplicate_of"] = duplicate["s3_key"] or duplicate["receipt_id"]

    missing = [name for name in ("vendor", "amount", "receipt_date") if fields[name] is None]
    review = []
    if missing:
        review.append("Could not find: " + ", ".join(missing))
    if fields.get("amount_warning"):
        review.append("Check the total: " + fields["amount_warning"])
    if str(checks.get("gst_check", "OK")).startswith("PROBLEM"):
        review.append("GST " + checks["gst_check"][len("PROBLEM: "):])
    if checks.get("duplicate_of"):
        review.append(f"Possible duplicate of {checks['duplicate_of']} (same vendor, total and date)")

    if note:
        status = "FAILED"
    elif not text.strip():
        status, note = "NEEDS_REVIEW", "No text found. If this is a scanned PDF, upload it as a JPG or PNG."
    elif review:
        status, note = "NEEDS_REVIEW", "; ".join(review)
    else:
        status = "PROCESSED"

    finished_ms = int(time.time() * 1000)
    item = {
        # Same file uploaded again gives the same id, so retries never create duplicates.
        "receipt_id": receipt_id,
        "s3_key": key,
        "source": SOURCE,
        "status": status,
        "extraction_method": method,
        "text_chars": len(text),
        "processed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "processing_ms": int((time.perf_counter() - started) * 1000),
        "started_at_ms": started_ms,
        "processed_at_ms": finished_ms,
    }
    if ocr_calls:
        item["ocr_calls"] = ocr_calls
    if uploaded_at_ms is not None:
        item["uploaded_at_ms"] = int(uploaded_at_ms)
        item["upload_time_source"] = upload_time_source or "unknown"
    item.update({name: value for name, value in fields.items() if value is not None})
    item.update({name: value for name, value in checks.items() if value is not None})
    if note:
        item["note"] = note[:500]

    save_item(item)
    if not experiment:
        aws("sns").publish(TopicArn=TOPIC_ARN, Subject=_subject(item), Message=_message(item))

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
        f"File:     {item['s3_key']}",
        f"Vendor:   {item.get('vendor', 'not found')}",
        f"Amount:   {amount_text}",
        f"Date:     {item.get('receipt_date', 'not found')}",
        f"Category: {item.get('category', '-')}",
        f"Status:   {item['status']}",
    ]
    if item.get("gstin"):
        validity = "valid" if item.get("gstin_valid") else "INVALID check digit"
        rows.append(f"GSTIN:    {item['gstin']} ({validity}, {item.get('gst_state', 'unknown state')})")
    if item.get("gst_total") is not None:
        parts = [f"{n.upper()} {item[n]}" for n in ("cgst", "sgst", "igst") if item.get(n) is not None]
        rows.append(f"GST:      {', '.join(parts)} = {item['gst_total']} -> {item.get('gst_check', '')}")
    if item.get("amount_corrected_from") is not None:
        rows.append(f"Check:    amount corrected from {item['amount_corrected_from']} "
                    "(OCR misread the currency sign; matches the item prices)")
    if item.get("note"):
        rows.append(f"Note:     {item['note']}")
    rows += [
        "",
        f"Receipt ID: {item['receipt_id']}",
        f"Processed by {item['source']} in {item['processing_ms']} ms ({item['extraction_method']}).",
    ]
    return "\n".join(rows)


# --------------------------------------------------------------------------
# Lambda entry point: called by S3 for every new object under serverless/
# --------------------------------------------------------------------------

def _event_time_ms(text):
    """S3 event time ("2026-10-26T19:31:28.123Z", millisecond precision) as epoch ms."""
    if not text:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return int(datetime.strptime(text, fmt).replace(tzinfo=timezone.utc).timestamp() * 1000)
        except ValueError:
            pass
    return None


def lambda_handler(event, context):
    results = []
    for record in event.get("Records", []):
        bucket = record["s3"]["bucket"]["name"]
        key = urllib.parse.unquote_plus(record["s3"]["object"]["key"])  # S3 URL-encodes keys
        ext = key.lower().rsplit(".", 1)[-1]

        if key.endswith("/") or ext not in SUPPORTED_TYPES:
            log.info(json.dumps({"event": "skipped", "s3_key": key, "reason": "unsupported type"}))
            continue

        item = process_receipt(bucket, key, record["s3"]["object"].get("eTag", ""),
                               _event_time_ms(record.get("eventTime")), "s3-event")
        results.append({"receipt_id": item["receipt_id"], "s3_key": key, "status": item["status"]})

    return {"processed": len(results), "receipts": results}
