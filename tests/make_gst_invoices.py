"""Build the GST test set in comparison/gst-invoices/.

    python3 tests/make_gst_invoices.py <BillBook data/bills folder>

* 100 genuine invoice images made with the BillBook project's own generator (Apache-2.0,
  github.com/Abh1j1th1/BillBook): the 20 images it ships plus 80 more. They are computer-generated,
  made to look like photographed bills (tilted, blurred, JPEG-compressed), and every one is correct.
* 60 tampered copies, drawn the same way: six kinds of realistic mistake, ten invoices each,
  that the GST check must flag; plus 10 re-shot copies to test duplicate detection.
answers.json lists what the pipeline should report for each file.
"""
import json
import os
import random
import shutil
import sys
import zlib

from PIL import Image, ImageDraw, ImageFilter, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(os.path.dirname(HERE), "comparison", "gst-invoices")
sys.path[:0] = [HERE, os.path.join(os.path.dirname(HERE), "serverless-lambda")]
import fake_aws  # noqa: E402
fake_aws.install()
import lambda_function as lf  # noqa: E402

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"


def iso(d):
    day, month, year = d.split("/")
    return f"{year}-{month}-{day}"


def draw(inv, path, seed):
    """Same layout as the BillBook generator, so OCR sees the same kind of page."""
    rnd = random.Random(seed)
    img = Image.new("RGB", (900, 1100), "white")
    d = ImageDraw.Draw(img)
    f = lambda size: ImageFont.truetype(FONT, size)
    y = 40
    for text, size in [(inv["vendor_name"], 34), (f"GSTIN: {inv['vendor_gstin']}", 22),
                       (f"Invoice No: {inv['invoice_number']}    Date: {inv['invoice_date']}", 22),
                       (f"Bill To: {inv['buyer_name']}", 22), (f"Buyer GSTIN: {inv['buyer_gstin']}", 22), ("", 22),
                       ("Description            HSN     Qty     Rate      Amount", 20)]:
        d.text((40, y), text, fill="black", font=f(size)); y += size + 18
    for it in inv["line_items"]:
        d.text((40, y), f"{it['description'][:22]:<22} {it['hsn']:<7} {it['quantity']:<7.0f} {it['rate']:<9.2f} {it['amount']:.2f}",
               fill="black", font=f(20)); y += 36
    y += 20
    for label, v in [("Taxable value", inv["taxable_value"]), ("CGST", inv["cgst"]), ("SGST", inv["sgst"]),
                     ("IGST", inv["igst"]), ("TOTAL", inv["total"])]:
        d.text((520, y), f"{label}: {v:.2f}", fill="black", font=f(22)); y += 36
    img = img.rotate(rnd.uniform(-3, 3), expand=True, fillcolor=(190, 190, 190))
    img = img.filter(ImageFilter.GaussianBlur(rnd.uniform(0.3, 0.9)))
    img.save(path, quality=rnd.randint(60, 80))


def as_text(inv):
    """The text a perfect OCR would return, for checking answers offline."""
    rows = [inv["vendor_name"], f"GSTIN: {inv['vendor_gstin']}",
            f"Invoice No: {inv['invoice_number']} Date: {inv['invoice_date']}",
            f"Bill To: {inv['buyer_name']}", f"Buyer GSTIN: {inv['buyer_gstin']}",
            "Description HSN Qty Rate Amount"]
    rows += [f"{i['description']} {i['hsn']} {i['quantity']:.0f} {i['rate']:.2f} {i['amount']:.2f}" for i in inv["line_items"]]
    rows += [f"Taxable value: {inv['taxable_value']:.2f}", f"CGST: {inv['cgst']:.2f}", f"SGST: {inv['sgst']:.2f}",
             f"IGST: {inv['igst']:.2f}", f"TOTAL: {inv['total']:.2f}"]
    return "\n".join(rows)


def answer(file, inv, expect, why=""):
    return {"file": file, "vendor": inv["vendor_name"], "amount": f"{inv['total']:.2f}", "date": iso(inv["invoice_date"]),
            "gstin": inv["vendor_gstin"], "expect_gst": expect, "mistake": why, "invoice": inv}


def main(src):
    os.makedirs(OUT, exist_ok=True)
    answers = []
    originals = {}
    files = sorted(n for n in os.listdir(src) if n.endswith(".json"))
    for fn in files:
        n = int(fn[:-5])
        inv = json.load(open(os.path.join(src, fn)))
        originals[n] = inv
        name = f"billbook-{n:03d}.jpg"
        shutil.copy(os.path.join(src, f"{n:03d}.jpg"), os.path.join(OUT, name))
        answers.append(answer(name, inv, "OK"))

    intra = sorted(n for n, i in originals.items() if i["cgst"])
    inter = sorted(n for n, i in originals.items() if i["igst"])
    anyone = sorted(originals)

    def bad_check_digit(i):
        g = i["vendor_gstin"]
        i["vendor_gstin"] = g[:14] + ("A" if g[14] != "A" else "B")

    def bad_state(i):
        i["vendor_gstin"] = "45" + i["vendor_gstin"][2:14]
        i["vendor_gstin"] += lf.gstin_check_char(i["vendor_gstin"])  # checksum fine, state code does not exist

    def unequal(i):
        i["sgst"] = round(i["sgst"] + 50, 2)
        i["total"] = round(i["taxable_value"] + i["cgst"] + i["sgst"], 2)

    def wrong_total(i):
        i["total"] = round(i["total"] + 900, 2)

    def mixed(i):
        half = round(i["igst"] / 2, 2)
        i["cgst"] = i["sgst"] = half
        i["total"] = round(i["taxable_value"] + i["igst"] + 2 * half, 2)

    def wrong_tax(i):
        i["igst"] = round(i["igst"] * 1.5, 2)  # wrong rate applied; total left as printed

    # one realistic mistake per kind, each applied to PER_KIND different invoices
    PER_KIND = 10
    kinds = [("gstin-check-digit", "GSTIN check character changed", bad_check_digit, anyone),
             ("gstin-state-code", "GSTIN state code 45 does not exist", bad_state, anyone),
             ("cgst-not-sgst", "SGST raised by 50, so CGST != SGST", unequal, intra),
             ("total-inflated", "total inflated by 900", wrong_total, intra),
             ("igst-and-cgst", "IGST charged together with CGST/SGST", mixed, inter),
             ("tax-does-not-add-up", "IGST changed but total not", wrong_tax, inter)]
    for k, (slug, why, change, pool) in enumerate(kinds, 1):
        rnd = random.Random(k)
        for n in rnd.sample(pool, min(PER_KIND, len(pool))):
            inv = json.loads(json.dumps(originals[n]))
            change(inv)
            file = f"tampered-{k}-{slug}-{n:03d}.jpg"
            draw(inv, os.path.join(OUT, file), seed=zlib.crc32(file.encode()) % 1000)
            answers.append(answer(file, inv, "PROBLEM", why))

    DUPLICATES = 10
    # Duplicates: the same bill photographed again (different file, different tilt).
    for n in random.Random(7).sample(sorted(originals), DUPLICATES):
        file = f"duplicate-of-billbook-{n:03d}.jpg"
        draw(originals[n], os.path.join(OUT, file), seed=zlib.crc32(file.encode()) % 1000)
        a = answer(file, originals[n], "OK")
        a["expect_duplicate_of"] = f"billbook-{n:03d}.jpg"
        answers.append(a)

    json.dump(answers, open(os.path.join(OUT, "answers.json"), "w"), indent=2)
    print(f"wrote {len(answers)} invoices to {OUT}")
    return originals


if __name__ == "__main__":
    main(sys.argv[1])
