#!/usr/bin/env bash
# =============================================================
# download-fonts.sh
# Run once from the repo root to populate the /fonts/ directory
# with the 4 JetBrains Mono woff2 files that index.html and
# resume.html reference via @font-face.
#
# Source: jsDelivr CDN mirror of the @fontsource/jetbrains-mono
# npm package. Fontsource is the de-facto standard for self-
# hostable open-source web fonts.
#
# Usage:
#   chmod +x download-fonts.sh
#   ./download-fonts.sh
#
# After running, commit the /fonts/ directory to git.
# =============================================================

set -euo pipefail

FONTS_DIR="fonts"
BASE="https://cdn.jsdelivr.net/npm/@fontsource/jetbrains-mono@5/files"
WEIGHTS=(400 500 600 700)

mkdir -p "$FONTS_DIR"

echo "Downloading JetBrains Mono woff2 files to $FONTS_DIR/ ..."
for w in "${WEIGHTS[@]}"; do
  src="$BASE/jetbrains-mono-latin-$w-normal.woff2"
  dst="$FONTS_DIR/jetbrains-mono-$w.woff2"
  echo "  $w → $dst"
  curl -fsSL --retry 3 -o "$dst" "$src"
done

echo ""
echo "Done. Files in $FONTS_DIR/:"
ls -lah "$FONTS_DIR/"
echo ""
echo "Next: commit the fonts/ directory and push."
