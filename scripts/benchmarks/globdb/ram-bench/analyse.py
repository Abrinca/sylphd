"""Turn runs.jsonl / requests.csv of one configuration into tables.

usage: analyse.py CONFIG [--out results] [--csv]
  prints a markdown table (one row per memory limit) and, with --csv, also writes
  results/<CONFIG>/summary.csv.  Numbers are the same ones REPORT.md quotes.
"""
import argparse, csv, json, os, pathlib, statistics, sys

ROOT = pathlib.Path(os.environ.get("RAMBENCH_WORK") or "ram-bench-work").expanduser().resolve()
GIB = 1 << 30


def load(out, config):
    runs = []
    p = pathlib.Path(out) / config / "runs.jsonl"
    for line in p.read_text().splitlines():
        r = json.loads(line)
        if r.get("config") == config:
            runs.append(r)
    runs.sort(key=lambda r: (r["limit_gib"] is None, r["limit_gib"] or 0))
    return runs


def g(x, d=2):
    return "" if x is None else f"{x / GIB:.{d}f}"


def ms(x):
    return "" if x is None else f"{x * 1000:.0f}"


def row(r):
    st = r["after_startup"]
    fin = r.get("fincore_after") or {}
    ev = r.get("memory_events_final") or {}
    s = r["summary"]
    ok = r["startup_ok"]
    return dict(
        limit="unlimited" if r["limit_gib"] is None else f"{r['limit_gib']:g}",
        startup_ok="yes" if ok else ("OOM" if r.get("oom_killed") else "NO"),
        startup_s=f"{r['startup_s']:.1f}",
        peak_startup=g(st.get("peak")),
        anon_load=g(st.get("anon")),
        file_load=g(st.get("file")),
        anon_peak=g(r.get("timeseries_peak_anon")),
        file_final=g((r.get("final") or {}).get("file")),
        cold_ms=ms(s["cold"]["median_s"]),
        cold_p90=ms(s["cold"]["p90_s"]),
        seq_ms=ms(s["warm-seq"]["median_s"]),
        seq_p90=ms(s["warm-seq"]["p90_s"]),
        c2_ms=ms(s["warm-c2"]["median_s"]),
        c2_p90=ms(s["warm-c2"]["p90_s"]),
        fails=s["cold"]["fail"] + s["warm-seq"]["fail"] + s["warm-c2"]["fail"],
        oom_kill=ev.get("oom_kill", ""),
        majflt=(r.get("final") or {}).get("pgmajfault", ""),
        refault=(r.get("final") or {}).get("workingset_refault_file", ""),
        peak_all=g(max((r.get("final") or {}).get("peak") or 0, r.get("timeseries_peak_current") or 0) or None),
        cached=g(sum(v for v in fin.values() if v)),
        answers=("ok" if all(len(v) == 1 for v in (r.get("answer_digests") or {}).values()) else "DIFFER"),
    )


def per_sample_tables(out, config):
    """Per-sample latency and cold-phase database reads, straight from requests.csv."""
    p = pathlib.Path(out) / config / "requests.csv"
    if not p.exists():
        return
    rows = list(csv.DictReader(open(p)))
    if not rows:
        return
    samples = sorted({r["sample"] for r in rows})
    limits = []
    for r in rows:
        lim = r["limit_gib"] or "unlimited"
        if lim not in limits:
            limits.append(lim)

    def med(vals):
        vals = [float(v) for v in vals if v]
        return statistics.median(vals) if vals else None

    def fmt(phase, sub):
        v = med([r["latency_s"] for r in sub if r["phase"] == phase])
        return "" if v is None else f"{v * 1000:.0f}"

    print("\nPer-sample latency (ms) and cold-pass database reads (MB):\n")
    print("| limit | sample | cold ms | seq ms | c2 ms | cold IO MB | rows |")
    print("|---|---|---|---|---|---|---|")
    for lim in limits:
        for s in samples:
            sub = [r for r in rows if (r["limit_gib"] or "unlimited") == lim and r["sample"] == s]
            cold = [r for r in sub if r["phase"] == "cold"]
            io = max((int(r["io_rbytes"]) for r in cold if r["io_rbytes"]), default=None)
            nrows = next((r["n_rows"] for r in cold if r["n_rows"]), "")
            print(f"| {lim} | {s} | {fmt('cold', sub)} | {fmt('warm-seq', sub)} | {fmt('warm-c2', sub)} | "
                  + (f"{io / 1e6:.0f}" if io is not None else "")
                  + f" | {nrows} |")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("config")
    ap.add_argument("--out", default=str(ROOT / "results"))
    ap.add_argument("--csv", action="store_true")
    ap.add_argument("--per-sample", action="store_true")
    a = ap.parse_args()
    runs = load(a.out, a.config)
    rows = [row(r) for r in runs]
    cols = ["limit", "startup_ok", "startup_s", "peak_startup", "anon_load", "file_load", "anon_peak",
            "file_final", "peak_all", "cold_ms", "cold_p90", "seq_ms", "seq_p90", "c2_ms", "c2_p90",
            "fails", "oom_kill", "majflt", "refault", "cached", "answers"]
    hdr = {"limit": "limit GiB", "startup_ok": "start ok", "startup_s": "start s",
           "peak_startup": "peak@start", "anon_load": "anon@load", "file_load": "file@load",
           "anon_peak": "peak anon", "file_final": "file@end", "peak_all": "peak run",
           "cold_ms": "cold ms", "cold_p90": "cold p90", "seq_ms": "seq ms", "seq_p90": "seq p90",
           "c2_ms": "c2 ms", "c2_p90": "c2 p90", "fails": "fails", "oom_kill": "oom_kill",
           "majflt": "majflt", "refault": "refault", "cached": "cached GiB", "answers": "answers"}
    print("| " + " | ".join(hdr[c] for c in cols) + " |")
    print("|" + "---|" * len(cols))
    for r in rows:
        print("| " + " | ".join(str(r[c]) for c in cols) + " |")

    base = next((r for r in rows if r["limit"] == "unlimited" and r["seq_ms"]), None)
    if base:
        print("\nWarm sequential latency relative to unlimited (median, all samples):\n")
        print("| limit GiB | seq ms | vs unlimited | c2 ms | vs unlimited |")
        print("|---|---|---|---|---|")
        for r in rows:
            if not r["seq_ms"]:
                continue
            seq = int(r["seq_ms"]) / int(base["seq_ms"])
            c2 = int(r["c2_ms"]) / int(base["c2_ms"]) if r["c2_ms"] and base["c2_ms"] else None
            print(f"| {r['limit']} | {r['seq_ms']} | {seq*100:.0f}% | {r['c2_ms']} | "
                  + (f"{c2*100:.0f}% |" if c2 else " |"))

    if a.csv:
        dest = pathlib.Path(a.out) / a.config / "summary.csv"
        with open(dest, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            w.writerows(rows)
        print(f"\nwrote {dest}", file=sys.stderr)
    if a.per_sample:
        per_sample_tables(a.out, a.config)


if __name__ == "__main__":
    main()
