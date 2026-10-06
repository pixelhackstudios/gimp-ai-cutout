#!/usr/bin/env bash
# Run tests/gimp_e2e.py in a separate headless GIMP (does not touch an open GIMP window).
#   tests/run_gimp_e2e.sh IMAGE OUTDIR [X,Y]
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMAGE="$(realpath "$1")"
OUT="$(realpath -m "$2")"
mkdir -p "$OUT"
if command -v gimp >/dev/null; then
    GIMP=(gimp)
else
    GIMP=(flatpak run --env=E2E_IMAGE="$IMAGE" --env=E2E_OUT="$OUT" --env=E2E_POINTS="${3:-}" org.gimp.GIMP)
fi
E2E_IMAGE="$IMAGE" E2E_OUT="$OUT" E2E_POINTS="${3:-}" "${GIMP[@]}" -n -i \
    --batch-interpreter=python-fu-eval -b "exec(open('$HERE/gimp_e2e.py').read())" --quit >"$OUT/gimp.log" 2>&1
cat "$OUT/report.txt"
grep -q '^0 failure' "$OUT/report.txt"
