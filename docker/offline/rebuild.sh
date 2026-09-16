#!/bin/sh
set -eu
mode=${1:-all}
case "$mode" in all|backend|frontend) ;; *) echo 'Usage: rebuild.sh [all|backend|frontend]' >&2; exit 2;; esac
export PIP_NO_INDEX=1 PIP_FIND_LINKS=/opt/offline/wheels PIP_DISABLE_PIP_VERSION_CHECK=1
export UV_OFFLINE=1 NEXT_TELEMETRY_DISABLED=1
root=${DEER_FLOW_PROJECT_ROOT:-/app}
venv=${VIRTUAL_ENV:-/opt/deerflow-venv}
if [ "$mode" != frontend ]; then
    cmp "$root/backend/uv.lock" /opt/offline/uv.lock || {
        echo 'uv.lock changed: prepare an updated resource image while online first.' >&2; exit 1;
    }
    (cd /opt/offline && sha256sum -c SHA256SUMS >/dev/null)
    /usr/local/bin/python -m venv "$venv"
    "$venv/bin/python" -m pip install --no-cache-dir pip hatchling build setuptools wheel editables
    # Avoid eagerly compiling every third-party SDK; Python caches bytecode on import.
    "$venv/bin/python" -m pip install --no-cache-dir --no-compile -r /opt/offline/requirements.txt
    # Exercise wheel construction even though development uses an editable install.
    mkdir -p "$root/backend/dist"
    "$venv/bin/python" -m build --wheel --no-isolation \
        --outdir "$root/backend/dist" "$root/backend/packages/harness"
    "$venv/bin/python" -m pip install --no-cache-dir --no-build-isolation \
        -e "$root/backend/packages/harness"
    "$venv/bin/python" -m pip check
    "$venv/bin/python" -m compileall -q "$root/backend/app" "$root/backend/packages/harness/deerflow"
    "$venv/bin/python" /opt/offline-tools/verify.py "$root/backend"
fi
if [ "$mode" != backend ]; then
    cd "$root/frontend"
    cmp pnpm-lock.yaml /opt/offline/pnpm-lock.yaml || {
        echo 'pnpm-lock.yaml changed: prepare an updated resource image while online first.' >&2; exit 1;
    }
    CI=true pnpm install --offline --frozen-lockfile --prod=false --store-dir /opt/offline/pnpm-store
    NODE_ENV=production SKIP_ENV_VALIDATION=1 pnpm build
fi
