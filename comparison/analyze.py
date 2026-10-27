#!/usr/bin/env python3
"""Turn the experiment results into charts and one results table file.

    python3 analyze.py --full <run>   # <run> = the full accuracy run (all 626 receipts + the GST set)
    (needs matplotlib:  sudo apt install -y python3-matplotlib)

Writes comparison/report/*.png and comparison/report/numbers.json, which
RESULTS.md (in the project folder) refers to.

Which run each experiment comes from, and why:
  accuracy, tune     exp-20261026-184235  (the first full run)
  coldstart          exp-20261026-191209  (after the start-up fix; on ARM)
  steady stream      exp-20261027-145512  (delays timed on AWS's own clocks;
                                            the uploads arrived over 1-2 minutes)
  true burst         exp-20261026-193128  (400 uploads in 16 s; EC2 delays and
                                            completion times are usable, Lambda
                                            delays are not - see RESULTS.md)
  total fix          exp-20261027-154225  (accuracy after the total sanity check, 40 receipts)
  full accuracy      the run you pass with --full: all 626 receipts and the GST invoices
  EC2 every 2 s,     exp-20261027-161506  (burst with --poll 2, and accuracy with
  parallel OCR                              the OCR bands read in parallel)
"""

import csv
import json
import os
import statistics as st

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
RESULTS = os.path.join(HERE, "results")
OUT = os.path.join(HERE, "report")
RUNS = {"accuracy": "exp-20261026-184235", "tune": "exp-20261026-184235", "coldstart": "exp-20261026-191209",
        "stream": "exp-20261027-145512", "burst": "exp-20261026-193128",
        "accuracy_fixed": "exp-20261027-154225", "poll2": "exp-20261027-161506"}

# Chart colours: blue and orange for the two series, plus text and grid greys.
BLUE, ORANGE = "#2a78d6", "#eb6834"
INK, INK2, MUTED, GRID, SURFACE = "#0b0b0b", "#52514e", "#8a8984", "#e6e5e0", "#fcfcfb"


def style():
    plt.rcParams.update({
        "figure.facecolor": SURFACE, "axes.facecolor": SURFACE, "savefig.facecolor": SURFACE,
        "font.family": "DejaVu Sans", "font.size": 10, "text.color": INK, "axes.labelcolor": INK2,
        "axes.edgecolor": GRID, "xtick.color": INK2, "ytick.color": INK2, "axes.spines.top": False,
        "axes.spines.right": False, "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.8,
        "axes.axisbelow": True, "axes.titleweight": "bold", "axes.titlesize": 11, "axes.titlelocation": "left",
        "legend.frameon": False})


def rows(run, name):
    return list(csv.DictReader(open(os.path.join(RESULTS, run, name))))


def load(run, name):
    return json.load(open(os.path.join(RESULTS, run, name)))


def num(x):
    return float(x) if x not in (None, "", "None") else None


def save(fig, name):
    fig.savefig(os.path.join(OUT, name), dpi=160, bbox_inches="tight")
    plt.close(fig)


def label_bars(ax, bars, fmt, inside=False):
    for b in bars:
        h = b.get_height()
        ax.annotate(fmt(h), (b.get_x() + b.get_width() / 2, h), xytext=(0, 3), textcoords="offset points",
                    ha="center", va="bottom", fontsize=8.5, color=INK2)


# ---------------------------------------------------------------- 1. accuracy

def chart_accuracy(numbers):
    """Everything here was read by the Lambda function on AWS. The two 40-receipt runs are the
    first live test and the one after the total fix; the full run is every one of the 626 receipts."""
    fields = [("Vendor", "vendor_pct"), ("Total", "amount_pct"), ("Date", "date_pct"), ("All three", "all_pct")]
    series = [("First live run, 40 receipts", RUNS["accuracy"], ORANGE),
              ("After the total fix, 40 receipts", RUNS["accuracy_fixed"], "#f2a97d")]
    if RUNS.get("accuracy_full"):
        series = [("All 626 SROIE receipts", RUNS["accuracy_full"], BLUE)]  # the 40-receipt runs stay in numbers.json
    numbers["accuracy_live"] = {name: load(run, "accuracy.json") for name, run, _ in series}
    fig, ax = plt.subplots(figsize=(7.2, 3.6))
    w = 0.8 / len(series)
    for k, (name, run, color) in enumerate(series):
        vals = [load(run, "accuracy.json")["real_receipts"][key] for _, key in fields]
        bars = ax.bar([i + (k - (len(series) - 1) / 2) * (w + 0.01) for i in range(len(fields))], vals, w, color=color, label=name)
        label_bars(ax, bars, lambda h: f"{h:.1f}%")
    ax.set_xticks(range(len(fields)), [f for f, _ in fields])
    ax.set_ylim(0, 118)
    ax.set_yticks([0, 20, 40, 60, 80, 100])
    ax.set_ylabel("Correct (%)")
    ax.set_title("Real receipts read by the Lambda function on AWS")
    ax.legend(loc="upper left", ncols=len(series), bbox_to_anchor=(0, 1.0), fontsize=8, frameon=False)
    ax.grid(axis="x", visible=False)
    save(fig, "accuracy.png")


# ---------------------------------------------------------------- 2. cold start

def chart_coldstart(numbers):
    data = rows(RUNS["coldstart"], "coldstart.csv")
    order = [("pdf", "startup"), ("pdf", "lazy"), ("photo", "startup"), ("photo", "lazy")]
    names = {"startup": "load at start-up", "lazy": "load on first use"}
    med = {}
    for kind, mode in order:
        sel = [r for r in data if r["kind"] == kind and r["mode"] == mode]
        med[(kind, mode)] = (st.median(num(r["init_ms"]) for r in sel), st.median(num(r["duration_ms"]) for r in sel),
                             st.median(num(r["billed_ms"]) for r in sel))
    numbers["coldstart_medians_ms"] = {f"{k}_{m}": {"startup_phase": a, "first_request": b, "billed": c}
                                       for (k, m), (a, b, c) in med.items()}
    fig, ax = plt.subplots(figsize=(7.2, 3.0))
    labels = [f"{'PDF' if k == 'pdf' else 'Photo'}: {names[m]}" for k, m in order]
    y = list(range(len(order)))[::-1]
    init = [med[o][0] for o in order]
    req = [med[o][1] for o in order]
    ax.barh(y, init, 0.6, color=BLUE, label="Start-up phase (full CPU)", edgecolor=SURFACE, linewidth=2)
    ax.barh(y, req, 0.6, left=init, color=ORANGE, label="First request (CPU share of 256 MB)", edgecolor=SURFACE, linewidth=2)
    for yi, a, b in zip(y, init, req):
        ax.annotate(f"{(a + b) / 1000:.2f} s", (a + b, yi), xytext=(5, 0), textcoords="offset points",
                    va="center", fontsize=9, color=INK)
    ax.set_yticks(y, labels)
    ax.set_xlabel("Time for the first receipt after a cold start (ms, median of 5)")
    ax.set_xlim(0, max(a + b for a, b in zip(init, req)) * 1.15)
    ax.set_title("Cold start: loading at start-up is faster (ARM, 256 MB)")
    ax.legend(loc="lower right", fontsize=8.5)
    ax.grid(axis="y", visible=False)
    save(fig, "coldstart.png")


# ---------------------------------------------------------------- 3. tuning

def chart_tune(numbers):
    t = load(RUNS["tune"], "tune.json")
    numbers["tune"] = t
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(8.6, 3.3))
    for arch, color, name in (("x86_64", ORANGE, "Intel (x86)"), ("arm64", BLUE, "ARM (Graviton)")):
        sel = sorted((c for c in t["configs"] if c["arch"] == arch), key=lambda c: c["memory_mb"])
        mem = [c["memory_mb"] for c in sel]
        a1.plot(mem, [c["warm_duration_ms_avg"] for c in sel], color=color, lw=2, marker="o", ms=6, label=name)
        a2.plot(mem, [c["cost_per_1000_receipts_usd"] * 1000 for c in sel], color=color, lw=2, marker="o", ms=6, label=name)
    for ax in (a1, a2):
        ax.set_xscale("log", base=2)
        ax.set_xticks([128, 256, 512, 1024, 2048], ["128", "256", "512", "1024", "2048"])
        ax.set_xlabel("Lambda memory (MB)")
        ax.minorticks_off()
    a1.set_ylabel("ms per receipt (warm)")
    a1.set_title("Speed")
    a2.set_ylabel("US$ per 1 million receipts")
    a2.set_title("Cost")
    a1.legend(loc="upper right", fontsize=8.5)
    a1.set_ylim(0, None)
    a2.set_ylim(0, None)
    a2.annotate("chosen: ARM 256 MB", (256, next(c["cost_per_1000_receipts_usd"] for c in t["configs"]
                                                  if c["arch"] == "arm64" and c["memory_mb"] == 256) * 1000),
                xytext=(18, -28), textcoords="offset points", fontsize=8.5, color=INK,
                arrowprops={"arrowstyle": "-", "color": MUTED, "lw": 0.8})
    fig.suptitle("Memory and processor tuning (12 test receipts each, warm)", x=0.01, ha="left", fontweight="bold", fontsize=11)
    fig.tight_layout()
    save(fig, "tuning.png")


# ---------------------------------------------------------------- 4. delays: steady stream (AWS clocks)

def chart_stream(numbers):
    b = load(RUNS["stream"], "burst.json")
    numbers["stream"] = b
    sizes = sorted({e["burst"] for e in b["bursts"]})
    get = lambda size, v, p: next(e for e in b["bursts"] if e["burst"] == size and e["version"] == v)["end_to_end_ms"][p] / 1000
    fig, axes = plt.subplots(1, 2, figsize=(8.6, 3.3), sharey=True)
    for ax, p, title in ((axes[0], "p50", "Typical receipt (median)"), (axes[1], "p95", "Slowest 5% (95th percentile)")):
        x = range(len(sizes))
        w = 0.36
        l = ax.bar([i - w / 2 - 0.01 for i in x], [get(s, "Lambda", p) for s in sizes], w, color=BLUE, label="Lambda")
        e = ax.bar([i + w / 2 + 0.01 for i in x], [get(s, "EC2", p) for s in sizes], w, color=ORANGE, label="EC2")
        for bars in (l, e):
            label_bars(ax, bars, lambda h: f"{h:.1f} s")
        ax.set_xticks(list(x), [f"{s} receipts" for s in sizes])
        ax.set_title(title, fontsize=10)
        ax.grid(axis="x", visible=False)
    axes[0].set_ylabel("Upload to result (s)")
    axes[0].legend(loc="upper left", fontsize=8.5)
    fig.suptitle("Delay from upload to result, timed on AWS's own clocks", x=0.01, ha="left", fontweight="bold", fontsize=11)
    fig.tight_layout()
    save(fig, "delay.png")


# ---------------------------------------------------------------- 5. true burst timeline

def chart_burst(numbers):
    data = [r for r in rows(RUNS["burst"], "burst.csv") if r["burst"] == "200" and r.get("finished_ms")]
    # Only AWS clocks here: time zero is when the first receipt started processing in Lambda
    # (finish time minus its processing time). The uploads themselves took 16 s from the laptop.
    t0 = min(int(r["finished_ms"]) - int(r["processing_ms"]) for r in data if r["version"] == "Lambda")
    uploads_span = (max(int(r["uploaded_ms"]) for r in data) - min(int(r["uploaded_ms"]) for r in data)) / 1000
    fig, ax = plt.subplots(figsize=(7.2, 3.4))
    summary = {}
    for v, color in (("Lambda", BLUE), ("EC2", ORANGE)):
        done = sorted((int(r["finished_ms"]) - t0) / 1000 for r in data if r["version"] == v)
        ax.step(done, range(1, len(done) + 1), where="post", color=color, lw=2, label=f"{v}: done")
        ax.annotate(f"all done at {done[-1]:.1f} s", (done[-1], len(done)), xytext=(8, -4),
                    textcoords="offset points", ha="left", va="center", fontsize=8.5, color=INK)
        summary[v] = {"all_done_s": round(done[-1], 1)}
    ec2 = [int(r["end_to_end_ms"]) / 1000 for r in data if r["version"] == "EC2"]
    summary["EC2"].update({"delay_p50_s": round(st.median(ec2), 1), "delay_max_s": round(max(ec2), 1)})
    summary["uploads_took_s"] = round(uploads_span, 1)
    summary["time_zero"] = "first receipt starting in Lambda (AWS clock)"
    numbers["true_burst_200"] = summary
    ax.set_xlabel("Seconds since the first receipt reached AWS")
    ax.set_ylabel("Receipts (of 200)")
    ax.set_ylim(0, 215)
    ax.set_xlim(0, max(float(x.get_xdata()[-1]) for x in ax.lines) + 7)
    ax.set_title(f"200 receipts uploaded over {uploads_span:.0f} s: Lambda keeps up, EC2 works in 10-second batches")
    ax.legend(loc="lower right", fontsize=8.5)
    save(fig, "burst-timeline.png")


# ---------------------------------------------------------------- 6. is it fair to EC2? check interval

def chart_poll(numbers):
    stream = load(RUNS["stream"], "burst.json")      # EC2 every 10 s
    fast = load(RUNS["poll2"], "burst.json")         # EC2 every 2 s; Lambda with the final code
    pick = lambda b, v: next(e for e in b["bursts"] if e["burst"] == 200 and e["version"] == v)["end_to_end_ms"]
    rows = [("Lambda (event-driven)", pick(fast, "Lambda"), BLUE, "$0 when idle"),
            ("EC2, checks every 2 s", pick(fast, "EC2"), ORANGE, "$20.62 / month"),
            ("EC2, checks every 10 s", pick(stream, "EC2"), ORANGE, "$15.37 / month")]
    numbers["poll_comparison_200"] = {name: {"p50_s": d["p50"] / 1000, "p95_s": d["p95"] / 1000, "max_s": d["max"] / 1000,
                                             "monthly": cost} for name, d, _, cost in rows}
    fig, ax = plt.subplots(figsize=(7.2, 2.6))
    y = list(range(len(rows)))[::-1]
    for yi, (name, d, color, cost) in zip(y, rows):
        ax.barh(yi, d["p50"] / 1000, 0.5, color=color)
        ax.plot([d["p50"] / 1000, d["p95"] / 1000], [yi, yi], color=INK2, lw=1.5)
        ax.plot([d["p95"] / 1000], [yi], marker="|", ms=12, color=INK2)
        ax.annotate(f"median {d['p50'] / 1000:.1f} s, 95% within {d['p95'] / 1000:.1f} s   ({cost})",
                    (d["p95"] / 1000, yi), xytext=(8, 0), textcoords="offset points", va="center", fontsize=8.5, color=INK)
    ax.set_yticks(y, [r[0] for r in rows])
    ax.set_xlim(0, 21)
    ax.set_xlabel("Upload to result (s): bar = median, line = up to the 95th percentile")
    ax.set_title("Checking every 2 s makes EC2 about as quick as Lambda, at a higher monthly cost")
    ax.grid(axis="y", visible=False)
    save(fig, "poll.png")


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", help="run folder name of the full accuracy test (626 receipts + GST set), e.g. exp-20261028-101500")
    args = ap.parse_args()
    if args.full:
        RUNS["accuracy_full"] = args.full
    os.makedirs(OUT, exist_ok=True)
    style()
    numbers = {"runs": RUNS}
    chart_accuracy(numbers)
    chart_coldstart(numbers)
    chart_tune(numbers)
    chart_stream(numbers)
    chart_burst(numbers)
    chart_poll(numbers)
    numbers["long_photo_speed"] = {
        "before_parallel_ms_p50": load(RUNS["accuracy_fixed"], "accuracy.json")["real_receipts"]["processing_ms"]["p50"],
        "after_parallel_ms_p50": load(RUNS["poll2"], "accuracy.json")["real_receipts"]["processing_ms"]["p50"],
        "billed_mean_before_ms": load(RUNS["accuracy_fixed"], "accuracy.json")["real_receipts"]["billed_ms"]["mean"],
        "billed_mean_after_ms": load(RUNS["poll2"], "accuracy.json")["real_receipts"]["billed_ms"]["mean"]}
    with open(os.path.join(OUT, "numbers.json"), "w") as fh:
        json.dump(numbers, fh, indent=2, default=str)
    print("charts and numbers written to", OUT)


if __name__ == "__main__":
    main()
