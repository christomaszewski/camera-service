#!/usr/bin/env python3
"""Where does the CPU go when an 8-bit Bayer mosaic is recorded on the hardware lossless HEVC path,
and do the candidate shortcuts keep the pixels exact?

Part 1 -- CPU-side steps on their own (runs anywhere):

  wrap + push            the per-frame copy into GStreamer
  videoconvert -> NV24   the recorder's colour step (GRAY8 mosaic into the luma of full-range NV24)
  bayer_tile=<mode>      the numpy tiling that runs on the capture thread when recording.bayer_tile is set
  NV24 from the feeder   building the NV24 buffer directly: mosaic as luma + one shared constant chroma block

Part 2 -- whole recorder variants, each recorded, decoded with the service's own read path and compared
with the input (CPU per frame, size, bit-exact count):

  A  recorder fragment as shipped       GRAY8 in, `videoconvert` does the colour step
  B  NV24 from the feeder               the shipped fragment with its `videoconvert` stage removed
  C  nvvidconv does the colour step     GRAY8 straight into `nvvidconv` (hardware encoder only)
  A+tile                                variant A fed CFA-tiled frames (size and CPU only)

    # software stand-in (x265 lossless); variant C is skipped, variant B only proves the buffer layout
    python3 docs/encoding-optimization/scripts/bayer_hw_path_cpu.py --frames 12
    # on a Jetson, inside the cam-core image
    python3 docs/encoding-optimization/scripts/bayer_hw_path_cpu.py --encoder hw-hevc-lossless
    # show the exact fragments the hardware run would use, without running them
    python3 docs/encoding-optimization/scripts/bayer_hw_path_cpu.py --print-fragments

Process CPU excludes kernel-side encoder work and cannot split threads. On a Jetson run
`top -H -p $(pgrep -f bayer_hw_path_cpu)` and `tegrastats` alongside. Writes only to temporary directories.
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
from cam_driver.sources.replay import raw_decode_chain  # noqa: E402


def mosaics(n, w, h, sigma):
    """A smooth scene behind an RGGB colour filter, plus sensor noise."""
    rng = np.random.default_rng(3)
    yy, xx = np.mgrid[0:h, 0:w]
    scene = (96 + 0.03 * xx + 0.02 * yy).astype(np.float32)
    cfa = np.where((yy % 2 == 0) & (xx % 2 == 0), 1.25, np.where((yy % 2 == 1) & (xx % 2 == 1), 0.7, 1.0))
    return [np.clip(scene * cfa + rng.normal(0, sigma, (h, w)), 0, 255).astype(np.uint8).tobytes() for _ in range(n)]


def run(caps, chain, payloads, fps):
    """Push payloads through `appsrc ! chain`; return process CPU ms per frame."""
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
    msg = p.get_bus().timed_pop_filtered(900 * Gst.SECOND, Gst.MessageType.EOS | Gst.MessageType.ERROR)
    cpu = (time.process_time() - t0) * 1000 / len(payloads)
    p.set_state(Gst.State.NULL)
    if msg is None:
        raise RuntimeError("timeout")
    if msg.type != Gst.MessageType.EOS:
        err, dbg = msg.parse_error()
        raise RuntimeError(f"{err.message} ({(dbg or '').splitlines()[0][:120]})")
    return cpu


def decode_luma(out_dir, w, h):
    """Decode with the service's read path (replay.raw_decode_chain) back to the GRAY8 plane."""
    chain = raw_decode_chain("video/x-h265", {"recording_colorimetry": LUMA_COLORIMETRY})
    got = []
    for path in sorted(glob.glob(out_dir + "/cam-*.mkv")):
        p = Gst.parse_launch(f'filesrc location="{path}" ! matroskademux ! {chain} ! videoconvert ! '
                             f'video/x-raw,format=GRAY8 ! appsink name=o sync=false')
        sink = p.get_by_name("o")
        p.set_state(Gst.State.PLAYING)
        while True:
            sample = sink.emit("try-pull-sample", 20 * Gst.SECOND)
            if sample is None:
                break
            buf = sample.get_buffer()
            got.append(buf.extract_dup(0, buf.get_size()))
        p.set_state(Gst.State.NULL)
    return got


def recorder_cfg(encoder):
    return types.SimpleNamespace(encoder=encoder, segment_seconds=3600, keyframe_interval_s=0, bframes=0,
                                 nvenc_preset="", nvenc_maxperf=True, videoconvert_threads=4,
                                 x264_crf=23, x264_preset="ultrafast")


def fragments(encoder, location, fps, assume_available=False):
    """The shipped recorder fragment, and the same fragment with its `videoconvert` stage removed."""
    if assume_available:
        rec._resolve_available = lambda enc: enc      # --print-fragments only: do not probe this host
    desc, resolved = rec.build_recorder_description(recorder_cfg(encoder), 8, location, float(fps), False, None)
    if resolved != encoder:
        return None, None, resolved
    stages = desc.split(" ! ")
    if not (stages[1].startswith("videoconvert") and stages[2].startswith("video/x-raw,format=")):
        raise SystemExit(f"unexpected recorder fragment shape, cannot derive variants: {desc}")
    return desc, " ! ".join(stages[:1] + stages[3:]), resolved


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--encoder", default="x265-lossless", choices=("x265-lossless", "hw-hevc-lossless"))
    ap.add_argument("--width", type=int, default=2448)
    ap.add_argument("--height", type=int, default=2048)
    ap.add_argument("--frames", type=int, default=120)
    ap.add_argument("--fps", type=int, default=24)
    ap.add_argument("--sigma", type=float, default=2.5, help="sensor noise of the synthetic mosaic")
    ap.add_argument("--print-fragments", action="store_true",
                    help="print the hardware fragments variants A/B/C would use and exit")
    a = ap.parse_args()
    w, h, n, fps = a.width, a.height, a.frames, a.fps
    hw = a.encoder == "hw-hevc-lossless"

    if a.print_fragments:
        shipped, no_vconv, _ = fragments("hw-hevc-lossless", "/tmp/out/cam", fps, assume_available=True)
        print("A  GRAY8 in :", shipped)
        print("B  NV24 in  :", no_vconv)
        print("C  GRAY8 in :", no_vconv)
        return

    frames = mosaics(n, w, h, a.sigma)
    raw = n * w * h
    g8 = f"video/x-raw,format=GRAY8,width={w},height={h}"
    nv24_caps = f"video/x-raw,format=NV24,width={w},height={h},colorimetry={LUMA_COLORIMETRY}"
    sink = "fakesink sync=false"
    print(f"{n} frames {w}x{h} Bayer mosaic, {os.cpu_count()} CPUs, {Gst.version_string()}, encoder {a.encoder}")

    print("Part 1 -- CPU-side steps")
    print(f"  wrap + push                 : {run(g8, sink, frames, fps):6.2f} ms CPU/frame")
    for nt in (1, 4):
        c = run(g8, f"videoconvert n-threads={nt} ! {nv24_caps.replace(f',width={w},height={h}', '')} ! {sink}", frames, fps)
        print(f"  videoconvert -> NV24, {nt} thr : {c:6.2f} ms CPU/frame (includes the push)")
    tiled, tile_ms = {}, {}
    for mode in ("plain", "green_diff", "rct"):
        t0 = time.process_time()
        tiled[mode] = [bayer_tile.tile_cfa(f, w, h, mode, "rggb") for f in frames]
        tile_ms[mode] = (time.process_time() - t0) * 1000 / n
        print(f"  bayer_tile={mode:10s}       : {tile_ms[mode]:6.2f} ms CPU/frame")
    # The bytes object must outlive the memory: Gst.Memory.new_wrapped does not copy it. Passing a temporary
    # here left the chroma block dangling, and on the Orin variant B then read garbage (NvMap write errors,
    # 123.7% of raw, bit-exact 0/120) -- a fault of this script, not of the layout. Held, it is 48/48.
    chroma_bytes = bytes([128]) * (2 * w * h)
    chroma = Gst.Memory.new_wrapped(Gst.MemoryFlags.READONLY, chroma_bytes, 2 * w * h, 0, None, None)

    def nv24(f):
        b = Gst.Buffer.new_wrapped(f)
        b.append_memory(chroma)
        return b
    print(f"  NV24 from the feeder        : {run(nv24_caps, sink, [nv24(f) for f in frames], fps):6.2f} ms CPU/frame"
          " (push only; shared chroma)")

    print("Part 2 -- whole recorder variants (recorded, decoded with the service's read path, compared)")

    def variant(label, caps, payloads, make_fragment, expect=frames):
        out = tempfile.mkdtemp(prefix="bayer_")
        try:
            desc = make_fragment(f"{out}/cam")
            if desc is None:
                print(f"  {label:34s}: skipped (encoder elements not available on this host)")
                return
            cpu = run(caps, desc, payloads, fps)
            size = sum(os.path.getsize(f) for f in glob.glob(out + "/cam-*.mkv"))
            verdict = ""
            if expect is not None:
                got = decode_luma(out, w, h)
                ok = sum(g == f for g, f in zip(got, expect))
                verdict = f"  bit-exact {ok}/{len(expect)}" + ("" if len(got) == len(expect) else f" (decoded {len(got)})")
            print(f"  {label:34s}: {cpu:6.2f} ms CPU/frame  {size / raw * 100:5.1f}% of raw{verdict}")
        except (RuntimeError, GLibError) as e:
            print(f"  {label:34s}: FAILED -- {str(e)[:150]}")

    from gi.repository import GLib
    GLibError = GLib.Error
    variant("A  recorder fragment as shipped", g8, frames, lambda loc: fragments(a.encoder, loc, fps)[0])
    if hw:
        variant("B  NV24 from the feeder", nv24_caps, [nv24(f) for f in frames],
                lambda loc: fragments(a.encoder, loc, fps)[1])
        variant("C  nvvidconv does the colour step", g8, frames, lambda loc: fragments(a.encoder, loc, fps)[1])
    else:
        # software stand-in: x265 has no NV24 input, so this only proves the two-memory NV24 buffer is laid
        # out the way GStreamer expects (videoconvert reads it back to the same luma)
        variant("B  NV24 from the feeder (layout)", nv24_caps, [nv24(f) for f in frames],
                lambda loc: (f"queue ! videoconvert ! video/x-raw,format=Y444,colorimetry={LUMA_COLORIMETRY} ! "
                             'x265enc option-string="lossless=1:bframes=0" speed-preset=ultrafast ! h265parse ! '
                             + rec._splitmux(loc, 3600)))
        print("  C  nvvidconv does the colour step : skipped (hardware encoder only)")
    variant("A + bayer_tile=plain (add tiling)", g8, tiled["plain"],
            lambda loc: fragments(a.encoder, loc, fps)[0], expect=tiled["plain"])
    print(f"     (tiling itself costs {tile_ms['plain']:.2f} ms CPU/frame on top; see Part 1)")


if __name__ == "__main__":
    main()
