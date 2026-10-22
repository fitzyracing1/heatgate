#!/usr/bin/env bash
# Build packaging/mcpb/dist/heatgate.mcpb. Uses the official MCPB CLI (npx @anthropic-ai/mcpb) when
# available, else falls back to a plain zip (an .mcpb is a zip with manifest.json at the root).
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"; root="$(cd "$here/../.." && pwd)"
ver="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$root/heatgate/__init__.py")"
b="$here/build"; rm -rf "$b" "$here/dist"; mkdir -p "$b/server/lib" "$here/dist"
sed "s/\"version\": \"[^\"]*\"/\"version\": \"$ver\"/" "$here/manifest.json" > "$b/manifest.json"
cp "$here/main.py" "$b/server/main.py"
cp -r "$root/heatgate" "$b/server/lib/heatgate"; find "$b" -name __pycache__ -prune -exec rm -rf {} +
cp "$root/LICENSE" "$b/LICENSE"
out="$here/dist/heatgate-$ver.mcpb"
if command -v npx >/dev/null 2>&1 && npx -y @anthropic-ai/mcpb validate "$b/manifest.json"; then
  npx -y @anthropic-ai/mcpb pack "$b" "$out"
else
  echo "mcpb CLI unavailable; zipping manually" >&2
  (cd "$b" && python3 -m zipfile -c "$out" manifest.json server LICENSE)
fi
echo "built $out"
