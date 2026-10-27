# Datasets used

All accuracy results in this project were measured on AWS: every image below is uploaded to S3 and read by the
Lambda function with Amazon Rekognition (`python3 comparison/experiments.py accuracy`).

| Set | Size | What it is | Source and licence |
| --- | --- | --- | --- |
| SROIE receipts | 626 images | Real scanned shop receipts (mostly Malaysian), each with the correct company, date and total | ICDAR 2019 Robust Reading Challenge on Scanned Receipts OCR and Information Extraction (SROIE). Copy used: github.com/zzzDavid/ICDAR-2019-SROIE (repository carries an MIT licence; the images are the competition data, used here only for evaluation and not redistributed) |
| GST invoices, genuine | 100 images | Computer-generated Indian GST invoices made to look like photographed bills (tilted, blurred, compressed), with correct GSTIN, tax split and total | BillBook, github.com/Abh1j1th1/BillBook (Apache-2.0). The 20 images it ships, plus 80 more from its own `generate_samples.py` |
| GST invoices, tampered | 60 images | Copies of the genuine invoices with one mistake each: wrong GSTIN check character, non-existent state code, CGST not equal to SGST, inflated total, IGST together with CGST/SGST, tax that does not add up (10 of each kind) | Made by `tests/make_gst_invoices.py` |
| GST invoices, re-shot | 10 images | The same invoice drawn again with a different tilt, to test duplicate detection | Made by `tests/make_gst_invoices.py` |
| Sample PDFs and photos | 12 files | The small set in `comparison/batch/` used for cold starts, memory tuning and the delay and burst tests | Written for this project |

The GST invoices are computer-generated, not photographs of real shop bills.

## Where the files are

After the download step, the images sit in the project folder:

- `comparison/real-receipts/` holds the 626 SROIE images and `answers.json`
- `comparison/gst-invoices/` holds the 170 invoice images and `answers.json`

The images are large (about 340 MB for SROIE), so Git ignores them. Each `answers.json` is kept in Git.
To download everything and rebuild both folders:

```bash
pip install pillow          # or: sudo apt install -y python3-pil
python3 tests/fetch_datasets.py
```

## References

- Z. Huang, K. Chen, J. He, X. Bai, D. Karatzas, S. Lu, C. V. Jawahar. "ICDAR2019 Competition on Scanned Receipt OCR and Information Extraction." ICDAR 2019.
- BillBook, github.com/Abh1j1th1/BillBook (licence text in `comparison/gst-invoices/BILLBOOK-LICENSE.txt`).
