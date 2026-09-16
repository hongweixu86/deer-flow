ARG PYTHON_IMAGE=python:3.12-alpine3.22
FROM ${PYTHON_IMAGE} AS resources
ARG PIP_INDEX_URL=https://pypi.org/simple
ARG NPM_REGISTRY=https://registry.npmjs.org
ARG BUILD_JOBS=2
ENV CMAKE_BUILD_PARALLEL_LEVEL=${BUILD_JOBS} MAKEFLAGS=-j${BUILD_JOBS} \
    NEXT_TELEMETRY_DISABLED=1 PNPM_HOME=/opt/pnpm
RUN apk add --no-cache bash build-base linux-headers git vim cmake ninja \
    cargo openssl-dev libffi-dev zlib-dev jpeg-dev libxml2-dev libxslt-dev \
    openblas-dev gfortran postgresql-dev patchelf nodejs npm tini
RUN python -m pip install --no-cache-dir uv==0.11.6 \
    && npm install --global --registry="$NPM_REGISTRY" pnpm@10.26.2
WORKDIR /app
COPY backend/pyproject.toml backend/uv.lock backend/README.md ./backend/
COPY backend/packages/harness/pyproject.toml ./backend/packages/harness/
COPY frontend/package.json frontend/pnpm-lock.yaml frontend/pnpm-workspace.yaml ./frontend/
COPY docker/offline/prepare.sh /opt/offline-tools/prepare.sh
ENV CMAKE_GENERATOR=Ninja
RUN sh /opt/offline-tools/prepare.sh export
RUN sh /opt/offline-tools/prepare.sh native-source
COPY docker/offline/native-compat.sh /opt/offline-tools/native-compat.sh
RUN sh /opt/offline-tools/native-compat.sh
RUN sh /opt/offline-tools/prepare.sh native-wheel
COPY docker/offline/sqlite-vec.sh docker/offline/sqlite-vec.py /opt/offline-tools/
RUN sh /opt/offline-tools/sqlite-vec.sh
COPY docker/offline/seed-wheels/ /opt/offline/wheels/
RUN python -m pip wheel --wheel-dir=/opt/offline/wheels 'editables==0.5'
RUN sh /opt/offline-tools/prepare.sh wheels
ENV TIKTOKEN_CACHE_DIR=/opt/offline/tiktoken
COPY docker/offline/warm-tokenizers.py /opt/offline-tools/warm-tokenizers.py
RUN python -m pip install --no-index --find-links=/opt/offline/wheels tiktoken \
    && python /opt/offline-tools/warm-tokenizers.py
RUN sh /opt/offline-tools/prepare.sh frontend
