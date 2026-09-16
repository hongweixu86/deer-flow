"""Build musl vec0.so; preserve the locked upstream Python API and metadata."""
import hashlib
from pathlib import Path
import platform
import string
import subprocess
import sys
import tomllib
import urllib.request


def run(*args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


root = Path("/opt/offline/sources/sqlite-vec")
packages = tomllib.loads(Path("/opt/offline/uv.lock").read_text())["package"]
package = next(p for p in packages if p["name"] == "sqlite-vec")
version = package["version"]
commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
Path("/opt/offline/sqlite-vec-commit.txt").write_text(commit + "\n")

# This glibc wheel is ONLY a template for the upstream Python wrapper/metadata.
# Its native library is replaced entirely with a locally compiled musl library.
artifact = next(w for w in package["wheels"] if "manylinux" in w["url"] and "x86_64" in w["url"])
template = root / artifact["url"].rsplit("/", 1)[-1]
urllib.request.urlretrieve(artifact["url"], template)
if hashlib.sha256(template.read_bytes()).hexdigest() != artifact["hash"].removeprefix("sha256:"):
    raise RuntimeError("sqlite-vec upstream wheel checksum mismatch")
run(sys.executable, "-m", "wheel", "unpack", str(template), "-d", str(root / "unpacked"))
unpacked = root / "unpacked" / f"sqlite_vec-{version}"
libraries = list(unpacked.rglob("*.so"))
if libraries != [unpacked / "sqlite_vec" / "vec0.so"]:
    raise RuntimeError(f"Unexpected native payload in upstream wheel: {libraries}")

major, minor, patch = version.split(".")
header = string.Template((root / "sqlite-vec.h.tmpl").read_text()).substitute(
    VERSION=version, DATE="1970-01-01T00:00:00Z", SOURCE=commit,
    VERSION_MAJOR=major, VERSION_MINOR=minor, VERSION_PATCH=patch,
)
(root / "sqlite-vec.h").write_text(header)
# Upstream redundantly aliases BSD u_int*_t names, which musl does not expose.
# The standard uint*_t types already come from the included <stdint.h>.
source = root / "sqlite-vec.c"
code = source.read_text()
for bits in (8, 16, 64):
    code = code.replace(f"typedef u_int{bits}_t uint{bits}_t;", "")
source.write_text(code)
run("cc", "-fPIC", "-shared", "-O3", "-I", str(root), str(root / "sqlite-vec.c"),
    "-o", str(libraries[0]), "-lm")
metadata = unpacked / f"sqlite_vec-{version}.dist-info" / "WHEEL"
tag = f"py3-none-linux_{platform.machine()}"
metadata.write_text("\n".join(
    f"Tag: {tag}" if line.startswith("Tag:") else line
    for line in metadata.read_text().splitlines()
) + "\n")
run(sys.executable, "-m", "wheel", "pack", str(unpacked), "-d", "/opt/offline/wheels")
# Exercise actual extension loading and execution, not only Python imports.
sys.path.insert(0, str(unpacked))
import sqlite3
import sqlite_vec

with sqlite3.connect(":memory:") as db:
    db.enable_load_extension(True)
    sqlite_vec.load(db)
    actual = db.execute("select vec_version()").fetchone()[0]
    if actual != f"v{version}":
        raise RuntimeError(f"Unexpected sqlite-vec version: {actual}")
print(f"sqlite-vec {version} built and loaded on musl")
