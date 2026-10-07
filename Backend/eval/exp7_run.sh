#!/bin/sh
# Wrapper for local exp7/exp8 eval runs against the throwaway testdb.
# Doppler would otherwise substitute its remote DATABASE_URL; this applies the
# local override *after* Doppler. The credential below is the documented
# throwaway from docs/design/exp8-dim-confirmation-plan.md, not a secret.
#
# Usage: sh eval/exp7_run.sh [NAME=VALUE ...] uv run python -m eval.run_eval ...
EXP7_DATABASE_URL="${EXP7_DATABASE_URL:-postgresql://loadtest:loadtest@localhost:5433/loadtest}"
exec doppler run -- env DATABASE_URL="$EXP7_DATABASE_URL" "$@"
