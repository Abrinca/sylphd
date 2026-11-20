#!/bin/bash
# Minimum-ANI sweep: profile the same sketches at several --minimum-ani values against two
# configurations (main DB + UHGV + RefSeq fungi), one temporary server at a time. Then run
# analyse.py in the output directory to get ani_sweep.tsv and the plots.
#
# usage: run.sh            (everything through environment variables)
#   OUT         output directory                       (default: globdb-ani)
#   DB_DIR      database directory                     (default: db)
#   GTDB_DB     GTDB main database    (default: $DB_DIR/gtdb-r232-c200-dbv2.syl2db)
#   GLOBDB_DB   GlobDB main database  (default: $DB_DIR/globdb_r232_sylph_v2_c200.syl2db)
#   UHGV, FUNGI the two extra databases (default: $DB_DIR/uhgv_c200_dbv1.syldb, $DB_DIR/fungi-refseq-2025-10-11-c200-dbv1.syldb)
#   META        metadata database that knows both GTDB_r232 and GlobDB_r232 (default: $DB_DIR/meta.sqlite)
#   SKDIR       directory with the sketches (SAMPLE.sylsp)   (default: sketches)
#   SAMPLES     sample names, in the order wanted            (default: all *.sylsp in SKDIR)
#   ANIS        minimum ANI values                           (default: "95 90"; the sweep used "95 94 93 92 91 90")
#   SYLPHD      the sylphd command                           (default: sylphd)
# Writes $OUT/{gtdb,globdb}_<ani>.json, which analyse.py reads. analyse.py expects the sample names
# used in its SAMPLES table (edit that table for your own samples).
set -euo pipefail
SYLPHD=${SYLPHD:-sylphd}
O=${OUT:-globdb-ani}; mkdir -p "$O"
DB_DIR=${DB_DIR:-db}
UHGV=${UHGV:-$DB_DIR/uhgv_c200_dbv1.syldb}
FUNGI=${FUNGI:-$DB_DIR/fungi-refseq-2025-10-11-c200-dbv1.syldb}
META=${META:-$DB_DIR/meta.sqlite}
SKDIR=${SKDIR:-sketches}
if [ -z "${SAMPLES:-}" ]; then SAMPLES=$(cd "$SKDIR" && for f in *.sylsp; do echo "${f%.sylsp}"; done); fi
SK=(); for s in $SAMPLES; do SK+=("$SKDIR/$s.sylsp"); done
for cfg in gtdb globdb; do
  if [ $cfg = gtdb ]; then main=${GTDB_DB:-$DB_DIR/gtdb-r232-c200-dbv2.syl2db}
  else main=${GLOBDB_DB:-$DB_DIR/globdb_r232_sylph_v2_c200.syl2db}; fi
  sock=$O/$cfg.sock; rm -f "$sock"
  $SYLPHD serve --db main="$main" --db uhgv="$UHGV" --db fungi="$FUNGI" \
    --meta "$META" --socket "$sock" > "$O/$cfg-server.log" 2>&1 &
  pid=$!
  for i in $(seq 180); do $SYLPHD info --socket "$sock" >/dev/null 2>&1 && break; sleep 1; done
  for ani in ${ANIS:-95 90}; do
    $SYLPHD profile --socket "$sock" --estimate-unknown --minimum-ani "$ani" --format json \
      -o "$O/${cfg}_$ani.json" "${SK[@]}"
  done
  kill $pid; wait $pid 2>/dev/null || true
done
echo done
