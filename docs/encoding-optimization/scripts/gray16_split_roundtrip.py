#!/usr/bin/env python3
"""Can a 16-bit mono stream ride the service's 8-bit lossless HEVC recorder path?

Each GRAY16 frame is split into its high and low bytes, laid side by side as one GRAY8 frame twice as
wide, pushed through the recorder fragment that `cam_driver.recorder.build_recorder_description`
builds for the chosen 8-bit lossless encoder, decoded with the service's own read path
(`sources.replay.raw_decode_chain`), recombined and compared with the input. FFV1 on the 16-bit
frame (what the service does today) is run first as the baseline.

    # anywhere with GStreamer + PyGObject + numpy (software stand-in for the hardware path):
    python3 docs/encoding-optimization/scripts/gray16_split_roundtrip.py
    # on a Jetson, inside the cam-core image, against the hardware encoder:
    python3 docs/encoding-optimization/scripts/gray16_split_roundtrip.py --encoder hw-hevc-lossless
    # with real footage (headerless little-endian 16-bit frames, e.g. dumped from a recording):
    python3 ... --encoder hw-hevc-lossless --raw-file thermal.gray16 --width 640 --height 512

Prints, per variant: resolved encoder, size as a share of raw, bit-exact frame count, and process CPU
per frame. Process CPU does NOT include kernel-side encoder work; on a Jetson also watch `tegrastats`
or `top -H` while it runs. Writes only to a temporary directory.
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

from cam_driver import recorder as rec  # noqa: E402
from cam_driver.formats import FFV1_DECODER, LUMA_COLORIMETRY  # noqa: E402
from cam_driver.sources.replay import raw_decode_chain  # noqa: E402


def synthetic(n, w, h, sigma):
    """Thermal-like frames: a ~300 K scene in 0.01 K counts, smooth structure, a moving warm blob, noise."""
    rng = np.random.default_rng(7)
    yy, xx = np.mgrid[0:h, 0:w]
    out = []
    for i in range(n):
        base = 29500 + 4.0 * xx + 2.0 * yy + 900 * np.exp(-(((xx - w / 2 - 2 * i) ** 2 + (yy - h / 2) ** 2) / (2 * 60 ** 2)))
        out.append(np.clip(base + rng.normal(0, sigma, (h, w)), 0, 65535).astype("<u2"))
    return out


def from_file(path, n, w, h):
    data = np.fromfile(path, dtype="<u2", count=n * w * h)
    got = data.size // (w * h)
    if got == 0:
        raise SystemExit(f"{path}: not even one {w}x{h} 16-bit frame")
    return [data[i * w * h:(i + 1) * w * h].reshape(h, w) for i in range(got)]


def split_plain(f):
    return np.hstack([(f >> 8).astype(np.uint8), (f & 0xFF).astype(np.uint8)])


def split_folded(f):
    """Mirror the low byte wherever the high byte is odd, so it has no sawtooth edge at each carry."""
    hi = (f >> 8).astype(np.uint8)
    lo = (f & 0xFF).astype(np.uint8)
    return np.hstack([hi, np.where(hi & 1, 255 - lo, lo).astype(np.uint8)])


def join(img, w, folded):
    hi = img[:, :w].astype(np.uint16)
    lo = img[:, w:].astype(np.uint16)
    if folded:
        lo = np.where(hi & 1, 255 - lo, lo)
    return ((hi << 8) | lo).astype("<u2")


def record(encoder, bits, fmt, w, h, fps, payloads):
    out = tempfile.mkdtemp(prefix="g16_")
    cfg = types.SimpleNamespace(encoder=encoder, segment_seconds=3600, keyframe_interval_s=0, bframes=0,
                                nvenc_preset="", nvenc_maxperf=True, videoconvert_threads=4,
                                x264_crf=23, x264_preset="ultrafast")
    desc, resolved = rec.build_recorder_description(cfg, bits, f"{out}/cam", float(fps), False, None)
    p = Gst.parse_launch(f'appsrc name=s format=time caps="video/x-raw,format={fmt},width={w},height={h},'
                         f'framerate={fps}/1" ! {desc}')
    src = p.get_by_name("s")
    p.set_state(Gst.State.PLAYING)
    t0 = time.process_time()
    for i, d in enumerate(payloads):
        b = Gst.Buffer.new_wrapped(d)
        b.pts = i * Gst.SECOND // fps
        b.duration = Gst.SECOND // fps
        if src.emit("push-buffer", b) != Gst.FlowReturn.OK:
            raise SystemExit("push-buffer failed")
    src.emit("end-of-stream")
    msg = p.get_bus().timed_pop_filtered(300 * Gst.SECOND, Gst.MessageType.EOS | Gst.MessageType.ERROR)
    cpu = time.process_time() - t0
    p.set_state(Gst.State.NULL)
    if msg is None or msg.type != Gst.MessageType.EOS:
        raise SystemExit(f"recorder failed: {msg.parse_error() if msg else 'timeout'}")
    files = sorted(glob.glob(out + "/cam-*.mkv"))
    return files, sum(os.path.getsize(f) for f in files), cpu, resolved


def decode(files, chain, fmt):
    got = []
    for path in files:
        p = Gst.parse_launch(f'filesrc location="{path}" ! matroskademux ! {chain} ! videoconvert ! '
                             f'video/x-raw,format={fmt} ! appsink name=o sync=false')
        sink = p.get_by_name("o")
        p.set_state(Gst.State.PLAYING)
        while True:
            sample = sink.emit("try-pull-sample", 10 * Gst.SECOND)
            if sample is None:
                break
            buf = sample.get_buffer()
            got.append(buf.extract_dup(0, buf.get_size()))
        p.set_state(Gst.State.NULL)
    return got


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--encoder", default="x265-lossless", choices=("x265-lossless", "hw-hevc-lossless"),
                    help="8-bit lossless encoder for the split frame (hw-hevc-lossless needs a Jetson)")
    ap.add_argument("--width", type=int, default=640)
    ap.add_argument("--height", type=int, default=512)
    ap.add_argument("--frames", type=int, default=120)
    ap.add_argument("--fps", type=int, default=60)
    ap.add_argument("--sigma", type=float, default=6.0, help="noise (counts) of the synthetic frames")
    ap.add_argument("--raw-file", help="headerless GRAY16_LE frames to use instead of synthetic ones")
    a = ap.parse_args()
    w, h = a.width, a.height
    if (2 * w) % 4:
        raise SystemExit("the split frame's width (2 x width) must be a multiple of 4 (see review finding DP-1)")
    frames = from_file(a.raw_file, a.frames, w, h) if a.raw_file else synthetic(a.frames, w, h, a.sigma)
    n = len(frames)
    raw = n * w * h * 2
    print(f"{n} frames {w}x{h} GRAY16 ({'file ' + a.raw_file if a.raw_file else f'synthetic, sigma {a.sigma}'}), "
          f"{Gst.version_string()}")

    files, size, cpu, enc = record("ffv1", 16, "GRAY16_LE", w, h, a.fps, [f.tobytes() for f in frames])
    got = decode(files, FFV1_DECODER, "GRAY16_LE")
    ok = sum(np.array_equal(np.frombuffer(g, "<u2").reshape(h, w), f) for g, f in zip(got, frames))
    print(f"  FFV1 on GRAY16 (today)        : {enc:16s} {size / raw * 100:5.1f}% of raw  bit-exact {ok}/{n}  "
          f"{cpu * 1000 / n:6.2f} ms CPU/frame")

    chain = raw_decode_chain("video/x-h265", {"recording_colorimetry": LUMA_COLORIMETRY})
    for label, split, folded in (("high | low", split_plain, False), ("high | folded low", split_folded, True)):
        t0 = time.process_time()
        halves = [split(f).tobytes() for f in frames]
        t_split = time.process_time() - t0
        files, size, cpu, enc = record(a.encoder, 8, "GRAY8", 2 * w, h, a.fps, halves)
        if enc != a.encoder:
            raise SystemExit(f"requested {a.encoder} but the recorder resolved {enc}: the encoder's GStreamer "
                             "elements are not available here")
        got = decode(files, chain, "GRAY8")
        ok = sum(np.array_equal(join(np.frombuffer(g, np.uint8).reshape(h, 2 * w), w, folded), f)
                 for g, f in zip(got, frames))
        print(f"  split, {label:22s} : {enc:16s} {size / raw * 100:5.1f}% of raw  bit-exact {ok}/{n}  "
              f"{(cpu + t_split) * 1000 / n:6.2f} ms CPU/frame (split {t_split * 1000 / n:.2f})")


if __name__ == "__main__":
    main()
