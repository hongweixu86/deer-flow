#!/bin/sh
set -eu
version=$(sed -n 's/^sqlite-vec==\([^ ;]*\).*$/\1/p' /opt/offline/requirements.txt)
[ -n "$version" ] || exit 0
if python -m pip wheel --only-binary=:all: --no-deps -w /opt/offline/wheels "sqlite-vec==$version"; then
    exit 0
fi
apk add --no-cache sqlite-dev
python -m pip install --no-index --find-links=/opt/offline/wheels setuptools wheel
git clone --depth 1 --branch "v$version" https://github.com/asg017/sqlite-vec.git /opt/offline/sources/sqlite-vec
python /opt/offline-tools/sqlite-vec.py
