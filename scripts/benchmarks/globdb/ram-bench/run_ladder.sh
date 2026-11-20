#!/bin/bash
# Run one whole memory-limit ladder for one configuration, one server at a time.
#
# usage: run_ladder.sh CONFIG [LIMIT ...]
#   Run it from the repository root. Environment: RAMBENCH_WORK (results, logs, sketches; default
#   ram-bench-work), PYTHON (interpreter with sylphd's dependencies; default `uv run python`),
#   TAG, FORCE=1. bench.py documents the other variables (SYLPHD_DB_DIR, DB_*, ...).
#   CONFIG  trio | globdb
#   LIMIT   GiB values, default: the ladder for that configuration
#
# Every run appends to results/<CONFIG>/runs.jsonl and requests.csv.  Limits already recorded there
# are skipped, so the script can be re-run after an interruption (FORCE=1 re-runs everything).
# Leftover containers named ramb-* are removed after each run.
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
WORK=${RAMBENCH_WORK:-ram-bench-work}; mkdir -p "$WORK"; WORK=$(cd "$WORK" && pwd); export RAMBENCH_WORK=$WORK
PY=${PYTHON:-uv run python}
CONFIG=${1:?usage: run_ladder.sh CONFIG [LIMIT ...]}
shift

case "$CONFIG" in
  trio)   DEFAULT="5 5.5 6 6.5 7 8 10 12 16 20 24 unlimited" ;;
  globdb) DEFAULT="6 6.5 7 8 9 10 12 16 20 24 28 unlimited" ;;
  *)      DEFAULT="4 6 8 12 16 24 unlimited" ;;
esac
LIMITS=${*:-$DEFAULT}
OUT=$WORK/results/$CONFIG
TAG=${TAG:-ladder}
LOG=$WORK/logs/ladder-$CONFIG.log
# FORCE=1 re-runs the given limits and replaces their earlier rows
[ "${FORCE:-0}" = 1 ] && REPLACE=--replace || REPLACE=
mkdir -p "$OUT" "$WORK/logs"
mkdir -p "$WORK/logs"; [ -f "$LOG" ] || : > "$LOG"

for L in $LIMITS; do
  if [ "${FORCE:-0}" != 1 ] && python3 - "$OUT/runs.jsonl" "$CONFIG" "$L" <<'EOF'
import json, sys
path, config, limit = sys.argv[1], sys.argv[2], sys.argv[3]
want = None if limit == "unlimited" else float(limit)
try:
    for line in open(path):
        r = json.loads(line)
        if r.get("config") == config and r.get("limit_gib") == want:
            sys.exit(0)          # already done
except FileNotFoundError:
    pass
sys.exit(1)
EOF
  then
    echo "skip $L (already in $OUT/runs.jsonl)" | tee -a "$LOG"
    continue
  fi
  echo "=== $(date '+%F %T') $CONFIG limit=$L   free: $(free -g | awk 'NR==2{print $7" GiB available, "$3" used"}')" \
    | tee -a "$LOG"
  timeout 1200 $PY -I "$HERE/bench.py" "$CONFIG" "$L" --reps 3 --out "$OUT" \
      --startup-timeout 420 --tag "$TAG" ${REPLACE:-} 2>&1 | tee -a "$LOG"
  for c in $(podman ps -a --filter name=ramb- --format '{{.Names}}'); do podman rm -f "$c" >/dev/null 2>&1; done
  sleep 3
done
echo "=== $(date '+%F %T') done; table:" | tee -a "$LOG"
$PY -I "$HERE/analyse.py" "$CONFIG" --out "$WORK/results" 2>&1 | tee -a "$LOG"
