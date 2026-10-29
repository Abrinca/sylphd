#!/usr/bin/env python3
"""Regenerate the ``--taxonomy tax`` goldens from the real sylph-tax.

Manual, not part of the test suite.  ``tests/data/sylphtax/cases.json`` holds
synthetic taxonomy sources and ``sylphd profile`` rows that cover every probe
rule of ``sylph_to_taxprof.py`` (v1.9.2) — empty keys and empty lineage tokens,
hosts resolved across sources, an equal-abundance tie, repeated strain rows —
and this script writes the sources under their *source names* (so the
``Taxonomies_used:`` line sylph-tax prints is the same list sylphd prints), a
sylph-style TSV of the rows, and then runs the real
``sylph_tax.sylph_to_taxprof.main`` and ``sylph_tax.merge_sylph_taxprof`` on
them:

* ``<sample>.sylphmpa`` — the profile;
* ``<sample>.annotated.sylphmpa`` — the same run with ``-a`` (virus hosts), used
  as the reference for the resolved hosts (sylphd does not emit the column yet);
* ``samples.matrix.<value>.tsv`` — both samples through ``sylph-tax merge``;
* ``sample-a.matrix.<value>.tsv`` — one sample, which keeps the profile order.

Usage::

    scratch/sylph-tax-venv/bin/python scripts/regen-sylphtax-golden.py

where ``scratch/sylph-tax-venv`` is a venv with ``ref/sylph-tax`` installed
(``uv venv scratch/sylph-tax-venv && uv pip install -e ref/sylph-tax``).  The
same test reruns this comparison with ``sylph_tax`` importable, so the goldens
only matter for CI without it.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from sylph_tax.merge_sylph_taxprof import merge_data
from sylph_tax.sylph_to_taxprof import main as taxprof_main

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "tests" / "data" / "sylphtax"

#: The columns sylph-tax reads; the full profile header works too, but `main`
#: only checks that these exist in the DataFrame.
TSV_COLUMNS = (
    "Sample_file",
    "Genome_file",
    "Taxonomic_abundance",
    "Sequence_abundance",
    "Adjusted_ANI",
    "Eff_cov",
    "Contig_name",
)

#: ``sylph-tax merge --column`` for each matrix suffix.
MATRIX_COLUMNS = {
    "relative": "relative_abundance",
    "sequence": "sequence_abundance",
    "ani": "ANI",
    "coverage": "Coverage",
}


def _number(value: object) -> str:
    return repr(float(value))  # type: ignore[arg-type]


def write_rows(path: Path, samples: list[dict]) -> None:
    lines = ["\t".join(TSV_COLUMNS)]
    for sample in samples:
        for row in sample["rows"]:
            lines.append(
                "\t".join(
                    [
                        sample["name"],
                        str(row["Genome_file"]),
                        _number(row["Taxonomic_abundance"]),
                        _number(row["Sequence_abundance"]),
                        _number(row["Adjusted_ANI"]),
                        _number(row["Eff_cov"]),
                        str(row["Contig_name"]),
                    ]
                )
            )
    path.write_text("\n".join(lines) + "\n")


def run_taxprof(names: list[str], *, annotate_hosts: bool, prefix: str) -> None:
    taxprof_main(
        argparse.Namespace(
            taxonomy_dir=None,
            taxonomy_metadata=names,
            sylph_results=["rows.tsv"],
            output_prefix=prefix,
            add_folder_information=False,
            overwrite=True,
            annotate_virus_hosts=annotate_hosts,
            pavian=False,
        ),
        None,
    )


def main() -> int:
    cases = json.loads((DATA / "cases.json").read_text())
    names = [source["name"] for source in cases["sources"]]
    with TemporaryDirectory() as temporary:
        directory = Path(temporary)
        for source in cases["sources"]:
            (directory / source["name"]).write_text(
                "".join(
                    f"{key}\t{lineage}\t{host}\n"
                    if host is not None
                    else f"{key}\t{lineage}\n"
                    for key, lineage, host in source["rows"]
                ),
                newline="",
            )
        write_rows(directory / "rows.tsv", cases["samples"])

        previous = os.getcwd()
        os.chdir(directory)
        try:
            run_taxprof(names, annotate_hosts=False, prefix="")
            run_taxprof(names, annotate_hosts=True, prefix="annotated-")
        finally:
            os.chdir(previous)

        written = []
        for sample in cases["samples"]:
            for source, suffix in (
                (f"{sample['name']}.sylphmpa", ""),
                (f"annotated-{sample['name']}.sylphmpa", ".annotated"),
            ):
                path = directory / source
                target = DATA / f"{sample['name']}{suffix}.sylphmpa"
                target.write_bytes(path.read_bytes())
                print(f"wrote {target.relative_to(ROOT)}")
            written.append(DATA / f"{sample['name']}.sylphmpa")

        # Two samples: pandas sorts the clade names; one sample keeps the order.
        for prefix, files in (
            ("samples", written),
            ("sample-a", [written[0]]),
        ):
            for suffix, column in MATRIX_COLUMNS.items():
                merged = merge_data(files, column)
                target = DATA / f"{prefix}.matrix.{suffix}.tsv"
                merged.to_csv(target, sep="\t")
                print(f"wrote {target.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
