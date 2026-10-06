#!/usr/bin/env bash
# Install AI Cutout for GIMP 3: the AI engine (in ~/.local/share/gimp-ai-cutout),
# the AI models, a background service that starts the engine whenever GIMP needs
# it, and the GIMP plug-in. Safe to run again: it repairs and updates.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP="${XDG_DATA_HOME:-$HOME/.local/share}/gimp-ai-cutout"
UNITS="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
PY="$APP/venv/bin/python"

say()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
fail() { printf '\n\033[1;31mProblem:\033[0m %s\n\n' "$*" >&2; exit 1; }
fetch() { if command -v curl >/dev/null; then curl -fsSL "$1"; else wget -qO- "$1"; fi; }

# -- checks, before downloading anything ---------------------------------------

[ "$(uname -s)" = Linux ] || fail "AI Cutout currently works on Linux only."
systemctl --user show-environment >/dev/null 2>&1 ||
    fail "This needs a normal Linux desktop session (systemd user services aren't available here)."
command -v curl >/dev/null || command -v wget >/dev/null || fail "Please install curl or wget first."

gimp_dirs=()
for dir in "$HOME"/.config/GIMP/3.* "$HOME"/.var/app/org.gimp.GIMP/config/GIMP/3.* \
           "$HOME"/snap/gimp/current/.config/GIMP/3.*; do
    [ -d "$dir" ] && gimp_dirs+=("$dir")
done
[ ${#gimp_dirs[@]} -gt 0 ] ||
    fail "Couldn't find GIMP 3's settings folder. Open GIMP 3 once, close it, then run this again."

mkdir -p "$APP"
free_gb=$(df -Pk "$APP" | awk 'NR==2 {print int($4 / 1048576)}')
if [ ! -x "$PY" ] && [ "$free_gb" -lt 9 ]; then
    fail "Not enough disk space. AI Cutout needs about 9 GB; ${free_gb} GB is free."
fi

if ! nvidia-smi >/dev/null 2>&1; then
    echo "Note: no NVIDIA graphics card found. AI Cutout will still work, but each picture takes several seconds."
fi

# -- 1. engine -----------------------------------------------------------------

say "Step 1 of 4: Setting up the AI engine (the big download; this can take a while)"
UV="$(command -v uv || true)"
if [ -z "$UV" ]; then
    UV="$APP/bin/uv"
    if [ ! -x "$UV" ]; then
        fetch https://astral.sh/uv/install.sh | env UV_INSTALL_DIR="$APP/bin" UV_NO_MODIFY_PATH=1 sh -s -- --quiet
    fi
fi
[ -x "$PY" ] || "$UV" venv --quiet --python 3.12 "$APP/venv"
"$UV" pip install --python "$PY" --reinstall-package gimp-ai-cutout-server "$HERE"

# -- 2. models -----------------------------------------------------------------

say "Step 2 of 4: Downloading the AI models (about 1.7 GB)"
"$APP/venv/bin/ai-cutout" --download-models

# -- 3. background service -----------------------------------------------------

say "Step 3 of 4: Setting up the background service"
install -Dm644 "$HERE/systemd/gimp-ai-cutout.socket" "$UNITS/gimp-ai-cutout.socket"
install -Dm644 "$HERE/systemd/gimp-ai-cutout.service" "$UNITS/gimp-ai-cutout.service"
systemctl --user daemon-reload
systemctl --user stop gimp-ai-cutout.service 2>/dev/null || true   # pick up new code on next use
systemctl --user enable --now gimp-ai-cutout.socket
device=$("$PY" -c "import json, urllib.request; print(json.load(urllib.request.urlopen('http://127.0.0.1:41879/health', timeout=120))['device'])") ||
    fail "The AI engine didn't start. See: journalctl --user -u gimp-ai-cutout"
case "$device" in
    cuda*) echo "The AI engine is working and uses your graphics card." ;;
    *)     echo "The AI engine is working (on the processor, so expect a few seconds per picture)." ;;
esac

# -- 4. GIMP plug-in -----------------------------------------------------------

say "Step 4 of 4: Adding AI Cutout to GIMP"
for dir in "${gimp_dirs[@]}"; do
    install -Dm755 "$HERE/plugin/ai-cutout/ai-cutout.py" "$dir/plug-ins/ai-cutout/ai-cutout.py"
    echo "Added to $dir"
done

say "Done! Close GIMP if it's open, then start it again."
echo "You'll find everything in the new AI menu at the top of the window."
echo "Start with AI > How to Use AI Cutout."
echo
