#!/usr/bin/env python3
"""Experiments that go beyond the first comparison. Run from the comparison folder:

    python3 experiments.py all          # everything below, about 25 minutes
    python3 experiments.py accuracy     # 40 real receipt scans + 15 GST invoices (Lambda)
    python3 experiments.py coldstart    # start-up time: load at start-up (default) vs load on first use
    python3 experiments.py tune         # Lambda memory 128-2048 MB x Intel/ARM: speed and cost
    python3 experiments.py burst        # 10 / 50 / 200 receipts at once: Lambda vs EC2

Results go to results/<run>/ as JSON and CSV (and a short summary.txt). Every
experiment uploads under serverless/experiments/<run>/ or ec2/experiments/<run>/,
so no emails are sent and the files never mix with real receipts.

The Lambda function is changed during coldstart and tune (environment, memory,
processor type) and is always put back the way it was, even if the run fails.
The EC2 server is started for burst and stopped again if this script started it.
"""

import argparse
import concurrent.futures
import csv
import difflib
import email.utils
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
import random
import re
import statistics
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

import boto3
from boto3.dynamodb.types import TypeDeserializer

try:
    from botocore.config import Config
    from botocore.exceptions import (ConnectionClosedError, ConnectTimeoutError, EndpointConnectionError,
                                     ReadTimeoutError)
except ImportError:  # the offline rehearsal replaces botocore
    Config = None
    ConnectionClosedError = ConnectTimeoutError = EndpointConnectionError = ReadTimeoutError = OSError

HERE = os.path.dirname(os.path.abspath(__file__))
LOG_GROUP = "/aws/lambda/receipt-pipeline-processor"

# Lambda list prices per GB-second and per request (aws.amazon.com/lambda/pricing).
# Check the Sydney column there before quoting costs; change these if they differ.
PRICE_GB_S = {"x86_64": Decimal("0.0000166667"), "arm64": Decimal("0.0000133334")}
PRICE_REQUEST = Decimal("0.0000002")


def say(msg=""):
    print(msg, flush=True)


# ============================================================== small helpers (pure)

def receipt_id_for(bucket, key, etag):
    """Same as lambda_function.receipt_id_for."""
    return hashlib.sha256(f"{bucket}/{key}/{etag.strip(chr(34))}".encode()).hexdigest()[:16]


def pct(values, p):
    """p-th percentile (nearest rank) of a list, or None."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, int(round(p / 100 * len(ordered) + 0.5)) - 1))]


def stats(values):
    values = [float(v) for v in values if v is not None]
    if not values:
        return {"n": 0}
    return {"n": len(values), "mean": round(statistics.mean(values), 1), "p50": pct(values, 50),
            "p95": pct(values, 95), "max": max(values), "min": min(values)}


def to_decimal(value):
    try:
        return Decimal(str(value).replace(",", ""))
    except (InvalidOperation, TypeError, AttributeError):
        return None


def vendor_matches(got, want):
    g, w = (re.sub(r"[^a-z0-9]", "", (s or "").lower()) for s in (got, want))
    return bool(g and w) and (g == w or difflib.SequenceMatcher(None, g, w).ratio() >= 0.8)


def score_fields(item, answer):
    got_amount, want_amount = to_decimal(item.get("amount")), to_decimal(answer.get("amount"))
    ok = {"vendor": vendor_matches(item.get("vendor"), answer.get("vendor")),
          "amount": got_amount is not None and got_amount == want_amount,
          "date": item.get("receipt_date") == answer.get("date")}
    ok["all"] = all(ok.values())
    return ok


def parse_lambda_logs(messages):
    """Map s3_key -> {request_id, billed_ms, duration_ms, init_ms, max_memory_mb, memory_mb}.

    The function logs one JSON line per receipt (with the request ID in front, added by
    the Lambda Python runtime) and Lambda adds one REPORT line per invocation.
    """
    key_by_request, reports = {}, {}
    for m in messages:
        if m.startswith("REPORT"):
            rid = re.search(r"RequestId: (\S+)", m)
            if not rid:
                continue
            num = lambda pat: (float(x.group(1)) if (x := re.search(pat, m)) else None)
            reports[rid.group(1)] = {
                "duration_ms": num(r"\tDuration: ([\d.]+) ms"), "billed_ms": num(r"Billed Duration: ([\d.]+) ms"),
                "memory_mb": num(r"Memory Size: (\d+) MB"), "max_memory_mb": num(r"Max Memory Used: (\d+) MB"),
                "init_ms": num(r"Init Duration: ([\d.]+) ms")}
        elif '"receipt_processed"' in m:
            parts = m.split("\t")
            if len(parts) >= 4:
                try:
                    data = json.loads(parts[3])
                    key_by_request[parts[2]] = data.get("s3_key")
                except ValueError:
                    pass
    return {key: dict(reports[rid], request_id=rid) for rid, key in key_by_request.items() if rid in reports}


def bracket_offset(samples):
    """Laptop-to-AWS clock offset from sequential requests: [(local_send, local_receive, server_second)].

    Each response was stamped with whole second N somewhere between send and
    receive, so the offset lies in [N - receive, N + 1 - send]. Intersecting many
    such windows pins it down to roughly the network round trip.
    Returns (offset_ms, uncertainty_ms) or None if the windows do not overlap.
    """
    lo = max(server - recv for send, recv, server in samples)
    hi = min(server + 1 - send for send, recv, server in samples)
    if lo > hi:
        return None
    return (lo + hi) / 2 * 1000, (hi - lo) / 2 * 1000


def clock_offset_ms(samples):
    """How far the laptop clock is behind AWS, from (local_start, local_end, server_date) per upload.

    The S3 'Date' header has whole seconds, so each upload says only that the true
    offset lies within one second; the middle of the many samples is a good estimate.
    """
    estimates = [(server + 0.5 - (start + end) / 2) * 1000 for start, end, server in samples if server]
    return statistics.median(estimates) if estimates else 0.0


def make_pdf(lines):
    """A minimal one-page PDF with plain text lines (no libraries needed)."""
    esc = lambda s: s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    content = "BT /F1 11 Tf 15 TL 50 790 Td " + " ".join(f"({esc(l)}) Tj T*" for l in lines) + " ET"
    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
        f"<< /Length {len(content.encode('latin-1'))} >>\nstream\n{content}\nendstream",
    ]
    out, offsets = "%PDF-1.4\n", []
    for i, body in enumerate(objects, start=1):
        offsets.append(len(out.encode("latin-1")))
        out += f"{i} 0 obj\n{body}\nendobj\n"
    xref = len(out.encode("latin-1"))
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n" + "".join(f"{o:010d} 00000 n \n" for o in offsets)
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n"
    return out.encode("latin-1")


VENDORS = ["Saravana Bhavan", "Green Leaf Cafe", "City Medicals", "Sri Ganesh Stores", "Urban Fuel Station",
           "Paper Point Stationers", "Kumar Hardware", "Hotel Annapurna", "Fresh Basket Mart", "Metro Chemists"]
ITEMS = ["Masala Dosa", "Filter Coffee", "Notebook", "Paracetamol", "Rice 5kg", "Petrol", "Pen Set",
         "Bread", "Milk 1L", "LED Bulb", "Biscuits", "Tea"]


def make_burst_receipt(n, rnd):
    """A unique, realistic receipt with known answers (vendor, total, date)."""
    vendor = f"{rnd.choice(VENDORS)} {n}"
    day = datetime(2026, rnd.randint(1, 9), rnd.randint(1, 28))
    items = [(rnd.choice(ITEMS), Decimal(rnd.randint(1500, 90000)) / 100) for _ in range(rnd.randint(2, 5))]
    sub = sum(p for _, p in items)
    tax = (sub * Decimal("0.025")).quantize(Decimal("0.01"))
    total = sub + 2 * tax
    lines = [vendor, f"Bill No: B{n:05d}", f"Date: {day:%d/%m/%Y}", ""]
    lines += [f"{name}  {price:.2f}" for name, price in items]
    lines += ["", f"Sub Total  {sub:.2f}", f"CGST 2.5%  {tax:.2f}", f"SGST 2.5%  {tax:.2f}",
              f"Grand Total  Rs. {total:,.2f}", "Thank you, visit again"]
    return make_pdf(lines), {"vendor": vendor, "amount": f"{total:.2f}", "date": f"{day:%Y-%m-%d}"}


def lambda_cost_per_1000(billed_ms, memory_mb, arch):
    if billed_ms is None:
        return None
    per = Decimal(str(billed_ms)) / 1000 * Decimal(memory_mb) / 1024 * PRICE_GB_S[arch] + PRICE_REQUEST
    return float(round(per * 1000, 6))


# ============================================================== AWS plumbing

class Aws:
    def __init__(self, session=None):
        self.session = session or boto3.session.Session()
        # Enough connections for 32 parallel uploads, and patient retries for a shaky home connection.
        config = Config(max_pool_connections=48, retries={"max_attempts": 8, "mode": "standard"},
                        connect_timeout=10, read_timeout=60) if Config else None
        c = (lambda name: self.session.client(name, config=config)) if config else self.session.client
        self.cf, self.s3, self.ddb, self.logs = c("cloudformation"), c("s3"), c("dynamodb"), c("logs")
        self.lam, self.ec2, self.cw, self.ssm = c("lambda"), c("ec2"), c("cloudwatch"), c("ssm")
        shared = self.outputs("receipt-shared")
        self.bucket, self.table = shared["BucketName"], shared["TableName"]
        self.function = self.outputs("receipt-serverless").get("FunctionName", "receipt-pipeline-processor")
        self._de = TypeDeserializer()

    def outputs(self, stack):
        s = self.cf.describe_stacks(StackName=stack)["Stacks"][0]
        return {o["OutputKey"]: o["OutputValue"] for o in s.get("Outputs", [])}

    def parameters(self, stack):
        s = self.cf.describe_stacks(StackName=stack)["Stacks"][0]
        return {p["ParameterKey"]: p.get("ParameterValue") for p in s.get("Parameters", [])}

    def upload(self, key, body, content_type):
        """Upload one file; returns (etag, local_start, local_end, server_date_epoch)."""
        for attempt in range(6):  # on top of boto3's own retries: a dropped Wi-Fi link can take a few seconds
            start = time.time()
            try:
                r = self.s3.put_object(Bucket=self.bucket, Key=key, Body=body, ContentType=content_type)
                break
            except (EndpointConnectionError, ConnectionClosedError, ReadTimeoutError, ConnectTimeoutError):
                if attempt == 5:
                    raise
                time.sleep(2 ** attempt)
        end = time.time()
        date = r.get("ResponseMetadata", {}).get("HTTPHeaders", {}).get("date")
        server = email.utils.parsedate_to_datetime(date).timestamp() if date else None
        return r["ETag"].strip('"'), start, end, server

    def get_items(self, ids):
        """{receipt_id: item} for those that exist."""
        found, ids = {}, list(ids)
        for i in range(0, len(ids), 100):
            request = {self.table: {"Keys": [{"receipt_id": {"S": x}} for x in ids[i:i + 100]]}}
            while request:
                r = self.ddb.batch_get_item(RequestItems=request)
                for raw in r.get("Responses", {}).get(self.table, []):
                    item = {k: self._de.deserialize(v) for k, v in raw.items()}
                    found[item["receipt_id"]] = item
                request = r.get("UnprocessedKeys") or None
        return found

    def wait_items(self, ids, timeout, label="", every=2.0):
        ids = set(ids)
        deadline, found, last = time.time() + timeout, {}, -1
        while time.time() < deadline:
            found.update(self.get_items(ids - set(found)))
            if len(found) != last:
                say(f"    {label}{len(found)}/{len(ids)} saved")
                last = len(found)
            if len(found) == len(ids):
                break
            time.sleep(every)
        return found

    def lambda_logs(self, since_ms, keys, timeout=180):
        """REPORT data for every key (waits for CloudWatch Logs to catch up)."""
        deadline, result = time.time() + timeout, {}
        while True:
            messages, kwargs = [], {"logGroupName": LOG_GROUP, "startTime": int(since_ms)}
            while True:
                page = self.logs.filter_log_events(**kwargs)
                messages += [e["message"] for e in page.get("events", [])]
                if not page.get("nextToken"):
                    break
                kwargs["nextToken"] = page["nextToken"]
            result = parse_lambda_logs(messages)
            missing = [k for k in keys if k not in result]
            if not missing or time.time() > deadline:
                return result
            time.sleep(10)

    def wait_function(self):
        self.lam.get_waiter("function_updated_v2").wait(FunctionName=self.function)

    def ec2_start(self):
        """Make sure the EC2 server runs and its poller answers. Returns True if we started it."""
        instance = self.outputs("receipt-ec2")["InstanceId"]
        state = self.ec2.describe_instances(InstanceIds=[instance])["Reservations"][0]["Instances"][0]["State"]["Name"]
        started = False
        if state == "stopping":
            self.ec2.get_waiter("instance_stopped").wait(InstanceIds=[instance])
            state = "stopped"
        if state == "stopped":
            say("  Starting the EC2 server (about a minute)...")
            self.ec2.start_instances(InstanceIds=[instance])
            started, state = True, "pending"
        if state == "pending":
            self.ec2.get_waiter("instance_running").wait(InstanceIds=[instance])
        return instance, started

    def calibrate_clock(self, n=40):
        """(offset_ms, uncertainty_ms) between this laptop and AWS, from n quick one-at-a-time requests."""
        samples = []
        for _ in range(n):
            send = time.time()
            r = self.s3.list_objects_v2(Bucket=self.bucket, Prefix="deploy/", MaxKeys=1)
            recv = time.time()
            date = r.get("ResponseMetadata", {}).get("HTTPHeaders", {}).get("date")
            if date:
                samples.append((send, recv, email.utils.parsedate_to_datetime(date).timestamp()))
            time.sleep(random.uniform(0.02, 0.12))  # spread the requests across second boundaries
        return bracket_offset(samples) if samples else None

    def set_poll_seconds(self, instance, seconds, timeout=300):
        """Change how often the EC2 poller checks for new files (via Session Manager), and restart it."""
        command = (f"sed -i 's/^POLL_SECONDS=.*/POLL_SECONDS={int(seconds)}/' /etc/receipt-poller.env"
                   " && systemctl restart receipt-poller && grep ^POLL_SECONDS /etc/receipt-poller.env")
        deadline = time.time() + timeout
        while True:  # right after a start, the server needs a minute before Session Manager can reach it
            try:
                cid = self.ssm.send_command(InstanceIds=[instance], DocumentName="AWS-RunShellScript",
                                            Parameters={"commands": [command]})["Command"]["CommandId"]
                break
            except Exception as exc:
                if time.time() > deadline:
                    raise RuntimeError(f"could not reach the EC2 server through Session Manager: {exc!r}")
                time.sleep(10)
        while time.time() < deadline:
            time.sleep(3)
            try:
                r = self.ssm.get_command_invocation(CommandId=cid, InstanceId=instance)
            except Exception:
                continue
            if r["Status"] == "Success":
                return
            if r["Status"] in ("Failed", "Cancelled", "TimedOut"):
                raise RuntimeError(f"changing the poll interval failed: {r.get('StandardErrorContent', '')[:200]}")
        raise RuntimeError("changing the poll interval timed out")

    def ec2_stop(self, instance):
        self.ec2.stop_instances(InstanceIds=[instance])


def save(folder, name, data):
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, name)
    if name.endswith(".csv"):
        fields = sorted({k for row in data for k in row}, key=lambda k: (k not in ("experiment", "file", "version"), k))
        with open(path, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=fields)
            w.writeheader()
            w.writerows(data)
    else:
        with open(path, "w") as fh:
            json.dump(data, fh, indent=2, default=str)
    return path


CONTENT_TYPES = {"pdf": "application/pdf", "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg"}


def ctype(name):
    return CONTENT_TYPES[name.lower().rsplit(".", 1)[-1]]


# ============================================================== 1. accuracy on real files

def run_accuracy(aws, run, out):
    say("\n[accuracy] every SROIE receipt scan and every GST invoice, read by the Lambda version on AWS")
    real = json.load(open(os.path.join(HERE, "real-receipts", "answers.json")))
    gst = json.load(open(os.path.join(HERE, "gst-invoices", "answers.json")))
    base = f"serverless/experiments/{run}/accuracy"
    jobs = [("real", a, os.path.join(HERE, "real-receipts", a["file"])) for a in real]
    # Tampered copies share vendor, total and date with their originals (a duplicate by definition),
    # so they go in their own folder; the duplicate test uses only the re-shot copy.
    jobs += [("gst" if a["expect_gst"] == "OK" else "gst-tampered", a, os.path.join(HERE, "gst-invoices", a["file"]))
             for a in gst if not a.get("expect_duplicate_of")]
    later = [("gst", a, os.path.join(HERE, "gst-invoices", a["file"])) for a in gst if a.get("expect_duplicate_of")]
    missing = [p for _, _, p in jobs + later if not os.path.exists(p)]
    if missing:
        say(f"  {len(missing)} test files are missing (for example {missing[0]}). Run: python3 tests/fetch_datasets.py")
        return None

    def send(batch):
        """Upload a chunk with several connections at once (one at a time is slow on a home connection)."""
        ids = {}

        def one(item):
            group, a, path = item
            key = f"{base}/{group}/{a['file']}"
            etag, *_ = aws.upload(key, open(path, "rb").read(), ctype(path))
            return receipt_id_for(aws.bucket, key, etag), (group, a, key)

        with ThreadPoolExecutor(8) as pool:
            ids.update(pool.map(one, batch))
        return ids

    # Sent in chunks so Rekognition (a few calls per photo) is never flooded; each chunk is read back before the next.
    since = time.time() * 1000 - 5000
    ids, found = {}, {}
    CHUNK = 40
    say(f"  {len(jobs)} files + {len(later)} re-shot duplicates, {CHUNK} at a time")
    for i in range(0, len(jobs), CHUNK):
        t0 = time.time()
        part = send(jobs[i:i + CHUNK])
        say(f"    uploaded {i + len(part)}/{len(jobs)} in {time.time() - t0:.0f} s")
        found.update(aws.wait_items(part, 300, f"read {i + len(part)}/{len(jobs)}: ", every=2))
        ids.update(part)
    ids2 = send(later)  # the re-shot invoices go last, so the originals are already on record
    found.update(aws.wait_items(ids2, 300, "duplicates: "))
    ids.update(ids2)
    reports = aws.lambda_logs(since, [k for _, _, k in ids.values()], timeout=300)

    rows = []
    for rid, (group, a, key) in ids.items():
        item = found.get(rid, {})
        ok = score_fields(item, a) if item else {"vendor": False, "amount": False, "date": False, "all": False}
        row = {"experiment": "accuracy", "group": group, "file": a["file"], "status": item.get("status", "MISSING"),
               "vendor": item.get("vendor"), "amount": str(item.get("amount")), "date": item.get("receipt_date"),
               "want_vendor": a.get("vendor"), "want_amount": a.get("amount"), "want_date": a.get("date"),
               "vendor_ok": ok["vendor"], "amount_ok": ok["amount"], "date_ok": ok["date"], "all_ok": ok["all"],
               "ocr_calls": item.get("ocr_calls"), "processing_ms": item.get("processing_ms"),
               "billed_ms": reports.get(key, {}).get("billed_ms"), "category": item.get("category"),
               "gstin": item.get("gstin"), "gst_check": item.get("gst_check"), "duplicate_of": item.get("duplicate_of")}
        if group.startswith("gst"):
            row["want_gst"] = a["expect_gst"]
            row["gst_ok"] = str(item.get("gst_check", "")).startswith(a["expect_gst"])
            row["gstin_ok"] = item.get("gstin") == a.get("gstin")
            if a.get("expect_duplicate_of"):
                row["duplicate_ok"] = str(item.get("duplicate_of", "")).endswith(a["expect_duplicate_of"])
        rows.append(row)

    def share(rs, field):
        return round(100 * sum(1 for r in rs if r.get(field)) / len(rs), 1) if rs else None

    real_rows = [r for r in rows if r["group"] == "real"]
    gst_rows = [r for r in rows if r["group"].startswith("gst")]
    summary = {
        "real_receipts": {"n": len(real_rows), **{f"{f}_pct": share(real_rows, f + "_ok") for f in ("vendor", "amount", "date", "all")},
                          "ocr_calls": stats([r["ocr_calls"] for r in real_rows]),
                          "processing_ms": stats([r["processing_ms"] for r in real_rows]),
                          "billed_ms": stats([r["billed_ms"] for r in real_rows]),
                          "over_100_words": sum(1 for r in real_rows if (r["ocr_calls"] or 0) > 1),
                          "needs_review": sum(1 for r in real_rows if r["status"] == "NEEDS_REVIEW"),
                          "not_saved": sum(1 for r in real_rows if r["status"] in ("MISSING", "FAILED")),
                          "gst_false_alarms": sum(1 for r in real_rows if str(r.get("gst_check") or "").startswith("PROBLEM"))},
        "gst_invoices": {"n": len(gst_rows),
                         "verdict_correct": f"{sum(1 for r in gst_rows if r['gst_ok'])}/{len(gst_rows)}",
                         "valid_passed": f"{sum(1 for r in gst_rows if r['want_gst'] == 'OK' and r['gst_ok'])}/{sum(1 for r in gst_rows if r['want_gst'] == 'OK')}",
                         "tampered_caught": f"{sum(1 for r in gst_rows if r['want_gst'] == 'PROBLEM' and r['gst_ok'])}/{sum(1 for r in gst_rows if r['want_gst'] == 'PROBLEM')}",
                         "gstin_read_correctly": f"{sum(1 for r in gst_rows if r['gstin_ok'] or r['want_gst'] == 'PROBLEM')}/{len(gst_rows)}",
                         "fields_all_correct_pct": share(gst_rows, "all_ok"),
                         "duplicate_detected": all(r.get("duplicate_ok", True) for r in gst_rows)},
    }
    save(out, "accuracy.csv", rows)
    save(out, "accuracy.json", summary)
    r, g = summary["real_receipts"], summary["gst_invoices"]
    say(f"  Real scans: vendor {r['vendor_pct']}%, total {r['amount_pct']}%, date {r['date_pct']}%, all three {r['all_pct']}%"
        f"  (OCR calls avg {r['ocr_calls'].get('mean')}, {r['over_100_words']} needed banding)")
    say(f"  GST: verdict right {g['verdict_correct']}, valid passed {g['valid_passed']}, tampered caught {g['tampered_caught']},"
        f" duplicate caught: {g['duplicate_detected']}")
    return summary


# ============================================================== 2. cold starts

COLD_MODES = {"startup": {}, "lazy": {"LAZY_INIT": "1"}}  # what each mode adds to the function's environment


def run_coldstart(aws, run, out, reps=5):
    say(f"\n[coldstart] {reps} forced cold starts each: load at start-up (default) vs load on first use, PDF and photo")
    batch = os.path.join(HERE, "batch")
    files = {"pdf": "01-nilgiri-office-supplies.pdf", "photo": "11-fresh-mart-photo.png"}
    original = aws.lam.get_function_configuration(FunctionName=aws.function)["Environment"]["Variables"]
    since = time.time() * 1000 - 5000
    keys = []
    try:
        for mode in COLD_MODES:
            for kind, name in files.items():
                for i in range(reps):
                    env = {k: v for k, v in original.items() if k not in ("LAZY_INIT", "EAGER_INIT", "COLD_START_RUN")}
                    env.update(COLD_MODES[mode], COLD_START_RUN=f"{run}-{mode}-{kind}-{i}")
                    aws.lam.update_function_configuration(FunctionName=aws.function, Environment={"Variables": env})
                    aws.wait_function()  # a configuration change replaces every warm copy: the next call is cold
                    key = f"serverless/experiments/{run}/coldstart/{mode}-{kind}-{i}/{name}"
                    etag, *_ = aws.upload(key, open(os.path.join(batch, name), "rb").read(), ctype(name))
                    aws.wait_items([receipt_id_for(aws.bucket, key, etag)], 120, f"{mode} {kind} #{i + 1}: ", every=1)
                    keys.append((mode, kind, key))
    finally:
        aws.lam.update_function_configuration(FunctionName=aws.function, Environment={"Variables": original})
        aws.wait_function()
        say("  Function environment restored.")

    reports = aws.lambda_logs(since, [k for _, _, k in keys])
    rows, summary = [], {}
    for mode, kind, key in keys:
        r = reports.get(key, {})
        total = (r.get("init_ms") or 0) + (r.get("duration_ms") or 0) if r else None
        rows.append({"experiment": "coldstart", "mode": mode, "kind": kind, "file": key,
                     "init_ms": r.get("init_ms"), "duration_ms": r.get("duration_ms"), "billed_ms": r.get("billed_ms"),
                     "cold_total_ms": total, "max_memory_mb": r.get("max_memory_mb")})
    for mode in COLD_MODES:
        for kind in files:
            sel = [x for x in rows if x["mode"] == mode and x["kind"] == kind]
            summary[f"{mode}_{kind}"] = {f: stats([x[f] for x in sel]) for f in ("init_ms", "duration_ms", "billed_ms", "cold_total_ms")}
    for kind in files:
        s, l = summary[f"startup_{kind}"]["cold_total_ms"].get("p50"), summary[f"lazy_{kind}"]["cold_total_ms"].get("p50")
        if s and l:
            summary[f"{kind}_lazy_slowdown_x"] = round(l / s, 2)
            say(f"  {kind}: first request {s:.0f} ms loading at start-up vs {l:.0f} ms loading on first use (median)")
    save(out, "coldstart.csv", rows)
    save(out, "coldstart.json", summary)
    return summary


# ============================================================== 3. memory x processor tuning

def run_tune(aws, run, out, memories=(128, 256, 512, 1024, 2048), arches=("x86_64", "arm64")):
    say(f"\n[tune] memory {list(memories)} MB x {list(arches)}, the 12 test receipts one after another for each")
    answers = json.load(open(os.path.join(HERE, "batch", "answers.json")))
    config = aws.lam.get_function_configuration(FunctionName=aws.function)
    original_memory, original_arch = config["MemorySize"], config.get("Architectures", ["x86_64"])[0]
    code_key = aws.parameters("receipt-serverless")["CodeS3Key"]
    since = time.time() * 1000 - 5000
    runs = []
    try:
        for arch in arches:
            aws.lam.update_function_code(FunctionName=aws.function, S3Bucket=aws.bucket, S3Key=code_key, Architectures=[arch])
            aws.wait_function()
            for memory in memories:
                aws.lam.update_function_configuration(FunctionName=aws.function, MemorySize=memory)
                aws.wait_function()
                for a in answers:
                    key = f"serverless/experiments/{run}/tune/{arch}-{memory}/{a['file']}"
                    path = os.path.join(HERE, "batch", a["file"])
                    etag, *_ = aws.upload(key, open(path, "rb").read(), ctype(path))
                    rid = receipt_id_for(aws.bucket, key, etag)
                    item = aws.wait_items([rid], 90, "", every=0.5).get(rid, {})
                    runs.append((arch, memory, a, key, item))
                say(f"  {arch} {memory} MB: done")
    finally:
        aws.lam.update_function_code(FunctionName=aws.function, S3Bucket=aws.bucket, S3Key=code_key, Architectures=[original_arch])
        aws.wait_function()
        aws.lam.update_function_configuration(FunctionName=aws.function, MemorySize=original_memory)
        aws.wait_function()
        say(f"  Function restored to {original_memory} MB, {original_arch}.")

    reports = aws.lambda_logs(since, [r[3] for r in runs])
    rows = []
    for arch, memory, a, key, item in runs:
        r = reports.get(key, {})
        rows.append({"experiment": "tune", "arch": arch, "memory_mb": memory, "file": a["file"],
                     "cold": r.get("init_ms") is not None, "init_ms": r.get("init_ms"), "duration_ms": r.get("duration_ms"),
                     "billed_ms": r.get("billed_ms"), "max_memory_mb": r.get("max_memory_mb"),
                     "processing_ms": item.get("processing_ms"), "status": item.get("status", "MISSING"),
                     "all_ok": score_fields(item, a)["all"] if item else False})
    summary = []
    for arch in arches:
        for memory in memories:
            sel = [x for x in rows if x["arch"] == arch and x["memory_mb"] == memory]
            warm = [x for x in sel if not x["cold"] and x["billed_ms"] is not None]
            warm_billed = statistics.mean(x["billed_ms"] for x in warm) if warm else None
            summary.append({"arch": arch, "memory_mb": memory, "receipts": len(sel),
                            "fields_all_correct": sum(1 for x in sel if x["all_ok"]),
                            "warm_duration_ms_avg": round(statistics.mean(x["duration_ms"] for x in warm), 1) if warm else None,
                            "warm_billed_ms_avg": round(warm_billed, 1) if warm_billed else None,
                            "cold_init_ms": next((x["init_ms"] for x in sel if x["cold"]), None),
                            "max_memory_used_mb": max((x["max_memory_mb"] or 0) for x in sel) if sel else None,
                            "cost_per_1000_receipts_usd": lambda_cost_per_1000(warm_billed, memory, arch)})
    priced = [s for s in summary if s["cost_per_1000_receipts_usd"] is not None]
    best = min(priced, key=lambda s: s["cost_per_1000_receipts_usd"]) if priced else None
    fastest = min(priced, key=lambda s: s["warm_duration_ms_avg"]) if priced else None
    result = {"configs": summary, "cheapest": best, "fastest": fastest,
              "prices_used": {k: str(v) for k, v in PRICE_GB_S.items()}}
    for s in summary:
        say(f"  {s['arch']:<7} {s['memory_mb']:>5} MB  warm {s['warm_duration_ms_avg']} ms, "
            f"${s['cost_per_1000_receipts_usd']} per 1,000, max memory {s['max_memory_used_mb']} MB")
    if best:
        say(f"  Cheapest: {best['arch']} {best['memory_mb']} MB.  Fastest: {fastest['arch']} {fastest['memory_mb']} MB.")
    save(out, "tune.csv", rows)
    save(out, "tune.json", result)
    return result


# ============================================================== 4. burst load: Lambda vs EC2

DEFAULT_POLL_SECONDS = 10  # what ec2-server/template.yaml sets


def run_burst(aws, run, out, sizes=(10, 50, 200), keep_running=False, seed=7, poll=None):
    say(f"\n[burst] {list(sizes)} receipts uploaded at once to both versions"
        + (f", EC2 checking every {poll} s" if poll else ""))
    rnd = random.Random(seed)
    limit = aws.lam.get_account_settings().get("AccountLimit", {}).get("ConcurrentExecutions")
    say(f"  This account may run up to {limit} Lambda copies at the same time.")
    instance, started = aws.ec2_start()
    changed_poll = False
    try:
        if poll and poll != DEFAULT_POLL_SECONDS:
            say(f"  Setting the EC2 poller to check every {poll} s...")
            aws.set_poll_seconds(instance, poll)
            changed_poll = True
        # Warm the poller so the first burst does not include the server's start-up.
        body, _ = make_burst_receipt(0, rnd)
        key = f"ec2/experiments/{run}/burst-warmup/warmup.pdf"
        etag, *_ = aws.upload(key, body, "application/pdf")
        if not aws.wait_items([receipt_id_for(aws.bucket, key, etag)], 420, "EC2 warm-up: "):
            say("  The EC2 poller did not answer within 7 minutes; check: bash ../ec2-server/ec2-power.sh logs")
            return None

        rows, windows, offsets, n = [], {}, {}, 0
        for size in sizes:
            jobs = []
            for _ in range(size):
                n += 1
                body, answer = make_burst_receipt(n, rnd)
                for version in ("serverless", "ec2"):
                    jobs.append((version, f"{version}/experiments/{run}/burst-{size}/r{n:05d}.pdf", body, answer))
            rnd.shuffle(jobs)
            calibration = aws.calibrate_clock()
            say(f"  Burst of {size} (x2 versions): clock offset {calibration[0]:.0f} ms (+/- {calibration[1]:.0f} ms); "
                f"uploading {len(jobs)} files with 32 parallel uploads..." if calibration else
                f"  Burst of {size}: clock calibration failed, using the upload estimate")
            t0 = time.time()

            def put(job):
                version, key, body, answer = job
                etag, start, end, server = aws.upload(key, body, "application/pdf")
                return version, key, answer, etag, start, end, server

            with concurrent.futures.ThreadPoolExecutor(32) as pool:
                sent = list(pool.map(put, jobs))
            offset = calibration[0] if calibration else clock_offset_ms([(s, e, srv) for *_, s, e, srv in sent])
            offsets[size] = {"offset_ms": round(offset), "uncertainty_ms": round(calibration[1]) if calibration else None}
            ids = {receipt_id_for(aws.bucket, key, etag): (version, key, answer, end)
                   for version, key, answer, etag, start, end, server in sent}
            found = aws.wait_items(ids, 600 + 3 * size, f"burst {size}: ")
            windows[size] = (t0 - 60, time.time() + 60)
            for rid, (version, key, answer, uploaded) in ids.items():
                item = found.get(rid)
                up_ms = uploaded * 1000 + offset
                row = {"experiment": "burst", "burst": size, "version": "Lambda" if version == "serverless" else "EC2",
                       "file": key, "status": item.get("status") if item else "MISSING", "clock_offset_ms": round(offset)}
                if item and item.get("uploaded_at_ms") is not None:
                    # AWS's own record of when the upload finished: exact (ms) from the S3 event for Lambda;
                    # whole seconds from the S3 listing for EC2, so add half a second (the average rounding).
                    up_ms = float(item["uploaded_at_ms"]) + (500 if item.get("upload_time_source") == "s3-listing" else 0)
                    row["upload_clock"] = item.get("upload_time_source")
                else:
                    row["upload_clock"] = "laptop"
                if item:
                    started_ms, finished_ms = float(item["started_at_ms"]), float(item["processed_at_ms"])
                    row.update({"wait_ms": max(0, int(started_ms - up_ms)),
                                "processing_ms": int(item["processing_ms"]),
                                "end_to_end_ms": max(0, int(finished_ms - up_ms)),
                                "uploaded_ms": int(up_ms), "finished_ms": int(finished_ms),
                                "all_ok": score_fields(item, answer)["all"]})
                rows.append(row)
    finally:
        if changed_poll:
            try:
                aws.set_poll_seconds(instance, DEFAULT_POLL_SECONDS)
                say(f"  EC2 poller set back to every {DEFAULT_POLL_SECONDS} s.")
            except Exception as exc:
                say(f"  Could not set the poller back ({exc!r}); run: bash ../ec2-server/deploy.sh")
        if started and not keep_running:
            aws.ec2_stop(instance)
            say("  EC2 server stopped again (this script started it).")

    summary = {"lambda_concurrency_limit": limit, "ec2_poll_seconds": poll or DEFAULT_POLL_SECONDS,
               "clock": offsets, "bursts": []}
    say("  Waiting a minute for CloudWatch metrics...")
    time.sleep(70)
    for size in sizes:
        for version in ("Lambda", "EC2"):
            sel = [r for r in rows if r["burst"] == size and r["version"] == version and "end_to_end_ms" in r]
            span = (max(r["finished_ms"] for r in sel) - min(r["uploaded_ms"] for r in sel)) / 1000 if sel else None
            entry = {"burst": size, "version": version, "done": len(sel),
                     "fields_all_correct": sum(1 for r in sel if r["all_ok"]),
                     "end_to_end_ms": stats([r["end_to_end_ms"] for r in sel]),
                     "wait_ms": stats([r["wait_ms"] for r in sel]),
                     "processing_ms": stats([r["processing_ms"] for r in sel]),
                     "all_done_after_s": round(span, 1) if span else None,
                     "throughput_per_s": round(len(sel) / span, 2) if span else None}
            if version == "Lambda":
                start, end = windows[size]
                entry.update(lambda_metrics(aws, start, end))
            summary["bursts"].append(entry)
            say(f"  {size:>4} {version:<6} all done after {entry['all_done_after_s']} s, "
                f"delay p50 {entry['end_to_end_ms'].get('p50')} ms / p95 {entry['end_to_end_ms'].get('p95')} ms"
                + (f", peak {entry.get('peak_concurrency')} copies" if version == "Lambda" else ""))
    save(out, "burst.csv", rows)
    save(out, "burst.json", summary)
    return summary


def lambda_metrics(aws, start, end):
    def metric(name, stat):
        try:
            r = aws.cw.get_metric_statistics(
                Namespace="AWS/Lambda", MetricName=name, Dimensions=[{"Name": "FunctionName", "Value": aws.function}],
                StartTime=datetime.fromtimestamp(start, timezone.utc), EndTime=datetime.fromtimestamp(end, timezone.utc),
                Period=60, Statistics=[stat])
            points = [p[stat] for p in r.get("Datapoints", [])]
            return (max(points) if stat == "Maximum" else sum(points)) if points else None
        except Exception:  # metrics are a bonus; never fail the run over them
            return None
    return {"peak_concurrency": metric("ConcurrentExecutions", "Maximum"), "throttles": metric("Throttles", "Sum")}


# ============================================================== main

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("experiment", nargs="+", choices=["all", "accuracy", "coldstart", "tune", "burst"])
    ap.add_argument("--sizes", default="10,50,200", help="burst sizes, e.g. 10,50,200")
    ap.add_argument("--reps", type=int, default=5, help="cold starts per mode and file type")
    ap.add_argument("--memories", default="128,256,512,1024,2048")
    ap.add_argument("--arches", default="x86_64,arm64")
    ap.add_argument("--keep-running", action="store_true", help="leave the EC2 server on after burst")
    ap.add_argument("--poll", type=int, help="burst only: EC2 check interval in seconds for this run (default 10)")
    args = ap.parse_args()

    aws = Aws()
    run = "exp-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out = os.path.join(HERE, "results", run)
    say(f"Run {run} in {aws.session.region_name}; results will be saved in comparison/results/{run}/")
    todo = ["accuracy", "coldstart", "tune", "burst"] if "all" in args.experiment else args.experiment
    done = {}
    for name in todo:
        try:
            if name == "accuracy":
                done[name] = run_accuracy(aws, run, out)
            elif name == "coldstart":
                done[name] = run_coldstart(aws, run, out, args.reps)
            elif name == "tune":
                done[name] = run_tune(aws, run, out, [int(x) for x in args.memories.split(",")], args.arches.split(","))
            elif name == "burst":
                done[name] = run_burst(aws, run, out, [int(x) for x in args.sizes.split(",")], args.keep_running,
                                       poll=args.poll)
        except Exception as exc:  # one failed experiment should not lose the others
            say(f"  {name} FAILED: {exc!r}")
            done[name] = {"error": repr(exc)}
    save(out, "summary.json", {"run": run, "region": aws.session.region_name, **done})
    say(f"\nAll results are in comparison/results/{run}/. To chart them: python3 analyze.py --full {run}")


if __name__ == "__main__":
    main()
