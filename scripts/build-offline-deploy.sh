#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
OUTPUT=${OUTPUT:-$ROOT/.offline-deploy-output}
action=${1:-help}
images=(deerflow-offline:local deerflow-offline-resources:local deerflow-offline-deploy:local
        deerflow-offline-nginx:local deerflow-offline-sandbox:local)
case "$action" in
  prepare)
    docker image inspect deerflow-offline:local >/dev/null
    docker pull "${NGINX_IMAGE:-nginx:alpine}"
    docker tag "${NGINX_IMAGE:-nginx:alpine}" deerflow-offline-nginx:local
    docker pull "${SANDBOX_IMAGE:-enterprise-public-cn-beijing.cr.volces.com/vefaas-public/all-in-one-sandbox:latest}"
    docker tag "${SANDBOX_IMAGE:-enterprise-public-cn-beijing.cr.volces.com/vefaas-public/all-in-one-sandbox:latest}" deerflow-offline-sandbox:local
    context=$(mktemp -d)
    trap 'rm -rf -- "$context"' EXIT
    cp "$ROOT/docker/offline/Dockerfile.deploy" "$context/"
    docker build --pull=false -f "$context/Dockerfile.deploy" -t deerflow-offline-deploy:local "$context"
    ;;
  bundle)
    mkdir -p "$OUTPUT"
    # Never overwrite a live deployment's initialized files or data.
    if [[ -e "$OUTPUT/.env" || -d "$OUTPUT/data" ]]; then
      echo 'Output contains an initialized deployment; choose a new OUTPUT directory.' >&2
      exit 1
    fi
    cp "$ROOT/docker/offline/deploy/"*.{yaml,conf,py,sh,md} "$OUTPUT/"
    docker image inspect "${images[@]}" > "$OUTPUT/images.json"
    docker save -o "$OUTPUT/images.tar" "${images[@]}"
    (cd "$OUTPUT" && sha256sum images.tar images.json compose*.yaml nginx.conf init.py deploy.sh README.zh-CN.md > SHA256SUMS)
    echo "Offline deployment bundle: $OUTPUT"
    ;;
  *) echo 'Usage: scripts/build-offline-deploy.sh prepare|bundle'
     echo 'Variables: OUTPUT NGINX_IMAGE SANDBOX_IMAGE (upstream images may be pinned by digest)' ;;
esac
