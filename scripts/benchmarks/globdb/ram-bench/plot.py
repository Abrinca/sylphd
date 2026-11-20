"""Draw the two figures of REPORT.md from results/<config>/summary.csv (no dependencies).

usage: plot.py CONFIG [--out results]
writes results/<CONFIG>/latency.svg and results/<CONFIG>/startup.svg
"""
import argparse, csv, os, pathlib, sys

ROOT = pathlib.Path(os.environ.get("RAMBENCH_WORK") or "ram-bench-work").expanduser().resolve()
W, H = 980, 460
PAD_L, PAD_R, PAD_T, PAD_B = 78, 150, 46, 62
COL = {"seq_ms": "#1f77b4", "c2_ms": "#d62728", "cold_ms": "#7f7f7f", "startup_s": "#2ca02c"}
LABEL = {"seq_ms": "warm, 1 request", "c2_ms": "warm, 2 concurrent", "cold_ms": "cold",
         "startup_s": "startup"}
UNLIM_OFFSET = 3.0            # "unlimited" sits this many GiB right of the biggest limit


def read(config, out):
    p = pathlib.Path(out) / config / "summary.csv"
    rows = list(csv.DictReader(open(p)))
    out = []
    for r in rows:
        lim = None if r["limit"] == "unlimited" else float(r["limit"])
        out.append((lim, r))
    return out


def xpos(lim, maxlim):
    x = maxlim + UNLIM_OFFSET if lim is None else lim
    return x


def series(rows, key):
    pts = []
    for lim, r in rows:
        v = r.get(key) or ""
        if v == "":
            continue
        pts.append((lim, float(v)))
    return pts


def svg(rows, keys, title, ylabel, path, logy=False, oom_below=None):
    pts = {k: series(rows, k) for k in keys}
    allp = [p for v in pts.values() for p in v]
    if not allp:
        return
    xs = [xpos(lim, max(l for l, _ in allp if l is not None)) for lim, _ in allp]
    ys = [v for _, v in allp]
    xmin, xmax = min(xs), max(xs)
    ymin, ymax = 0, max(ys) * 1.15
    if logy:
        ymin = 1
        ymax = max(ys) * 1.3

    def X(lim, maxlim):
        return PAD_L + (xpos(lim, maxlim) - xmin) / (xmax - xmin) * (W - PAD_L - PAD_R)

    def Y(v):
        if logy:
            import math
            f = (math.log10(max(v, 1)) - math.log10(ymin)) / (math.log10(ymax) - math.log10(ymin))
        else:
            f = (v - ymin) / (ymax - ymin)
        return H - PAD_B - f * (H - PAD_T - PAD_B)

    maxlim = max(l for l, _ in allp if l is not None)
    s = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
         f'font-family="DejaVu Sans, sans-serif" font-size="13">',
         f'<rect width="{W}" height="{H}" fill="white"/>',
         f'<text x="{PAD_L}" y="24" font-size="16" font-weight="bold">{title}</text>',
         f'<text x="18" y="{H/2}" transform="rotate(-90 18 {H/2})" text-anchor="middle">{ylabel}</text>']
    if oom_below is not None:
        x = X(oom_below, maxlim)
        s.append(f'<rect x="{PAD_L}" y="{PAD_T}" width="{x-PAD_L:.1f}" height="{H-PAD_T-PAD_B}" '
                 f'fill="#d62728" fill-opacity="0.07"/>')
        s.append(f'<text x="{(PAD_L+x)/2:.1f}" y="{PAD_T+18}" text-anchor="middle" fill="#a01f1f" '
                 f'font-size="12">OOM at {oom_below:g} GiB or less</text>')
    # y grid
    ticks = 6
    for i in range(ticks + 1):
        if logy:
            import math
            v = 10 ** (math.log10(ymin) + i * (math.log10(ymax) - math.log10(ymin)) / ticks)
        else:
            v = ymin + i * (ymax - ymin) / ticks
        y = Y(v)
        s.append(f'<line x1="{PAD_L}" y1="{y:.1f}" x2="{W-PAD_R}" y2="{y:.1f}" stroke="#e8e8e8"/>')
        s.append(f'<text x="{PAD_L-6}" y="{y+4:.1f}" text-anchor="end" fill="#555">{v:.0f}</text>')
    # x ticks at the measured limits
    for lim, _ in sorted({(l, None) for l, _ in allp}, key=lambda t: (t[0] is None, t[0] or 0)):
        x = X(lim, maxlim)
        label = "unlimited" if lim is None else f"{lim:g}"
        s.append(f'<line x1="{x:.1f}" y1="{H-PAD_B}" x2="{x:.1f}" y2="{H-PAD_B+6}" stroke="#333"/>')
        s.append(f'<text x="{x:.1f}" y="{H-PAD_B+22}" text-anchor="middle" fill="#333" '
                 f'transform="rotate(-35 {x:.1f} {H-PAD_B+22})">{label}</text>')
    s.append(f'<line x1="{PAD_L}" y1="{H-PAD_B}" x2="{W-PAD_R}" y2="{H-PAD_B}" stroke="#333"/>')
    s.append(f'<text x="{(PAD_L+W-PAD_R)/2}" y="{H-12}" text-anchor="middle">memory limit, GiB</text>')
    # baseline = unlimited value of the first key
    base = next((v for lim, v in pts[keys[0]] if lim is None), None)
    if base:
        y = Y(base)
        s.append(f'<line x1="{PAD_L}" y1="{y:.1f}" x2="{W-PAD_R}" y2="{y:.1f}" stroke="{COL.get(keys[0], "#000")}" '
                 f'stroke-dasharray="6 4" stroke-width="1"/>')
    for i, k in enumerate(keys):
        col = COL.get(k, "#333")
        d = " ".join(f"{'M' if j == 0 else 'L'}{X(l, maxlim):.1f},{Y(v):.1f}"
                     for j, (l, v) in enumerate(pts[k]))
        if d:
            s.append(f'<path d="{d}" fill="none" stroke="{col}" stroke-width="2"/>')
        for l, v in pts[k]:
            s.append(f'<circle cx="{X(l, maxlim):.1f}" cy="{Y(v):.1f}" r="3.5" fill="{col}"/>')
        ly = 60 + i * 22
        s.append(f'<rect x="{W-PAD_R+12}" y="{ly-9}" width="12" height="12" fill="{col}"/>')
        s.append(f'<text x="{W-PAD_R+30}" y="{ly+1}">{LABEL.get(k, k)}</text>')
    s.append("</svg>")
    path.write_text("\n".join(s))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("config")
    ap.add_argument("--out", default=str(ROOT / "results"))
    a = ap.parse_args()
    rows = read(a.config, a.out)
    if not rows:
        sys.exit(f"no {a.config}/summary.csv; run analyse.py --csv first")
    d = pathlib.Path(a.out) / a.config
    oom = [float(r["limit"]) for _, r in rows if r["startup_ok"] != "yes" and r["limit"] != "unlimited"]
    svg(rows, ["cold_ms", "seq_ms", "c2_ms"], f"{a.config}: profiling latency by memory limit",
        "latency, ms (median)", d / "latency.svg", oom_below=max(oom) if oom else None)
    ok = [(l, r) for l, r in rows if r["startup_ok"] == "yes"]
    if ok:
        svg(ok, ["startup_s"], f"{a.config}: startup time by memory limit", "startup, s",
            d / "startup.svg")
    print(f"wrote {d}/latency.svg and {d}/startup.svg")


if __name__ == "__main__":
    main()
