"""Text-to-image with Z-Image-Turbo through stable-diffusion.cpp.

sd-cli runs as a separate process for each picture. Reloading the models costs a
few seconds once the files are in the page cache, but every byte of GPU memory
is released afterwards, so generation coexists with the cutout models even on
small cards. Weights are kept in RAM and streamed to the GPU (--offload-to-cpu),
which keeps peak VRAM around 4.5 GB at 1024x1024.
"""

import logging
import os
import random
import subprocess
import tempfile
import time

import cv2

from .models import GENERATION

log = logging.getLogger("ai-cutout")

SD_DIR = os.environ.get("AI_CUTOUT_SD_DIR") or os.path.join(
    os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"), "gimp-ai-cutout", "sd")
STEPS = 8                  # Z-Image-Turbo is distilled for 8 steps at CFG 1
MIN_SIDE, MAX_SIDE = 256, 2048
MAX_PIXELS = 1536 * 1536   # beyond this a picture takes minutes even on a fast GPU
MAX_PROMPT = 4000


class GenerationError(Exception):
    pass


def _model_files():
    """Paths of the cached generation models, or None if any is missing. Never downloads."""
    from huggingface_hub import try_to_load_from_cache

    paths = []
    for repo, revision, (filename,) in GENERATION:
        path = try_to_load_from_cache(repo, filename, revision=revision)
        if not isinstance(path, str):
            return None
        paths.append(path)
    return paths


def status():
    """"gpu", "cpu" (works, but takes minutes per picture), or None when not installed."""
    if not os.path.exists(os.path.join(SD_DIR, "sd-cli")) or _model_files() is None:
        return None
    return "gpu" if os.path.exists(os.path.join(SD_DIR, "libggml-vulkan.so")) else "cpu"


def _side(value, name):
    value = int(value)
    if not MIN_SIDE <= value <= MAX_SIDE:
        raise GenerationError(f"{name} must be between {MIN_SIDE} and {MAX_SIDE} pixels")
    return value // 64 * 64


def generate(prompt, width, height, seed=-1, timeout=3600):
    """Returns (RGB uint8 array, info). seed < 0 picks a random one, reported in info."""
    prompt = (prompt or "").strip()
    if not prompt:
        raise GenerationError("Describe the picture you want first.")
    if len(prompt) > MAX_PROMPT:
        raise GenerationError(f"The description is too long (over {MAX_PROMPT} characters).")
    width, height = _side(width, "Width"), _side(height, "Height")
    if width * height > MAX_PIXELS:
        raise GenerationError("That size is too large; keep it to about 2 megapixels.")
    files = _model_files()
    exe = os.path.join(SD_DIR, "sd-cli")
    if files is None or not os.path.exists(exe):
        raise GenerationError("Image generation isn't installed. Run the AI Cutout installer again.")
    seed = int(seed) if int(seed) >= 0 else random.randrange(2**31 - 1)

    with tempfile.TemporaryDirectory(prefix="ai-cutout-gen-") as tmp:
        out = os.path.join(tmp, "image.png")
        diffusion, text_encoder, vae = files
        cmd = [exe, "--diffusion-model", diffusion, "--llm", text_encoder, "--vae", vae,
               "-p", prompt, "--cfg-scale", "1.0", "--steps", str(STEPS),
               "-W", str(width), "-H", str(height), "--seed", str(seed),
               "--diffusion-fa", "--offload-to-cpu", "--vae-tiling", "-o", out]
        env = dict(os.environ, LD_LIBRARY_PATH=SD_DIR)
        start = time.perf_counter()
        try:
            proc = subprocess.run(cmd, cwd=tmp, env=env, capture_output=True, text=True,
                                  errors="replace", timeout=timeout)
        except subprocess.TimeoutExpired:
            raise GenerationError(f"Generating took longer than {timeout // 60} minutes and was stopped.")
        image = cv2.imread(out, cv2.IMREAD_COLOR) if proc.returncode == 0 else None
        if image is None:
            output = (proc.stdout + proc.stderr).strip().splitlines()
            log.error("sd-cli failed (exit %s):\n%s", proc.returncode, "\n".join(output[-30:]))
            errors = [line for line in output if "error" in line.lower() or "failed" in line.lower()]
            raise GenerationError("Image generation failed: " + (errors[-1] if errors else
                                                                  f"sd-cli exited with {proc.returncode}"))
    ms = round((time.perf_counter() - start) * 1000)
    log.info("generated %dx%d seed %d in %d ms", width, height, seed, ms)
    return cv2.cvtColor(image, cv2.COLOR_BGR2RGB), {"seed": seed, "ms": ms, "width": width, "height": height}
