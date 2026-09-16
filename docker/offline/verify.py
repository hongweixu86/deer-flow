"""Check root virtual-project requirements and load musl native extensions."""
import importlib
import importlib.metadata
import pathlib
import sys
import tomllib

from packaging.requirements import Requirement

root = pathlib.Path(sys.argv[1])
project = tomllib.loads((root / "pyproject.toml").read_text())
for value in project["project"]["dependencies"]:
    req = Requirement(value)
    if req.marker and not req.marker.evaluate():
        continue
    installed = importlib.metadata.version(req.name)
    if installed not in req.specifier:
        raise RuntimeError(f"{req.name}: installed {installed}, required {req.specifier}")
for name in ("deerflow", "fastapi", "duckdb", "onnxruntime", "magika", "tiktoken", "cryptography.hazmat.bindings._rust"):
    importlib.import_module(name)
print("Backend requirements and native imports verified")

import duckdb

with duckdb.connect(":memory:") as db:
    assert db.execute("select 6 * 7").fetchone() == (42,)
print("DuckDB executed an in-memory query")

import sqlite3
import sqlite_vec

with sqlite3.connect(":memory:") as db:
    db.enable_load_extension(True)
    sqlite_vec.load(db)
    assert db.execute("select vec_version()").fetchone()[0] == "v" + importlib.metadata.version("sqlite-vec")
    assert db.execute("select vec_length(vec_f32('[1,2,3]'))").fetchone()[0] == 3
print("SQLite vector extension loaded and executed")

import tiktoken

for name in ("cl100k_base", "o200k_base"):
    assert tiktoken.get_encoding(name).encode("DeerFlow 离线验证")
print("Tokenizer data loaded from offline cache")
