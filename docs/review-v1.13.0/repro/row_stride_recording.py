"""Reproduces DP-1 (docs/review-v1.13.0/02-core-data-path.md): what the real recorder does with the
tight (unpadded) frames the core feeds it when the row size is not a multiple of 4 bytes.

Run from the repository root inside any image/host with GStreamer + PyGObject + numpy, e.g.
    python3 docs/review-v1.13.0/repro/row_stride_recording.py
It drives cam_driver.recorder.build_recorder_description + cam_driver.session.RecordingSession with
random frames, decodes the result and compares bytes. Observed on GStreamer 1.24.2:

    GRAY8 1024x16 ffv1            20/20 byte-exact (control)
    GRAY8 1624x16 x265-lossless   20/20 byte-exact (control)
    GRAY8 1626x16 x265-lossless   20 frames "ok", no session error, 0/20 match (blank frames)
    GRAY8  659x16 x265-lossless   loud failure: x265 refuses the odd width (session error)
    GRAY8 1626x16 ffv1            process abort (exit -6) or an undecodable file
    GRAY16_LE 641x16 ffv1         process abort (exit -6)
    RGB   1001x16 ffv1            20 frames "ok", no session error, 0/20 match

Each case runs in its own subprocess so one abort does not hide the rest. Writes only to a temp dir.
"""
import os, sys, time, tempfile, glob, types
sys.dont_write_bytecode = True
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "core-driver"))
import gi
gi.require_version("Gst", "1.0")
from gi.repository import Gst, GLib
Gst.init(None)
import numpy as np
from cam_driver import recorder as rec
from cam_driver.session import RecordingSession, PushResult
from cam_driver.sidecar import SidecarHeader
from cam_driver.timestamps import FrameStamp, TimestampSource
from cam_driver.formats import bytes_per_frame, FFV1_DECODER

def run(fmt, w, h, encoder, n=20, bits=8, is_color=False):
    out = tempfile.mkdtemp(prefix="stride_")
    cfg = types.SimpleNamespace(encoder=encoder, segment_seconds=60, keyframe_interval_s=0, bframes=0,
                                nvenc_preset="", nvenc_maxperf=True, videoconvert_threads=1,
                                x264_crf=23, x264_preset="ultrafast")
    loc = f"{out}/cam"
    desc, enc = rec.build_recorder_description(cfg, bits, loc, 10.0, is_color, None)
    size = bytes_per_frame(fmt, w, h)
    caps = f"video/x-raw,format={fmt},width={w},height={h},framerate=10/1"
    full = f'appsrc name=recsrc is-live=true do-timestamp=false format=time max-bytes={32*size} caps="{caps}" ! {desc}'
    def header(stamp, pts, sess):
        return SidecarHeader(created_unix_s=time.time(), base_timestamp_ns=0, timestamp_source="system",
                             ptp_synced=False, pixel_format=fmt, bayer_pattern=None, bits_per_pixel=bits,
                             width=w, height=h, tick_frequency_hz=0)
    errors = []
    sess = RecordingSession(1, "cam", out, full, header_factory=header, on_error=lambda s: errors.append(s.error))
    warnings = []
    sess.start({})
    bus = sess._pipeline.get_bus()
    rng = np.random.default_rng(1)
    frames = []
    results = []
    ctx = GLib.MainContext.default()
    for i in range(n):
        data = rng.integers(0, 256, size=size, dtype=np.uint8).tobytes()
        frames.append(data)
        st = FrameStamp(frame_id=i, timestamp_ns=1_000_000_000 + i * 100_000_000, source=TimestampSource.SYSTEM,
                        system_ns=0, camera_ns=0)
        results.append(sess.push(data, i * 100_000_000, st).value)
        time.sleep(0.02)
        while ctx.iteration(False):
            pass
    time.sleep(0.3)
    while ctx.iteration(False):
        pass
    sess.begin_close()
    info = sess.finish_close(5.0, {})
    files = sorted(glob.glob(out + "/cam-*.mkv"))
    print(f"--- {fmt} {w}x{h} enc={enc}: tight size={size}  push results={ {r: results.count(r) for r in set(results)} }")
    print("    session error:", (info.get("error") or "")[:300])
    print("    frames:", info["frames"], "truncated:", info.get("truncated"), "files:", [(os.path.basename(f), os.path.getsize(f)) for f in files])
    return out, files, frames, enc

def decode(files, fmt, w, h, enc):
    if not files:
        return []
    dec = {"ffv1": FFV1_DECODER, "x265-lossless": "h265parse ! avdec_h265", "x264": "h264parse ! avdec_h264"}[enc]
    p = Gst.parse_launch(f'filesrc location="{files[0]}" ! matroskademux ! {dec} ! videoconvert ! '
                         f'video/x-raw,format={fmt} ! appsink name=s sync=false')
    s = p.get_by_name("s")
    p.set_state(Gst.State.PLAYING)
    got = []
    while True:
        sample = s.emit("try-pull-sample", 3 * Gst.SECOND)
        if sample is None:
            break
        b = sample.get_buffer()
        got.append(b.extract_dup(0, b.get_size()))
    p.set_state(Gst.State.NULL)
    return got

CASES = (
    ("GRAY8", 1024, 16, "ffv1", 8, False),            # control: row size % 4 == 0
    ("GRAY8", 1624, 16, "x265-lossless", 8, False),   # control
    ("GRAY8", 1626, 16, "x265-lossless", 8, False),   # e.g. a 1626x1236 sensor: silent blank frames
    ("GRAY8", 659, 16, "x265-lossless", 8, False),    # odd width: x265 refuses it, loudly
    ("GRAY8", 1626, 16, "ffv1", 8, False),            # FFV1: process abort or an undecodable file
    ("GRAY16_LE", 641, 16, "ffv1", 16, False),
    ("RGB", 1001, 16, "ffv1", 8, True),
)


def one(fmt, w, h, enc, bits, color):
    out, files, frames, renc = run(fmt, w, h, enc, bits=bits, is_color=color)
    got = decode(files, fmt, w, h, renc)
    exact = sum(1 for a, b in zip(frames, got) if a == b)
    print(f"    decoded {len(got)} frame(s); byte-exact vs pushed: {exact}/{len(frames)}", flush=True)


if __name__ == "__main__":
    if len(sys.argv) > 1:
        fmt, w, h, enc, bits, color = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), sys.argv[4], int(sys.argv[5]), sys.argv[6] == "1"
        one(fmt, w, h, enc, bits, color)
    else:
        import subprocess
        for fmt, w, h, enc, bits, color in CASES:
            rc = subprocess.run([sys.executable, os.path.abspath(__file__), fmt, str(w), str(h), enc, str(bits),
                                 "1" if color else "0"]).returncode
            print(f"=== {fmt} {w}x{h} {enc}: exit {rc}", flush=True)
