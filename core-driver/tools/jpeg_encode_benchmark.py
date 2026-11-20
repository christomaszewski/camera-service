"""Compare the frame-capture plugin's JPEG encoders on this host: CPU `jpegenc` vs the Jetson's
hardware `nvjpegenc` (docs/CAPTURE.md "Image"), per frame size, through the plugin's own encoder.

Each encoder runs in its OWN process: NVIDIA's libjpeg replacement and the stock libjpeg have been
reported to clash when both are loaded, which is also why the plugin picks one per process.

Reports wall time per frame (what a capture waits for) and CPU time per frame (what it costs the
cores; a hardware encode should show here). Frames are noise, the worst case for size and speed.

Run in the core image on the Jetson (the NVJPG engine must be exposed: CDI on JP7, the nvidia
runtime on JP6):
    python3 tools/jpeg_encode_benchmark.py
    python3 tools/jpeg_encode_benchmark.py --sizes 1920x1080,4096x3000 --format bayer --count 30
"""
import argparse
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

FORMATS = {"gray": ("GRAY8", 1, None), "bayer": ("GRAY8", 1, "rggb"), "rgb": ("RGB", 3, None),
           "gray16": ("GRAY16_LE", 2, None)}


def run_one(backend: str, sizes, fmt: str, count: int, quality: int) -> dict:
    import gi
    gi.require_version("Gst", "1.0")
    from gi.repository import Gst
    Gst.init(None)
    import numpy as np
    from cam_driver.capture.encode import JpegEncoder
    from cam_driver.capture.frames import Frame

    gst_fmt, bpp, bayer = FORMATS[fmt]
    enc = JpegEncoder(quality=quality, encoder=backend)
    out = {"backend": enc.backend, "hardware": enc.hardware, "fallback": enc.fallback, "rows": []}
    if backend == "hw" and not enc.hardware:
        enc.close()
        return out
    rng = np.random.default_rng(0)
    for w, h in sizes:
        data = rng.integers(0, 256, w * h * bpp, dtype=np.uint8).tobytes()
        frame = Frame(0, 0, 0, "bench", 0, None, gst_fmt, w, h, bayer, data)
        enc.encode(frame)                                   # builds the pipeline; not timed
        wall0, cpu0 = time.perf_counter(), time.process_time()
        size = 0
        for _ in range(count):
            size = len(enc.encode(frame).jpeg)
        wall, cpu = time.perf_counter() - wall0, time.process_time() - cpu0
        out["rows"].append({"size": f"{w}x{h}", "wall_ms": round(wall / count * 1e3, 2),
                            "cpu_ms": round(cpu / count * 1e3, 2), "bytes": size})
    enc.close()
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--sizes", default="640x480,1920x1080,2448x2048,4096x3000")
    ap.add_argument("--format", choices=sorted(FORMATS), default="gray")
    ap.add_argument("--count", type=int, default=20)
    ap.add_argument("--quality", type=int, default=90)
    ap.add_argument("--one", choices=("cpu", "hw"), help=argparse.SUPPRESS)   # internal: one backend
    args = ap.parse_args()
    sizes = [tuple(int(v) for v in s.lower().split("x")) for s in args.sizes.split(",") if s]
    if args.one:
        print(json.dumps(run_one(args.one, sizes, args.format, args.count, args.quality)))
        return 0
    results = []
    for backend in ("cpu", "hw"):
        proc = subprocess.run([sys.executable, __file__, "--one", backend, "--sizes", args.sizes,
                               "--format", args.format, "--count", str(args.count),
                               "--quality", str(args.quality)], capture_output=True, text=True)
        if proc.returncode != 0:
            print(f"{backend}: failed\n{proc.stderr.strip()}", file=sys.stderr)
            continue
        results.append(json.loads(proc.stdout.strip().splitlines()[-1]))
    print(f"format={args.format} quality={args.quality} count={args.count}")
    print(f"{'size':>10}  {'encoder':>10}  {'wall ms':>8}  {'cpu ms':>8}  {'bytes':>9}")
    for r in results:
        if not r["rows"]:
            print(f"{'-':>10}  {'nvjpegenc':>10}  unavailable: {r['fallback']}")
        for row in r["rows"]:
            print(f"{row['size']:>10}  {r['backend']:>10}  {row['wall_ms']:>8}  {row['cpu_ms']:>8}  {row['bytes']:>9}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
