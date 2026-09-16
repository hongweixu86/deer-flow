# Optional prebuilt Python wheels

Place trusted wheels for the target Python/musl/CPU architecture here before
running `scripts/build-offline.sh prepare`. The dependency lock still controls
installed versions; pip ignores incompatible wheels. This is useful for reusing
expensive native builds such as DuckDB. Wheel files are intentionally gitignored.

The resulting resource image includes these files in `/opt/offline/wheels` and
records their SHA256 checksums. An empty seed directory is supported.
