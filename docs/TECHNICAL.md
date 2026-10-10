# AI Cutout for GIMP: technical notes

## How it works

```
GIMP 3 (e.g. Flatpak sandbox)                   Your machine (own Python, CUDA)
ai-cutout.py plug-in  -- HTTP 127.0.0.1:41879 -->  ai-cutout-server
  reads pixels, shows dialogs                      BiRefNet -> rough mask
  writes masks/selections                          trimap -> ViTMatte (edge band, tiled)
                                                   blur-fusion foreground colours
                                                   SAM 2.1 for clicks
                                                   sd-cli (stable-diffusion.cpp)
                                                     -> Z-Image-Turbo for Generate Image
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
- **Image generation** (`src/cutout_server/generate.py`): the server runs
  [stable-diffusion.cpp](https://github.com/leejet/stable-diffusion.cpp)'s `sd-cli` as a
  separate process for each picture: Z-Image-Turbo Q4_K, Qwen3-4B text encoder,
  8 steps at CFG 1, `--offload-to-cpu --diffusion-fa --vae-tiling`. A fresh process
  costs a few seconds of model loading (the files stay in the page cache) but releases
  all GPU memory afterwards, so it coexists with the cutout models. The Vulkan build
  runs on NVIDIA, AMD and Intel GPUs; without a GPU the installer fetches the CPU build.
  Measured on an RTX 3080 (Vulkan): about 18–23 s per 1024×1024 picture, about 4.5 GB
  peak VRAM. On an 8-core i7-10700K without the GPU: about 4.5 minutes for 512×512.
- **Placement** (plug-in): "Fit" generates about one megapixel at the selection's or
  canvas's aspect ratio, scales it to cover that area and crops the overflow; fixed
  shapes are placed at native size, centred. With no image open, a new image is created.

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
3. downloads the pinned model checkpoints into the Hugging Face cache: about 1.7 GB for
   cutouts plus 6.7 GB for image generation;
4. downloads the pinned stable-diffusion.cpp release (Vulkan build when a GPU and the Vulkan
   loader are present, else the CPU build), checks its SHA-256 and unpacks it into
   `~/.local/share/gimp-ai-cutout/sd`;
5. installs and enables `gimp-ai-cutout.socket` / `.service` as systemd **user** units;
6. copies the plug-in into every GIMP 3.x settings folder it finds: `~/.config/GIMP/3.*`, `~/.var/app/org.gimp.GIMP/config/GIMP/3.*` and `~/snap/gimp/current/.config/GIMP/3.*`.

`bash install.sh --no-generate` (or `AI_CUTOUT_NO_GENERATE=1`) skips step 4 and the 6.7 GB of
generation models.

`get.sh` (the one-line installer in the README) downloads the `main` branch into
`~/.local/share/gimp-ai-cutout/src` and runs `install.sh` from there, passing its
arguments through (`... | bash -s -- --no-generate`).

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
| `python-fu-ai-cutout-generate` | `prompt`, `shape` (`fit`/`square`/`portrait`/`landscape`/`wide`), `seed` (-1 = random). Works with no image (creates one). |

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
| Z-Image-Turbo Q4_K | `leejet/Z-Image-Turbo-GGUF` | Apache-2.0 |
| Qwen3-4B-Instruct-2507 Q4_K_M (text encoder) | `unsloth/Qwen3-4B-Instruct-2507-GGUF` | Apache-2.0 |
| FLUX VAE (`ae.safetensors`) | `Comfy-Org/z_image_turbo` | Apache-2.0 |

The stable-diffusion.cpp release (`master-954-05845fd`) and its SHA-256 checksums are
pinned at the top of `install.sh`.

Tested with GIMP 3.2.6 (Flatpak), PyTorch 2.14 (CUDA 13), transformers 5.18,
stable-diffusion.cpp master-954-05845fd (Vulkan), RTX 3080.
