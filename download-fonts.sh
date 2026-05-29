#!/usr/bin/env bash
# =============================================================
# download-fonts.sh
# Run once from the repo root to populate the /fonts/ directory
# with the 4 JetBrains Mono woff2 files that index.html and
# resume.html reference via @font-face.
#
# Source: jsDelivr CDN mirror of the @fontsource/jetbrains-mono
# npm package. Fontsource is the de-facto standard for self-
# hostable open-source web fonts. The "latin" files are already
# subset to the Latin block; the optional pyftsubset pass below
# trims them further to only the glyphs this site actually
# renders (basic Latin plus a handful of typographic symbols).
#
# Usage:
#   chmod +x download-fonts.sh
#   ./download-fonts.sh
#
# For the smaller, fully-subset fonts, first install the tooling:
#   pip install fonttools brotli
# then run the script. Without it, the script still works and
# downloads the Fontsource latin subset as-is.
#
# After running, commit the /fonts/ directory to git.
# =============================================================

set -euo pipefail

FONTS_DIR="fonts"
BASE="https://cdn.jsdelivr.net/npm/@fontsource/jetbrains-mono@5/files"
WEIGHTS=(400 500 600 700)

# Character set the site renders: basic Latin (U+0020-007E) plus nbsp,
# copyright, middot, en/em dash, curly quotes, bullet, ellipsis, and the
# right arrow. Generous on purpose so no glyph ever goes missing.
SUBSET_UNICODES="U+0020-007E,U+00A0,U+00A9,U+00B7,U+2013,U+2014,U+2018,U+2019,U+201C,U+201D,U+2022,U+2026,U+2192"

mkdir -p "$FONTS_DIR"

if command -v pyftsubset >/dev/null 2>&1; then
  SUBSET=1
  echo "pyftsubset found — fonts will be subset to the site's character set."
else
  SUBSET=0
  echo "pyftsubset not found — saving the Fontsource 'latin' subset as-is."
  echo "(For smaller files: pip install fonttools brotli, then re-run.)"
fi

echo "Downloading JetBrains Mono woff2 files to $FONTS_DIR/ ..."
for w in "${WEIGHTS[@]}"; do
  src="$BASE/jetbrains-mono-latin-$w-normal.woff2"
  dst="$FONTS_DIR/jetbrains-mono-$w.woff2"
  echo "  $w -> $dst"
  curl -fsSL --retry 3 -o "$dst" "$src"
  if [ "$SUBSET" -eq 1 ]; then
    pyftsubset "$dst" \
      --unicodes="$SUBSET_UNICODES" \
      --flavor=woff2 \
      --output-file="$dst.tmp"
    mv "$dst.tmp" "$dst"
    echo "       subset -> $(du -h "$dst" | cut -f1)"
  fi
done

echo ""
echo "Done. Files in $FONTS_DIR/:"
ls -lah "$FONTS_DIR/"
echo ""
echo "Next: commit the fonts/ directory and push."
