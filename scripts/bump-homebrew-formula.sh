#!/usr/bin/env bash
set -euo pipefail
# Update Formula/continuum.rb to a new release tag.
# Usage: scripts/bump-homebrew-formula.sh v0.1.3
if [ $# -ne 1 ]; then
  echo "usage: $0 <tag>" >&2
  exit 1
fi
TAG="$1"
URL="https://github.com/Cyrax321/CONTINUUM/archive/refs/tags/${TAG}.tar.gz"
TMP="$(mktemp)"
curl -sL "$URL" -o "$TMP"
SHA="$(shasum -a 256 "$TMP" | cut -d' ' -f1)"
rm -f "$TMP"
FORMULA="Formula/continuum.rb"
python3 - "$FORMULA" "$URL" "$SHA" <<'PY'
import sys
path, url, sha = sys.argv[1], sys.argv[2], sys.argv[3]
text = open(path).read()
import re
text = re.sub(r'url "https://github.com/Cyrax321/CONTINUUM/archive/refs/tags/.*?"', f'url "{url}"', text)
text = re.sub(r'sha256 "[0-9a-f]{64}"', f'sha256 "{sha}"', text)
open(path, "w").write(text)
PY
echo "updated $FORMULA to $TAG $SHA"
ruby -c "$FORMULA"
