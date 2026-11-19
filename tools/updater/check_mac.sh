#!/bin/bash
# Checks a built dist/DimensionHopper.app without any boards plugged in: runs its --selftest (bundled esptool, AI
# packages, Bluetooth, flash worker...) and takes a --screenshot of the main window. Fails if the self-test finds
# something missing or the app doesn't open. Run after build_mac.sh:
#   bash tools/updater/check_mac.sh [output folder]   (default: dist/check)
set -euo pipefail
cd "$(dirname "$0")"
OUT="${1:-dist/check}"
mkdir -p "$OUT"
OUT="$(cd "$OUT" && pwd)"
EXE="dist/DimensionHopper.app/Contents/MacOS/DimensionHopper"
[ -x "$EXE" ] || { echo "No $EXE - run build_mac.sh first" >&2; exit 1; }

# macOS has no timeout command: perl's alarm stops a hung app instead of the whole CI job
limit() { local s=$1; shift; perl -e 'alarm shift; exec @ARGV or die "exec: $!"' "$s" "$@"; }

rm -f "$OUT/selftest.txt" "$OUT/screenshot.png"
limit 180 "$EXE" --selftest "$OUT/selftest.txt"
echo "--- self-test report"
cat "$OUT/selftest.txt"; echo
if grep -q "MISSING" "$OUT/selftest.txt"; then
    echo "The self-test found something missing from the app (lines with MISSING above)" >&2; exit 1
fi

# The window is grabbed after 8 s, once the first scan and the tab images have drawn
limit 120 "$EXE" --screenshot "$OUT/screenshot.png" 8
[ -s "$OUT/screenshot.png" ] || { echo "The app didn't save a screenshot" >&2; exit 1; }
echo "--- screenshot: $OUT/screenshot.png ($(sips -g pixelWidth -g pixelHeight "$OUT/screenshot.png" \
    | awk '/pixel/ {print $2}' | paste -sd x -))"
