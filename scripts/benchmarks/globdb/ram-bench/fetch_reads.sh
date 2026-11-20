#!/bin/bash
# usage: fetch_reads.sh [BYTES]   (writes under $RAMBENCH_WORK, default ram-bench-work)
# Fetch the first BYTES (default 200 MB) of both mates of ENA run DRR1003640 (human gut metagenome, 21.5M pairs,
# gzip FASTQ) into data/ena-DRR1003640/*.prefix.fq.gz. A truncated gzip prefix is fine; prepare_reads.sh cuts it
# at whole reads. ENA often needs ~1 min before the first byte, so each attempt gets a long timeout and resumes.
W=${RAMBENCH_WORK:-ram-bench-work}; D="$W/data/ena-DRR1003640"; mkdir -p "$D"; BYTES=${1:-200000000}
for m in 1 2; do
  f="$D/DRR1003640_$m.prefix.fq.gz"; : > "$f"
  for try in $(seq 1 40); do
    have=$(stat -c %s "$f"); [ "$have" -ge "$BYTES" ] && break
    curl -s -r "$have-$((BYTES-1))" -m 300 --connect-timeout 60 \
      "https://ftp.sra.ebi.ac.uk/vol1/fastq/DRR100/000/DRR1003640/DRR1003640_$m.fastq.gz" >> "$f"
    echo "mate $m try $try: $(stat -c %s "$f") bytes" >&2
  done
done
echo done >&2
