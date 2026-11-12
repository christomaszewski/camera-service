#!/usr/bin/env python3
"""Long-running headless WebRTC viewer for bench work: webrtcsrc -> decode -> count.

    webrtc_viewer.py <signalling uri> <seconds> [tag]

Prints `t=<s> fps=<frames decoded in the last second> total=<n>` once a second, the time to the first
decoded frame, and every gap longer than 1 s (a frozen picture), so a dropout and the recovery after it can
be read from the log. Runs inside the webrtc-bridge image. The `videoconvert` matters: an appsink alone
accepts application/x-rtp, and webrtcsrc then hands over RTP packets instead of decoding.

To put a viewer behind an impaired link, run it in its own container on the Docker bridge network and the
producer in another (not host-networked: libnice's UDP checks from a host-networked producer to a container
on the same host go unanswered and ICE falls back to TCP, which hides packet loss); then drop or rate-limit
between the two container addresses in the FORWARD chain (`-m statistic --mode random --probability p`,
`tc ... tbf`). The R39 kernel has no netem.
"""
import sys
import time

import gi
gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # noqa: E402

Gst.init(None)
URI = sys.argv[1] if len(sys.argv) > 1 else "ws://127.0.0.1:8443"
DUR = float(sys.argv[2]) if len(sys.argv) > 2 else 60
TAG = sys.argv[3] if len(sys.argv) > 3 else "c"

pipeline = Gst.Pipeline.new("viewer")
src = Gst.ElementFactory.make("webrtcsrc", "src")
src.set_property("connect-to-first-producer", True)
src.get_property("signaller").set_property("uri", URI)
queue = Gst.ElementFactory.make("queue", "q")
conv = Gst.ElementFactory.make("videoconvert", "conv")   # forces webrtcsrc to DECODE (appsink alone takes RTP)
sink = Gst.ElementFactory.make("appsink", "sink")
sink.set_property("emit-signals", True)
sink.set_property("max-buffers", 4)
sink.set_property("drop", True)
sink.set_property("sync", False)
for e in (src, queue, conv, sink):
    pipeline.add(e)
queue.link(conv)
conv.link(sink)

T0 = time.monotonic()
st = {"n": 0, "sec": 0, "last": None, "first": None, "gap_open": False}
loop = GLib.MainLoop()


def now():
    return time.monotonic() - T0


def on_sample(appsink):
    sample = appsink.emit("pull-sample")
    if sample is None:
        return Gst.FlowReturn.OK
    t = now()
    if st["first"] is None:
        st["first"] = t
        caps = sample.get_caps()
        print(f"[{TAG}] first frame at t={t:.2f}s caps={caps.to_string() if caps else '?'}", flush=True)
    elif st["last"] is not None and t - st["last"] > 1.0:
        print(f"[{TAG}] GAP {t - st['last']:.2f}s ended at t={t:.2f}s", flush=True)
    st["last"] = t
    st["n"] += 1
    st["sec"] += 1
    return Gst.FlowReturn.OK


sink.connect("new-sample", on_sample)


def on_pad_added(_el, pad):
    caps = pad.get_current_caps() or pad.query_caps(None)
    print(f"[{TAG}] pad added at t={now():.2f}s: {caps.to_string()[:160] if caps else '?'}", flush=True)
    sp = queue.get_static_pad("sink")
    if not sp.is_linked():
        pad.link(sp)


src.connect("pad-added", on_pad_added)


def on_bus(_bus, msg):
    if msg.type == Gst.MessageType.ERROR:
        err, dbg = msg.parse_error()
        print(f"[{TAG}] ERROR at t={now():.2f}s: {err.message} ({(dbg or '').splitlines()[0][:200]})", flush=True)
        loop.quit()
    elif msg.type == Gst.MessageType.EOS:
        print(f"[{TAG}] EOS at t={now():.2f}s", flush=True)
        loop.quit()
    return True


bus = pipeline.get_bus()
bus.add_signal_watch()
bus.connect("message", on_bus)


def tick():
    print(f"[{TAG}] t={now():5.1f} fps={st['sec']:3d} total={st['n']}", flush=True)
    st["sec"] = 0
    if now() >= DUR:
        loop.quit()
        return False
    return True


GLib.timeout_add_seconds(1, tick)
pipeline.set_state(Gst.State.PLAYING)
try:
    loop.run()
finally:
    pipeline.set_state(Gst.State.NULL)
print(f"[{TAG}] done: {st['n']} frames, first at {st['first']}", flush=True)
