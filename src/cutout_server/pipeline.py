"""Background removal pipeline.

    rough mask (BiRefNet, or SAM 2 from clicks, or a mask the user drew)
      -> trimap: sure foreground / sure background / unsure band along the edge
      -> ViTMatte on the unsure band only, in full-resolution tiles
      -> foreground colour estimation so edges don't carry the old background

Images are float32 HxWx3 in [0, 1] (perceptual sRGB, as GIMP's R'G'B' formats
deliver them); masks and alphas are float32 HxW in [0, 1].
"""

import time
from collections import OrderedDict

import cv2
import numpy as np
import torch
import torch.nn.functional as F

from .models import Models

SEG_SIZE = 1024
TILE = 1024
TILE_OVERLAP = 128
IMAGENET_MEAN = (0.485, 0.456, 0.406)
IMAGENET_STD = (0.229, 0.224, 0.225)


def auto_band(height, width):
    """Half-width of the unsure band, scaled to image size (10 px at 1 MP, 40 px at 4000 px)."""
    return max(8, round(max(height, width) / 100))


def make_trimap(alpha, band):
    """0 = background, 128 = unsure, 255 = foreground."""
    hard = (alpha > 0.5).astype(np.uint8)
    k3 = np.ones((3, 3), np.uint8)
    edge = cv2.dilate(hard, k3) != cv2.erode(hard, k3)
    soft = (alpha > 0.03) & (alpha < 0.97)
    seed = edge | soft
    # Distance to the nearest edge/soft pixel; cheaper than dilating with a huge kernel.
    dist = cv2.distanceTransform((~seed).astype(np.uint8), cv2.DIST_L2, 3)
    trimap = np.where(hard > 0, 255, 0).astype(np.uint8)
    trimap[dist <= band] = 128
    return trimap


def _tile_starts(length, tile, overlap):
    if length <= tile:
        return [0]
    starts = list(range(0, length - tile, tile - overlap))
    starts.append(length - tile)
    return starts


def _ramp(n, overlap):
    i = np.arange(n, dtype=np.float32)
    return np.clip(np.minimum(i + 1, n - i) / overlap, 1e-3, 1.0)


def _blur_fusion(image, fg, bg, alpha, radius):
    a = alpha[..., None]
    blurred_a = cv2.blur(alpha, (radius, radius))[..., None]
    blurred_fg = cv2.blur(fg * a, (radius, radius)) / (blurred_a + 1e-5)
    blurred_bg = cv2.blur(bg * (1 - a), (radius, radius)) / ((1 - blurred_a) + 1e-5)
    fg = blurred_fg + a * (image - a * blurred_fg - (1 - a) * blurred_bg)
    return np.clip(fg, 0, 1), blurred_bg


def estimate_foreground(image, alpha):
    """Approximate fast foreground colour estimation (Forte & Pitie, ICIP 2021): two blur-fusion passes."""
    fg, bg = _blur_fusion(image, image, image, alpha, 90)
    fg, _ = _blur_fusion(image, fg, bg, alpha, 6)
    return fg.astype(np.float32)


class Pipeline:
    def __init__(self, device=None):
        self.models = Models(device)
        self.device = self.models.device
        self._sessions = OrderedDict()
        self.last_trimap = None

    # -- stages -----------------------------------------------------------

    @torch.inference_mode()
    def segment(self, image):
        h, w = image.shape[:2]
        interp = cv2.INTER_AREA if max(h, w) > SEG_SIZE else cv2.INTER_LINEAR
        x = cv2.resize(image, (SEG_SIZE, SEG_SIZE), interpolation=interp)
        x = torch.from_numpy(x).to(self.device).permute(2, 0, 1)[None]
        mean = torch.tensor(IMAGENET_MEAN, device=self.device).view(1, 3, 1, 1)
        std = torch.tensor(IMAGENET_STD, device=self.device).view(1, 3, 1, 1)
        x = (x - mean) / std
        model = self.models.segmentation
        pred = model(x.to(next(model.parameters()).dtype))[-1].float().sigmoid()
        pred = F.interpolate(pred, size=(h, w), mode="bilinear", align_corners=False)
        return pred[0, 0].cpu().numpy()

    @torch.inference_mode()
    def _matte_tile(self, image, trimap):
        h, w = trimap.shape
        x = torch.from_numpy(np.ascontiguousarray(image)).to(self.device).permute(2, 0, 1)[None]
        t = torch.from_numpy(np.ascontiguousarray(trimap)).to(self.device)[None, None].float() / 255
        # Same preprocessing as VitMatteImageProcessor: normalise with mean=std=0.5,
        # append the trimap as a 4th channel, zero-pad to a multiple of 32.
        x = torch.cat([(x - 0.5) / 0.5, t], dim=1)
        x = F.pad(x, (0, (32 - w % 32) % 32, 0, (32 - h % 32) % 32))
        with torch.autocast(self.device.type, dtype=torch.float16, enabled=self.models.half):
            alpha = self.models.matting(pixel_values=x).alphas
        return alpha[0, 0, :h, :w].float().clamp(0, 1).cpu().numpy()

    def matte(self, image, trimap):
        unknown = trimap == 128
        alpha = (trimap == 255).astype(np.float32)
        if not unknown.any():
            return alpha
        h, w = trimap.shape
        acc = np.zeros((h, w), np.float32)
        weight = np.zeros((h, w), np.float32)
        for y in _tile_starts(h, TILE, TILE_OVERLAP):
            for x in _tile_starts(w, TILE, TILE_OVERLAP):
                ys, xs = slice(y, min(y + TILE, h)), slice(x, min(x + TILE, w))
                if not unknown[ys, xs].any():
                    continue
                tile_alpha = self._matte_tile(image[ys, xs], trimap[ys, xs])
                th, tw = tile_alpha.shape
                wt = np.outer(_ramp(th, TILE_OVERLAP), _ramp(tw, TILE_OVERLAP))
                acc[ys, xs] += tile_alpha * wt
                weight[ys, xs] += wt
        done = unknown & (weight > 0)
        alpha[done] = acc[done] / weight[done]
        return alpha

    # -- full jobs --------------------------------------------------------

    def run(self, image, mask=None, matting=True, band=0, foreground=False):
        """Cut out the subject of `image`. With `mask`, refine that mask instead of finding the subject."""
        timings = {}
        t = time.perf_counter()
        if mask is None:
            mask = self.segment(image)
            timings["segment_ms"] = _ms(t)
        alpha, timings = self.finish(image, mask, matting, band, timings)
        fg = None
        if foreground:
            t = time.perf_counter()
            fg = estimate_foreground(image, alpha)
            timings["foreground_ms"] = _ms(t)
        if self.device.type == "cuda":
            torch.cuda.empty_cache()
        return alpha, fg, timings

    def finish(self, image, mask, matting, band, timings):
        if not matting:
            return mask.astype(np.float32), timings
        h, w = mask.shape
        t = time.perf_counter()
        trimap = make_trimap(mask, band or auto_band(h, w))
        self.last_trimap = trimap
        alpha = self.matte(image, trimap)
        timings["matte_ms"] = _ms(t)
        # Snap near-binary values so flat regions are exactly opaque/transparent.
        alpha[alpha < 1 / 512] = 0
        alpha[alpha > 1 - 1 / 512] = 1
        return alpha, timings

    # -- SAM 2 click-to-select ---------------------------------------------

    @torch.inference_mode()
    def sam_open(self, session_id, image):
        model, processor = self.models.sam
        rgb8 = (image * 255 + 0.5).astype(np.uint8)
        inputs = processor(images=rgb8, return_tensors="pt").to(self.device)
        embeddings = model.get_image_embeddings(inputs["pixel_values"])
        self._sessions[session_id] = (image, embeddings)
        while len(self._sessions) > 2:
            self._sessions.popitem(last=False)

    def sam_has(self, session_id):
        return session_id in self._sessions

    def release_gpu_cache(self):
        """Hand cached but unused GPU memory back, so image generation (a separate process) can use it."""
        if self.device.type == "cuda":
            torch.cuda.empty_cache()

    def sam_close(self, session_id):
        self._sessions.pop(session_id, None)
        if self.device.type == "cuda":
            torch.cuda.empty_cache()

    @torch.inference_mode()
    def sam_logits(self, session_id, points, labels, box, size):
        """Mask logits (>0 = object) resized to `size` = (h, w), plus the model's quality score."""
        image, embeddings = self._sessions[session_id]
        self._sessions.move_to_end(session_id)
        model, processor = self.models.sam
        h, w = image.shape[:2]
        kwargs = {}
        if points:
            kwargs["input_points"] = [[[[float(x), float(y)] for x, y in points]]]
            kwargs["input_labels"] = [[[int(v) for v in labels]]]
        if box:
            kwargs["input_boxes"] = [[[float(v) for v in box]]]
        if not kwargs:
            return np.full(size, -1.0, np.float32), 0.0
        prompts = processor(original_sizes=[[h, w]], return_tensors="pt", **kwargs).to(self.device)
        # One click is ambiguous (shirt? person?), so ask for 3 candidates and keep the best.
        multimask = len(points) + (1 if box else 0) == 1
        out = model(image_embeddings=embeddings, multimask_output=multimask,
                    **{k: prompts[k] for k in ("input_points", "input_labels", "input_boxes") if k in prompts})
        scores = out.iou_scores[0, 0]
        best = int(scores.argmax())
        logits = out.pred_masks[0, 0, best][None, None].float()
        logits = F.interpolate(logits, size=size, mode="bilinear", align_corners=False)
        return logits[0, 0].cpu().numpy(), float(scores[best])

    def sam_overlay(self, session_id, points, labels, box, preview_w, preview_h):
        """RGBA u8 tint of the predicted object at preview size, for the GIMP dialog."""
        logits, score = self.sam_logits(session_id, points, labels, box, (preview_h, preview_w))
        inside = (logits > 0).astype(np.uint8)
        overlay = np.zeros((preview_h, preview_w, 4), np.uint8)
        overlay[inside > 0] = (40, 140, 255, 110)
        outline = cv2.morphologyEx(inside, cv2.MORPH_GRADIENT, np.ones((3, 3), np.uint8)) > 0
        overlay[outline] = (255, 255, 255, 230)
        return overlay, score

    def sam_apply(self, session_id, points, labels, box, matting=True, band=0):
        image, _ = self._sessions[session_id]
        h, w = image.shape[:2]
        t = time.perf_counter()
        logits, score = self.sam_logits(session_id, points, labels, box, (h, w))
        # SAM's logits are only moderately confident even deep inside an object, so
        # use the hard mask (otherwise the whole object lands in the unsure band).
        # It decodes at 256x256, so its border is approximate: widen the band instead.
        mask = (logits > 0).astype(np.float32)
        timings = {"sam_ms": _ms(t), "sam_score": round(score, 3)}
        if not matting:
            mask = cv2.GaussianBlur(mask, (0, 0), 1.0)  # anti-alias the upscaled edge
        alpha, timings = self.finish(image, mask, matting,
                                     band or round(auto_band(h, w) * 1.5), timings)
        if self.device.type == "cuda":
            torch.cuda.empty_cache()
        return alpha, timings


def _ms(start):
    return round((time.perf_counter() - start) * 1000)
