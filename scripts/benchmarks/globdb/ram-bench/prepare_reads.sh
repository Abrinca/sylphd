#!/bin/bash
# Cut the truncated gzip prefixes fetched by fetch_reads.sh into valid FASTQ.
#
# fetch_reads.sh grabs a byte prefix of each mate, so the gzip stream is cut mid-member: zcat
# prints everything it can and then fails.  A FASTQ file must end on a whole record (4 lines),
# so this script decompresses the prefix once, keeps min(reads mate1, mate2) pairs aligned, and
# writes plain (uncompressed) FASTQ for the sketching step:
#
#   data/reads/gut_R1.fq    gut_R2.fq        2.6 M read pairs (the whole prefix)
#   data/reads/gut1M_R1.fq  gut1M_R2.fq      1 M read pairs
#   data/reads/gut250k_R1.fq gut250k_R2.fq   0.25 M read pairs
#
# The names end in _R1/_R2 so sketching pairs them into one sample each.
# Re-runnable: it overwrites its outputs, and skips work if they already look complete.
set -euo pipefail
cd "${RAMBENCH_WORK:-ram-bench-work}"   # the work directory (see fetch_reads.sh)

ENA=data/ena-DRR1003640
OUT=data/reads
PAIRS=${PAIRS:-2600000}          # must be <= the smaller mate (2,646,039 reads here)
mkdir -p "$OUT"

tmp1=$OUT/.all_R1.fq
tmp2=$OUT/.all_R2.fq
if [ ! -s "$tmp1" ] || [ "$(wc -l < "$tmp1")" -lt $((4 * PAIRS)) ]; then
  echo "decompressing mate 1 (truncated stream, errors expected)..." >&2
  zcat "$ENA/DRR1003640_1.prefix.fq.gz" 2>/dev/null | head -n $((4 * PAIRS)) > "$tmp1" || true
fi
if [ ! -s "$tmp2" ] || [ "$(wc -l < "$tmp2")" -lt $((4 * PAIRS)) ]; then
  echo "decompressing mate 2 (truncated stream, errors expected)..." >&2
  zcat "$ENA/DRR1003640_2.prefix.fq.gz" 2>/dev/null | head -n $((4 * PAIRS)) > "$tmp2" || true
fi

reads1=$(( $(wc -l < "$tmp1") / 4 ))
reads2=$(( $(wc -l < "$tmp2") / 4 ))
usable=$reads1
if [ "$reads2" -lt "$usable" ]; then usable=$reads2; fi
echo "usable read pairs: $usable (mate 1: $reads1, mate 2: $reads2)" >&2

cp "$tmp1" "$OUT/gut_R1.fq"
cp "$tmp2" "$OUT/gut_R2.fq"
head -n $((4 * 1000000)) "$tmp1" > "$OUT/gut1M_R1.fq"
head -n $((4 * 1000000)) "$tmp2" > "$OUT/gut1M_R2.fq"
head -n $((4 * 250000)) "$tmp1" > "$OUT/gut250k_R1.fq"
head -n $((4 * 250000)) "$tmp2" > "$OUT/gut250k_R2.fq"

for f in "$OUT"/*.fq; do
  n=$(wc -l < "$f")
  [ $((n % 4)) -eq 0 ] || { echo "ERROR: $f has $n lines, not a multiple of 4" >&2; exit 1; }
  echo "$(basename "$f"): $((n / 4)) reads" >&2
done
rm -f "$tmp1" "$tmp2"
echo "reads ready; now: uv run python -I scripts/benchmarks/globdb/ram-bench/make_sketches.py data/sketches gut:data/reads/gut_R1.fq+data/reads/gut_R2.fq ..." >&2
