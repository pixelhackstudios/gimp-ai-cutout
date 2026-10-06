#!/usr/bin/env bash
# Remove AI Cutout for GIMP: the plug-in, its saved settings, the background
# service, the AI engine and the AI models. Close GIMP first.
set -uo pipefail

APP="${XDG_DATA_HOME:-$HOME/.local/share}/gimp-ai-cutout"
UNITS="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
HUB="${HF_HUB_CACHE:-${HF_HOME:-$HOME/.cache/huggingface}/hub}"

systemctl --user disable --now gimp-ai-cutout.socket 2>/dev/null
systemctl --user stop gimp-ai-cutout.service 2>/dev/null
rm -f "$UNITS/gimp-ai-cutout.socket" "$UNITS/gimp-ai-cutout.service"
systemctl --user daemon-reload 2>/dev/null

for dir in "$HOME"/.config/GIMP/3.* "$HOME"/.var/app/org.gimp.GIMP/config/GIMP/3.* \
           "$HOME"/snap/gimp/current/.config/GIMP/3.*; do
    [ -d "$dir" ] || continue
    rm -rf "$dir/plug-ins/ai-cutout"
    rm -f "$dir"/plug-in-settings/*python-fu-ai-cutout-*
done

rm -rf "$HUB/models--ZhengPeng7--BiRefNet" \
       "$HUB/models--hustvl--vitmatte-base-composition-1k" \
       "$HUB/models--facebook--sam2.1-hiera-large"
rm -rf "$APP"

printf '\n\033[1mAI Cutout has been removed.\033[0m Restart GIMP to clear the AI menu.\n\n'
