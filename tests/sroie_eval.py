"""Accuracy on real shop receipts from the public SROIE dataset (ICDAR 2019, 626 receipts).

Each receipt comes with its printed text lines (with positions) and the correct
company, date and total, so the parser can be scored without any AWS calls:

    python3 tests/sroie_eval.py <path to SROIE data folder (with box/ and key/)>
                                [--ocr-limit]   # also replay Rekognition's 100-word limit

--ocr-limit turns each receipt into a simulated Rekognition response (WORD and
LINE detections with positions) that, like the real service, returns at most
100 words per call and respects RegionsOfInterest bands. It compares
  * one call (the old code: everything after word 100 is lost), with
  * banded reading (the new code).
"""
import argparse
import collections
import difflib
import glob
import json
import os
import re
import sys
from decimal import Decimal

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [HERE, os.path.join(os.path.dirname(HERE), "serverless-lambda")]
import fake_aws  # noqa: E402
fake_aws.install()
import lambda_function as lf  # noqa: E402


def load_lines(path):
    rows = []
    for ln in open(path, encoding="utf-8", errors="replace"):
        parts = ln.rstrip("\n").split(",", 8)
        if len(parts) < 9 or not parts[8].strip():
            continue
        xs, ys = list(map(int, parts[0:8:2])), list(map(int, parts[1:8:2]))
        rows.append((min(xs), min(ys), max(xs), max(ys), parts[8].strip()))
    width = max(r[2] for r in rows) * 1.05
    height = max(r[3] for r in rows) * 1.02
    out = []
    for x0, y0, x1, y1, text in rows:
        box = {"Left": x0 / width, "Top": y0 / height, "Width": (x1 - x0) / width, "Height": (y1 - y0) / height}
        words, step = text.split(), box["Width"] / max(len(text), 1)
        pos, word_boxes = 0, []
        for w in words:
            start = text.index(w, pos)
            word_boxes.append((w, dict(box, Left=box["Left"] + start * step, Width=len(w) * step)))
            pos = start + len(w)
        out.append((text, box, word_boxes))
    return sorted(out, key=lambda r: r[1]["Top"])


def as_response(lines):
    return {"TextDetections": [{"Type": "LINE", "DetectedText": t, "Geometry": {"BoundingBox": b}} for t, b, _ in lines]}


class FakeRekognition:
    """Behaves like DetectText: at most 100 words per call, top of the image first."""

    def __init__(self, lines):
        self.lines, self.calls = lines, 0

    def __call__(self, Filters=None):
        self.calls += 1
        region = (Filters or {}).get("RegionsOfInterest", [{}])[0].get("BoundingBox")
        detections, words = [], 0
        for text, box, word_boxes in self.lines:
            keep = []
            for w, wb in word_boxes:
                if region:  # a word counts when more than half of it is inside the band
                    top, bottom = max(wb["Top"], region["Top"]), min(wb["Top"] + wb["Height"], region["Top"] + region["Height"])
                    if bottom - top <= wb["Height"] / 2:
                        continue
                if words >= lf.REKOGNITION_WORD_LIMIT:
                    break
                keep.append((w, wb))
                words += 1
            if keep:
                detections.append({"Type": "LINE", "DetectedText": " ".join(w for w, _ in keep),
                                   "Geometry": {"BoundingBox": dict(box, Left=keep[0][1]["Left"])}})
                detections += [{"Type": "WORD", "DetectedText": w, "Geometry": {"BoundingBox": wb}} for w, wb in keep]
        return {"TextDetections": detections}


def vendor_ok(got, want):
    g, w = (re.sub(r"[^a-z0-9]", "", (s or "").lower()) for s in (got, want))
    return bool(g and w) and (g == w or difflib.SequenceMatcher(None, g, w).ratio() >= 0.8)


def expected_date(text):
    for pattern, kind in lf.DATE_PATTERNS:
        m = pattern.search(text or "")
        if m and lf._to_date(kind, m.groups()):
            return lf._to_date(kind, m.groups())
    return None


def score(text, gt):
    p = lf.parse_receipt(text)
    m = re.search(r"\d[\d,]*(?:\.\d+)?", gt.get("total") or "")
    want = Decimal(m.group(0).replace(",", "")) if m else None
    ok = {"vendor": vendor_ok(p["vendor"], gt.get("company")),
          "amount": p["amount"] is not None and want is not None and Decimal(p["amount"]) == want,
          "date": p["receipt_date"] == expected_date(gt.get("date"))}
    ok["all three"] = all(ok.values())
    return ok, p


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("data")
    ap.add_argument("--ocr-limit", action="store_true")
    ap.add_argument("--json", help="write the summary here")
    args = ap.parse_args()

    keys = sorted(glob.glob(os.path.join(args.data, "key", "*.json")))
    modes = ["perfect text"] + (["one OCR call (old)", "banded OCR (new)"] if args.ocr_limit else [])
    totals = {m: collections.Counter() for m in modes}
    words_per_receipt, calls, gst_false_alarms, over_limit = [], [], 0, 0
    for path in keys:
        name = os.path.basename(path)[:-5]
        gt = json.load(open(path))
        lines = load_lines(os.path.join(args.data, "box", name + ".csv"))
        n_words = sum(len(w) for _, _, w in lines)
        words_per_receipt.append(n_words)
        over_limit += n_words > lf.REKOGNITION_WORD_LIMIT
        text = lf._rows_from_rekognition(as_response(lines))
        ok, parsed = score(text, gt)
        totals["perfect text"].update(k for k, v in ok.items() if v)
        if lf.gst_check(text, parsed["amount"]):
            gst_false_alarms += 1
        if args.ocr_limit:
            fake = FakeRekognition(lines)
            ok, _ = score(lf._rows_from_rekognition(fake()), gt)
            totals["one OCR call (old)"].update(k for k, v in ok.items() if v)
            fake = FakeRekognition(lines)
            response, n_calls = lf.detect_text_all(fake)
            calls.append(n_calls)
            ok, _ = score(lf._rows_from_rekognition(response), gt)
            totals["banded OCR (new)"].update(k for k, v in ok.items() if v)

    n = len(keys)
    summary = {"receipts": n,
               "words_per_receipt_median": sorted(words_per_receipt)[n // 2],
               "receipts_over_100_words": over_limit,
               "gst_checks_triggered_on_non_indian_receipts": gst_false_alarms,
               "accuracy_pct": {m: {f: round(100 * totals[m][f] / n, 1) for f in ("vendor", "amount", "date", "all three")}
                                for m in modes}}
    if calls:
        summary["ocr_calls_avg"] = round(sum(calls) / len(calls), 2)
        summary["ocr_calls_max"] = max(calls)
    print(json.dumps(summary, indent=2))
    if args.json:
        json.dump(summary, open(args.json, "w"), indent=2)


if __name__ == "__main__":
    main()
