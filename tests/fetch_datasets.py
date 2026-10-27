"""Download the two public datasets and build the full test sets.

    python3 tests/fetch_datasets.py            # downloads (about 650 MB), then builds
    python3 tests/fetch_datasets.py --src DIR  # reuse a folder that already holds both clones

Creates (images are git-ignored; each answers.json is kept in git):
  comparison/real-receipts/   all 626 SROIE receipts + answers.json  (vendor, total, date, address)
  comparison/gst-invoices/    170 invoice images + answers.json
                              (100 genuine BillBook-style invoices, 60 tampered copies, 10 re-shot duplicates)

Datasets
  SROIE, ICDAR 2019 Robust Reading Challenge on Scanned Receipts OCR and Information Extraction
         copy used: github.com/zzzDavid/ICDAR-2019-SROIE (MIT licence)
  BillBook, computer-generated Indian GST invoice images
         github.com/Abh1j1th1/BillBook (Apache-2.0)
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from decimal import Decimal

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import make_gst_invoices  # noqa: E402
from sroie_eval import expected_date  # noqa: E402

REPOS = {"sroie": "https://github.com/zzzDavid/ICDAR-2019-SROIE.git", "billbook": "https://github.com/Abh1j1th1/BillBook.git"}


def clone(src):
    for name, url in REPOS.items():
        if not os.path.isdir(os.path.join(src, name)):
            print(f"downloading {name} ...")
            subprocess.run(["git", "clone", "--depth", "1", "-q", url, os.path.join(src, name)], check=True)


def build_sroie(folder):
    out = os.path.join(ROOT, "comparison", "real-receipts")
    os.makedirs(out, exist_ok=True)
    answers = []
    for key in sorted(os.listdir(os.path.join(folder, "data", "key"))):
        n = key[:-5]
        gt = json.load(open(os.path.join(folder, "data", "key", key), encoding="utf-8"))
        m = re.search(r"\d[\d,]*(?:\.\d+)?", gt.get("total") or "")
        name = f"sroie-{n}.jpg"
        shutil.copy(os.path.join(folder, "data", "img", n + ".jpg"), os.path.join(out, name))
        answers.append({"file": name, "vendor": gt.get("company"),
                        "amount": str(Decimal(m.group(0).replace(",", ""))) if m else None,
                        "date": expected_date(gt.get("date")), "address": gt.get("address")})
    json.dump(answers, open(os.path.join(out, "answers.json"), "w"), indent=2)
    lic = os.path.join(folder, "LICENSE")
    if os.path.exists(lic):
        shutil.copy(lic, os.path.join(out, "SROIE-REPO-LICENSE.txt"))
    print(f"real-receipts: {len(answers)} SROIE receipts")


def billbook_bills(folder, count=100):
    """BillBook ships 20 invoices; its own generator (same seed) makes more. The shipped images are kept as they are."""
    work = tempfile.mkdtemp(prefix="billbook-")
    for f in ("generate_samples.py", "validators.py"):
        shutil.copy(os.path.join(folder, f), work)
    subprocess.run([sys.executable, "-c", f"import generate_samples as g\nfor n in range(1, {count + 1}): g.make(n)"], cwd=work, check=True,
                   stdout=subprocess.DEVNULL)
    bills = os.path.join(work, "data", "bills")
    shipped = os.path.join(folder, "data", "bills")
    for n in range(1, 21):
        a, b = (json.load(open(os.path.join(d, f"{n:03d}.json"))) for d in (shipped, bills))
        assert a == b, f"generator output differs from the shipped invoice {n:03d}"
        shutil.copy(os.path.join(shipped, f"{n:03d}.jpg"), os.path.join(bills, f"{n:03d}.jpg"))
    return bills


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src")
    args = ap.parse_args()
    src = args.src or tempfile.mkdtemp(prefix="datasets-")
    clone(src)
    build_sroie(os.path.join(src, "sroie"))
    make_gst_invoices.main(billbook_bills(os.path.join(src, "billbook")))
    lic = os.path.join(src, "billbook", "LICENSE")
    if os.path.exists(lic):
        shutil.copy(lic, os.path.join(ROOT, "comparison", "gst-invoices", "BILLBOOK-LICENSE.txt"))
    print("done")


if __name__ == "__main__":
    main()
