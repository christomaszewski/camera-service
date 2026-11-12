#!/usr/bin/env python3
"""Recorder-variant bench for a Jetson: per-thread CPU, system-wide CPU, throughput, size and bit-exactness.

Complements bayer_hw_path_cpu.py / gray16_split_roundtrip.py: those answer "is it exact, roughly what does it
cost"; this one feeds each variant AT THE CAMERA RATE the way the service's capture thread would (the
feeder-side transform -- tiling, byte split, building the NV12/NV24 buffer -- runs in the push loop and is
charged to the main thread), records to disk, decodes with the service's own read path and compares every
frame with the input. Results from an R39 AGX Orin are in ../RESULTS.md.

    # inside the cam-core image, the checkout mounted at /repo (CAM_REPO overrides), a scratch dir at /work
    docker run --rm --device nvidia.com/gpu=all -v "$PWD":/repo -v /tmp/work:/work -w /repo --entrypoint python3 \
        <cam-core image> docs/encoding-optimization/scripts/recorder_variants_bench.py --set bayer
    ... --set bayer --variants A,D-NV12 --content ramp      # every 8-bit code in every frame
    ... --set bayer --content noise                         # worst case for the lossless bitstream
    ... --set bayer --unpaced                               # saturated: max throughput, CPU at full clock
    ... --set gray16 --sigma 40                             # 16-bit thermal-like frames, noisier
    ... --raw-file frames.gray8|frames.gray16 --width W --height H   # real footage, headerless frames

8-bit set (2448x2048@24 by default): A = the recorder fragment as shipped; A-1thr = videoconvert n-threads=1;
A-diag / B-diag / D-NV12-diag = queues between elements so each gets its own thread; B* = NV24 built in the
feeder; C* = nvvidconv asked to do the colour step; D-* = NV12 or I420 built in the feeder (constant chroma),
the VIC upsampling it to NV24; +tile = bayer_tile=plain first; ffv1 / raw / null for scale.
16-bit set (640x512@60): ffv1 (today); raw; split-* = high|low bytes side by side through the shipped 8-bit
fragment; splitB-* / splitD-* = the same split fed as NV24 / NV12; planes-* = Y = high byte, U = low byte.

CPU is reported three ways, all per frame:
  proc   user+sys of this process (all threads)
  sys+   system-wide busy time above the idle baseline measured just before the run. sys+ minus proc is
         work done outside the process on its behalf: kernel threads and IRQs of the encoder / VIC drivers.
  thr    per-thread user+sys by thread name (GStreamer names streaming threads <element>:src)
A paced run under the schedutil governor runs at a reduced clock, so its CPU-ms are higher than the same
work saturated (--unpaced); compare variants within one mode.
"""
import argparse
import glob
import os
import shutil
import sys
import threading
import time
import types

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.join(os.environ.get("CAM_REPO", "/repo"), "core-driver"))

import gi  # noqa: E402
gi.require_version("Gst", "1.0")
gi.require_version("GstVideo", "1.0")
from gi.repository import Gst, GstVideo  # noqa: E402
Gst.init(None)
import numpy as np  # noqa: E402

from cam_driver import bayer_tile, recorder as rec  # noqa: E402
from cam_driver.formats import FFV1_DECODER, LUMA_COLORIMETRY  # noqa: E402
from cam_driver.sources.replay import raw_decode_chain  # noqa: E402

CLK = os.sysconf("SC_CLK_TCK")


# ---------------------------------------------------------------- accounting
def thread_cpu():
    out = {}
    for tid in os.listdir("/proc/self/task"):
        try:
            with open(f"/proc/self/task/{tid}/stat") as f:
                s = f.read()
            comm = s[s.index("(") + 1:s.rindex(")")]
            fld = s[s.rindex(")") + 2:].split()
            out[int(tid)] = (comm, int(fld[11]), int(fld[12]))
        except (OSError, ValueError):
            pass
    return out


def sys_busy():
    with open("/proc/stat") as f:
        v = list(map(int, f.readline().split()[1:9]))
    return v[0] + v[1] + v[2] + v[5] + v[6]      # user nice system irq softirq (jiffies, all CPUs)


class Sampler(threading.Thread):
    """Keeps the last-seen CPU of every thread, so threads that exit at EOS are still counted."""
    def __init__(self):
        super().__init__(daemon=True)
        self.base = thread_cpu()
        self.seen = {}
        self._halt = threading.Event()

    def run(self):
        while not self._halt.wait(0.5):
            self.seen.update(thread_cpu())

    def finish(self):
        self._halt.set()
        self.join()
        self.seen.update(thread_cpu())
        agg = {}
        for tid, (comm, ut, st) in self.seen.items():
            b = self.base.get(tid, (comm, 0, 0))
            d_u, d_s = ut - b[1], st - b[2]
            if d_u + d_s > 0:
                a = agg.setdefault(comm, [0, 0, 0])
                a[0] += d_u
                a[1] += d_s
                a[2] += 1
        return agg


# ---------------------------------------------------------------- frames
def bayer_frames(n, w, h, sigma):
    rng = np.random.default_rng(3)
    yy, xx = np.mgrid[0:h, 0:w]
    scene = (96 + 0.03 * xx + 0.02 * yy).astype(np.float32)
    cfa = np.where((yy % 2 == 0) & (xx % 2 == 0), 1.25, np.where((yy % 2 == 1) & (xx % 2 == 1), 0.7, 1.0))
    base = (scene * cfa).astype(np.float32)
    return [np.clip(base + rng.normal(0, sigma, (h, w)), 0, 255).astype(np.uint8).tobytes() for _ in range(n)]


def ramp_frames(n, w, h):
    """Every one of the 256 codes in every frame: top half a rolling ramp, bottom half uniform noise."""
    rng = np.random.default_rng(11)
    out = []
    for i in range(n):
        f = np.empty((h, w), np.uint8)
        f[:h // 2] = ((np.arange(w, dtype=np.uint32) + 7 * i) % 256).astype(np.uint8)[None, :]
        f[h // 2:] = rng.integers(0, 256, (h - h // 2, w), dtype=np.uint8)
        out.append(f.tobytes())
    return out


def noise_frames(n, w, h):
    rng = np.random.default_rng(5)
    return [rng.integers(0, 256, (h, w), dtype=np.uint8).tobytes() for _ in range(n)]


def thermal_frames(n, w, h, sigma):
    rng = np.random.default_rng(7)
    yy, xx = np.mgrid[0:h, 0:w]
    out = []
    for i in range(n):
        base = 29500 + 4.0 * xx + 2.0 * yy + 900 * np.exp(-(((xx - w / 2 - 2 * i) ** 2 + (yy - h / 2) ** 2) / (2 * 60 ** 2)))
        out.append(np.clip(base + rng.normal(0, sigma, (h, w)), 0, 65535).astype("<u2").tobytes())
    return out


def raw_file_frames(path, n, w, h, bpp):
    size = w * h * bpp
    with open(path, "rb") as f:
        data = f.read(n * size)
    return [data[i * size:(i + 1) * size] for i in range(len(data) // size)]


# ---------------------------------------------------------------- 16-bit transforms
def split16(b, w, h, folded):
    f = np.frombuffer(b, "<u2").reshape(h, w)
    hi = (f >> 8).astype(np.uint8)
    lo = (f & 0xFF).astype(np.uint8)
    if folded:
        lo = np.where(hi & 1, 255 - lo, lo).astype(np.uint8)
    return np.hstack([hi, lo]).tobytes()


def join16(img, w, h, folded):
    a = np.frombuffer(img, np.uint8).reshape(h, 2 * w)
    hi = a[:, :w].astype(np.uint16)
    lo = a[:, w:].astype(np.uint16)
    if folded:
        lo = np.where(hi & 1, 255 - lo, lo)
    return ((hi << 8) | lo).astype("<u2").tobytes()


def planes16(b, w, h, folded):
    """GRAY16_LE -> NV24: Y = high byte, U = low byte (folded or not), V = 128. Same geometry."""
    a = np.frombuffer(b, np.uint8).reshape(h * w, 2)        # [lo, hi] per pixel (little endian)
    hi = a[:, 1]
    lo = a[:, 0]
    if folded:
        lo = np.where(hi & 1, 255 - lo, lo).astype(np.uint8)
    uv = np.empty((h * w, 2), np.uint8)
    uv[:, 0] = lo
    uv[:, 1] = 128
    return hi.tobytes() + uv.tobytes()


def unplanes16(nv24, w, h, folded):
    y = np.frombuffer(nv24, np.uint8, w * h)
    uv = np.frombuffer(nv24, np.uint8, 2 * w * h, w * h).reshape(h * w, 2)
    lo = uv[:, 0]
    if folded:
        lo = np.where(y & 1, 255 - lo, lo).astype(np.uint8)
    out = np.empty((h * w, 2), np.uint8)
    out[:, 0] = lo
    out[:, 1] = y
    return out.tobytes(), bool((uv[:, 1] == 128).all())


# ---------------------------------------------------------------- pipeline pieces
def cfg(encoder, gop_s=0, threads=4):
    return types.SimpleNamespace(encoder=encoder, segment_seconds=3600, keyframe_interval_s=gop_s, bframes=0,
                                 nvenc_preset="", nvenc_maxperf=True, videoconvert_threads=threads,
                                 x264_crf=23, x264_preset="ultrafast")


def shipped(encoder, bits, loc, fps, gop_s=0):
    desc, resolved = rec.build_recorder_description(cfg(encoder, gop_s), bits, loc, float(fps), False, None)
    if resolved != encoder:
        raise RuntimeError(f"wanted {encoder}, recorder resolved {resolved}")
    return desc


def hw_tail(loc, gop=0, diag=False):
    """nvvidconv -> NVMM NV24 -> nvv4l2h265enc lossless -> h265parse -> splitmuxsink (the shipped tail)."""
    opts = " maxperf-enable=1" + (f" iframeinterval={gop}" if gop else "")
    q = (lambda n: f"queue name={n} max-size-buffers=4 ! ") if diag else (lambda n: "")
    return ("nvvidconv ! video/x-raw(memory:NVMM),format=NV24 ! " + q("q_nv") +
            f"nvv4l2h265enc enable-lossless=1{opts} ! " + q("q_enc") + "h265parse ! " + q("q_par") +
            rec._splitmux(loc, 3600))


NV24 = f"video/x-raw,format=NV24,colorimetry={LUMA_COLORIMETRY}"


def decode_iter(out_dir, chain, fmt):
    for path in sorted(glob.glob(out_dir + "/cam-*.mkv")):
        p = Gst.parse_launch(f'filesrc location="{path}" ! matroskademux ! {chain} ! videoconvert ! '
                             f'video/x-raw,format={fmt} ! appsink name=o sync=false max-buffers=4')
        sink = p.get_by_name("o")
        p.set_state(Gst.State.PLAYING)
        while True:
            sample = sink.emit("try-pull-sample", 30 * Gst.SECOND)
            if sample is None:
                break
            buf = sample.get_buffer()
            yield buf.extract_dup(0, buf.get_size())
        p.set_state(Gst.State.NULL)


def h265_chain():
    return raw_decode_chain("video/x-h265", {"recording_colorimetry": LUMA_COLORIMETRY})


# ---------------------------------------------------------------- the run
def run_variant(label, caps, fps, frames, n, make_desc, feed, verify, work, paced=True, keep=False):
    out = os.path.join(work, "v")
    shutil.rmtree(out, ignore_errors=True)
    os.makedirs(out)
    desc = make_desc(out + "/cam")
    blk = "false" if paced else "true"
    p = Gst.parse_launch(f'appsrc name=s format=time is-live=true block={blk} max-bytes={12 * len(frames[0]) * 3} '
                         f'caps="{caps},framerate={fps}/1" ! {desc}')
    src = p.get_by_name("s")
    bus = p.get_bus()
    # idle baseline for the system-wide number
    b0, t0 = sys_busy(), time.monotonic()
    time.sleep(2.0)
    idle_rate = (sys_busy() - b0) / (time.monotonic() - t0)
    p.set_state(Gst.State.PLAYING)
    sampler = Sampler()
    sampler.start()
    main_tid = threading.get_native_id()
    tm0 = os.times()
    sb0, w0 = sys_busy(), time.monotonic()
    late = 0
    err = None
    for i in range(n):
        if paced:
            due = w0 + i / fps
            now = time.monotonic()
            if due > now:
                time.sleep(due - now)
            elif now - due > 1.0 / fps:
                late += 1
        b = feed(frames[i % len(frames)])
        if not isinstance(b, Gst.Buffer):
            b = Gst.Buffer.new_wrapped(b)
        b.pts = i * Gst.SECOND // fps
        b.duration = Gst.SECOND // fps
        if src.emit("push-buffer", b) != Gst.FlowReturn.OK:
            err = "push-buffer failed"
            break
        if i % 8 == 0:
            m = bus.pop_filtered(Gst.MessageType.ERROR)
            if m is not None:
                e, dbg = m.parse_error()
                err = f"{e.message} ({(dbg or '').splitlines()[0][:160]})"
                break
            # a stalled pipeline (an element that refuses frames without posting an error) must not hang the bench
            if time.monotonic() - w0 > max(60.0, 6.0 * n / fps):
                err = "stalled: the feeder is far behind real time"
                break
    t_fed = time.monotonic() - w0
    msg = None
    if not err:
        src.emit("end-of-stream")
        msg = bus.timed_pop_filtered(90 * Gst.SECOND, Gst.MessageType.EOS | Gst.MessageType.ERROR)
    wall = time.monotonic() - w0
    sb1 = sys_busy()
    tm1 = os.times()
    thr = sampler.finish()
    if err:
        # do not wait on a wedged element: abandon the pipeline (the process exits at the end anyway)
        threading.Thread(target=lambda: p.set_state(Gst.State.NULL), daemon=True).start()
        time.sleep(2)
    else:
        p.set_state(Gst.State.NULL)
        if msg is None:
            err = "timeout waiting for EOS"
        elif msg.type != Gst.MessageType.EOS:
            e, dbg = msg.parse_error()
            err = f"{e.message} ({(dbg or '').splitlines()[0][:160]})"
    if err:
        print(f"\n### {label}: FAILED -- {err}")
        shutil.rmtree(out, ignore_errors=True)
        return None
    size = sum(os.path.getsize(f) for f in glob.glob(out + "/cam-*.mkv"))
    raw = n * len(frames[0])
    ms = 1000.0 / CLK / n
    proc_u = (tm1.user - tm0.user) * 1000 / n
    proc_s = (tm1.system - tm0.system) * 1000 / n
    sys_plus = (sb1 - sb0 - idle_rate * wall) * ms
    verdict = verify(out) if verify else ""
    print(f"\n### {label}")
    print(f"  {n} frames, fed in {t_fed:.1f}s ({n / t_fed:.1f} fps{' paced' if paced else ' unpaced'}"
          f"{f', {late} late pushes' if late else ''}), drained {wall:.1f}s")
    print(f"  size {size / raw * 100:6.1f}% of raw ({size / n / 1e6:.2f} MB/frame)   {verdict}")
    print(f"  CPU ms/frame: proc {proc_u + proc_s:6.2f} (user {proc_u:.2f} sys {proc_s:.2f})   "
          f"sys+ {sys_plus:6.2f}   outside-proc {sys_plus - proc_u - proc_s:6.2f}")
    rows = sorted(thr.items(), key=lambda kv: -(kv[1][0] + kv[1][1]))
    main_comm = None
    try:
        with open(f"/proc/self/task/{main_tid}/comm") as f:
            main_comm = f.read().strip()
    except OSError:
        pass
    for comm, (u, s, cnt) in rows:
        t = (u + s) * ms
        if t < 0.05:
            continue
        tag = " (feeder: main thread)" if comm == main_comm else ""
        print(f"    {comm:18s} x{cnt:<2d} {t:6.2f}  (user {u * ms:5.2f} sys {s * ms:5.2f}){tag}")
    if not keep:
        shutil.rmtree(out, ignore_errors=True)
    return {"label": label, "proc": proc_u + proc_s, "sys_plus": sys_plus, "size": size / raw,
            "fps": n / t_fed, "verdict": verdict}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", default="bayer", choices=("bayer", "gray16"))
    ap.add_argument("--variants", default="", help="comma list; empty = all of the set")
    ap.add_argument("--width", type=int)
    ap.add_argument("--height", type=int)
    ap.add_argument("--fps", type=int)
    ap.add_argument("--frames", type=int, default=480)
    ap.add_argument("--distinct", type=int, default=96, help="distinct frames generated, then cycled")
    ap.add_argument("--sigma", type=float)
    ap.add_argument("--content", default="scene", choices=("scene", "noise", "ramp"))
    ap.add_argument("--raw-file")
    ap.add_argument("--work", default="/work")
    ap.add_argument("--unpaced", action="store_true")
    ap.add_argument("--gop", type=int, default=0, help="iframeinterval in frames for hand-built hw tails")
    ap.add_argument("--keep", action="store_true")
    a = ap.parse_args()
    want = [v for v in a.variants.split(",") if v]
    paced = not a.unpaced
    results = []

    def go(name, *args, **kw):
        if want and name not in want:
            return
        try:
            r = run_variant(name, *args, work=a.work, paced=paced, keep=a.keep, **kw)
        except Exception as e:   # noqa: BLE001
            print(f"\n### {name}: FAILED -- {type(e).__name__}: {str(e)[:300]}")
            r = None
        if r:
            results.append(r)

    if a.set == "bayer":
        w, h, fps = a.width or 2448, a.height or 2048, a.fps or 24
        n = a.frames
        if a.raw_file:
            frames = raw_file_frames(a.raw_file, a.distinct, w, h, 1)
        elif a.content == "noise":
            frames = noise_frames(a.distinct, w, h)
        elif a.content == "ramp":
            frames = ramp_frames(a.distinct, w, h)
        else:
            frames = bayer_frames(a.distinct, w, h, a.sigma if a.sigma is not None else 2.5)
        print(f"# bayer set: {w}x{h}@{fps}, {n} frames ({len(frames)} distinct, {a.raw_file or a.content}), "
              f"{os.cpu_count()} CPUs, {Gst.version_string()}")
        g8 = f"video/x-raw,format=GRAY8,width={w},height={h}"
        nv = f"{NV24},width={w},height={h}"
        chroma_bytes = bytes([128]) * (2 * w * h)
        chroma = Gst.Memory.new_wrapped(Gst.MemoryFlags.READONLY, chroma_bytes, 2 * w * h, 0, None, None)

        def luma_check(expect_fn=lambda f: f, post=lambda g: g):
            def v(out):
                ok = cnt = 0
                note = ""
                for i, g in enumerate(decode_iter(out, h265_chain(), "GRAY8")):
                    cnt += 1
                    e = expect_fn(frames[i % len(frames)])
                    if i < n and post(g) == e:
                        ok += 1
                    elif not note and i < n:
                        ga = np.frombuffer(post(g), np.uint8).astype(np.int16)
                        ea = np.frombuffer(e, np.uint8).astype(np.int16)
                        if ga.size == ea.size:
                            d = ga - ea
                            # is the damage a per-code mapping (a range squeeze) or spatial?
                            lut_ok = all(np.unique(ga[ea == c]).size <= 1 for c in np.unique(ea)[:64])
                            note = (f"  [first bad frame {i}: {np.count_nonzero(d) * 100.0 / d.size:.1f}% px differ, "
                                    f"max|d| {np.abs(d).max()}, mean d {d.mean():+.2f}, out range {ga.min()}..{ga.max()} "
                                    f"vs in {ea.min()}..{ea.max()}, per-code mapping {'yes' if lut_ok else 'no'}]")
                        else:
                            note = f"  [size {ga.size} vs {ea.size}]"
                return f"bit-exact {ok}/{n}" + ("" if cnt == n else f" (decoded {cnt})") + note
            return v

        def nv24_2mem(f):
            b = Gst.Buffer.new_wrapped(f)
            b.append_memory(chroma)
            return b

        def nv24_2mem_meta(f):
            b = nv24_2mem(f)
            GstVideo.buffer_add_video_meta_full(b, GstVideo.VideoFrameFlags.NONE, GstVideo.VideoFormat.NV24,
                                                w, h, 2, [0, w * h, 0, 0], [w, 2 * w, 0, 0])
            return b

        def nv24_1mem(f):
            return f + chroma_bytes

        ident = lambda f: f   # noqa: E731
        tile = lambda f: bayer_tile.tile_cfa(f, w, h, "plain", "rggb")   # noqa: E731
        untile = lambda g: bayer_tile.untile_cfa(g, w, h, "plain", "rggb")   # noqa: E731

        go("A", g8, fps, frames, n, lambda loc: shipped("hw-hevc-lossless", 8, loc, fps), ident, luma_check())
        go("A-diag", g8, fps, frames, n,
           lambda loc: (f"queue max-size-buffers=12 name=rec_q ! videoconvert n-threads=4 ! {NV24} ! "
                        "queue name=q_vc max-size-buffers=4 ! " + hw_tail(loc, a.gop, diag=True)),
           ident, luma_check())
        go("A-1thr", g8, fps, frames, n,
           lambda loc: (f"queue max-size-buffers=12 name=rec_q ! videoconvert n-threads=1 ! {NV24} ! "
                        + hw_tail(loc, a.gop)), ident, luma_check())
        go("B", nv, fps, frames, n, lambda loc: "queue max-size-buffers=12 name=rec_q ! " + hw_tail(loc, a.gop),
           nv24_2mem, luma_check())
        go("B-meta", nv, fps, frames, n, lambda loc: "queue max-size-buffers=12 name=rec_q ! " + hw_tail(loc, a.gop),
           nv24_2mem_meta, luma_check())
        go("B-1mem", nv, fps, frames, n, lambda loc: "queue max-size-buffers=12 name=rec_q ! " + hw_tail(loc, a.gop),
           nv24_1mem, luma_check())
        go("B-diag", nv, fps, frames, n,
           lambda loc: "queue max-size-buffers=12 name=rec_q ! " + hw_tail(loc, a.gop, diag=True),
           nv24_2mem, luma_check())
        go("C", g8, fps, frames, n, lambda loc: "queue max-size-buffers=12 name=rec_q ! " + hw_tail(loc, a.gop),
           ident, luma_check())
        go("C2", g8, fps, frames, n,
           lambda loc: ("queue max-size-buffers=12 name=rec_q ! nvvidconv ! video/x-raw(memory:NVMM),format=I420 ! "
                        + hw_tail(loc, a.gop)), ident, luma_check())
        pin = ",colorimetry=" + LUMA_COLORIMETRY
        go("C3", g8 + pin, fps, frames, n,
           lambda loc: ("queue max-size-buffers=12 name=rec_q ! nvvidconv ! "
                        f"video/x-raw(memory:NVMM),format=I420{pin} ! nvvidconv ! "
                        f"video/x-raw(memory:NVMM),format=NV24{pin} ! " + hw_tail(loc, a.gop).split(" ! ", 2)[2]),
           ident, luma_check())
        go("C4", g8, fps, frames, n,
           lambda loc: ("queue max-size-buffers=12 name=rec_q ! nvvidconv ! video/x-raw(memory:NVMM),format=GRAY8 ! "
                        + hw_tail(loc, a.gop)), ident, luma_check())
        c420 = bytes([128]) * (w * h // 2)
        c420_mem = Gst.Memory.new_wrapped(Gst.MemoryFlags.READONLY, c420, len(c420), 0, None, None)

        def yuv420_2mem(f):
            b = Gst.Buffer.new_wrapped(f)
            b.append_memory(c420_mem)
            return b
        for fmt in ("NV12", "I420"):
            caps420 = f"video/x-raw,format={fmt},width={w},height={h}{pin}"
            go(f"D-{fmt}", caps420, fps, frames, n,
               lambda loc: "queue max-size-buffers=12 name=rec_q ! " + hw_tail(loc, a.gop), yuv420_2mem, luma_check())
            go(f"D-{fmt}-1mem", caps420, fps, frames, n,
               lambda loc: "queue max-size-buffers=12 name=rec_q ! " + hw_tail(loc, a.gop),
               lambda f: f + c420, luma_check())
        go("D-NV12-nopin", f"video/x-raw,format=NV12,width={w},height={h}", fps, frames, n,
           lambda loc: "queue max-size-buffers=12 name=rec_q ! " + hw_tail(loc, a.gop), yuv420_2mem, luma_check())
        go("D-NV12-diag", f"video/x-raw,format=NV12,width={w},height={h}{pin}", fps, frames, n,
           lambda loc: "queue max-size-buffers=12 name=rec_q ! " + hw_tail(loc, a.gop, diag=True),
           yuv420_2mem, luma_check())
        go("D-NV12+tile", f"video/x-raw,format=NV12,width={w},height={h}{pin}", fps, frames, n,
           lambda loc: "queue max-size-buffers=12 name=rec_q ! " + hw_tail(loc, a.gop),
           lambda f: yuv420_2mem(tile(f)), luma_check(post=untile))
        go("A+tile", g8, fps, frames, n, lambda loc: shipped("hw-hevc-lossless", 8, loc, fps), tile,
           luma_check(post=untile))
        go("B+tile", nv, fps, frames, n, lambda loc: "queue max-size-buffers=12 name=rec_q ! " + hw_tail(loc, a.gop),
           lambda f: nv24_2mem(tile(f)), luma_check(post=untile))
        go("A-gop", g8, fps, frames, n, lambda loc: shipped("hw-hevc-lossless", 8, loc, fps, gop_s=10), ident,
           luma_check())
        go("ffv1", g8, fps, frames, n, lambda loc: shipped("ffv1", 8, loc, fps), ident,
           lambda out: "bit-exact %d/%d" % (sum(g == frames[i % len(frames)] for i, g in
                                                 enumerate(decode_iter(out, FFV1_DECODER, "GRAY8"))), n))
        go("raw", g8, fps, frames, n,
           lambda loc: "queue max-size-buffers=12 name=rec_q ! " + rec._splitmux(loc, 3600), ident,
           lambda out: "bit-exact %d/%d" % (sum(g == frames[i % len(frames)] for i, g in
                                                 enumerate(decode_iter(out, "identity", "GRAY8"))), n))
        go("null", g8, fps, frames, n, lambda loc: "queue max-size-buffers=12 name=rec_q ! fakesink sync=false",
           ident, None)
    else:
        w, h, fps = a.width or 640, a.height or 512, a.fps or 60
        n = a.frames
        frames = (raw_file_frames(a.raw_file, a.distinct, w, h, 2) if a.raw_file
                  else thermal_frames(a.distinct, w, h, a.sigma if a.sigma is not None else 6.0))
        print(f"# gray16 set: {w}x{h}@{fps}, {n} frames ({len(frames)} distinct, "
              f"{a.raw_file or 'synthetic'}), {os.cpu_count()} CPUs, {Gst.version_string()}")
        g16 = f"video/x-raw,format=GRAY16_LE,width={w},height={h}"
        g8w = f"video/x-raw,format=GRAY8,width={2 * w},height={h}"
        nvw = f"{NV24},width={2 * w},height={h}"
        nvp = f"{NV24},width={w},height={h}"
        chroma_bytes = bytes([128]) * (2 * 2 * w * h)
        chroma = Gst.Memory.new_wrapped(Gst.MemoryFlags.READONLY, chroma_bytes, len(chroma_bytes), 0, None, None)

        def exact(decoder, fmt, post=lambda g: g):
            def v(out):
                ok = cnt = 0
                for i, g in enumerate(decode_iter(out, decoder, fmt)):
                    cnt += 1
                    if i < n and post(g) == frames[i % len(frames)]:
                        ok += 1
                return f"bit-exact {ok}/{n}" + ("" if cnt == n else f" (decoded {cnt})")
            return v

        def planes_check(folded):
            def v(out):
                ok = cnt = vok = 0
                # decode to planar Y444 (avdec's native 4:4:4), compare all planes
                for i, g in enumerate(decode_iter(out, "h265parse ! avdec_h265", "Y444")):
                    cnt += 1
                    y = np.frombuffer(g, np.uint8, w * h)
                    u = np.frombuffer(g, np.uint8, w * h, w * h)
                    vv = np.frombuffer(g, np.uint8, w * h, 2 * w * h)
                    lo = np.where(y & 1, 255 - u, u).astype(np.uint8) if folded else u
                    o = np.empty((h * w, 2), np.uint8)
                    o[:, 0] = lo
                    o[:, 1] = y
                    if i < n and o.tobytes() == frames[i % len(frames)]:
                        ok += 1
                    vok += bool((vv == 128).all())
                return (f"bit-exact {ok}/{n}" + ("" if cnt == n else f" (decoded {cnt})")
                        + f"  V-plane constant {vok}/{cnt}")
            return v

        ident = lambda f: f   # noqa: E731
        go("ffv1", g16, fps, frames, n, lambda loc: shipped("ffv1", 16, loc, fps), ident,
           exact(FFV1_DECODER, "GRAY16_LE"))
        go("raw", g16, fps, frames, n,
           lambda loc: "queue max-size-buffers=12 name=rec_q ! " + rec._splitmux(loc, 3600), ident,
           exact("identity", "GRAY16_LE"))
        for folded in (False, True):
            tag = "fold" if folded else "plain"
            go(f"split-{tag}", g8w, fps, frames, n, lambda loc: shipped("hw-hevc-lossless", 8, loc, fps),
               lambda f, fo=folded: split16(f, w, h, fo),
               exact(h265_chain(), "GRAY8", post=lambda g, fo=folded: join16(g, w, h, fo)))

        keep = []

        def split_nv24(f, folded):
            b = Gst.Buffer.new_wrapped(split16(f, w, h, folded))
            b.append_memory(chroma)
            return b
        for folded in (False, True):
            tag = "fold" if folded else "plain"
            go(f"splitB-{tag}", nvw, fps, frames, n,
               lambda loc: "queue max-size-buffers=12 name=rec_q ! " + hw_tail(loc, a.gop),
               lambda f, fo=folded: split_nv24(f, fo),
               exact(h265_chain(), "GRAY8", post=lambda g, fo=folded: join16(g, w, h, fo)))
            c420w = bytes([128]) * (2 * w * h // 2)
            c420w_mem = Gst.Memory.new_wrapped(Gst.MemoryFlags.READONLY, c420w, len(c420w), 0, None, None)
            keep.append((c420w, c420w_mem))

            def split_nv12(f, fo=folded, mem=c420w_mem):
                b = Gst.Buffer.new_wrapped(split16(f, w, h, fo))
                b.append_memory(mem)
                return b
            go(f"splitD-{tag}", f"video/x-raw,format=NV12,width={2 * w},height={h},colorimetry={LUMA_COLORIMETRY}",
               fps, frames, n, lambda loc: "queue max-size-buffers=12 name=rec_q ! " + hw_tail(loc, a.gop),
               split_nv12, exact(h265_chain(), "GRAY8", post=lambda g, fo=folded: join16(g, w, h, fo)))
            go(f"planes-{tag}", nvp, fps, frames, n,
               lambda loc: "queue max-size-buffers=12 name=rec_q ! " + hw_tail(loc, a.gop),
               lambda f, fo=folded: planes16(f, w, h, fo), planes_check(folded))
        go("null", g16, fps, frames, n, lambda loc: "queue max-size-buffers=12 name=rec_q ! fakesink sync=false",
           ident, None)

    print("\n# summary")
    print(f"  {'variant':14s} {'proc ms':>8s} {'sys+ ms':>8s} {'size':>7s} {'fps':>6s}  result")
    for r in results:
        print(f"  {r['label']:14s} {r['proc']:8.2f} {r['sys_plus']:8.2f} {r['size'] * 100:6.1f}% {r['fps']:6.1f}  {r['verdict']}")


if __name__ == "__main__":
    main()
