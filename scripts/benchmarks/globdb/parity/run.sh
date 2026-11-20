#!/bin/bash
# Parity: stock sylph + sylph-tax vs sylphd on one database (GlobDB r232 in our run).
# Reads are sketched by stock sylph and by sylphd (pysylph); the same stock sketches are also
# profiled by sylphd, so profiling and sketching can be compared separately. Then run compare.py
# in the output directory.
#
# usage: run.sh           (environment variables, no arguments)
#   OUT        output directory                      (default: globdb-parity)
#   READS      directory with the reads              (default: reads)
#   DB         the database (.syl2db)                (default: db/globdb_r232_sylph_v2_c200.syl2db)
#   META       metadata database                     (default: db/meta.sqlite)
#   TAXONOMY   sylph-tax taxonomy TSV (.gz)          (default: tax/globdb_r232_taxonomy.tsv.gz)
#   TAX_SOURCE the sylphd taxonomy source name       (default: GlobDB_r232)
#   SYLPH      stock sylph binary                    (default: sylph)
#   SYLPH_TAX  stock sylph-tax command               (default: sylph-tax)
#   SYLPHD     the sylphd command                    (default: sylphd)
#   THREADS    threads                               (default: 16)
# Sample files expected in $READS: ERR17228984.fastq.gz (soil, single-end),
#   SRR37337921.fastq.gz (bat cave, single-end), SRR41427583_{1,2}.fastq.gz (bioreactor),
#   SRR36700019_{1,2}.fastq.gz (ocean). Edit the sketch/profile commands for other samples.
set -euo pipefail
P=${OUT:-globdb-parity}; D=${READS:-reads}
SYLPH=${SYLPH:-sylph}; SYLPH_TAX=${SYLPH_TAX:-sylph-tax}; SYLPHD=${SYLPHD:-sylphd}
DB=${DB:-db/globdb_r232_sylph_v2_c200.syl2db}; META=${META:-db/meta.sqlite}
TAXONOMY=${TAXONOMY:-tax/globdb_r232_taxonomy.tsv.gz}; TAX_SOURCE=${TAX_SOURCE:-GlobDB_r232}
T=${THREADS:-16}
mkdir -p "$P/sylph_sk" "$P/sylph" "$P/sylphd" "$P/sylphd_on_sylph_sk"
# 1. stock sylph: sketch + profile
$SYLPH sketch -t "$T" -d "$P/sylph_sk" -S soil -r "$D/ERR17228984.fastq.gz"
$SYLPH sketch -t "$T" -d "$P/sylph_sk" -S batcave -r "$D/SRR37337921.fastq.gz"
$SYLPH sketch -t "$T" -d "$P/sylph_sk" -S bioreactor -1 "$D/SRR41427583_1.fastq.gz" -2 "$D/SRR41427583_2.fastq.gz"
$SYLPH sketch -t "$T" -d "$P/sylph_sk" -S ocean -1 "$D/SRR36700019_1.fastq.gz" -2 "$D/SRR36700019_2.fastq.gz"
$SYLPH profile -t "$T" -u "$DB" "$P"/sylph_sk/*.sylsp -o "$P/sylph/profile.tsv"
# 2. sylph-tax on the stock output
$SYLPH_TAX taxprof "$P/sylph/profile.tsv" -t "$TAXONOMY" -o "$P/sylph/"
# 3. sylphd, this database only: from FASTQ (pysylph sketching) and from the stock sketches
sock=$P/s.sock; rm -f "$sock"
$SYLPHD serve --db "$DB" --meta "$META" --socket "$sock" > "$P/server.log" 2>&1 &
pid=$!; trap 'kill $pid 2>/dev/null; wait $pid 2>/dev/null || true' EXIT
for i in $(seq 180); do $SYLPHD info --socket "$sock" >/dev/null 2>&1 && break; sleep 1; done
C="$SYLPHD profile --socket $sock --estimate-unknown -t $T --fields none"
$C --sample "soil:$D/ERR17228984.fastq.gz" --sample "batcave:$D/SRR37337921.fastq.gz" \
   --sample "bioreactor:$D/SRR41427583_1.fastq.gz+$D/SRR41427583_2.fastq.gz" \
   --sample "ocean:$D/SRR36700019_1.fastq.gz+$D/SRR36700019_2.fastq.gz" \
   -o "$P/sylphd/profile.tsv" --taxonomy "tax:$TAX_SOURCE" --taxprof "$P/sylphd/"
$C "$P"/sylph_sk/*.sylsp -o "$P/sylphd_on_sylph_sk/profile.tsv" --taxonomy "tax:$TAX_SOURCE" --taxprof "$P/sylphd_on_sylph_sk/"
echo done
