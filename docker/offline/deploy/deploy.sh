#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
action=${1:-help}
case "$action" in
  load)
    sha256sum -c SHA256SUMS
    docker load -i images.tar
    exit ;;
  init)
    docker run --rm --network=none --user "$(id -u):$(id -g)" \
      --mount "type=bind,src=$PWD,dst=/deployment-output" \
      deerflow-offline-deploy:local python /deployment-output/init.py "${2:-local}" "$PWD"
    exit ;;
  help|-h|--help)
    echo 'Usage: ./deploy.sh load|init [local|aio]|up|down|logs|ps|config|exec [command...]'
    exit ;;
esac
test -f .env || { echo 'Run ./deploy.sh init first' >&2; exit 1; }
mode=$(sed -n 's/^SANDBOX_MODE=//p' .env)
args=(--env-file .env -f compose.yaml)
case "$mode" in
  local) ;;
  aio) args+=(-f compose.aio.yaml) ;;
  *) echo 'Invalid SANDBOX_MODE in .env' >&2; exit 1 ;;
esac
case "$action" in
  up)
    # Fail before runtime can attempt an implicit sandbox image download.
    docker image inspect deerflow-offline-sandbox:local >/dev/null
    docker compose "${args[@]}" up -d --no-build --pull never ;;
  down) docker compose "${args[@]}" down ;;
  logs) docker compose "${args[@]}" logs --tail 100 -f ;;
  ps|config) docker compose "${args[@]}" "$action" ;;
  exec)
    shift
    if [[ $# -eq 0 ]]; then set -- bash; fi
    docker compose "${args[@]}" exec app "$@" ;;
  *) echo "Unknown action: $action" >&2; exit 2 ;;
esac
