#!/usr/bin/env bash
# Install AI Cutout for GIMP 3: the AI engine (in ~/.local/share/gimp-ai-cutout),
# the AI models, the image generator, a background service that starts the engine
# whenever GIMP needs it, and the GIMP plug-in. Safe to run again: it repairs and
# updates.
#
#   bash install.sh                 everything
#   bash install.sh --no-generate   skip image generation (saves about 7 GB)
set -euo pipefail

GENERATE=1
for arg in "$@"; do
    case "$arg" in
        --no-generate) GENERATE=0 ;;
        *) echo "Unknown option: $arg" >&2; exit 2 ;;
    esac
done
[ "${AI_CUTOUT_NO_GENERATE:-0}" = 1 ] && GENERATE=0

# stable-diffusion.cpp release used for image generation, pinned by checksum.
SD_TAG="master-954-05845fd"
SD_VULKAN_ZIP="sd-master-05845fd-bin-Linux-Ubuntu-24.04-x86_64-vulkan.zip"
SD_VULKAN_SHA256="15c0c3ddd0468389151902c59e7ae4fbc1c4768b0a306b6bf8ce1b2e36b1ea4d"
SD_CPU_ZIP="sd-master-05845fd-bin-Linux-Ubuntu-24.04-x86_64.zip"
SD_CPU_SHA256="ea088f3cab0db1322d183865b16878df9836d41fb71552d56a3679bac5e1c808"

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
need_gb=$([ "$GENERATE" = 1 ] && echo 16 || echo 9)
free_gb=$(df -Pk "$APP" | awk 'NR==2 {print int($4 / 1048576)}')
if [ ! -x "$PY" ] && [ "$free_gb" -lt "$need_gb" ]; then
    fail "Not enough disk space. AI Cutout needs about $need_gb GB; ${free_gb} GB is free."
fi

if ! nvidia-smi >/dev/null 2>&1; then
    echo "Note: no NVIDIA graphics card found. Background removal will still work, but each picture takes several seconds."
fi

# Image generation runs on any graphics card through Vulkan, or (very slowly) on the processor.
# (No `ldconfig | grep -q` here: with pipefail, grep exiting early fails the pipeline.)
sd_variant=cpu
libs="$(ldconfig -p 2>/dev/null || true)"
if [[ "$libs" == *libvulkan.so.1* ]] &&
   grep -qsE '0x(10de|1002|8086)' /sys/class/drm/card*/device/vendor; then
    sd_variant=vulkan
fi
if [ "$GENERATE" = 1 ] && [ "$sd_variant" = cpu ]; then
    echo "Note: no usable graphics card found for image generation. It will work, but each picture takes several minutes."
fi

# -- 1. engine -----------------------------------------------------------------

say "Step 1 of 5: Setting up the AI engine (the big download; this can take a while)"
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

if [ "$GENERATE" = 1 ]; then
    say "Step 2 of 5: Downloading the AI models (about 8.4 GB)"
    "$APP/venv/bin/ai-cutout" --download-models --with-generation
else
    say "Step 2 of 5: Downloading the AI models (about 1.7 GB)"
    "$APP/venv/bin/ai-cutout" --download-models
fi

# -- 3. image generator -------------------------------------------------------

if [ "$GENERATE" = 1 ]; then
    say "Step 3 of 5: Setting up the image generator"
    if [ "$sd_variant" = vulkan ]; then zip="$SD_VULKAN_ZIP"; sha="$SD_VULKAN_SHA256"
    else zip="$SD_CPU_ZIP"; sha="$SD_CPU_SHA256"; fi
    if [ "$(cat "$APP/sd/.release" 2>/dev/null)" != "$zip" ]; then
        fetch "https://github.com/leejet/stable-diffusion.cpp/releases/download/$SD_TAG/$zip" > "$APP/sd.zip"
        echo "$sha  $APP/sd.zip" | sha256sum --check --status ||
            fail "The image generator download was corrupted. Please run the installer again."
        rm -rf "$APP/sd.new"
        "$PY" -m zipfile -e "$APP/sd.zip" "$APP/sd.new"
        chmod +x "$APP/sd.new/sd-cli" "$APP/sd.new/sd-server"
        echo "$zip" > "$APP/sd.new/.release"
        rm -rf "$APP/sd" "$APP/sd.zip"
        mv "$APP/sd.new" "$APP/sd"
    fi
    LD_LIBRARY_PATH="$APP/sd" "$APP/sd/sd-cli" --help >/dev/null 2>&1 ||
        fail "The image generator doesn't run on this system."
    echo "Image generator ready ($([ "$sd_variant" = vulkan ] && echo "graphics card" || echo "processor only"))."
else
    say "Step 3 of 5: Skipping the image generator"
    rm -rf "$APP/sd"
fi

# -- 4. background service -----------------------------------------------------

say "Step 4 of 5: Setting up the background service"
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

# -- 5. GIMP plug-in -----------------------------------------------------------

say "Step 5 of 5: Adding AI Cutout to GIMP"
for dir in "${gimp_dirs[@]}"; do
    install -Dm755 "$HERE/plugin/ai-cutout/ai-cutout.py" "$dir/plug-ins/ai-cutout/ai-cutout.py"
    echo "Added to $dir"
done

say "Done! Close GIMP if it's open, then start it again."
echo "You'll find everything in the new AI menu at the top of the window."
echo "Start with AI > How to Use AI Cutout."
echo
