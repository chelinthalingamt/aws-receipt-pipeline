"""Dress rehearsal for comparison/experiments.py against a simulated AWS.

    python3 tests/test_experiments.py

Every upload is processed immediately by the real lambda_function.process_receipt
(with fake S3/DynamoDB/Rekognition from fake_aws.py), and the simulated Lambda
writes the same log lines the real one does, so the whole runner - uploading,
waiting, matching log lines, scoring, restoring the function - is exercised
without an AWS account. Photos are "read" from their known answers.
"""
import email.utils
import hashlib
import io
import json
import os
import sys
import tempfile
import time
import types

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path[:0] = [HERE, os.path.join(ROOT, "serverless-lambda"), os.path.join(ROOT, "comparison")]
import fake_aws  # noqa: E402
fake_aws.install()
import lambda_function as lf  # noqa: E402
from make_gst_invoices import as_text  # noqa: E402

STORE, LOGS, STATE = {}, [], {"memory": 256, "arch": "x86_64", "cold": True, "env": {"TABLE_NAME": "t", "TOPIC_ARN": "a", "PIPELINE_SOURCE": "lambda"},
                              "ec2": "stopped", "updates": 0}
PHOTO_TEXT = {}


def photo_text():
    for folder in ("real-receipts", "gst-invoices", "batch"):
        for a in json.load(open(os.path.join(ROOT, "comparison", folder, "answers.json"))):
            if "invoice" in a:
                PHOTO_TEXT[a["file"]] = as_text(a["invoice"])
            else:
                PHOTO_TEXT[a["file"]] = "\n".join([a["vendor"] or "", f"Date: {a['date'] or ''}", f"TOTAL {a['amount']}"])


def rekognition_detect_text(Image, Filters=None):
    name = Image["S3Object"]["Name"].rsplit("/", 1)[-1]
    lines = PHOTO_TEXT[name].splitlines()
    dets = []
    for i, text in enumerate(lines):
        box = {"Left": 0.05, "Top": 0.02 + i * 0.9 / max(len(lines), 1), "Width": 0.5, "Height": 0.02}
        dets.append({"Type": "LINE", "DetectedText": text, "Geometry": {"BoundingBox": box}})
        dets += [{"Type": "WORD", "DetectedText": w, "Geometry": {"BoundingBox": box}} for w in text.split()]
    return {"TextDetections": dets}


def lambda_run(key, etag):
    rid = f"req-{len(LOGS):06d}"
    os.environ.update(STATE["env"])
    item = lf.process_receipt("bucket", key, etag, int(time.time() * 1000) - 200, "s3-event")
    duration = item["processing_ms"] + 5
    LOGS.append(f"[INFO]\t2026-10-25T00:00:00Z\t{rid}\t" + json.dumps({"event": "receipt_processed", **item}, default=str))
    report = (f"REPORT RequestId: {rid}\tDuration: {duration:.2f} ms\tBilled Duration: {duration + 1} ms\t"
              f"Memory Size: {STATE['memory']} MB\tMax Memory Used: 90 MB")
    if STATE["cold"]:
        report += f"\tInit Duration: {(600 if STATE['env'].get('LAZY_INIT') else 900) * 256 / STATE['memory']:.2f} ms"
        STATE["cold"] = False
    LOGS.append(report)


class Client(fake_aws.FakeClient):
    pass


def make_session():
    c = {}

    def put_object(Bucket, Key, Body, ContentType):
        STORE[Key] = Body
        etag = hashlib.md5(Body).hexdigest()
        if Key.startswith("serverless/"):
            lambda_run(Key, etag)
        elif Key.startswith("ec2/") and STATE["ec2"] == "running":
            os.environ["PIPELINE_SOURCE"] = "ec2"
            lf.process_receipt("bucket", Key, etag, int(time.time()) * 1000 - 1000, "s3-listing")
        return {"ETag": f'"{etag}"', "ResponseMetadata": {"HTTPHeaders": {"date": email.utils.formatdate(time.time(), usegmt=True)}}}

    fake_aws.client("s3").handlers["get_object"] = lambda Bucket, Key: {"Body": io.BytesIO(STORE[Key])}
    fake_aws.client("rekognition").handlers["detect_text"] = rekognition_detect_text
    fake_aws.client("sns")

    cf = Client("cloudformation")
    cf.handlers["describe_stacks"] = lambda StackName: {"Stacks": [{
        "Outputs": {"receipt-shared": [{"OutputKey": "BucketName", "OutputValue": "bucket"}, {"OutputKey": "TableName", "OutputValue": "t"}],
                    "receipt-serverless": [{"OutputKey": "FunctionName", "OutputValue": "fn"}],
                    "receipt-ec2": [{"OutputKey": "InstanceId", "OutputValue": "i-1"}]}[StackName],
        "Parameters": [{"ParameterKey": "CodeS3Key", "ParameterValue": "deploy/x.zip"}]}]}
    s3 = Client("s3")
    s3.handlers["put_object"] = put_object
    s3.handlers["list_objects_v2"] = lambda **kw: {"ResponseMetadata": {"HTTPHeaders": {"date": email.utils.formatdate(time.time(), usegmt=True)}}}
    ddb = fake_aws.client("dynamodb")

    def batch_get_item(RequestItems):
        (table, req), = RequestItems.items()
        out = []
        for k in req["Keys"]:
            got = ddb.get_item(TableName=table, Key=k).get("Item")
            if got:
                out.append(got)
        return {"Responses": {table: out}}
    ddb.handlers["batch_get_item"] = batch_get_item

    logs = Client("logs")
    logs.handlers["filter_log_events"] = lambda **kw: {"events": [{"message": m} for m in LOGS]}

    lam = Client("lambda")

    def update_cfg(FunctionName, Environment=None, MemorySize=None):
        if Environment is not None:
            STATE["env"] = dict(Environment["Variables"])
        if MemorySize is not None:
            STATE["memory"] = MemorySize
        STATE["cold"] = True
        STATE["updates"] += 1
        return {}

    def update_code(FunctionName, S3Bucket, S3Key, Architectures):
        STATE["arch"], STATE["cold"] = Architectures[0], True
        return {}
    lam.handlers.update(
        get_function_configuration=lambda FunctionName: {"Environment": {"Variables": dict(STATE["env"])},
                                                         "MemorySize": STATE["memory"], "Architectures": [STATE["arch"]]},
        update_function_configuration=update_cfg, update_function_code=update_code,
        get_waiter=lambda name: types.SimpleNamespace(wait=lambda **kw: None),
        get_account_settings=lambda: {"AccountLimit": {"ConcurrentExecutions": 10}})

    ec2 = Client("ec2")

    def start(InstanceIds):
        STATE["ec2"] = "running"
    ec2.handlers.update(
        describe_instances=lambda InstanceIds: {"Reservations": [{"Instances": [{"State": {"Name": STATE["ec2"]}}]}]},
        start_instances=start, stop_instances=lambda InstanceIds: STATE.update(ec2="stopped"),
        get_waiter=lambda name: types.SimpleNamespace(wait=lambda **kw: None))
    ssm = Client("ssm")
    ssm.handlers.update(send_command=lambda **kw: STATE.update(poll=int(kw["Parameters"]["commands"][0].split("POLL_SECONDS=")[2].split("/")[0])) or {"Command": {"CommandId": "c1"}},
                        get_command_invocation=lambda **kw: {"Status": "Success"})
    cw = Client("cloudwatch")
    cw.handlers["get_metric_statistics"] = lambda **kw: {"Datapoints": [{"Maximum": 10.0, "Sum": 0.0}]}

    clients = {"cloudformation": cf, "s3": s3, "dynamodb": ddb, "logs": logs, "lambda": lam, "ec2": ec2, "cloudwatch": cw, "ssm": ssm}
    return types.SimpleNamespace(client=lambda name: clients[name], region_name="ap-southeast-2")


def main():
    photo_text()
    import experiments as ex
    ex.TypeDeserializer = fake_aws.TypeDeserializer
    ex.time.sleep = lambda s: None
    ex.HERE = os.path.join(ROOT, "comparison")
    out = tempfile.mkdtemp()
    aws = ex.Aws(make_session())
    failures = []

    def expect(name, cond, detail=""):
        print(("ok   " if cond else "FAIL ") + name + ("" if cond else f"  {detail}"))
        if not cond:
            failures.append(name)

    acc = ex.run_accuracy(aws, "r1", out)
    expect("accuracy ran", acc and acc["real_receipts"]["n"] == 626, acc)
    right, total = map(int, acc["gst_invoices"]["verdict_correct"].split("/"))
    # with perfect OCR one tampered invoice slips through: its changed check character happens to be
    # valid for an OCR-style repair of an ambiguous character (see RESULTS.md, limits)
    expect("GST verdicts right (all but at most one)", total == 170 and right >= 169, acc["gst_invoices"])
    expect("duplicate caught", acc["gst_invoices"]["duplicate_detected"] is True)
    expect("billed time matched from logs", acc["real_receipts"]["billed_ms"]["n"] == 626, acc["real_receipts"]["billed_ms"])

    env_before = dict(STATE["env"])
    cs = ex.run_coldstart(aws, "r1", out, reps=2)
    expect("coldstart restored the environment", STATE["env"] == env_before, STATE["env"])
    expect("coldstart measured both modes", cs["startup_pdf"]["init_ms"]["n"] == 2 and cs["lazy_photo"]["init_ms"]["n"] == 2, cs)

    tune = ex.run_tune(aws, "r1", out, memories=(128, 1024), arches=("x86_64", "arm64"))
    expect("tune restored memory and processor", STATE["memory"] == 256 and STATE["arch"] == "x86_64", STATE)
    expect("tune priced every config", all(c["cost_per_1000_receipts_usd"] for c in tune["configs"]), tune["configs"])
    expect("tune read all receipts right", all(c["fields_all_correct"] >= 11 for c in tune["configs"]), tune["configs"])

    import math
    true_offset, samples, send = 0.85, [], 100.0
    for _ in range(40):  # 120 ms round trips, server stamps the reply mid-way, laptop clock 850 ms behind
        samples.append((send, send + 0.12, math.floor(send + 0.06 + true_offset)))
        send += 0.17
    off = ex.bracket_offset(samples)
    expect("clock bracketing recovers a known offset", off and abs(off[0] - 850) < 100 and off[1] < 100, off)
    burst = ex.run_burst(aws, "r1", out, sizes=(5, 20))
    burst2 = ex.run_burst(aws, "r2", tempfile.mkdtemp(), sizes=(5,), poll=2)
    expect("burst with a 2 s poll recorded it and set it back", burst2["ec2_poll_seconds"] == 2 and STATE.get("poll") == 10, (burst2.get("ec2_poll_seconds"), STATE.get("poll")))
    expect("burst used the calibrated clock", all(v["uncertainty_ms"] is not None for v in burst["clock"].values()), burst["clock"])
    import csv
    clocks = {r["upload_clock"] for r in csv.DictReader(open(os.path.join(out, "burst.csv")))}
    expect("burst timed uploads with AWS's own clock", clocks == {"s3-event", "s3-listing"}, clocks)
    lam = [b for b in burst["bursts"] if b["version"] == "Lambda"]
    expect("Lambda delays measured, not clamped to 0", all(b["end_to_end_ms"]["min"] > 0 for b in lam), lam)
    expect("burst ran both versions", len(burst["bursts"]) == 4, burst)
    expect("burst receipts read right", all(b["fields_all_correct"] == b["burst"] for b in burst["bursts"]), burst["bursts"])
    expect("burst stopped the server it started", STATE["ec2"] == "stopped")
    expect("files written", sorted(os.listdir(out)) == sorted(
        ["accuracy.csv", "accuracy.json", "coldstart.csv", "coldstart.json", "tune.csv", "tune.json", "burst.csv", "burst.json"]),
        os.listdir(out))
    expect("no emails during experiments", not [c for c in fake_aws.client("sns").calls if c[0] == "publish"])
    print(f"\n{'all good' if not failures else str(len(failures)) + ' FAILED'}; sample output in {out}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
