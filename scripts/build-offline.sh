#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
RESOURCE_IMAGE=${RESOURCE_IMAGE:-deerflow-offline-resources:local}
IMAGE=${IMAGE:-deerflow-offline:local}
OUTPUT=${OUTPUT:-$ROOT/.offline-output}
action=${1:-help}
cd "$ROOT"
CONTEXT=
trap 'if [[ -n "$CONTEXT" ]]; then rm -rf -- "$CONTEXT"; fi' EXIT
source_tar() {
    tar --exclude='.env' --exclude='.env.*' --exclude='config.yaml' \
        --exclude='extensions_config.json' --exclude='.venv' \
        --exclude='node_modules' --exclude='.next' --exclude='__pycache__' \
        --exclude='.deer-flow' --exclude='*.log' --exclude='dist' \
        -cf - backend frontend skills config.example.yaml \
        docker/offline scripts/build-offline.sh
}
context() {
    if [[ -z "$CONTEXT" ]]; then
        CONTEXT=$(mktemp -d)
        source_tar | tar -xf - -C "$CONTEXT"
        # Also works with legacy Docker without Dockerfile-specific ignores.
        cp docker/offline/Dockerfile.dockerignore "$CONTEXT/.dockerignore"
    fi
}
build() {
    docker image inspect "$RESOURCE_IMAGE" >/dev/null
    context
    docker build --pull=false --network=none --progress=plain \
        --build-arg "RESOURCE_IMAGE=$RESOURCE_IMAGE" \
        -f "$CONTEXT/docker/offline/Dockerfile.install" -t "$IMAGE" "$CONTEXT"
}
verify() {
    # Empty volumes hide ALL installed dependencies and frontend build output.
    # --rm removes these anonymous test volumes when the container exits.
    docker run --rm --network=none \
        --mount type=volume,destination=/opt/deerflow-venv,volume-nocopy \
        --mount type=volume,destination=/app/frontend/node_modules,volume-nocopy \
        --mount type=volume,destination=/app/frontend/.next,volume-nocopy \
        "$IMAGE" sh /opt/offline-tools/rebuild.sh all
}
case "$action" in
    prepare)
        context
        docker build --progress=plain -f "$CONTEXT/docker/offline/Dockerfile" \
            --build-arg "PYTHON_IMAGE=${PYTHON_IMAGE:-python:3.12-alpine3.22}" \
            --build-arg "PIP_INDEX_URL=${PIP_INDEX_URL:-https://pypi.org/simple}" \
            --build-arg "NPM_REGISTRY=${NPM_REGISTRY:-https://registry.npmjs.org}" \
            --build-arg "BUILD_JOBS=${BUILD_JOBS:-2}" \
            -t "$RESOURCE_IMAGE" "$CONTEXT"
        build
        verify
        ;;
    build) build ;;
    verify) verify ;;
    bundle)
        mkdir -p "$OUTPUT"
        docker save -o "$OUTPUT/images.tar" "$RESOURCE_IMAGE" "$IMAGE"
        source_tar | gzip > "$OUTPUT/source.tar.gz"
        printf 'RESOURCE_IMAGE=%q\nIMAGE=%q\n' "$RESOURCE_IMAGE" "$IMAGE" > "$OUTPUT/images.env"
        (cd "$OUTPUT" && sha256sum images.tar source.tar.gz images.env > SHA256SUMS)
        echo "Bundle: $OUTPUT"
        ;;
    load)
        (cd "$OUTPUT" && sha256sum -c SHA256SUMS)
        docker load -i "$OUTPUT/images.tar"
        ;;
    shell) docker run --rm -it --network=none "$IMAGE" bash ;;
    help|--help|-h)
        echo 'Usage: scripts/build-offline.sh prepare|build|verify|bundle|load|shell'
        echo 'prepare: online resources, then network-disabled build and clean reinstall test'
        echo 'build/verify: offline; bundle/load: export/import images and source archive'
        echo 'Variables: IMAGE RESOURCE_IMAGE OUTPUT PYTHON_IMAGE PIP_INDEX_URL NPM_REGISTRY BUILD_JOBS'
        ;;
    *) echo "Unknown action: $action" >&2; exit 2 ;;
esac
