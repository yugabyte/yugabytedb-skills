#!/usr/bin/env bash
# Create isolated run directories for a fixture: <root>/<fixture>/<skill>-<model>/bundle
# Usage: prepare.sh <fixture> <root> [skills...] [-- models...]
#   defaults: skills "base head", models "haiku sonnet fable"
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
fixture="$1"; root="$2"; shift 2
skills=(base head); models=(haiku sonnet fable)
if [ $# -gt 0 ]; then
  skills=(); while [ $# -gt 0 ] && [ "$1" != "--" ]; do skills+=("$1"); shift; done
  [ "${1:-}" = "--" ] && { shift; models=("$@"); }
fi
# Synthetic fixtures are tracked under fixtures/; real-world ones stay in the ignored
# fixtures-private/.
src="$here/fixtures/$fixture/bundle"
[ -d "$src" ] || src="$here/fixtures-private/$fixture/bundle"
[ -d "$src" ] || { echo "no bundle for $fixture in fixtures/ or fixtures-private/" >&2; exit 2; }
for s in "${skills[@]}"; do
  for m in "${models[@]}"; do
    d="$root/$fixture/$s-$m"
    rm -rf "$d"; mkdir -p "$d/bundle"
    cp "$src"/* "$d/bundle/"
    echo "$d"
  done
done
