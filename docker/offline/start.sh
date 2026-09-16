#!/bin/bash
set -euo pipefail
cd "${DEER_FLOW_PROJECT_ROOT:-/app}"
pids=()
cleanup() {
    trap - EXIT TERM INT
    if ((${#pids[@]})); then kill "${pids[@]}" 2>/dev/null || true; fi
    wait || true
}
trap cleanup EXIT
trap 'exit 143' TERM
trap 'exit 130' INT
(cd backend && exec python -m uvicorn app.gateway.app:app --host 0.0.0.0 --port 8001) &
pids+=("$!")
(cd frontend && exec pnpm start --hostname 0.0.0.0) &
pids+=("$!")
set +e
wait -n "${pids[@]}"
status=$?
set -e
exit "$status"
