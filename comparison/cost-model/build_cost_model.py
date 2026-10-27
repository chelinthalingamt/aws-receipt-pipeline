"""Builds Receipt-Pipeline-Cost-Model.xlsx: Lambda vs EC2 monthly cost, all formulas."""
from openpyxl import Workbook
from openpyxl.chart import LineChart, Reference
from openpyxl.comments import Comment
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

import os
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Receipt-Pipeline-Cost-Model.xlsx")

F = "Arial"
BLUE, GREEN, BLACK = "0000FF", "008000", "000000"
title_font = Font(name=F, size=14, bold=True)
head_font = Font(name=F, size=10, bold=True, color="FFFFFF")
sect_font = Font(name=F, size=11, bold=True)
body = Font(name=F, size=10, color=BLACK)
inp = Font(name=F, size=10, color=BLUE)
link = Font(name=F, size=10, color=GREEN)
note_font = Font(name=F, size=9, italic=True, color="555555")
head_fill = PatternFill("solid", fgColor="1F4E5A")
yellow = PatternFill("solid", fgColor="FFFF00")
thin = Side(style="thin", color="BFBFBF")
box = Border(top=thin, bottom=thin, left=thin, right=thin)
wrap = Alignment(wrap_text=True, vertical="top")

USD2 = '$#,##0.00;($#,##0.00);"-"'
USD_TINY = '$0.0000000000'
INT = '#,##0'

LAMBDA_URL = "https://aws.amazon.com/lambda/pricing/"
EC2_URL = "https://aws-pricing.com/ap-southeast-2.html"
EC2_URL2 = "https://www.doit.com/compute/spot/ap-southeast-2/t3.micro"
IPV4_URL = "https://aws.amazon.com/blogs/aws/new-aws-public-ipv4-address-charge-public-ip-insights"
INIT_URL = "https://aws.amazon.com/blogs/compute/aws-lambda-standardizes-billing-for-init-phase/"

wb = Workbook()

# ------------------------------------------------------------------ Inputs
ws = wb.active
ws.title = "Inputs"
ws["A1"] = "Receipt Pipeline Cost Model: Inputs"
ws["A1"].font = title_font
ws["A2"] = ("Blue values are inputs: change them and every result updates. Black values are formulas. "
            "Yellow cells should be checked before the final report.")
ws["A2"].font = note_font
ws.merge_cells("A2:D2")

def header(row):
    for col, text in enumerate(["Item", "Value", "Unit", "Source or note"], start=1):
        c = ws.cell(row=row, column=col, value=text)
        c.font, c.fill, c.border = head_font, head_fill, box

def section(row, text):
    ws.cell(row=row, column=1, value=text).font = sect_font

def put(row, item, value, unit, note, fmt=None, kind="input", check=False):
    ws.cell(row=row, column=1, value=item).font = body
    v = ws.cell(row=row, column=2, value=value)
    v.font = inp if kind == "input" else body
    if fmt:
        v.number_format = fmt
    if check:
        v.fill = yellow
    ws.cell(row=row, column=3, value=unit).font = body
    n = ws.cell(row=row, column=4, value=note)
    n.font, n.alignment = body, wrap
    for col in range(1, 5):
        ws.cell(row=row, column=col).border = box

section(4, "Prices (USD)")
header(5)
put(6, "Lambda: price per 1 million requests", 0.20, "USD per 1M requests",
    f"AWS Lambda pricing page, US East figure: {LAMBDA_URL}. The Sydney rate could not be confirmed; "
    "check it in calculator.aws (region Asia Pacific (Sydney)) and replace if different.", '$0.00', check=True)
put(7, "Lambda: price per GB-second (ARM / Graviton)", 0.0000133334, "USD per GB-second",
    f"AWS Lambda pricing page, US East Arm figure: {LAMBDA_URL} (x86 is 0.0000166667). The function runs on ARM since "
    "the tuning experiment showed ARM 256 MB is about 14% cheaper per receipt at nearly the same speed. "
    "Check the Sydney rate as above.", '$0.0000000000', check=True)
put(8, "Lambda free tier: requests per month", 1000000, "requests",
    f"AWS Lambda pricing page: the free tier includes 1 million requests per month. {LAMBDA_URL}", INT)
put(9, "Lambda free tier: GB-seconds per month", 400000, "GB-seconds",
    f"AWS Lambda pricing page: the free tier includes 400,000 GB-seconds per month. {LAMBDA_URL}", INT)
put(10, "Apply the Lambda free tier? (1 = yes, 0 = no)", 1, "switch",
    "Set to 0 to compare list prices without any free allowance.", '0')
put(11, "EC2 t3.micro on-demand, Sydney", 0.0132, "USD per hour",
    f"Linux on-demand rate for ap-southeast-2: {EC2_URL} and {EC2_URL2}", '$0.0000')
put(12, "Public IPv4 address", 0.005, "USD per hour",
    f"Charged per public IPv4 address per hour since 1 Feb 2024, all regions: {IPV4_URL}", '$0.000')
put(13, "EBS gp3 disk storage, Sydney", 0.096, "USD per GB-month", f"ap-southeast-2 rate: {EC2_URL}", '$0.000')

put(14, "S3 LIST requests (EC2 polling)", 0.005, "USD per 1,000 requests",
    "S3 pricing page, US East figure for PUT/COPY/POST/LIST requests (https://aws.amazon.com/s3/pricing/); "
    "Sydney is slightly higher. The EC2 poller lists the ec2/ folder on every check, so this is an EC2-only cost.",
    '$0.0000', check=True)

section(15, "Workload assumptions")
header(16)
put(17, "Lambda memory", 256, "MB", "As set in serverless-lambda/template.yaml (MemorySize: 256, Architectures: arm64).", INT)
put(18, "Lambda average billed time per receipt", 203, "ms",
    "Measured (tuning experiment, run exp-20261026-184235): average billed time of warm runs on ARM at 256 MB over the "
    "12 test receipts, from Lambda's REPORT logs. Cold starts bill more (about 1.1 s for a PDF; start-up time is billed "
    f"since 1 Aug 2025, {INIT_URL}) and long photo receipts bill about 2.4 s (5 OCR calls, read in parallel since 27 Oct; 5.4 s before): "
    "see the scenario table on the Comparison sheet.", INT)
put(19, "EC2 hours per month (always on)", 730, "hours", "24 hours x 365 days / 12 months.", INT)
put(20, "EC2 disk size", 8, "GB", "Amazon Linux 2023 default root volume.", INT)
put(22, "EC2 check interval (poller)", 10, "seconds",
    "POLL_SECONDS in ec2-server/template.yaml. A shorter interval cuts EC2's delay but adds S3 LIST requests.", INT)
put(21, "EC2 processing time per receipt", 35, "ms",
    "Measured in the first comparison: median EC2 processing time over 12 receipts (average 130 ms, driven up by the two photo receipts). "
    "Used only for the capacity check.", INT)

section(23, "Results (formulas)")
header(24)
put(25, "GB-seconds per Lambda receipt", "=B17/1024*B18/1000", "GB-seconds",
    "Memory in GB x billed time in seconds.", '0.000000', kind="formula")
put(26, "Lambda cost per receipt, before free tier", "=B6/1000000+B25*B7", "USD",
    "Request charge + compute charge for one receipt.", USD_TINY, kind="formula")
put(27, "EC2 cost per month (fixed, any volume)", "=B11*B19+B12*B19+B13*B20+B19*3600/B22*B14/1000", "USD per month",
    "Instance hours + public IPv4 hours + disk + the S3 LIST requests of checking for new files. "
    "The same whether it processes 0 or millions of receipts.", USD2, kind="formula")
put(28, "Break-even volume", "=IF(B10=1,(B27+B8*B6/1000000+B9*B7)/B26,B27/B26)", "receipts per month",
    "Below this volume Lambda is cheaper; above it EC2 is. Solves Lambda cost = EC2 cost.", INT, kind="formula")
put(29, "Break-even formula valid?", '=IF(OR(B10=0,AND(B28>B8,B28*B25>B9)),"Yes","No: break-even falls inside the free tier")',
    "check", "The break-even formula assumes the volume is past both free-tier limits.", None, kind="formula")
put(30, "One EC2 server's capacity", "=1000/B21*3600*B19", "receipts per month",
    "If receipts arrived evenly and one poller handled them one at a time. Real bursts need headroom.", INT, kind="formula")
ws["B28"].font = Font(name=F, size=10, bold=True)

for col, width in zip("ABCD", (44, 16, 20, 90)):
    ws.column_dimensions[col].width = width
ws.freeze_panes = "A4"

# ------------------------------------------------------------------ Comparison
cs = wb.create_sheet("Comparison")
cs["A1"] = "Lambda vs EC2: monthly cost by volume"
cs["A1"].font = title_font
cs["A2"] = ("Compute cost only. S3, DynamoDB, SNS and CloudWatch cost the same for both versions, so they cancel out. "
            "All values come from the Inputs sheet.")
cs["A2"].font = note_font
cs.merge_cells("A2:G2")

cs["A3"] = "Break-even (receipts per month):"
cs["A3"].font = sect_font
cs["C3"] = "=Inputs!B28"
cs["C3"].font = Font(name=F, size=11, bold=True, color=GREEN)
cs["C3"].number_format = INT
cs["D3"] = '="Lambda is cheaper below this volume; EC2 above it. EC2 fixed cost: $"&TEXT(Inputs!B27,"0.00")&" per month."'
cs["D3"].font = Font(name=F, size=10, color=GREEN)

heads = ["Receipts per month", "Lambda requests ($)", "Lambda compute ($)", "Lambda total ($/month)",
         "EC2 total ($/month)", "Cheaper option", "Monthly saving ($)", "Lambda cost per 1,000 receipts ($)"]
HR = 5
for col, text in enumerate(heads, start=1):
    c = cs.cell(row=HR, column=col, value=text)
    c.font, c.fill, c.border = head_font, head_fill, box
    c.alignment = Alignment(wrap_text=True, vertical="center")
cs.row_dimensions[HR].height = 30

volumes = [100, 1000, 10000, 100000, 1000000, 2000000, 5000000, 10000000, 15000000, 20000000]
first = HR + 1
for i, vol in enumerate(volumes):
    r = first + i
    cells = [
        (vol, INT, inp),
        (f"=MAX(0,A{r}-Inputs!$B$8*Inputs!$B$10)*Inputs!$B$6/1000000", USD2, body),
        (f"=MAX(0,A{r}*Inputs!$B$25-Inputs!$B$9*Inputs!$B$10)*Inputs!$B$7", USD2, body),
        (f"=B{r}+C{r}", USD2, Font(name=F, size=10, bold=True)),
        (f"=Inputs!$B$27", USD2, Font(name=F, size=10, bold=True, color=GREEN)),
        (f'=IF(D{r}<E{r},"Lambda",IF(D{r}>E{r},"EC2","Equal"))', None, body),
        (f"=ABS(E{r}-D{r})", USD2, body),
        (f"=D{r}/A{r}*1000", '$0.0000', body),
    ]
    for col, (val, fmt, font) in enumerate(cells, start=1):
        c = cs.cell(row=r, column=col, value=val)
        c.font, c.border = font, box
        if fmt:
            c.number_format = fmt
last = first + len(volumes) - 1
note_r = last + 1
cs.cell(row=note_r, column=1,
        value="Volumes in column A are inputs too: change them to model your own scenarios. "
              "One EC2 server could keep up with all these volumes if receipts arrived evenly (Inputs, row 30); real bursts need headroom.").font = note_font
cs.merge_cells(start_row=note_r, start_column=1, end_row=note_r, end_column=8)

chart = LineChart()
chart.title = "Monthly cost: Lambda grows with volume, EC2 stays flat"
chart.y_axis.title = "USD per month"
chart.x_axis.title = "Receipts per month"
chart.y_axis.numFmt = '$#,##0'
chart.height, chart.width = 9, 22
data = Reference(cs, min_col=4, max_col=5, min_row=HR, max_row=last)
chart.add_data(data, titles_from_data=True)
chart.set_categories(Reference(cs, min_col=1, min_row=first, max_row=last))
chart.x_axis.number_format = '#,##0'
chart.x_axis.delete = False
chart.y_axis.delete = False
chart.series[0].graphicalProperties.line.solidFill = "1F77B4"
chart.series[0].graphicalProperties.line.width = 28000
chart.series[1].graphicalProperties.line.solidFill = "D62728"
chart.series[1].graphicalProperties.line.width = 28000
cs.add_chart(chart, f"A{note_r + 2}")

# ---- what-if: the break-even depends heavily on Lambda's billed time per receipt
cs["J4"] = "Break-even by Lambda billed time"
cs["J4"].font = sect_font
for col, text in zip("JKL", ["Scenario", "Billed time (ms)", "Break-even (receipts/month)"]):
    c = cs[f"{col}5"]
    c.value, c.font, c.fill, c.border = text, head_font, head_fill, box
    c.alignment = Alignment(wrap_text=True, vertical="center")
scenarios = [
    ("Long photo receipt, 5 OCR calls read in parallel (measured median, 40 real scans)", 2448),
    ("Every run a cold start, PDF (measured median)", 1101),
    ("Every run warm (measured average, ARM 256 MB)", 203),
]
for i, (name, ms) in enumerate(scenarios):
    r = 6 + i
    cs[f"J{r}"], cs[f"K{r}"] = name, ms
    per = f"(Inputs!$B$6/1000000+Inputs!$B$17/1024*K{r}/1000*Inputs!$B$7)"
    cs[f"L{r}"] = f"=IF(Inputs!$B$10=1,(Inputs!$B$27+Inputs!$B$8*Inputs!$B$6/1000000+Inputs!$B$9*Inputs!$B$7)/{per},Inputs!$B$27/{per})"
    cs[f"J{r}"].font, cs[f"K{r}"].font, cs[f"L{r}"].font = body, inp, Font(name=F, size=10, bold=True)
    cs[f"K{r}"].number_format, cs[f"L{r}"].number_format = INT, INT
    for col in "JKL":
        cs[f"{col}{r}"].border = box
        cs[f"{col}{r}"].alignment = Alignment(wrap_text=True, vertical="top")
cs["J9"] = ("Steady traffic keeps Lambda copies warm, so most receipts bill like the last row. Long photos that need "
            "several OCR calls bill the longest, even with their reads running in parallel.")
cs["J9"].font, cs["J9"].alignment = note_font, wrap
cs.merge_cells("J9:L11")

for col, width in zip("ABCDEFGHIJKL", (18, 16, 16, 18, 18, 14, 16, 20, 3, 34, 12, 18)):
    cs.column_dimensions[col].width = width
cs.freeze_panes = cs.cell(row=first, column=1)

# ------------------------------------------------------------------ Notes
ns = wb.create_sheet("Notes")
ns["A1"] = "How to read and use this model"
ns["A1"].font = title_font
rows = [
    ("What it compares", "The monthly cost of the compute part of each version: Lambda (pay per receipt) "
     "against one always-on EC2 t3.micro server (pay per hour)."),
    ("What it leaves out", "Costs both versions pay equally per receipt, so they do not change which one is cheaper: "
     "Rekognition (about $0.001 per image read, US East list price; a short photo needs 1 read, a long one up to 5), "
     "S3 uploads, DynamoDB writes, SNS emails and CloudWatch logs. For photo receipts Rekognition is by far the largest "
     "cost: about $1-5 per 1,000 photos, against under $0.01 per 1,000 for Lambda compute. PDFs need no Rekognition."),
    ("How Lambda is priced", "Requests x price per request, plus GB-seconds x price per GB-second. "
     "GB-seconds = memory in GB x billed seconds. The free tier removes the first 1M requests and 400,000 GB-seconds each month."),
    ("How EC2 is priced", "Hours switched on x hourly rate, plus the public IPv4 address and the disk. "
     "It does not change with volume, and it is charged even when the server is idle."),
    ("Measured", "First comparison: same 12 receipts through both versions, 36/36 fields correct on both. "
     "Experiments (comparison/RESULTS.md): warm billed time 203 ms on ARM 256 MB; cold start 1.1 s (PDF) / 1.6 s (photo); "
     "upload-to-result delay median about 1.0 s on Lambda vs 5-6 s on EC2 (it checks for new files every 10 s)."),
    ("Before the final report", "1) Check the Lambda Sydney prices (Inputs B6 and B7) in calculator.aws. "
     "2) Billed time (Inputs B18) is the measured warm average; the Comparison sheet shows the range. Every result updates by itself."),
    ("Free plan note", "On the AWS Free plan, usage is paid from credits, not cash. The model shows list prices so "
     "the comparison holds for any account."),
    ("Sources", f"Lambda prices and free tier: {LAMBDA_URL}  |  EC2 t3.micro and EBS gp3 in Sydney: {EC2_URL} ; {EC2_URL2}  |  "
     f"Public IPv4 charge: {IPV4_URL}  |  Lambda start-up billing: {INIT_URL}"),
]
for i, (k, v) in enumerate(rows, start=3):
    a = ns.cell(row=i, column=1, value=k)
    a.font, a.alignment, a.border = Font(name=F, size=10, bold=True), wrap, box
    b = ns.cell(row=i, column=2, value=v)
    b.font, b.alignment, b.border = body, wrap, box
ns.column_dimensions["A"].width = 24
ns.column_dimensions["B"].width = 110

ws["B6"].comment = Comment("US East rate. Check Sydney in calculator.aws.", "Model")
ws["B18"].comment = Comment("Assumption: replace with the dashboard's average Lambda run time.", "Model")

wb.save(OUT)
print("saved", OUT)
