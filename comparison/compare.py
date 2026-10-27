#!/usr/bin/env python3
"""Comparison: run the same batch of receipts through both versions and compare them.

Run in AWS CloudShell, inside the comparison folder:

    python3 compare.py                 # full comparison (about 5 minutes)
    python3 compare.py --keep-running  # leave the EC2 server on afterwards

What it does:
  1. Makes sure the EC2 server is on and its poller is working (warm-up file).
  2. Uploads the same 12 test receipts to serverless/ (Lambda) and ec2/ (EC2).
  3. Waits until both versions have saved a row for every receipt.
  4. Measures, for each version: accuracy against the known answers, processing
     time, delay from upload to result, and (for Lambda) billed time and cold starts.
  5. Prints a summary, saves the details to CSV and JSON, and stops the server
     again if this script started it.
"""

import argparse
import csv
import json
import os
import re
import statistics
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

import boto3

LOG_GROUP = "/aws/lambda/receipt-pipeline-processor"
VERSIONS = ("serverless", "ec2")  # S3 folder for each version
LABEL = {"serverless": "Lambda", "ec2": "EC2"}


# ---------------------------------------------------------------- helpers

def say(msg=""):
    print(msg, flush=True)


def stack_outputs(cf, name):
    stack = cf.describe_stacks(StackName=name)["Stacks"][0]
    return {o["OutputKey"]: o["OutputValue"] for o in stack.get("Outputs", [])}


def iso_to_epoch(text):
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()


def to_decimal(value):
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError):
        return None


def scan_rows(table, prefixes):
    """All table items whose s3_key starts with one of the prefixes."""
    names = [f":p{i}" for i in range(len(prefixes))]
    expr = " OR ".join(f"begins_with(s3_key, {n})" for n in names)
    values = dict(zip(names, prefixes))
    items, kwargs = [], {"FilterExpression": expr, "ExpressionAttributeValues": values}
    while True:
        page = table.scan(**kwargs)
        items.extend(page.get("Items", []))
        if "LastEvaluatedKey" not in page:
            return items
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def wait_for_rows(table, keys, timeout, label):
    """Wait until every key has a row. Returns {s3_key: item}."""
    prefixes = sorted({k.rsplit("/", 1)[0] + "/" for k in keys})
    deadline = time.time() + timeout
    found = {}
    while time.time() < deadline:
        found = {i["s3_key"]: i for i in scan_rows(table, prefixes) if i["s3_key"] in keys}
        done = {v: sum(1 for k in found if k.startswith(v + "/")) for v in VERSIONS}
        total = {v: sum(1 for k in keys if k.startswith(v + "/")) for v in VERSIONS}
        progress = "  ".join(f"{LABEL[v]} {done[v]}/{total[v]}" for v in VERSIONS if total[v])
        say(f"  {label}: {progress}")
        if len(found) == len(keys):
            return found
        time.sleep(5)
    return found


# ---------------------------------------------------------------- analysis (pure, testable)

def check_fields(item, answer):
    """Which of the three fields match the known answer."""
    vendor_ok = (item.get("vendor") or "").strip().lower() == (answer["vendor"] or "").strip().lower()
    got_amount, want_amount = to_decimal(item.get("amount")), to_decimal(answer["amount"])
    amount_ok = got_amount is not None and want_amount is not None and got_amount == want_amount
    date_ok = item.get("receipt_date") == answer["date"]  # None == None counts: the date is genuinely absent
    return {"vendor": vendor_ok, "amount": amount_ok, "date": date_ok}


def summarize(rows, answers, uploaded_at):
    """rows: {s3_key: item}; answers: list from answers.json; uploaded_at: {s3_key: epoch seconds}."""
    by_file = {a["file"]: a for a in answers}
    detail, summary = [], {}
    for version in VERSIONS:
        times, delays, fields_ok, fully_ok, statuses = [], [], 0, 0, {}
        for file, answer in by_file.items():
            key = None
            for k in rows:
                if k.startswith(version + "/") and k.endswith("/" + file):
                    key = k
            item = rows.get(key) if key else None
            if item is None:
                detail.append({"version": LABEL[version], "file": file, "status": "MISSING"})
                statuses["MISSING"] = statuses.get("MISSING", 0) + 1
                continue
            ok = check_fields(item, answer)
            fields_ok += sum(ok.values())
            fully_ok += all(ok.values())
            status = item.get("status", "?")
            statuses[status] = statuses.get(status, 0) + 1
            ms = int(item.get("processing_ms", 0))
            times.append(ms)
            delay = None
            if key in uploaded_at and item.get("processed_at"):
                # processed_at has 1-second resolution, so delays are approximate (+/- 1 s).
                delay = max(0.0, iso_to_epoch(item["processed_at"]) - uploaded_at[key])
                delays.append(delay)
            detail.append({
                "version": LABEL[version], "file": file, "status": status,
                "vendor": item.get("vendor"), "amount": str(item.get("amount")) if item.get("amount") is not None else None,
                "date": item.get("receipt_date"), "method": item.get("extraction_method"),
                "processing_ms": ms, "delay_s": round(delay, 1) if delay is not None else None,
                "vendor_ok": ok["vendor"], "amount_ok": ok["amount"], "date_ok": ok["date"],
            })
        n = len(by_file)
        summary[LABEL[version]] = {
            "receipts": n,
            "saved": n - statuses.get("MISSING", 0),
            "statuses": statuses,
            "fields_correct": f"{fields_ok}/{3 * n}",
            "field_accuracy_pct": round(100 * fields_ok / (3 * n), 1) if n else 0,
            "receipts_fully_correct": fully_ok,
            "processing_ms_avg": round(statistics.mean(times), 1) if times else None,
            "processing_ms_median": statistics.median(times) if times else None,
            "processing_ms_max": max(times) if times else None,
            "delay_s_avg": round(statistics.mean(delays), 1) if delays else None,
            "delay_s_max": round(max(delays), 1) if delays else None,
        }
    # Do the two versions agree with each other, receipt by receipt?
    pairs = {}
    for d in detail:
        if d["status"] != "MISSING":
            pairs.setdefault(d["file"], {})[d["version"]] = (d["vendor"], d["amount"], d["date"])
    agree = sum(1 for p in pairs.values() if len(p) == 2 and p["Lambda"] == p["EC2"])
    summary["agreement"] = f"{agree}/{len(by_file)} receipts got identical results from both versions"
    return summary, detail


def parse_report_lines(messages):
    """Billed time and cold starts from Lambda's REPORT log lines."""
    billed, inits = [], []
    for m in messages:
        if not m.startswith("REPORT"):
            continue
        b = re.search(r"Billed Duration: (\d+) ms", m)
        if b:
            billed.append(int(b.group(1)))
        i = re.search(r"Init Duration: ([\d.]+) ms", m)
        if i:
            inits.append(float(i.group(1)))
    return {
        "invocations_seen": len(billed),
        "billed_ms_avg": round(statistics.mean(billed), 1) if billed else None,
        "billed_ms_max": max(billed) if billed else None,
        "cold_starts": len(inits),
        "init_ms_avg": round(statistics.mean(inits), 1) if inits else None,
    }


def lambda_reports(logs, since_ms, expected, timeout=150):
    """Read REPORT lines written since the upload, waiting for all of them to arrive."""
    deadline = time.time() + timeout
    while True:
        messages, kwargs = [], {"logGroupName": LOG_GROUP, "startTime": since_ms, "filterPattern": "REPORT"}
        while True:
            page = logs.filter_log_events(**kwargs)
            messages.extend(e["message"] for e in page.get("events", []))
            if not page.get("nextToken"):
                break
            kwargs["nextToken"] = page["nextToken"]
        stats = parse_report_lines(messages)
        if stats["invocations_seen"] >= expected or time.time() > deadline:
            return stats
        time.sleep(10)


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--batch", default="batch", help="folder with the test receipts and answers.json")
    ap.add_argument("--keep-running", action="store_true", help="do not stop the EC2 server at the end")
    ap.add_argument("--timeout", type=int, default=600, help="seconds to wait for results")
    args = ap.parse_args()

    session = boto3.session.Session()
    cf, s3, ec2, logs = (session.client(n) for n in ("cloudformation", "s3", "ec2", "logs"))
    shared = stack_outputs(cf, "receipt-shared")
    bucket = shared["BucketName"]
    table = session.resource("dynamodb").Table(shared["TableName"])
    instance = stack_outputs(cf, "receipt-ec2")["InstanceId"]
    answers = json.load(open(os.path.join(args.batch, "answers.json")))
    run = "comparison-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    say(f"Run {run}: {len(answers)} receipts x 2 versions, region {session.region_name}\n")

    # 1. EC2 server on, poller confirmed working --------------------------------
    say("[1/5] Getting the EC2 server ready...")
    state = ec2.describe_instances(InstanceIds=[instance])["Reservations"][0]["Instances"][0]["State"]["Name"]
    started_by_us = False
    if state == "stopping":
        ec2.get_waiter("instance_stopped").wait(InstanceIds=[instance])
        state = "stopped"
    if state == "stopped":
        say("  Server was stopped; starting it (about 1 minute)...")
        ec2.start_instances(InstanceIds=[instance])
        started_by_us = True
        state = "pending"
    if state == "pending":
        ec2.get_waiter("instance_running").wait(InstanceIds=[instance])
    warm_key = f"ec2/{run}-warmup/{answers[0]['file']}"
    s3.upload_file(os.path.join(args.batch, answers[0]["file"]), bucket, warm_key)
    if not wait_for_rows(table, {warm_key}, 360, "warm-up"):
        say("  The EC2 poller did not respond within 6 minutes. Check: bash ec2-power.sh logs")
        sys.exit(1)
    say("  Server and poller are ready.\n")

    # 2. Same batch to both versions --------------------------------------------
    say("[2/5] Uploading the same receipts to both versions...")
    since_ms = int(time.time() * 1000) - 5000
    uploaded_at = {}
    for a in answers:
        for version in VERSIONS:
            key = f"{version}/{run}/{a['file']}"
            s3.upload_file(os.path.join(args.batch, a["file"]), bucket, key)
            uploaded_at[key] = time.time()
    say(f"  Uploaded {len(uploaded_at)} files.\n")

    # 3. Wait for every result -----------------------------------------------------
    say("[3/5] Waiting for both versions to finish...")
    rows = wait_for_rows(table, set(uploaded_at), args.timeout, "saved")
    say("")

    # 4. Measure ------------------------------------------------------------------
    say("[4/5] Measuring...")
    summary, detail = summarize(rows, answers, uploaded_at)
    summary["Lambda"].update(lambda_reports(logs, since_ms, len(answers)))
    summary["run"] = run
    say("")

    # 5. Report and tidy up ----------------------------------------------------------
    say("[5/5] Results")
    L, E = summary["Lambda"], summary["EC2"]
    table_rows = [
        ("Receipts saved", f"{L['saved']}/{L['receipts']}", f"{E['saved']}/{E['receipts']}"),
        ("Fields correct (vendor, amount, date)", f"{L['fields_correct']} ({L['field_accuracy_pct']}%)", f"{E['fields_correct']} ({E['field_accuracy_pct']}%)"),
        ("Receipts fully correct", L["receipts_fully_correct"], E["receipts_fully_correct"]),
        ("Statuses", json.dumps(L["statuses"]), json.dumps(E["statuses"])),
        ("Processing time avg / median / max (ms)", f"{L['processing_ms_avg']} / {L['processing_ms_median']} / {L['processing_ms_max']}", f"{E['processing_ms_avg']} / {E['processing_ms_median']} / {E['processing_ms_max']}"),
        ("Upload-to-result delay avg / max (s)", f"{L['delay_s_avg']} / {L['delay_s_max']}", f"{E['delay_s_avg']} / {E['delay_s_max']}"),
        ("Lambda billed time avg / max (ms)", f"{L['billed_ms_avg']} / {L['billed_ms_max']}", "n/a (billed per hour)"),
        ("Lambda cold starts", f"{L['cold_starts']} (avg start-up {L['init_ms_avg']} ms)", "n/a (always on)"),
    ]
    w = max(len(r[0]) for r in table_rows)
    say(f"  {'':<{w}}  {'Lambda':<40} EC2")
    for name, lv, ev in table_rows:
        say(f"  {name:<{w}}  {str(lv):<40} {ev}")
    say(f"  {summary['agreement']}")

    csv_path, json_path = f"{run}-results.csv", f"{run}-summary.json"
    with open(csv_path, "w", newline="") as fh:
        fields = ["version", "file", "status", "vendor", "amount", "date", "method", "processing_ms", "delay_s", "vendor_ok", "amount_ok", "date_ok"]
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for d in detail:
            writer.writerow({k: d.get(k) for k in fields})
    with open(json_path, "w") as fh:
        json.dump(summary, fh, indent=2, default=str)
    say(f"\n  Saved: {csv_path} (one row per receipt per version) and {json_path}")

    if started_by_us and not args.keep_running:
        ec2.stop_instances(InstanceIds=[instance])
        say("  EC2 server stopped again (this script started it).")
    elif not args.keep_running:
        say("  EC2 server was already running before this script; stop it with: bash ec2-power.sh stop")
    say("\nThe summary JSON above holds every number for the write-up.")


if __name__ == "__main__":
    main()
