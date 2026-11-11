#!/usr/bin/env python3
"""CPU cost per frame of the candidate ways to record a 16-bit mono stream.

Feeds the same GRAY16 frames through each chain and reports process CPU per frame and output size:

  ffv1 (release)      the recorder's current FFV1 settings (range coder + context, 4 slices)
  ffv1 (speed knobs)  coder=0 context=0 slicecrc=0 -- the PR #65 knobs. FFV1 forces the range coder
                      above 8 bits, so expect little change on 16-bit input.
  uncompressed        GRAY16_LE written straight into Matroska (no encoder)
  split, CPU side     byte split + GRAY8->NV24 convert only: what stays on the CPU if the split frame
                      goes to the hardware encoder (encoder itself NOT included)
  split, hw encoder   the full hw-hevc-lossless fragment on the split frame (Jetson only; skipped when
                      the elements are missing)

    python3 docs/encoding-optimization/scripts/recording_cpu_bench.py [--frames 300] [--sigma 6]
    python3 ... --raw-file thermal.gray16 --width 640 --height 512      # real footage

Process CPU excludes kernel-side hardware encoder work; on a Jetson compare with `tegrastats` / `top -H`.
"""
import argparse
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

from cam_driver import recorder as rec  # noqa: E402
from cam_driver.formats import LUMA_COLORIMETRY  # noqa: E402


def frames_synthetic(n, w, h, sigma):
    rng = np.random.default_rng(7)
    yy, xx = np.mgrid[0:h, 0:w]
    return [np.clip(29500 + 4.0 * xx + 2.0 * yy + rng.normal(0, sigma, (h, w)), 0, 65535).astype("<u2")
            for _ in range(n)]


def split_folded(f):
    hi = (f >> 8).astype(np.uint8)
    lo = (f & 0xFF).astype(np.uint8)
    return np.hstack([hi, np.where(hi & 1, 255 - lo, lo).astype(np.uint8)]).tobytes()


def run(caps, chain, payloads, fps, to_file=True):
    out = tempfile.mktemp(suffix=".mkv") if to_file else None
    tail = f"matroskamux ! filesink location={out}" if to_file else "fakesink sync=false"
    p = Gst.parse_launch(f'appsrc name=s format=time caps="{caps},framerate={fps}/1" ! {chain} {tail}')
    src = p.get_by_name("s")
    p.set_state(Gst.State.PLAYING)
    t0 = time.process_time()
    for i, d in enumerate(payloads):
        b = Gst.Buffer.new_wrapped(d)
        b.pts = i * Gst.SECOND // fps
        b.duration = Gst.SECOND // fps
        src.emit("push-buffer", b)
    src.emit("end-of-stream")
    msg = p.get_bus().timed_pop_filtered(600 * Gst.SECOND, Gst.MessageType.EOS | Gst.MessageType.ERROR)
    cpu = time.process_time() - t0
    p.set_state(Gst.State.NULL)
    if msg is None or msg.type != Gst.MessageType.EOS:
        raise RuntimeError(str(msg.parse_error()) if msg else "timeout")
    size = os.path.getsize(out) if out else 0
    if out:
        os.unlink(out)
    return cpu, size


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--width", type=int, default=640)
    ap.add_argument("--height", type=int, default=512)
    ap.add_argument("--frames", type=int, default=300)
    ap.add_argument("--fps", type=int, default=60)
    ap.add_argument("--sigma", type=float, default=6.0)
    ap.add_argument("--raw-file", help="headerless GRAY16_LE frames to use instead of synthetic ones")
    a = ap.parse_args()
    w, h, fps = a.width, a.height, a.fps
    if a.raw_file:
        data = np.fromfile(a.raw_file, dtype="<u2", count=a.frames * w * h)
        frames = [data[i * w * h:(i + 1) * w * h].reshape(h, w) for i in range(data.size // (w * h))]
    else:
        frames = frames_synthetic(a.frames, w, h, a.sigma)
    n = len(frames)
    raw = n * w * h * 2
    raw16 = [f.tobytes() for f in frames]
    t0 = time.process_time()
    halves = [split_folded(f) for f in frames]
    t_split = (time.process_time() - t0) * 1000 / n
    c16 = f"video/x-raw,format=GRAY16_LE,width={w},height={h}"
    c8 = f"video/x-raw,format=GRAY8,width={2 * w},height={h}"
    print(f"{n} frames {w}x{h} GRAY16, {os.cpu_count()} CPUs, {Gst.version_string()}")

    def show(label, cpu, size, extra=0.0):
        share = f"{size / raw * 100:5.1f}% of raw" if size else "      (no file)"
        print(f"  {label:20s}: {cpu * 1000 / n + extra:6.2f} ms CPU/frame   {share}")

    show("ffv1 (release)", *run(c16, "avenc_ffv1 coder=1 context=1 threads=0 slices=4 slicecrc=1 !", raw16, fps))
    show("ffv1 (speed knobs)", *run(c16, "avenc_ffv1 coder=0 context=0 threads=0 slices=4 slicecrc=0 !", raw16, fps))
    show("uncompressed", *run(c16, "", raw16, fps))
    show("split, CPU side", *run(c8, f"videoconvert n-threads=4 ! video/x-raw,format=NV24,colorimetry={LUMA_COLORIMETRY} !",
                                  halves, fps, to_file=False), extra=t_split)
    cfg = types.SimpleNamespace(encoder="hw-hevc-lossless", segment_seconds=3600, keyframe_interval_s=0, bframes=0,
                                nvenc_preset="", nvenc_maxperf=True, videoconvert_threads=4,
                                x264_crf=23, x264_preset="ultrafast")
    desc, resolved = rec.build_recorder_description(cfg, 8, tempfile.mkdtemp(prefix="g16_") + "/cam", float(fps), False, None)
    if resolved != "hw-hevc-lossless":
        print("  split, hw encoder   : skipped (nvvidconv / nvv4l2h265enc not available on this host)")
        return
    out = desc[desc.index('location="') + 10:desc.index("-%05d.mkv")]
    p = Gst.parse_launch(f'appsrc name=s format=time caps="{c8},framerate={fps}/1" ! {desc}')
    src = p.get_by_name("s")
    p.set_state(Gst.State.PLAYING)
    t0 = time.process_time()
    for i, d in enumerate(halves):
        b = Gst.Buffer.new_wrapped(d)
        b.pts = i * Gst.SECOND // fps
        b.duration = Gst.SECOND // fps
        src.emit("push-buffer", b)
    src.emit("end-of-stream")
    p.get_bus().timed_pop_filtered(600 * Gst.SECOND, Gst.MessageType.EOS | Gst.MessageType.ERROR)
    cpu = time.process_time() - t0
    p.set_state(Gst.State.NULL)
    import glob
    size = sum(os.path.getsize(f) for f in glob.glob(out + "-*.mkv"))
    show("split, hw encoder", cpu, size, extra=t_split)


if __name__ == "__main__":
    main()
