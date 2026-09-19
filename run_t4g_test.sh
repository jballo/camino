#!/usr/bin/env bash
# One t4g.small rehearsal run: enqueues ingest jobs, then samples container
# stats and API latency while the workers drain them.
#
# Prereqs: DATABASE_URL exported (Neon), stack already up via
#   docker compose -f docker-compose.yml -f docker-compose.t4g.yml \
#     --profile worker up -d --scale worker=2
#
# Usage:
#   ./run_t4g_test.sh --user-id user_xxx --installation-id 12345 \
#       --repo owner/a --repo owner/b --repo owner/c --repo owner/d --repo owner/e
#
# Results land in loadtest-results/<timestamp>/:
#   stats.csv   epoch,container,cpu%,mem usage (2 s samples)
#   probe.csv   epoch,seconds for GET /openapi.json ("timeout" past 10 s)
#   oom.log     any container OOM kills docker reported during the run
#   jobs.log    status transitions + final timing table
set -euo pipefail
cd "$(dirname "$0")"

: "${DATABASE_URL:?export your Neon connection string first}"

OUT="$PWD/loadtest-results/$(date +%Y%m%d-%H%M%S)"
mkdir -p "$OUT"
echo "results -> $OUT"

(while true; do
  docker stats --no-stream --format "$(date +%s),{{.Name}},{{.CPUPerc}},{{.MemUsage}}"
  sleep 2
done) >"$OUT/stats.csv" &
STATS_PID=$!

(while true; do
  t=$(curl -s -o /dev/null -w '%{time_total}' --max-time 10 \
    http://localhost:8001/openapi.json || echo timeout)
  echo "$(date +%s),$t"
  sleep 2
done) >"$OUT/probe.csv" &
PROBE_PID=$!

docker events --filter event=oom --format '{{.Time}} {{.Actor.Attributes.name}}' \
  >"$OUT/oom.log" &
EVENTS_PID=$!

trap 'kill $STATS_PID $PROBE_PID $EVENTS_PID 2>/dev/null || true' EXIT

cd Backend
uv run python -m scripts.loadtest_enqueue "$@" | tee "$OUT/enqueue.log"
JOB_IDS=$(sed -n 's/^JOB_IDS=//p' "$OUT/enqueue.log")
[ -n "$JOB_IDS" ] || { echo "no jobs enqueued" >&2; exit 1; }

# shellcheck disable=SC2086
uv run python -m scripts.loadtest_watch $JOB_IDS | tee "$OUT/jobs.log"

echo
echo "slowest API probes (want < 0.5 s throughout):"
sort -t, -k2 -rn "$OUT/probe.csv" | head -5
echo "OOM kills during run: $(wc -l <"$OUT/oom.log" | tr -d ' ')"
