"""ai-cutout: cut out the subject of an image file without GIMP.

    ai-cutout photo.jpg cutout.png
    ai-cutout photo.jpg cutout.png --decontaminate --save-trimap trimap.png
    ai-cutout photo.jpg dog.png --point 420,310 --exclude 600,200
    ai-cutout --download-models [--with-generation]
"""

import argparse
import sys
import time

import cv2
import numpy as np


def load_image(path):
    data = cv2.imread(path, cv2.IMREAD_UNCHANGED)
    if data is None:
        sys.exit(f"ai-cutout: cannot read {path}")
    scale = 65535.0 if data.dtype == np.uint16 else 255.0
    if data.ndim == 2:
        rgb = np.repeat(data[..., None], 3, axis=2)
    else:
        rgb = cv2.cvtColor(data[..., :3], cv2.COLOR_BGR2RGB)
    return np.ascontiguousarray(rgb, dtype=np.float32) / scale, data.dtype


def save_rgba(path, rgb, alpha, dtype):
    scale = 65535.0 if dtype == np.uint16 else 255.0
    rgba = np.dstack([cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR), alpha])
    cv2.imwrite(path, (np.clip(rgba, 0, 1) * scale + 0.5).astype(dtype))


def _xy(text):
    x, y = text.split(",")
    return float(x), float(y)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="ai-cutout", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input", nargs="?")
    ap.add_argument("output", nargs="?", help="RGBA PNG (16-bit if the input is 16-bit)")
    ap.add_argument("--no-matting", action="store_true", help="skip edge refinement (rough mask only)")
    ap.add_argument("--band", type=int, default=0, help="unsure-edge half-width in px (0 = auto)")
    ap.add_argument("--decontaminate", action="store_true", help="remove background colour from edge pixels")
    ap.add_argument("--mask", help="refine this greyscale mask instead of detecting the subject")
    ap.add_argument("--point", action="append", type=_xy, default=[], metavar="X,Y",
                    help="SAM 2: a point on the object you want (repeatable)")
    ap.add_argument("--exclude", action="append", type=_xy, default=[], metavar="X,Y",
                    help="SAM 2: a point that is not part of the object (repeatable)")
    ap.add_argument("--box", metavar="X0,Y0,X1,Y1", help="SAM 2: box around the object")
    ap.add_argument("--save-trimap", metavar="PATH")
    ap.add_argument("--device", help="cuda, cuda:1, cpu (default: cuda if available)")
    ap.add_argument("--download-models", action="store_true", help="fetch the pinned checkpoints and exit")
    ap.add_argument("--with-generation", action="store_true",
                    help="with --download-models: also fetch the Z-Image-Turbo generation models (about 6.7 GB)")
    args = ap.parse_args(argv)

    if args.download_models:
        from .models import fetch_all
        fetch_all(generation=args.with_generation)
        return
    if not args.input or not args.output:
        ap.error("input and output are required")

    from .pipeline import Pipeline

    image, dtype = load_image(args.input)
    pipe = Pipeline(args.device)
    start = time.perf_counter()
    fg = None
    if args.point or args.exclude or args.box:
        points = args.point + args.exclude
        labels = [1] * len(args.point) + [0] * len(args.exclude)
        box = [float(v) for v in args.box.split(",")] if args.box else None
        pipe.sam_open("cli", image)
        alpha, timings = pipe.sam_apply("cli", points, labels, box, not args.no_matting, args.band)
        if args.decontaminate:
            from .pipeline import estimate_foreground
            fg = estimate_foreground(image, alpha)
    else:
        mask = None
        if args.mask:
            mask_img, _ = load_image(args.mask)
            mask = mask_img[..., 0]
            if mask.shape != image.shape[:2]:
                sys.exit("ai-cutout: --mask must be the same size as the input")
        alpha, fg, timings = pipe.run(image, mask, not args.no_matting, args.band, args.decontaminate)

    save_rgba(args.output, fg if fg is not None else image, alpha, dtype)
    if args.save_trimap and pipe.last_trimap is not None:
        cv2.imwrite(args.save_trimap, pipe.last_trimap)
    total = round((time.perf_counter() - start) * 1000)
    print(f"{args.output}: {image.shape[1]}x{image.shape[0]} in {total} ms {timings}")


if __name__ == "__main__":
    main()
