#!/usr/bin/env python3
"""Offline benchmark: how much does CFA tiling (+ colour transform) shrink a REAL lossless recording,
and does it unlock temporal prediction?

Decodes the original Bayer-mosaic recording back to frames (the mosaic rides in the Y plane), then
re-encodes the SAME frames as mosaic vs plain/green_diff/rct through each available lossless encoder,
reporting bytes/frame + total, the ratio vs the mosaic baseline, and -- for HEVC -- the I-frame vs
P/B-frame sizes (so you can see whether tiling makes inter prediction start paying off).

The x265 / ffv1 legs run anywhere; the NVENC leg (hw-hevc-lossless) needs the Jetson -- run this on the
Orin to get the number that represents your actual recorder.

Usage (in the gige-core image):
  docker run --rm -v <dir>:/in -v /tmp/bench:/work --entrypoint python3 gige-core:jp7 \
      tools/tiling_benchmark.py /in/<rec>-00000.mkv --frames 120 --encoders x265,ffv1 --work /work
Geometry/pattern come from the sidecar <rec>.json; fps from <rec>.csv (override with --fps/--width/...).
"""
import argparse
import json
import os
import subprocess
import sys
import time
from types import SimpleNamespace

import gi
gi.require_version("Gst", "1.0")
gi.require_version("GstVideo", "1.0")
from gi.repository import Gst, GstVideo, GLib  # noqa: E402
import numpy as np  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # so gige_driver imports
from gige_driver import bayer_tile, recorder  # noqa: E402

ENCODERS = {  # cli name -> (recorder encoder, the gst element that must exist)
    "x265": ("x265-lossless", "x265enc"),
    "ffv1": ("ffv1", "avenc_ffv1"),
    "nvenc": ("hw-hevc-lossless", "nvv4l2h265enc"),
}


def _run_loop(pipeline, timeout_s=900):
    loop = GLib.MainLoop()
    bus = pipeline.get_bus()
    bus.add_signal_watch()
    err = []

    def on_msg(_bus, msg):
        if msg.type == Gst.MessageType.EOS:
            loop.quit()
        elif msg.type == Gst.MessageType.ERROR:
            err.append(msg.parse_error()[0].message)
            loop.quit()
    bus.connect("message", on_msg)
    pipeline.set_state(Gst.State.PLAYING)
    GLib.timeout_add_seconds(timeout_s, loop.quit)
    loop.run()
    pipeline.set_state(Gst.State.NULL)
    return err[0] if err else None


def decode_mosaic(path, n, w, h, out_raw):
    """Decode up to n frames; write the stride-corrected Y-plane mosaic to out_raw. Returns frame count."""
    Gst.init(None)
    cnt = [0]
    fo = open(out_raw, "wb")
    pipe = Gst.parse_launch(
        f'filesrc location="{path}" ! matroskademux ! avdec_h265 ! videoconvert ! '
        f'video/x-raw,format=GRAY8 ! fakesink name=fs sync=false')
    fs = pipe.get_by_name("fs")

    def cb(_pad, info):
        if cnt[0] >= n:
            return Gst.PadProbeReturn.OK
        buf = info.get_buffer()
        vm = GstVideo.buffer_get_video_meta(buf)
        stride = vm.stride[0] if vm else w
        off = vm.offset[0] if vm else 0
        ok, mi = buf.map(Gst.MapFlags.READ)
        if ok:
            y = np.frombuffer(mi.data, np.uint8)[off:off + stride * h].reshape(h, stride)[:, :w]
            fo.write(np.ascontiguousarray(y).tobytes())
            buf.unmap(mi)
            cnt[0] += 1
        return Gst.PadProbeReturn.OK

    fs.get_static_pad("sink").add_probe(Gst.PadProbeType.BUFFER, cb)
    _run_loop(pipe)
    fo.close()
    return cnt[0]


def make_mode_raw(mosaic_raw, mode, w, h, pattern, out_raw):
    fsz = w * h
    with open(mosaic_raw, "rb") as fi, open(out_raw, "wb") as fo:
        while True:
            b = fi.read(fsz)
            if len(b) < fsz:
                break
            fo.write(b if mode == "mosaic" else bayer_tile.tile_cfa(b, w, h, mode, pattern))


def hevc_stats(path):
    """(key_count, key_bytes, delta_count, delta_bytes) from the encoded HEVC, or None if not parseable."""
    Gst.init(None)
    st = {"k": [0, 0], "d": [0, 0]}
    try:
        pipe = Gst.parse_launch(
            f'filesrc location="{path}" ! matroskademux ! h265parse ! fakesink name=fs sync=false')
    except GLib.Error:
        return None
    fs = pipe.get_by_name("fs")

    def cb(_pad, info):
        buf = info.get_buffer()
        c = "d" if (buf.get_flags() & Gst.BufferFlags.DELTA_UNIT) else "k"
        st[c][0] += 1
        st[c][1] += buf.get_size()
        return Gst.PadProbeReturn.OK

    fs.get_static_pad("sink").add_probe(Gst.PadProbeType.BUFFER, cb)
    _run_loop(pipe, 300)
    if st["k"][0] + st["d"][0] == 0:
        return None
    return st["k"][0], st["k"][1], st["d"][0], st["d"][1]


def encode(mode_raw, w, h, fps, enc_name, gop_s, bframes, preset, maxperf, base):
    """Encode mode_raw with the recorder's real fragment; return (bytes, wall_seconds) or None on failure."""
    rec_enc = ENCODERS[enc_name][0]
    cfg = SimpleNamespace(encoder=rec_enc, segment_seconds=100000, keyframe_interval_s=gop_s,
                          bframes=bframes, nvenc_preset=preset, nvenc_maxperf=maxperf)
    frag = recorder.build_recorder_description(cfg, 8, base, fps)
    desc = (f'filesrc location="{mode_raw}" ! rawvideoparse width={w} height={h} format=gray8 '
            f'framerate={max(1, int(round(fps)))}/1 ! {frag}')
    # argv list, NOT shell=True: the NVMM caps contain "(memory:NVMM)" and a shell would choke on the
    # parens. gst-launch rejoins argv and does its own parsing (incl. stripping the prop="..." quotes).
    t0 = time.monotonic()
    r = subprocess.run(["gst-launch-1.0", "-e", *desc.split()],
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    secs = time.monotonic() - t0
    out = base + "-00000.mkv"
    if r.returncode != 0 or not os.path.exists(out):
        sys.stderr.write(f"  ! encode failed ({enc_name}): {r.stderr.decode()[-200:]}\n")
        return None
    return os.path.getsize(out), secs


def _fps_from_csv(csv_path, default=25.0):
    try:
        with open(csv_path) as f:
            f.readline()
            p0 = int(f.readline().split(",")[1])
            p1 = int(f.readline().split(",")[1])
        return 1e9 / (p1 - p0) if p1 > p0 else default
    except Exception:
        return default


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("recording")
    ap.add_argument("--frames", type=int, default=120)
    ap.add_argument("--encoders", default="x265,ffv1")
    ap.add_argument("--modes", default="mosaic,plain,green_diff,rct")
    ap.add_argument("--gop-seconds", type=float, default=4.0)
    ap.add_argument("--bframes", type=int, default=0)
    ap.add_argument("--preset", default="",
                    help="nvenc preset(s) to sweep, comma-separated: ultrafast,fast,medium,slow (or 1-4). "
                         "'' = encoder default. Only applies to the nvenc encoder.")
    ap.add_argument("--maxperf", type=int, default=1, help="nvenc maxperf-enable (0|1)")
    ap.add_argument("--fps", type=float, default=0.0)
    ap.add_argument("--width", type=int, default=0)
    ap.add_argument("--height", type=int, default=0)
    ap.add_argument("--pattern", default="")
    ap.add_argument("--work", default="/tmp/tiling_bench")
    a = ap.parse_args()
    Gst.init(None)

    base = a.recording.rsplit("-", 1)[0]                  # strip splitmux -00000 suffix
    meta = {}
    for j in (base + ".json", os.path.splitext(a.recording)[0] + ".json"):
        if os.path.exists(j):
            meta = json.load(open(j))
            break
    w = a.width or meta.get("width", 0)
    h = a.height or meta.get("height", 0)
    pattern = a.pattern or meta.get("bayer_pattern", "rggb")
    fps = a.fps or _fps_from_csv(base + ".csv")
    if not (w and h):
        sys.exit("need width/height (sidecar .json not found; pass --width/--height)")

    encs = [e for e in a.encoders.split(",") if e in ENCODERS
            and Gst.ElementFactory.find(ENCODERS[e][1]) is not None]
    skipped = [e for e in a.encoders.split(",") if e in ENCODERS and e not in encs]
    modes = [m for m in a.modes.split(",")]
    os.makedirs(a.work, exist_ok=True)

    presets = a.preset.split(",") if a.preset else [""]
    print(f"benchmark: {a.recording}\n  {w}x{h} {pattern}  {fps:.2f} fps  frames={a.frames}  "
          f"gop={a.gop_seconds}s bframes={a.bframes} maxperf={a.maxperf}  nvenc-presets={presets}")
    if skipped:
        print(f"  (skipped encoders not in this image: {','.join(skipped)} -- run on the Orin for nvenc)")
    mosaic_src = os.path.join(a.work, "_source.raw")          # decoded mosaic (distinct from the 'mosaic' mode)
    n = decode_mosaic(a.recording, a.frames, w, h, mosaic_src)
    print(f"  decoded {n} mosaic frames ({w*h/1e6:.1f} MB/frame raw)\n")

    base_bytes = {}   # (encoder, preset) -> mosaic total bytes (for ratios)
    rows = []
    for mode in modes:
        if mode == "mosaic":
            mode_raw = mosaic_src                              # encode the source as-is (no tiling)
        else:
            mode_raw = os.path.join(a.work, f"{mode}.raw")
            make_mode_raw(mosaic_src, mode, w, h, pattern, mode_raw)
        for enc in encs:
            for preset in (presets if enc == "nvenc" else [""]):   # preset only matters for nvenc
                outbase = os.path.join(a.work, f"{mode}_{enc}_{preset or 'def'}")
                res = encode(mode_raw, w, h, fps, enc, a.gop_seconds, a.bframes, preset, a.maxperf, outbase)
                if res is None:
                    continue
                size, secs = res
                if mode == "mosaic":
                    base_bytes[(enc, preset)] = size
                stats = hevc_stats(outbase + "-00000.mkv") if enc != "ffv1" else None
                rows.append((mode, enc, preset, size, secs, stats))
        if mode != "mosaic":
            os.remove(mode_raw)

    # report
    print(f"{'mode':<11}{'enc':<6}{'preset':<9}{'MB':>8}{'KB/fr':>8}{'vs mos':>8}{'enc-fps':>9}{'P/B vs I':>11}")
    print("-" * 78)
    for mode, enc, preset, size, secs, stats in rows:
        bb = base_bytes.get((enc, preset))
        ratio = (size / bb * 100) if bb else 100.0
        encfps = (n / secs) if secs > 0 else 0.0
        temporal = ""
        if stats:
            kc, kb, dc, db = stats
            temporal = (f"{100 * (db / dc) / (kb / kc):.0f}%" if (dc and kc) else
                        (f"all-I({kc})" if kc else ""))
        print(f"{mode:<11}{enc:<6}{(preset or 'default'):<9}{size/1e6:>8.1f}{size/n/1024:>8.1f}"
              f"{ratio:>7.0f}%{encfps:>9.1f}{temporal:>11}")
    print(f"\nvs mos <100% = smaller. enc-fps = encode throughput (must be >= camera {fps:.0f} fps to record")
    print("real-time at that preset). P/B vs I << 100% = temporal prediction working; ~100% = inter not helping.")
    os.remove(mosaic_src)


if __name__ == "__main__":
    main()
