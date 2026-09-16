#!/bin/sh
set -eu
deps=/opt/offline/sources/onnxruntime/cmake/deps.txt
[ -f "$deps" ] || exit 0
# GitLab-generated archives can change bytes while their Git tree is unchanged.
# Fetch the exact upstream-pinned commit, then create a locally verified archive.
commit=$(sed -nE 's|^eigen;https://gitlab.com/libeigen/eigen/-/archive/([0-9a-f]{40})/.*|\1|p' "$deps")
[ -n "$commit" ] || { echo 'Cannot identify pinned Eigen commit' >&2; exit 1; }
src=/opt/offline/sources/eigen
git init "$src"
git -C "$src" remote add origin https://gitlab.com/libeigen/eigen.git
git -C "$src" fetch --depth 1 origin "$commit"
git -C "$src" checkout --detach FETCH_HEAD
test "$(git -C "$src" rev-parse HEAD)" = "$commit"
printf '%s\n' "$commit" > /opt/offline/eigen-commit.txt
git -C "$src" archive --format=tar.gz --prefix=eigen/ HEAD > /opt/offline/sources/eigen.tar.gz
python - <<'PY'
from pathlib import Path
import hashlib

archive = Path('/opt/offline/sources/eigen.tar.gz')
deps = Path('/opt/offline/sources/onnxruntime/cmake/deps.txt')
digest = hashlib.sha1(archive.read_bytes()).hexdigest()
deps.write_text('\n'.join(
    f'eigen;{archive.as_uri()};{digest}' if line.startswith('eigen;') else line
    for line in deps.read_text().splitlines()
) + '\n')
PY
