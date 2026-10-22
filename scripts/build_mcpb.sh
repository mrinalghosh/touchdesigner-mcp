#!/usr/bin/env bash
# Build dist/touchdesigner-mcp.mcpb — a Claude Desktop MCP Bundle.
#
# Uses the MCPB `uv` server type: the bundle ships pyproject.toml + uv.lock
# instead of vendored deps, and Claude Desktop runs `uv sync` at install time
# against a Python it picks. Vendoring (the old `python` type) baked in
# compiled wheels — pydantic_core's .so — for whatever python3 ran this
# script, so the extension crashed on import whenever Desktop's python3 was a
# different minor version.
#
# Requires: uv, zip. Run from repo root.
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$repo_root"

build_dir="build/mcpb"
out="dist/touchdesigner-mcp.mcpb"

# Ship a lockfile that matches pyproject.toml, so every install resolves the
# same dependency versions.
uv lock --check

rm -rf "$build_dir" "$out"
mkdir -p "$build_dir" "dist"

# Project files uv needs to sync + install the package
cp pyproject.toml uv.lock "$build_dir/"

# Cap the Python Desktop's `uv sync` may pick. Uncapped, uv takes the newest
# it finds or can download, and a fresh release can predate the lock's wheels
# (3.15 shipped 2026-10-09; the locked pydantic-core stops at cp314) — uv then
# builds from source, which needs Rust and fails on most machines. The cap is
# the newest minor that every compiled dep in uv.lock has wheels for, so it
# rises on its own once a re-lock brings newer wheels. Bundle-only: a range in
# the repo's .python-version would confuse pyenv.
uv run --no-project --quiet --python '>=3.11' python - > "$build_dir/.python-version" <<'PY'
import re, tomllib
lock = tomllib.load(open("uv.lock", "rb"))
cap = None
for pkg in lock["package"]:
    names = [w["url"].rsplit("/", 1)[1] for w in pkg.get("wheels", [])]
    # abi3 and pure-Python wheels keep working on future versions
    if not names or any("-abi3-" in n or "-none-any" in n for n in names):
        continue
    minors = [int(m) for n in names for m in re.findall(r"-cp3(\d+)-cp3\d+t?-", n)]
    if minors:
        cap = max(minors) if cap is None else min(cap, max(minors))
floor = lock["requires-python"]
print(f"{floor},<3.{cap + 1}" if cap is not None else floor)
PY
echo "bundle python: $(cat "$build_dir/.python-version")"
cp -R touchdesigner_mcp "$build_dir/touchdesigner_mcp"
find "$build_dir/touchdesigner_mcp" -type d -name __pycache__ -exec rm -rf {} +

# Manifest + metadata
cp mcpb/manifest.json "$build_dir/manifest.json"
cp README.md "$build_dir/README.md"
cp LICENSE  "$build_dir/LICENSE"

# TD-side component. The bridge can't auto-install into TD (separate process),
# so ship the .tox in the bundle and tell the user to drag it into /project1.
mkdir -p "$build_dir/td_component"
cp td_component/touchdesigner_mcp.tox "$build_dir/td_component/"
cp td_component/webserver_callbacks.py "$build_dir/td_component/"

# Zip. Claude Desktop expects manifest.json at the archive root.
(cd "$build_dir" && zip -qr "$repo_root/$out" .)

size=$(du -h "$out" | awk '{print $1}')
echo "built $out ($size)"
