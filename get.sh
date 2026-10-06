#!/usr/bin/env bash
# One-line installer for AI Cutout for GIMP:
#
#   wget -qO- https://raw.githubusercontent.com/pixelhackstudios/gimp-ai-cutout/main/get.sh | bash
#
# Downloads the latest version into ~/.local/share/gimp-ai-cutout/src and runs
# install.sh from there. Everything is wrapped in main() so a partly downloaded
# script never runs.
set -euo pipefail

main() {
    local repo="pixelhackstudios/gimp-ai-cutout"
    local src="${XDG_DATA_HOME:-$HOME/.local/share}/gimp-ai-cutout/src"
    local url="https://github.com/$repo/archive/refs/heads/main.tar.gz"

    printf '\n\033[1mDownloading AI Cutout for GIMP\033[0m\n'
    mkdir -p "$src.new"
    if command -v curl >/dev/null; then
        curl -fsSL "$url" | tar -xz --strip-components=1 -C "$src.new"
    elif command -v wget >/dev/null; then
        wget -qO- "$url" | tar -xz --strip-components=1 -C "$src.new"
    else
        echo "Please install curl or wget first." >&2
        exit 1
    fi
    rm -rf "$src"
    mv "$src.new" "$src"
    exec bash "$src/install.sh" </dev/null
}

main "$@"
