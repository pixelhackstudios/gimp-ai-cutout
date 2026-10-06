"""Pinned model checkpoints and lazy loading.

Revisions are pinned so a change upstream (including BiRefNet's remote code) can
never alter what runs on this machine without an explicit update here.
"""

import threading

import torch

SEGMENTATION = ("ZhengPeng7/BiRefNet", "e2bf8e4460fc8fa32bba5ea4d94b3233d367b0e4",
                ["*.py", "config.json", "model.safetensors"])
MATTING = ("hustvl/vitmatte-base-composition-1k", "bf486d01a7d9e3dbcc8400f7942835caf0eaf76e",
           ["config.json", "preprocessor_config.json", "pytorch_model.bin"])
SAM = ("facebook/sam2.1-hiera-large", "665f8e2ad61cf5f53d65644ff27c8ee525124610",
       ["config.json", "model.safetensors", "preprocessor_config.json", "processor_config.json"])

ALL = (SEGMENTATION, MATTING, SAM)


def fetch(spec):
    """Return the local snapshot path for a pinned checkpoint, downloading it if needed."""
    from huggingface_hub import snapshot_download

    repo, revision, patterns = spec
    return snapshot_download(repo, revision=revision, allow_patterns=patterns)


def fetch_all():
    for spec in ALL:
        print(f"{spec[0]} @ {spec[1][:10]}: {fetch(spec)}", flush=True)


def pick_device(requested=None):
    if requested:
        return torch.device(requested)
    return torch.device("cuda" if torch.cuda.is_available() else "cpu")


class Models:
    """Loads each network on first use and keeps it resident."""

    def __init__(self, device=None):
        self.device = pick_device(device)
        self.half = self.device.type == "cuda"
        self._lock = threading.Lock()
        self._seg = self._matte = self._sam = self._sam_processor = None

    @property
    def segmentation(self):
        with self._lock:
            if self._seg is None:
                from transformers import AutoModelForImageSegmentation

                model = AutoModelForImageSegmentation.from_pretrained(
                    fetch(SEGMENTATION), trust_remote_code=True)
                model.to(self.device).eval()
                if self.half:
                    model.half()
                self._seg = model
            return self._seg

    @property
    def matting(self):
        with self._lock:
            if self._matte is None:
                from transformers import VitMatteForImageMatting

                self._matte = VitMatteForImageMatting.from_pretrained(fetch(MATTING)).to(self.device).eval()
            return self._matte

    @property
    def sam(self):
        with self._lock:
            if self._sam is None:
                from transformers import Sam2Model, Sam2Processor

                path = fetch(SAM)
                self._sam = Sam2Model.from_pretrained(path).to(self.device).eval()
                self._sam_processor = Sam2Processor.from_pretrained(path)
            return self._sam, self._sam_processor

    def loaded(self):
        return {"segmentation": self._seg is not None,
                "matting": self._matte is not None,
                "sam": self._sam is not None}
