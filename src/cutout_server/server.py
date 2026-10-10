"""HTTP server used by the GIMP plug-in.

Binary protocol, kept dependency-free on the GIMP side (the plug-in only has the
Python standard library). Parameters travel as JSON in the `X-Cutout` header;
pixels are raw, row-major, little-endian:

  GET  /health                -> {"status", "device", "loaded", "version", "generation"}
  POST /v1/matte              X-Cutout {width, height, depth: "u8"|"u16", matting,
                              band, foreground, refine}; body = RGBA pixels, then a
                              u16 mask when refine is true.
                              -> u16 alpha, then RGBA (same depth) when foreground.
  POST /v1/sam/session        X-Cutout {width, height, depth}; body = RGBA pixels
                              -> {"session"}
  POST /v1/sam/predict        X-Cutout {session, points, labels, box, preview: [w, h]}
                              -> u8 RGBA overlay at preview size
  POST /v1/sam/apply          X-Cutout {session, points, labels, box, matting, band}
                              -> u16 alpha at full size
  POST /v1/sam/close          X-Cutout {session}
  POST /v1/generate           X-Cutout {prompt, width, height, seed (-1 = random)}
                              -> u8 RGB; X-Cutout-Info {seed, ms, width, height}

Under systemd socket activation the listening socket arrives as fd 3; the
process exits after --idle-timeout seconds without requests (freeing GPU memory)
and systemd starts it again on the next connection.
"""

import argparse
import json
import logging
import os
import socket
import sys
import time
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer

import numpy as np

from . import DEFAULT_PORT, __version__
from . import generate
from .pipeline import Pipeline

log = logging.getLogger("ai-cutout")
DEPTHS = {"u8": (np.dtype("u1"), 255.0), "u16": (np.dtype("<u2"), 65535.0)}


class ClientError(Exception):
    pass


def decode_rgba(raw, width, height, depth):
    """Raw RGBA -> (float RGB for the models, float RGBA as received)."""
    dtype, scale = DEPTHS[depth]
    expected = width * height * 4 * dtype.itemsize
    if len(raw) < expected:
        raise ClientError(f"expected {expected} bytes of RGBA, got {len(raw)}")
    rgba = np.frombuffer(raw, dtype, width * height * 4).reshape(height, width, 4).astype(np.float32) / scale
    rgb, alpha = rgba[..., :3], rgba[..., 3:]
    # Pixels under existing transparency hold arbitrary colours; show the models neutral grey there.
    if alpha.min() < 1:
        rgb = rgb * alpha + 0.5 * (1 - alpha)
    return np.ascontiguousarray(rgb), rgba, expected


def encode(array, dtype, scale):
    return (np.clip(array, 0, 1) * scale + 0.5).astype(dtype).tobytes()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = 60  # a stalled client must not wedge this single-threaded server
    server_version = f"ai-cutout/{__version__}"

    def log_message(self, fmt, *args):
        log.info("%s %s", self.address_string(), fmt % args)

    def _reply(self, status, body, content_type="application/octet-stream", info=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if info is not None:
            self.send_header("X-Cutout-Info", json.dumps(info))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status, obj):
        self._reply(status, json.dumps(obj).encode(), "application/json")

    def do_GET(self):
        self.server.touch()
        if self.path == "/health":
            pipe = self.server.pipeline
            self._json(200, {"status": "ok", "version": __version__, "device": str(pipe.device),
                             "loaded": pipe.models.loaded(), "generation": generate.status()})
        else:
            self._json(404, {"error": f"unknown path {self.path}"})

    def do_POST(self):
        self.server.touch()
        routes = {"/v1/matte": self.matte, "/v1/sam/session": self.sam_session,
                  "/v1/sam/predict": self.sam_predict, "/v1/sam/apply": self.sam_apply,
                  "/v1/sam/close": self.sam_close, "/v1/generate": self.generate_image}
        route = routes.get(self.path)
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length) if length else b""
            if route is None:
                raise ClientError(f"unknown path {self.path}")
            params = json.loads(self.headers.get("X-Cutout", "{}"))
            route(params, body)
        except ClientError as e:
            self._json(400, {"error": str(e)})
        except Exception as e:
            log.error("%s failed:\n%s", self.path, traceback.format_exc())
            self._json(500, {"error": f"{type(e).__name__}: {e}"})
        finally:
            self.server.touch()

    # -- endpoints ----------------------------------------------------------

    def matte(self, params, body):
        width, height = int(params["width"]), int(params["height"])
        depth = params.get("depth", "u8")
        if depth not in DEPTHS:
            raise ClientError(f"depth must be one of {list(DEPTHS)}")
        rgb, rgba, used = decode_rgba(body, width, height, depth)
        mask = None
        if params.get("refine"):
            need = width * height * 2
            if len(body) < used + need:
                raise ClientError("refine requires a u16 mask after the RGBA pixels")
            mask = np.frombuffer(body, "<u2", width * height, used).reshape(height, width).astype(np.float32) / 65535
        alpha, fg, timings = self.server.pipeline.run(
            rgb, mask, bool(params.get("matting", True)), int(params.get("band", 0)),
            bool(params.get("foreground", False)))
        out = encode(alpha, "<u2", 65535)
        if fg is not None:
            dtype, scale = DEPTHS[depth]
            out += encode(np.dstack([fg, rgba[..., 3]]), dtype, scale)
        log.info("matte %dx%d %s", width, height, timings)
        self._reply(200, out, info=timings)

    def sam_session(self, params, body):
        width, height = int(params["width"]), int(params["height"])
        rgb, _, _ = decode_rgba(body, width, height, params.get("depth", "u8"))
        session = uuid.uuid4().hex
        t = time.perf_counter()
        self.server.pipeline.sam_open(session, rgb)
        log.info("sam session %s %dx%d in %.0f ms", session[:8], width, height, (time.perf_counter() - t) * 1000)
        self._json(200, {"session": session})

    def _sam_args(self, params):
        session = params.get("session", "")
        if not self.server.pipeline.sam_has(session):
            raise ClientError("unknown or expired SAM session")
        points = params.get("points") or []
        labels = params.get("labels") or []
        if len(points) != len(labels):
            raise ClientError("points and labels must have the same length")
        return session, points, labels, params.get("box")

    def sam_predict(self, params, body):
        session, points, labels, box = self._sam_args(params)
        pw, ph = (int(v) for v in params["preview"])
        overlay, score = self.server.pipeline.sam_overlay(session, points, labels, box, pw, ph)
        self._reply(200, overlay.tobytes(), info={"score": round(score, 3)})

    def sam_apply(self, params, body):
        session, points, labels, box = self._sam_args(params)
        alpha, timings = self.server.pipeline.sam_apply(
            session, points, labels, box, bool(params.get("matting", True)), int(params.get("band", 0)))
        log.info("sam apply %s %s", session[:8], timings)
        self._reply(200, encode(alpha, "<u2", 65535), info=timings)

    def sam_close(self, params, body):
        self.server.pipeline.sam_close(params.get("session", ""))
        self._json(200, {"status": "closed"})

    def generate_image(self, params, body):
        self.server.pipeline.release_gpu_cache()
        try:
            rgb, info = generate.generate(params.get("prompt", ""), params.get("width", 1024),
                                          params.get("height", 1024), params.get("seed", -1))
        except generate.GenerationError as e:
            raise ClientError(str(e))
        self._reply(200, rgb.tobytes(), info=info)


class CutoutServer(HTTPServer):
    def __init__(self, address, pipeline, inherited_socket=None):
        self.pipeline = pipeline
        self.last_activity = time.monotonic()
        if inherited_socket is None:
            super().__init__(address, Handler)
        else:
            super().__init__(address, Handler, bind_and_activate=False)
            self.socket = inherited_socket
            self.server_address = inherited_socket.getsockname()
            self.server_name, self.server_port = self.server_address[:2]

    def touch(self):
        self.last_activity = time.monotonic()


def systemd_socket():
    if os.environ.get("LISTEN_PID") == str(os.getpid()) and int(os.environ.get("LISTEN_FDS", "0")) >= 1:
        sock = socket.socket(fileno=3)
        sock.setblocking(True)
        return sock
    return None


def main(argv=None):
    ap = argparse.ArgumentParser(prog="ai-cutout-server")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--device", help="cuda, cuda:1, cpu (default: cuda if available)")
    ap.add_argument("--idle-timeout", type=float, default=0,
                    help="exit after this many seconds without requests (0 = never)")
    ap.add_argument("--preload", action="store_true", help="load the segmentation and matting models at start")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stderr)

    pipeline = Pipeline(args.device)
    inherited = systemd_socket()
    server = CutoutServer((args.host, args.port), pipeline, inherited)
    host, port = server.server_address[:2]
    log.info("ai-cutout %s listening on %s:%s (%s, device %s)", __version__, host, port,
             "socket-activated" if inherited else "standalone", pipeline.device)
    if args.preload:
        pipeline.models.segmentation, pipeline.models.matting

    server.timeout = 5
    try:
        while not args.idle_timeout or time.monotonic() - server.last_activity < args.idle_timeout:
            server.handle_request()
        log.info("idle for %.0f s, exiting", args.idle_timeout)
    except KeyboardInterrupt:
        pass
    finally:
        if inherited is None:
            server.server_close()


if __name__ == "__main__":
    main()
