#!/usr/bin/env python3
"""Headless RTSP test consumer (no player UI).

Connects to the bridge with `rtspsrc`, decodes (decodebin -> codec-agnostic: the same counter
validates h264 and h265 scenarios), and counts frames -- the loopback that proves the whole egress
path:  core endpoint -> rtsp-bridge (GstRtspServer) -> rtspsrc ! decodebin ! appsink.

TCP-interleaved transport so it works in any netns without RTP/UDP port juggling.

Usage: rtsp_consumer.py [rtsp_url] [target_frames] [timeout_s]
Exit 0 if >= target_frames decoded video frames arrive, else 1 (timeout/error) / 2 (no element).
"""
import sys

import gi
gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst

Gst.init(None)

URL = sys.argv[1] if len(sys.argv) > 1 else "rtsp://127.0.0.1:8554/camera"
TARGET = int(sys.argv[2]) if len(sys.argv) > 2 else 30
TIMEOUT_S = int(sys.argv[3]) if len(sys.argv) > 3 else 40

for el in ("rtspsrc", "decodebin"):
    if Gst.ElementFactory.find(el) is None:
        print(f"ERROR: {el} element not available", file=sys.stderr)
        sys.exit(2)

# parse_launch resolves rtspsrc/decodebin's dynamic pads with delayed linking.
pipeline = Gst.parse_launch(
    f"rtspsrc location={URL} protocols=tcp latency=200 ! decodebin ! videoconvert ! "
    f"appsink name=sink emit-signals=true max-buffers=4 drop=true sync=false")
sink = pipeline.get_by_name("sink")

state = {"frames": 0}
loop = GLib.MainLoop()


def on_sample(appsink):
    sample = appsink.emit("pull-sample")
    if sample:
        state["frames"] += 1
        if state["frames"] == 1:
            caps = sample.get_caps()
            print(f"first decoded frame; caps={caps.to_string() if caps else '?'}", flush=True)
    return Gst.FlowReturn.OK


sink.connect("new-sample", on_sample)


def on_bus(_bus, msg):
    if msg.type == Gst.MessageType.ERROR:
        err, dbg = msg.parse_error()
        print(f"ERROR: {err} ({dbg})", file=sys.stderr, flush=True)
        loop.quit()
    elif msg.type == Gst.MessageType.EOS:
        print("EOS", flush=True)
        loop.quit()
    return True


bus = pipeline.get_bus()
bus.add_signal_watch()
bus.connect("message", on_bus)


def poll():
    if state["frames"] >= TARGET:
        loop.quit()
        return False
    return True


def on_timeout():
    print(f"TIMEOUT after {TIMEOUT_S}s: {state['frames']} frames", file=sys.stderr, flush=True)
    loop.quit()
    return False


GLib.timeout_add(200, poll)
GLib.timeout_add_seconds(TIMEOUT_S, on_timeout)

pipeline.set_state(Gst.State.PLAYING)
try:
    loop.run()
finally:
    pipeline.set_state(Gst.State.NULL)

ok = state["frames"] >= TARGET
print(f"RESULT: {'PASS' if ok else 'FAIL'} ({state['frames']}/{TARGET} decoded frames)", flush=True)
sys.exit(0 if ok else 1)
