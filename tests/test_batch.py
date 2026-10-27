"""Regression test: the 12 receipts in comparison/batch must still read correctly.

    python3 tests/test_batch.py      (from the project folder; needs pypdf)

PDFs are read for real with pypdf. The two photos need OCR, which only AWS can
do, so their text is replayed from a saved Rekognition response.
"""
import json
import os
import sys
from decimal import Decimal

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path[:0] = [HERE, os.path.join(ROOT, "serverless-lambda")]
import fake_aws  # noqa: E402
fake_aws.install()
import lambda_function as lf  # noqa: E402

BATCH = os.path.join(ROOT, "comparison", "batch")


def main():
    answers = json.load(open(os.path.join(BATCH, "answers.json")))
    failures = 0
    for a in answers:
        if not a["file"].endswith(".pdf"):
            continue
        reader = lf._pdf_reader()(os.path.join(BATCH, a["file"]))
        text = "\n".join(p.extract_text() or "" for p in reader.pages)
        got = lf.parse_receipt(text)
        ok = (got["vendor"] or "").lower() == a["vendor"].lower() \
            and got["amount"] == Decimal(a["amount"]) and got["receipt_date"] == a["date"]
        gst = lf.gst_check(text, got["amount"])
        print(f"{'ok  ' if ok else 'FAIL'} {a['file']:<34} {got['vendor']!s:<32} {got['amount']!s:>10} {got['receipt_date']}"
              f"  {lf.categorize(got['vendor'], text):<20} {gst.get('gst_check', '')}")
        failures += not ok
    print("all PDF receipts correct" if not failures else f"{failures} FAILED")
    return failures


if __name__ == "__main__":
    sys.exit(main())
