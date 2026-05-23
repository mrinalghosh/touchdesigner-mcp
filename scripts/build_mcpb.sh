#!/usr/bin/env bash
# Build dist/touchdesigner-mcp.mcpb — a Claude Desktop MCP Bundle.
#
# Bundles the server package + vendored deps (mcp, httpx, transitive) next to
# mcpb/manifest.json, then zips it. Double-clicking the resulting .mcpb in
# Claude Desktop installs the server with a user-config form for TD_HOST etc.
#
# Requires: python3.10+, pip, zip. Run from repo root.
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$repo_root"

build_dir="build/mcpb"
out="dist/touchdesigner-mcp.mcpb"

rm -rf "$build_dir" "$out"
mkdir -p "$build_dir/server" "dist"

# Vendor runtime deps into server/lib first so we can drop the package on top
# and end up with a single PYTHONPATH entry.
python3 -m pip install \
  --target "$build_dir/server/lib" \
  --no-compile \
  --quiet \
  "mcp>=1.2.0" \
  "httpx>=0.27"

# Bundle the server package into the same lib dir
cp -R touchdesigner_mcp "$build_dir/server/lib/touchdesigner_mcp"
find "$build_dir/server/lib/touchdesigner_mcp" -type d -name __pycache__ -exec rm -rf {} +

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
