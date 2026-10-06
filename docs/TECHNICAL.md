# AI Cutout for GIMP: technical notes

## How it works

```
GIMP 3 (e.g. Flatpak sandbox)                   Your machine (own Python, CUDA)
ai-cutout.py plug-in  -- HTTP 127.0.0.1:41879 -->  ai-cutout-server
  reads pixels, shows dialogs                      BiRefNet -> rough mask
  writes masks/selections                          trimap -> ViTMatte (edge band, tiled)
                                                   blur-fusion foreground colours
                                                   SAM 2.1 for clicks
```

- **Plug-in** (`plugin/ai-cutout/ai-cutout.py`): GIMP 3 Python plug-in using only the
  standard library, so GIMP's bundled Python is enough (the Flatpak has no numpy).
  It reads layer pixels through GEGL, sends them to the server, and writes the
  result as a layer mask or selection inside one undo group.
- **Server** (`src/cutout_server/`): runs in its own venv because the Flatpak sandbox
  can't install PyTorch. A systemd user socket on `127.0.0.1:41879` starts it on the
  first connection; it exits after 10 idle minutes to free GPU memory. It only
  listens on localhost and runs with `HF_HUB_OFFLINE=1`.
- **Pipeline** (`src/cutout_server/pipeline.py`):
  1. BiRefNet at 1024×1024 gives a rough mask (or SAM 2.1 from clicks, or the user's own mask).
  2. A trimap marks sure-foreground, sure-background and an uncertain band along the edge.
  3. ViTMatte computes alpha in the band only, in 1024 px tiles at full resolution.
  4. Optional foreground colour estimation (blur fusion, Forte & Pitié 2021) removes the old background's tint from edge pixels.

The HTTP protocol is documented at the top of `src/cutout_server/server.py`.

## Install from a clone

```bash
git clone https://github.com/pixelhackstudios/gimp-ai-cutout
cd gimp-ai-cutout
bash install.sh
```

`install.sh` is idempotent. It:

1. installs [uv](https://docs.astral.sh/uv/) into `~/.local/share/gimp-ai-cutout/bin` if it isn't on `PATH`;
2. creates a Python 3.12 venv in `~/.local/share/gimp-ai-cutout/venv` (about 6 GB, mostly PyTorch with CUDA) and installs the server package;
3. downloads the pinned model checkpoints (about 1.7 GB) into the Hugging Face cache;
4. installs and enables `gimp-ai-cutout.socket` / `.service` as systemd **user** units;
5. copies the plug-in into every GIMP 3.x settings folder it finds: `~/.config/GIMP/3.*`, `~/.var/app/org.gimp.GIMP/config/GIMP/3.*` and `~/snap/gimp/current/.config/GIMP/3.*`.

`get.sh` (the one-line installer in the README) downloads the `main` branch into
`~/.local/share/gimp-ai-cutout/src` and runs `install.sh` from there.

## Command line, without GIMP

```bash
~/.local/share/gimp-ai-cutout/venv/bin/ai-cutout photo.jpg cutout.png --decontaminate
~/.local/share/gimp-ai-cutout/venv/bin/ai-cutout photo.jpg dog.png --point 420,310 --exclude 600,200
~/.local/share/gimp-ai-cutout/venv/bin/ai-cutout photo.jpg cutout.png --mask rough-mask.png --save-trimap trimap.png
```

## Calling the plug-in from scripts

All commands are PDB procedures and run non-interactively:

| Procedure | Arguments besides image/drawables |
|---|---|
| `python-fu-ai-cutout-remove-background` | `refine-edges`, `edge-width`, `decontaminate`, `limit-to-selection` |
| `python-fu-ai-cutout-select-subject` | `refine-edges`, `edge-width` |
| `python-fu-ai-cutout-select-object` | `output` (`selection`/`mask`), `refine-edges`, `edge-width`, `points`, `exclude` (`"x,y x,y"` in layer coordinates) |
| `python-fu-ai-cutout-refine-selection` | `edge-width` |
| `python-fu-ai-cutout-refine-mask` | `edge-width` |

## Tests

```bash
tests/run_gimp_e2e.sh some-photo.png /tmp/cutout-test [X,Y]
```

This starts a separate headless GIMP (it doesn't touch an open GIMP window), runs
every procedure through the PDB, checks the masks and selections it produces, and
writes PNGs plus `report.txt` to the output folder.

## Troubleshooting

- Service status: `systemctl --user status gimp-ai-cutout.socket gimp-ai-cutout.service`
- Logs: `journalctl --user -u gimp-ai-cutout -f`
- Health: `curl http://127.0.0.1:41879/health`

## Models and licences

Checkpoints are pinned to exact revisions in `src/cutout_server/models.py`, so
upstream changes (including BiRefNet's remote code) never run without an explicit
update.

| Model | Repository | Licence |
|---|---|---|
| BiRefNet | `ZhengPeng7/BiRefNet` | MIT |
| ViTMatte | `hustvl/vitmatte-base-composition-1k` | Apache-2.0 weights, trained on Adobe's Composition-1k dataset, which Adobe licenses for non-commercial research |
| SAM 2.1 | `facebook/sam2.1-hiera-large` | Apache-2.0 |

Tested with GIMP 3.2.6 (Flatpak), PyTorch 2.14 (CUDA 13), transformers 5.18, RTX 3080.
