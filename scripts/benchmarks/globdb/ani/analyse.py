"""Minimum-ANI sweep (90-95), GTDB vs GlobDB: write ani_sweep.tsv and plots.

usage: uv run --no-project --with matplotlib python -I analyse.py [DIR]

DIR is the directory with {gtdb,globdb}_{90..95}.json written by run.sh (default: the current
directory); ani_sweep.tsv and the four PNGs are written there too.
"""

import collections
import csv
import json
import os
import sys

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

SAMPLES = {
    "gut": "Gut, 2.6 M pairs (DRR1003640)",
    "gut1M": "Gut, 1 M pairs subset",
    "soil_ERR17228984": "Soil, HiFi (ERR17228984)",
    "batcave_SRR37337921": "Bat cave, HiFi (SRR37337921)",
    "bioreactor_SRR41427583": "Gas-fermentation bioreactor (SRR41427583)",
    "ocean": "Open-ocean seawater (SRR36700019)",
    "sim_R1": "Simulated reads (sim_R1)",
    "myco_reads": "Mycobacterium isolate reads",
}
DBS = {"gtdb": "GTDB r232", "globdb": "GlobDB r232"}
os.chdir(sys.argv[1] if len(sys.argv) > 1 else ".")
ANIS = [90, 91, 92, 93, 94, 95]
PROK = ("GTDB_r232", "GlobDB_r232")

records = []
for db in DBS:
    for ani in ANIS:
        for sample in json.load(open(f"{db}_{ani}.json")):
            rows = sample["rows"]
            seq = collections.defaultdict(float)
            for r in rows:
                seq[r["tax_source"]] += r["Sequence_abundance"] or 0
            pro = [r for r in rows if r["tax_source"] in PROK]
            below = [r for r in pro if r["Adjusted_ANI"] < 95]
            mags = [r for r in pro if r["tax_source"] == "GlobDB_r232" and r["gtdb_taxonomy"] is None]
            records.append(
                {
                    "sample": sample["name"],
                    "description": SAMPLES[sample["name"]],
                    "database": DBS[db],
                    "min_ani": ani,
                    "prokaryotes_pct": round(seq["GTDB_r232"] + seq["GlobDB_r232"], 3),
                    "uhgv_pct": round(seq["UHGV_default"], 3),
                    "fungi_pct": round(seq["FungiRefSeq-latest"], 3),
                    "total_classified_pct": round(sum(seq.values()), 3),
                    "prokaryote_hits": len(pro),
                    "hits_below_95": len(below),
                    "reads_on_hits_below_95_pct": round(sum(r["Sequence_abundance"] for r in below), 3),
                    "hits_globdb_only_mags": len(mags),
                    "reads_on_globdb_only_mags_pct": round(sum(r["Sequence_abundance"] for r in mags), 3),
                }
            )

base = {
    r["sample"]: r["total_classified_pct"]
    for r in records
    if r["database"] == "GTDB r232" and r["min_ani"] == 95
}
for r in records:
    r["delta_vs_gtdb_95_pct"] = round(r["total_classified_pct"] - base[r["sample"]], 3)

order = {s: i for i, s in enumerate(SAMPLES)}
records.sort(key=lambda r: (order[r["sample"]], r["database"] != "GTDB r232", -r["min_ani"]))
fields = [
    "sample", "description", "database", "min_ani", "prokaryotes_pct", "uhgv_pct", "fungi_pct",
    "total_classified_pct", "delta_vs_gtdb_95_pct", "prokaryote_hits", "hits_below_95",
    "reads_on_hits_below_95_pct", "hits_globdb_only_mags", "reads_on_globdb_only_mags_pct",
]
with open("ani_sweep.tsv", "w", newline="") as handle:
    writer = csv.DictWriter(handle, fieldnames=fields, delimiter="\t", lineterminator="\n")
    writer.writeheader()
    writer.writerows(records)

COLORS = {"GTDB r232": "#2563eb", "GlobDB r232": "#e11d48"}


def series(sample, db, key):
    pts = sorted((r["min_ani"], r[key]) for r in records if r["sample"] == sample and r["database"] == db)
    return [p[0] for p in pts], [p[1] for p in pts]


def grid(key, ylabel, title, filename, ylim=None, log=False):
    fig, axes = plt.subplots(2, 4, figsize=(16, 7.5), sharex=True)
    for ax, (sample, label) in zip(axes.flat, SAMPLES.items()):
        for db in DBS.values():
            x, y = series(sample, db, key)
            ax.plot(x, y, marker="o", color=COLORS[db], label=db)
        ax.set_title(label, fontsize=10)
        ax.axvline(95, color="0.6", lw=0.8, ls=":")
        ax.grid(alpha=0.3)
        if ylim:
            ax.set_ylim(*ylim)
        if log:
            ax.set_yscale("log")
    axes.flat[0].invert_xaxis()  # shared x: invert once, 95 (strictest) on the left
    for ax in axes[1]:
        ax.set_xlabel("minimum ANI (%)")
    for ax in axes[:, 0]:
        ax.set_ylabel(ylabel)
    axes.flat[0].legend(loc="lower left", fontsize=9)
    fig.suptitle(title)
    fig.tight_layout()
    fig.savefig(filename, dpi=130)
    plt.close(fig)


grid("total_classified_pct", "reads classified (%)",
     "Reads classified vs minimum ANI (main DB + UHGV + fungi, --estimate-unknown)",
     "total_classified.png", ylim=(0, 105))
grid("prokaryote_hits", "prokaryote hits",
     "Prokaryote hits vs minimum ANI", "prokaryote_hits.png", log=True)
grid("reads_on_hits_below_95_pct", "reads on hits < 95% ANI (%)",
     "Share of reads assigned to hits below 95% ANI (double counting / relatives)",
     "reads_below_95.png", ylim=(0, 60))

# One overview panel: GlobDB gain over GTDB per sample and threshold.
fig, ax = plt.subplots(figsize=(9, 4.5))
for sample, label in SAMPLES.items():
    if sample in ("sim_R1", "myco_reads"):
        continue
    x, g = series(sample, "GTDB r232", "total_classified_pct")
    _, b = series(sample, "GlobDB r232", "total_classified_pct")
    ax.plot(x, [bb - gg for gg, bb in zip(g, b)], marker="o", label=label)
ax.axhline(0, color="0.4", lw=0.8)
ax.axvline(95, color="0.6", lw=0.8, ls=":")
ax.invert_xaxis()
ax.set_xlabel("minimum ANI (%)")
ax.set_ylabel("GlobDB − GTDB, reads classified (points)")
ax.set_title("GlobDB gain over GTDB by minimum ANI")
ax.grid(alpha=0.3)
ax.legend(fontsize=8)
fig.tight_layout()
fig.savefig("globdb_gain.png", dpi=130)
print(f"wrote ani_sweep.tsv ({len(records)} rows) and 4 PNGs")
