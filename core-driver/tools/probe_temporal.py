#!/usr/bin/env python3
"""Does an HEVC recording actually use temporal (inter) prediction?

Classifies every coded frame as a keyframe (I/IDR) vs a delta frame (P/B) and compares their sizes.
This is the linchpin for the CFA-tiling work: tiling's *temporal* half and the `keyframe_interval_s`
window only do anything if the encoder emits delta frames that are meaningfully smaller than keyframes.
It matters most for the Jetson HW path -- whether NVENC's *lossless* mode does inter prediction (vs going
all-intra) is firmware-dependent and has to be measured on-device, not assumed.

GStreamer-only (runs in the gige-core image); no ffmpeg/ffprobe needed. Reads the per-AU
GST_BUFFER_FLAG_DELTA_UNIT after h265parse: keyframes lack it, P/B frames carry it.

Make a test recording on the Orin first (HW encoder, a long GOP, a few seconds of MOVING scene), e.g. a
config with `recording.encoder: auto`, `bayer_tile: plain`, `keyframe_interval_s: 4`, then:
  docker run --rm -v /data/recordings:/rec --entrypoint python3 gige-core:jp7 \
      tools/probe_temporal.py /rec/<prefix>-00000.mkv
(static or noise-only scenes won't show a temporal win even if inter works -- use real motion.)
"""
import sys

import gi
gi.require_version("Gst", "1.0")
from gi.repository import Gst, GLib


def probe(path, parser="h265parse"):
    Gst.init(None)
    stats = {"key": [0, 0], "delta": [0, 0]}   # class -> [count, total_bytes]
    pipeline = Gst.parse_launch(
        f'filesrc location="{path}" ! matroskademux ! {parser} ! fakesink name=fs sync=false')
    fs = pipeline.get_by_name("fs")

    def on_buffer(_pad, info):
        buf = info.get_buffer()
        cls = "delta" if (buf.get_flags() & Gst.BufferFlags.DELTA_UNIT) else "key"
        stats[cls][0] += 1
        stats[cls][1] += buf.get_size()
        return Gst.PadProbeReturn.OK

    fs.get_static_pad("sink").add_probe(Gst.PadProbeType.BUFFER, on_buffer)
    loop = GLib.MainLoop()
    bus = pipeline.get_bus()
    bus.add_signal_watch()
    err = []

    def on_msg(_bus, msg):
        if msg.type == Gst.MessageType.EOS:
            loop.quit()
        elif msg.type == Gst.MessageType.ERROR:
            e, _ = msg.parse_error()
            err.append(e.message)
            loop.quit()

    bus.connect("message", on_msg)
    pipeline.set_state(Gst.State.PLAYING)
    GLib.timeout_add_seconds(120, loop.quit)   # safety net
    loop.run()
    pipeline.set_state(Gst.State.NULL)
    return stats, (err[0] if err else None)


def main():
    pos = [a for a in sys.argv[1:] if not a.startswith("-")]
    if not pos:
        print(__doc__)
        return 2
    parser = "h264parse" if "--h264" in sys.argv else "h265parse"
    stats, err = probe(pos[0], parser)
    (kc, kb), (dc, db) = stats["key"], stats["delta"]
    if (kc + dc) == 0:
        print(f"no frames parsed{f' ({err})' if err else ''} -- wrong codec? try --h264, or check the file")
        return 1
    kmean = kb / kc if kc else 0.0
    dmean = db / dc if dc else 0.0
    print(f"frames: {kc + dc}   keyframes(I): {kc} (mean {kmean / 1024:.1f} KiB)   "
          f"delta(P/B): {dc} (mean {dmean / 1024:.1f} KiB)   total: {(kb + db) / 1e6:.2f} MB")
    if dc == 0:
        print("VERDICT: ALL keyframes -> NO temporal prediction (intra-only on this encoder, or GOP=1).")
        print("  keyframe_interval_s won't help and tiling's win here is spatial-only; HW then buys")
        print("  throughput, not ratio (FFV1 is competitive on intra). Re-run with a long GOP to be sure.")
    elif kmean and dmean < 0.7 * kmean:
        print(f"VERDICT: temporal prediction ACTIVE + effective -- delta frames are {100 * dmean / kmean:.0f}%"
              f" of a keyframe. The window knob pays off; tile + long GOP is worth tuning.")
    else:
        print("VERDICT: delta frames exist but aren't much smaller than keyframes -> inter isn't helping")
        print("  on this content (weak lossless inter, or a near-static / noise-dominated scene).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
