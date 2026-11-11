#!/usr/bin/env python3
"""Where does the CPU go when an 8-bit Bayer mosaic is recorded on the hardware lossless HEVC path?

Measures process CPU per frame for each CPU-side step on its own, and (on a Jetson) for the whole
recorder fragment with and without CFA tiling:

  wrap + push            the per-frame copy into GStreamer
  videoconvert -> NV24   the recorder's colour step (GRAY8 mosaic into the luma of full-range NV24)
  bayer_tile=<mode>      the numpy tiling that runs on the capture thread when recording.bayer_tile is set
  NV24 from the feeder   an alternative to videoconvert: mosaic as luma + one shared constant chroma block
  hw fragment, <mode>    the full hw-hevc-lossless recorder fragment (Jetson only), with output size

    python3 docs/encoding-optimization/scripts/bayer_hw_path_cpu.py [--width 2448 --height 2048 --frames 120]

Process CPU excludes kernel-side encoder work. On a Jetson run `top -H -p <pid>` alongside to see the
per-thread split (nvvidconv, the encoder, h265parse, the muxer), which this script cannot show.
"""
import argparse
import glob
import os
import sys
import tempfile
import time
import types

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "core-driver"))

import gi  # noqa: E402
gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402
Gst.init(None)
import numpy as np  # noqa: E402

from cam_driver import bayer_tile, recorder as rec  # noqa: E402
from cam_driver.formats import LUMA_COLORIMETRY  # noqa: E402


def mosaics(n, w, h, sigma):
    """A smooth scene behind an RGGB colour filter, plus sensor noise."""
    rng = np.random.default_rng(3)
    yy, xx = np.mgrid[0:h, 0:w]
    scene = (96 + 0.03 * xx + 0.02 * yy).astype(np.float32)
    cfa = np.where((yy % 2 == 0) & (xx % 2 == 0), 1.25, np.where((yy % 2 == 1) & (xx % 2 == 1), 0.7, 1.0))
    return [np.clip(scene * cfa + rng.normal(0, sigma, (h, w)), 0, 255).astype(np.uint8).tobytes() for _ in range(n)]


def run(caps, chain, payloads, fps):
    p = Gst.parse_launch(f'appsrc name=s format=time caps="{caps},framerate={fps}/1" ! {chain}')
    src = p.get_by_name("s")
    p.set_state(Gst.State.PLAYING)
    t0 = time.process_time()
    for i, d in enumerate(payloads):
        b = d if isinstance(d, Gst.Buffer) else Gst.Buffer.new_wrapped(d)
        b.pts = i * Gst.SECOND // fps
        b.duration = Gst.SECOND // fps
        src.emit("push-buffer", b)
    src.emit("end-of-stream")
    msg = p.get_bus().timed_pop_filtered(600 * Gst.SECOND, Gst.MessageType.EOS | Gst.MessageType.ERROR)
    cpu = (time.process_time() - t0) * 1000 / len(payloads)
    p.set_state(Gst.State.NULL)
    if msg is None or msg.type != Gst.MessageType.EOS:
        raise RuntimeError(str(msg.parse_error()) if msg else "timeout")
    return cpu


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--width", type=int, default=2448)
    ap.add_argument("--height", type=int, default=2048)
    ap.add_argument("--frames", type=int, default=120)
    ap.add_argument("--fps", type=int, default=24)
    ap.add_argument("--sigma", type=float, default=2.5)
    a = ap.parse_args()
    w, h, n, fps = a.width, a.height, a.frames, a.fps
    frames = mosaics(n, w, h, a.sigma)
    g8 = f"video/x-raw,format=GRAY8,width={w},height={h}"
    sink = "fakesink sync=false"
    print(f"{n} frames {w}x{h} Bayer mosaic, {os.cpu_count()} CPUs, {Gst.version_string()}")
    print(f"  wrap + push                 : {run(g8, sink, frames, fps):6.2f} ms CPU/frame")
    for nt in (1, 4):
        c = run(g8, f"videoconvert n-threads={nt} ! video/x-raw,format=NV24,colorimetry={LUMA_COLORIMETRY} ! {sink}",
                frames, fps)
        print(f"  videoconvert -> NV24, {nt} thr : {c:6.2f} ms CPU/frame (includes the push)")
    tiled = {"off": frames}
    for mode in ("plain", "green_diff", "rct"):
        t0 = time.process_time()
        tiled[mode] = [bayer_tile.tile_cfa(f, w, h, mode, "rggb") for f in frames]
        print(f"  bayer_tile={mode:10s}       : {(time.process_time() - t0) * 1000 / n:6.2f} ms CPU/frame")
    chroma = Gst.Memory.new_wrapped(Gst.MemoryFlags.READONLY, bytes([128]) * (2 * w * h), 2 * w * h, 0, None, None)

    def nv24(f):
        b = Gst.Buffer.new_wrapped(f)
        b.append_memory(chroma)
        return b
    c = run(f"video/x-raw,format=NV24,width={w},height={h},colorimetry={LUMA_COLORIMETRY}", sink,
            [nv24(f) for f in frames], fps)
    print(f"  NV24 from the feeder        : {c:6.2f} ms CPU/frame (push only; shared chroma, no videoconvert)")

    cfg = types.SimpleNamespace(encoder="hw-hevc-lossless", segment_seconds=3600, keyframe_interval_s=0, bframes=0,
                                nvenc_preset="", nvenc_maxperf=True, videoconvert_threads=4,
                                x264_crf=23, x264_preset="ultrafast")
    raw = n * w * h
    for mode in ("off", "plain"):
        out = tempfile.mkdtemp(prefix="bayer_")
        desc, resolved = rec.build_recorder_description(cfg, 8, f"{out}/cam", float(fps), False, None)
        if resolved != "hw-hevc-lossless":
            print("  hw fragment                 : skipped (nvvidconv / nvv4l2h265enc not available on this host)")
            return
        c = run(g8, desc, tiled[mode], fps)
        size = sum(os.path.getsize(f) for f in glob.glob(out + "/cam-*.mkv"))
        print(f"  hw fragment, tile={mode:6s}   : {c:6.2f} ms CPU/frame   {size / raw * 100:5.1f}% of raw"
              "   (tiling time not included; add the bayer_tile row)")


if __name__ == "__main__":
    main()
