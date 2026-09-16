#!/bin/sh
set -eu
mkdir -p /opt/offline/wheels /opt/offline/pnpm-store
case "${1:?Preparation phase required}" in
export)
    cd /app/backend
    # The root project is a uv virtual project; package only the harness.
    uv export --locked --no-emit-workspace --no-hashes -o /opt/offline/requirements.txt >/dev/null
    cp uv.lock /opt/offline/uv.lock
    python -m pip wheel -w /opt/offline/wheels \
        'pip==26.0.1' 'hatchling==1.29.0' 'build==1.4.0' 'setuptools==82.0.1' 'wheel==0.46.3'
    ;;
native-source)
    # ONNX Runtime has no musllinux wheel or PyPI sdist at the locked version.
    ort_version=$(sed -n 's/^onnxruntime==\([^ ;]*\).*$/\1/p' /opt/offline/requirements.txt)
    if [ -n "$ort_version" ]; then
        python -m pip wheel --only-binary=:all: --no-deps \
            -w /opt/offline/wheels "onnxruntime==$ort_version" || {
            # Compile against the same NumPy ABI used by the locked runtime.
            sed -n '/^numpy==/p' /opt/offline/requirements.txt > /opt/offline/numpy-build.txt
            test -s /opt/offline/numpy-build.txt
            python -m pip install --find-links=/opt/offline/wheels \
                -r /opt/offline/numpy-build.txt 'setuptools==82.0.1' 'wheel==0.46.3' packaging
            mkdir -p /opt/offline/sources
            git clone --branch "v$ort_version" --depth 1 --recursive --shallow-submodules \
                https://github.com/microsoft/onnxruntime.git /opt/offline/sources/onnxruntime
            cd /opt/offline/sources/onnxruntime
            git rev-parse HEAD > /opt/offline/onnxruntime-commit.txt
            # musl has no execinfo.h; Release already disables the backtrace calls.
            sed -i 's/^#include <execinfo.h>/#if !defined(NDEBUG)\n#include <execinfo.h>\n#endif/' \
                onnxruntime/core/platform/posix/stacktrace.cc
        }
    fi
    ;;
native-wheel)
    if [ -d /opt/offline/sources/onnxruntime ]; then
        cd /opt/offline/sources/onnxruntime
        ./build.sh --config Release --build_wheel --skip_tests \
            --allow_running_as_root --parallel "${BUILD_JOBS:-2}" \
            --cmake_generator Ninja --compile_no_warning_as_error \
            --cmake_extra_defines onnxruntime_ENABLE_CPUINFO=OFF onnxruntime_BUILD_UNIT_TESTS=OFF
        cp build/Linux/Release/dist/*.whl /opt/offline/wheels/
    fi
    ;;
wheels)
    python -m pip wheel --prefer-binary --find-links=/opt/offline/wheels \
        -w /opt/offline/wheels -r /opt/offline/requirements.txt
    python -m pip freeze > /opt/offline/preparation-tools.txt
    cd /opt/offline
    find wheels -type f -name '*.whl' -exec sha256sum {} \; | sort > SHA256SUMS
    python -c 'import platform,sys; print(sys.version); print(platform.machine())' > platform.txt
    ;;
frontend)
    cd /app/frontend
    pnpm config set store-dir /opt/offline/pnpm-store
    pnpm config set registry "$NPM_REGISTRY"
    pnpm install --frozen-lockfile
    cp pnpm-lock.yaml /opt/offline/pnpm-lock.yaml
    ;;
*) echo 'Unknown preparation phase' >&2; exit 2 ;;
esac
