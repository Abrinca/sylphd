#!/bin/bash
# Profile pre-made sketches against one main database (+ UHGV + RefSeq fungi) on a temporary server.
#
# usage: run.sh NAME MAINDB
#   NAME    label of this configuration (e.g. gtdb, globdb); output goes to $OUT/NAME/
#   MAINDB  the main prokaryote database (.syl2db)
#
# Environment (all optional):
#   OUT         output directory                     (default: globdb-compare)
#   META        metadata database (sqlite)           (default: db/meta.sqlite)
#   UHGV, FUNGI the two extra databases              (default: db/uhgv_c200_dbv1.syldb,
#                                                     db/fungi-refseq-2025-10-11-c200-dbv1.syldb)
#   SKDIR       directory with SAMPLE.sylsp files    (default: sketches)
#   SAMPLES     space-separated sample names         (default: every *.sylsp in SKDIR)
#   SYLPHD      the sylphd command                   (default: sylphd; use "uv run sylphd" in a checkout)
#   EXTRA_ARGS  extra `sylphd profile` arguments, e.g. "--minimum-ani 90"
# Sketches: see make_sketches.py in ../ram-bench (c=200, k=31, like the databases).
# Writes NAME/SAMPLE.json (profile rows), NAME/SAMPLE* (sylph-tax-style taxonomic profiles per source),
# NAME/info.txt, NAME/time.txt and NAME/server.log.
set -euo pipefail
name=${1:?usage: run.sh NAME MAINDB}; main=${2:?usage: run.sh NAME MAINDB}
SYLPHD=${SYLPHD:-sylphd}
out=${OUT:-globdb-compare}/$name; mkdir -p "$out"
SKDIR=${SKDIR:-sketches}
META=${META:-db/meta.sqlite}
UHGV=${UHGV:-db/uhgv_c200_dbv1.syldb}
FUNGI=${FUNGI:-db/fungi-refseq-2025-10-11-c200-dbv1.syldb}
if [ -z "${SAMPLES:-}" ]; then
  SAMPLES=$(cd "$SKDIR" && for f in *.sylsp; do echo "${f%.sylsp}"; done)
fi
sock=$out/s.sock; rm -f "$sock"
$SYLPHD serve --db main="$main" --db uhgv="$UHGV" --db fungi="$FUNGI" \
  --meta "$META" --socket "$sock" > "$out/server.log" 2>&1 &
pid=$!
trap 'kill $pid 2>/dev/null; wait $pid 2>/dev/null || true' EXIT
for i in $(seq 180); do [ -S "$sock" ] && $SYLPHD info --socket "$sock" >/dev/null 2>&1 && break; sleep 1; done
$SYLPHD info --socket "$sock" > "$out/info.txt"
: > "$out/time.txt"
for s in $SAMPLES; do
  /usr/bin/time -f "$s %e s" $SYLPHD profile --socket "$sock" "$SKDIR/$s.sylsp" \
    --estimate-unknown --format json -o "$out/$s.json" --taxprof "$out/$s" ${EXTRA_ARGS:-} 2>>"$out/time.txt"
done
