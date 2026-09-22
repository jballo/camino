#!/usr/bin/env bash
# One t4g.small rehearsal run: enqueues ingest jobs, then samples container
# stats and API latency while the workers drain them, and finishes by printing
# a secret-free summary block to the terminal for pasting into a chat.
#
# A HUMAN runs this script — never an LLM/agent. The summary is what gets
# shared for interpretation; raw logs stay in loadtest-results/.
#
# Prereqs (environment comes from Doppler; there are no .env files).
# Recommended: the local throwaway test db, which keeps ingestion off Neon's
# 0.5 GB free tier entirely:
#   doppler run -- docker compose -f docker-compose.yml -f docker-compose.t4g.yml \
#     -f docker-compose.testdb.yml --profile worker up -d --scale worker=3
# Or against the Neon db doppler carries (watch the storage cap):
#   doppler run -- docker compose -f docker-compose.yml -f docker-compose.t4g.yml \
#     --profile worker up -d --scale worker=3
#
# Usage (run under doppler either way; --local-db must match how you launched):
#   doppler run -- ./run_t4g_test.sh --local-db --workers 3 \
#       --user-id user_xxx --installation-id 12345 \
#       --repo owner/a --repo owner/b --repo owner/c --repo owner/d --repo owner/e
#
# --workers N is required and must match the scale you launched; the preflight
# fails if the running stack disagrees, if the testdb container and the
# --local-db flag disagree, if any stray app process/container could steal
# queue jobs, or if per-container memory limits oversubscribe the shared
# parent cgroup.
#
# Secret safety, baked in:
#   - never runs `docker compose config`, never prints container environments
#   - never echoes env values; xtrace is force-disabled
#   - the summary is pattern-scanned for secret-looking strings before printing
#
# Raw samples land in loadtest-results/<timestamp>/:
#   stats.csv          epoch,container,cpu%,mem usage (2 s samples)
#   parent-cgroup.csv  aggregate memory + CPU throttling (2 s samples)
#   probe.csv          epoch,seconds for GET /openapi.json ("timeout" past 10 s)
#   oom.log            any container OOM kills docker reported during the run
#   jobs.log           status transitions + final timing table
#   summary.txt        the block printed at the end
set -euo pipefail
set +x  # never trace: tracing would echo the environment-derived guards below
cd "$(dirname "$0")"

fail() { echo "PRECHECK FAILED: $*" >&2; exit 1; }

COMPOSE=(docker compose -f docker-compose.yml -f docker-compose.t4g.yml --profile worker)
# local throwaway db (docker-compose.testdb.yml); credentials are not secrets
LOCAL_DB_URL="postgresql://loadtest:loadtest@localhost:5433/loadtest"

# ---- args: --workers/--local-db are ours; the rest goes to loadtest_enqueue
EXPECTED_WORKERS=""
LOCAL_DB=""
ENQUEUE_ARGS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --workers) EXPECTED_WORKERS="${2:-}"; shift 2 ;;
    --local-db) LOCAL_DB=1; shift ;;
    *) ENQUEUE_ARGS+=("$1"); shift ;;
  esac
done
[ -n "$EXPECTED_WORKERS" ] || fail "pass --workers N (the worker scale you launched)"

# Doppler must provide the environment either way (workers and the host
# scripts need the GitHub/OpenAI/Clerk secrets); with --local-db the database
# is the throwaway container instead of whatever doppler carries.
if [ -n "$LOCAL_DB" ]; then
  : "${OPENAI_API_KEY:?run under doppler: doppler run -- ./run_t4g_test.sh ...}"
  export DATABASE_URL="$LOCAL_DB_URL"
else
  : "${DATABASE_URL:?run under doppler: doppler run -- ./run_t4g_test.sh ...}"
fi

# The stack and the flag must agree on which database is in play, or the
# enqueued jobs land in one db while the workers poll another.
TESTDB_RUNNING=$(docker ps -q --filter "name=testdb")
if [ -n "$LOCAL_DB" ] && [ -z "$TESTDB_RUNNING" ]; then
  fail "--local-db passed but no testdb container is running; launch the stack with -f docker-compose.testdb.yml"
fi
if [ -z "$LOCAL_DB" ] && [ -n "$TESTDB_RUNNING" ]; then
  fail "a testdb container is running but --local-db was not passed; the workers are likely pointed at it — pass --local-db or take the testdb stack down"
fi

# ---- preflight: the stack is exactly what we think it is -------------------
STACK_CONTAINERS=$("${COMPOSE[@]}" ps -q)
[ -n "$STACK_CONTAINERS" ] || fail "t4g stack is not running; start it with docker-compose.t4g.yml"

ACTUAL_WORKERS=$("${COMPOSE[@]}" ps -q worker | wc -l | tr -d ' ')
[ "$ACTUAL_WORKERS" = "$EXPECTED_WORKERS" ] || \
  fail "expected $EXPECTED_WORKERS workers but $ACTUAL_WORKERS are running; rescale the stack or fix --workers"

# No host process may talk to the same Neon queue during the run (a dev
# `fastapi dev` or `python -m app.worker` steals jobs and invalidates the run).
STRAYS=""
for pid in $(pgrep -f 'python -m app\.worker|fastapi (dev|run)|uvicorn' || true); do
  [ "$pid" = "$$" ] && continue
  # on Linux, container processes show up in pgrep; skip those
  if [ -r "/proc/$pid/cgroup" ] && grep -q docker "/proc/$pid/cgroup"; then continue; fi
  STRAYS="$STRAYS $pid"
done
[ -z "$STRAYS" ] || fail "host app processes are running (pids:$STRAYS); stop dev servers/workers first"

# No container outside the stack may run the app either.
while IFS='|' read -r cid cname ccmd; do
  case "$STACK_CONTAINERS" in *"$cid"*) continue ;; esac
  if printf '%s' "$ccmd" | grep -qE 'app\.worker|app/main|fastapi'; then
    fail "container $cname (outside the stack) is running the app"
  fi
done < <(docker ps --no-trunc --format '{{.ID}}|{{.Names}}|{{.Command}}')

# ---- preflight: RAM budget is coherent -------------------------------------
# Every container in the shared cgroup must have a memory limit, and the
# limits must sum to <= the parent cap. Otherwise an aggregate OOM can fire
# before any container reaches its own limit, and "which config OOMed" is
# unanswerable — the flaw that invalidated the earlier runs.
T4G_MEMORY_MAX="${T4G_MEMORY_MAX:-1879048192}"
T4G_CPU_MAX="${T4G_CPU_MAX:-180000 100000}"
MEM_SUM=0
BUDGET_TABLE=""
while IFS= read -r container; do
  parent=$(docker inspect "$container" --format '{{.HostConfig.CgroupParent}}')
  name=$(docker inspect "$container" --format '{{.Name}}'); name=${name#/}
  # testdb models Neon: external to the box, so outside the cgroup and budget
  case "$name" in *testdb*) continue ;; esac
  [ "$parent" = /camino-t4g ] || fail "container $name is outside the shared /camino-t4g cgroup"
  mem=$(docker inspect "$container" --format '{{.HostConfig.Memory}}')
  [ "$mem" -gt 0 ] || fail "container $name has no memory limit; every container in the shared cgroup needs one"
  MEM_SUM=$((MEM_SUM + mem))
  BUDGET_TABLE+=$(printf '  %-40s %4d MiB' "$name" $((mem / 1048576)))$'\n'
done <<< "$STACK_CONTAINERS"
[ "$MEM_SUM" -le "$T4G_MEMORY_MAX" ] || \
  fail "per-container memory limits sum to $((MEM_SUM / 1048576)) MiB > parent cap $((T4G_MEMORY_MAX / 1048576)) MiB; lower API_MEM/DB_MEM/WORKER_MEM or the worker count"

# ---- preflight: the shared cgroup caps are actually applied ----------------
docker run --rm --memory 32m --memory-swap 32m \
  --cgroup-parent=/camino-t4g --cgroupns host \
  -e EXPECTED_CPU_MAX="$T4G_CPU_MAX" \
  -e EXPECTED_MEMORY_MAX="$T4G_MEMORY_MAX" \
  -v /sys/fs/cgroup:/host-cgroup:ro alpine:3.22 sh -euc '
    test "$(cat /host-cgroup/camino-t4g/cpu.max)" = "$EXPECTED_CPU_MAX"
    test "$(cat /host-cgroup/camino-t4g/memory.max)" = "$EXPECTED_MEMORY_MAX"
    test "$(cat /host-cgroup/camino-t4g/memory.swap.max)" = "0"
  ' || fail "shared t4g cgroup is not active; start the stack with docker-compose.t4g.yml"

# The api must be answering before jobs are enqueued — on a fresh testdb
# volume it is also what creates the schema on boot.
curl -sf -o /dev/null --max-time 5 http://localhost:8001/openapi.json || \
  fail "api on :8001 is not responding; wait for the stack to finish booting (first boot installs deps and creates the schema)"

OUT="$PWD/loadtest-results/$(date +%Y%m%d-%H%M%S)"
mkdir -p "$OUT"
echo "preflight ok: $EXPECTED_WORKERS workers, limits sum $((MEM_SUM / 1048576)) MiB <= $((T4G_MEMORY_MAX / 1048576)) MiB cap"
echo "raw samples -> $OUT"

# ---- monitors --------------------------------------------------------------
# The short-lived monitor joins the same parent cgroup, so its (capped, tiny)
# footprint is included in the rehearsal budget too.
CGROUP_MONITOR="camino-t4g-monitor-$$"
docker run --rm --name "$CGROUP_MONITOR" \
  --memory 32m --memory-swap 32m \
  --cgroup-parent=/camino-t4g --cgroupns host \
  -v /sys/fs/cgroup:/host-cgroup:ro alpine:3.22 sh -euc '
    echo "epoch,memory_current,memory_events_oom,memory_events_oom_kill,cpu_usage_usec,cpu_nr_throttled,cpu_throttled_usec"
    while true; do
      memory_current=$(cat /host-cgroup/camino-t4g/memory.current)
      memory_oom=$(awk '\''$1 == "oom" { print $2 }'\'' /host-cgroup/camino-t4g/memory.events)
      memory_oom_kill=$(awk '\''$1 == "oom_kill" { print $2 }'\'' /host-cgroup/camino-t4g/memory.events)
      cpu_usage=$(awk '\''$1 == "usage_usec" { print $2 }'\'' /host-cgroup/camino-t4g/cpu.stat)
      cpu_nr_throttled=$(awk '\''$1 == "nr_throttled" { print $2 }'\'' /host-cgroup/camino-t4g/cpu.stat)
      cpu_throttled=$(awk '\''$1 == "throttled_usec" { print $2 }'\'' /host-cgroup/camino-t4g/cpu.stat)
      echo "$(date +%s),$memory_current,$memory_oom,$memory_oom_kill,$cpu_usage,$cpu_nr_throttled,$cpu_throttled"
      sleep 2
    done
  ' >"$OUT/parent-cgroup.csv" &
CGROUP_PID=$!

(while true; do
  docker stats --no-stream --format "$(date +%s),{{.Name}},{{.CPUPerc}},{{.MemUsage}}"
  sleep 2
done) >"$OUT/stats.csv" &
STATS_PID=$!

# A failed probe logs curl's exit code (7=refused, 28=timed out, 56=reset) so
# failures are diagnosable and never pollute the latency samples.
(while true; do
  if t=$(curl -s -o /dev/null -w '%{time_total}' --max-time 10 \
      http://localhost:8001/openapi.json); then
    echo "$(date +%s),$t"
  else
    echo "$(date +%s),fail$?"
  fi
  sleep 2
done) >"$OUT/probe.csv" &
PROBE_PID=$!

docker events --filter event=oom --format '{{.Time}} {{.Actor.Attributes.name}}' \
  >"$OUT/oom.log" &
EVENTS_PID=$!

cleanup() {
  docker rm -f "$CGROUP_MONITOR" >/dev/null 2>&1 || true
  kill "$CGROUP_PID" "$STATS_PID" "$PROBE_PID" "$EVENTS_PID" 2>/dev/null || true
}
trap cleanup EXIT

# ---- the run ---------------------------------------------------------------
RUN_START=$(date +%s)
(cd Backend && uv run python -m scripts.loadtest_enqueue "${ENQUEUE_ARGS[@]}") | tee "$OUT/enqueue.log"
JOB_IDS=$(sed -n 's/^JOB_IDS=//p' "$OUT/enqueue.log")
[ -n "$JOB_IDS" ] || { echo "no jobs enqueued" >&2; exit 1; }

# shellcheck disable=SC2086
(cd Backend && uv run python -m scripts.loadtest_watch $JOB_IDS) | tee "$OUT/jobs.log"
RUN_END=$(date +%s)
cleanup
trap - EXIT

# ---- summary: the ONLY thing meant to be pasted into a chat ----------------
SUMMARY="$OUT/summary.txt"
{
  echo "==================== t4g rehearsal summary (safe to paste) ===================="
  echo "run: $(basename "$OUT")  duration: $((RUN_END - RUN_START)) s  workers: $EXPECTED_WORKERS"
  echo "parent cgroup: $((T4G_MEMORY_MAX / 1048576)) MiB RAM, cpu.max \"$T4G_CPU_MAX\", swap off"
  echo "per-container limits (sum $((MEM_SUM / 1048576)) MiB):"
  printf '%s' "$BUDGET_TABLE"

  awk -F, 'NR==2 { t0=$1; oom0=$3; kill0=$4; cpu0=$5; thr0=$6; thrus0=$7 }
    NR>1 { if ($2+0 > peak) peak=$2; t1=$1; oom1=$3; kill1=$4; cpu1=$5; thr1=$6; thrus1=$7 }
    END {
      dur = t1 - t0
      printf "aggregate RAM peak: %.0f MiB (%.0f%% of cap)   OOM events: %d (kills: %d)\n",
        peak/1048576, 100*peak/'"$T4G_MEMORY_MAX"', oom1-oom0, kill1-kill0
      printf "CPU: avg %.2f cores over %d s; throttled %d periods, %.1f s total throttle\n",
        (dur > 0 ? (cpu1-cpu0)/dur/1e6 : 0), dur, thr1-thr0, (thrus1-thrus0)/1e6
    }' "$OUT/parent-cgroup.csv"

  echo "per-container peaks:"
  awk -F, '
    function mib(s, v) { v = s + 0
      if (s ~ /GiB/) return v * 1024
      if (s ~ /KiB/) return v / 1024
      if (s ~ /MiB/) return v
      return v / 1048576 }
    NF >= 4 {
      split($4, a, " / "); m = mib(a[1]); c = $3 + 0
      if (m > peakm[$2]) peakm[$2] = m
      if (c > peakc[$2]) peakc[$2] = c
    }
    END { for (n in peakm) printf "  %-40s %4.0f MiB  %6.1f%% CPU\n", n, peakm[n], peakc[n] }
  ' "$OUT/stats.csv" | sort

  PROBE_OK=$(grep -vc ',fail' "$OUT/probe.csv" || true)
  PROBE_FAILS=$(grep -c ',fail' "$OUT/probe.csv" || true)
  if [ "$PROBE_OK" -gt 0 ]; then
    P95_IDX=$(( (PROBE_OK * 95 + 99) / 100 ))
    PROBE_SORTED=$(grep -v ',fail' "$OUT/probe.csv" | sort -t, -k2 -n | cut -d, -f2)
    P95=$(printf '%s\n' "$PROBE_SORTED" | sed -n "${P95_IDX}p")
    PMAX=$(printf '%s\n' "$PROBE_SORTED" | tail -1)
    echo "API probe: $PROBE_OK samples, p95 ${P95}s, max ${PMAX}s, $PROBE_FAILS failures (budget 0.5 s)"
  else
    echo "API probe: no successful samples, $PROBE_FAILS failures"
  fi
  if [ "$PROBE_FAILS" -gt 0 ]; then
    echo "  probe failures by curl exit code (7=refused, 28=timed out, 56=reset):"
    grep ',fail' "$OUT/probe.csv" | cut -d, -f2 | sort | uniq -c | sed 's/^/  /'
  fi
  echo "docker OOM-kill events: $(wc -l <"$OUT/oom.log" | tr -d ' ')"

  echo "jobs:"
  sed -n '/^peak concurrent running/,$p' "$OUT/jobs.log"
  echo "==============================================================================="
} >"$SUMMARY"

# Refuse to print anything that pattern-matches a secret. Belt over suspenders:
# nothing above should ever touch env values, but the run's job errors could in
# principle echo something they shouldn't.
if grep -qE '://[^[:space:]]*:[^[:space:]]*@|BEGIN [A-Z ]*PRIVATE KEY|sk-[A-Za-z0-9_-]{16,}|sk_(live|test)_|whsec_|api[_-]?key[[:space:]]*[:=]' "$SUMMARY"; then
  echo "SUMMARY WITHHELD: it matched a secret-looking pattern." >&2
  echo "Inspect and redact $SUMMARY manually before sharing." >&2
  exit 1
fi
echo
cat "$SUMMARY"
