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
