#!/usr/bin/env python3
"""Headless WebRTC test consumer (no browser).

Connects to the bridge's signalling server with `webrtcsrc`, pulls the decoded
stream, and counts frames. This is the gst-plugins-rs loopback that validates the
whole path without a browser:

    webrtcsink  ->  gst-webrtc-signalling-server  ->  webrtcsrc ! videoconvert ! appsink

Usage: webrtc_consumer.py [signalling_uri] [target_frames] [timeout_s]
Exit 0 if >= target_frames decoded video frames arrive, else 1 (timeout) / 2 (no element).
"""
import sys
import gi
gi.require_version("Gst", "1.0")
from gi.repository import Gst, GLib

Gst.init(None)

SIG_URI = sys.argv[1] if len(sys.argv) > 1 else "ws://127.0.0.1:8443"
TARGET = int(sys.argv[2]) if len(sys.argv) > 2 else 30
TIMEOUT_S = int(sys.argv[3]) if len(sys.argv) > 3 else 40

pipeline = Gst.Pipeline.new("webrtc-consumer")
src = Gst.ElementFactory.make("webrtcsrc", "src")
if src is None:
    print("ERROR: webrtcsrc element not available", file=sys.stderr)
    sys.exit(2)

# Grab whatever single producer is registered (no peer-id juggling needed for the test).
src.set_property("connect-to-first-producer", True)
# Point the signaller at our server (default is already ws://127.0.0.1:8443).
try:
    src.get_property("signaller").set_property("uri", SIG_URI)
except Exception as e:  # noqa: BLE001 - non-fatal; fall back to the default uri
    print(f"WARN: could not set signaller uri ({e}); using default", file=sys.stderr)

queue = Gst.ElementFactory.make("queue", "q")
conv = Gst.ElementFactory.make("videoconvert", "conv")
sink = Gst.ElementFactory.make("appsink", "sink")
sink.set_property("emit-signals", True)
sink.set_property("max-buffers", 4)
sink.set_property("drop", True)
sink.set_property("sync", False)

for e in (src, queue, conv, sink):
    pipeline.add(e)
queue.link(conv)
conv.link(sink)

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


def on_pad_added(element, pad):
    caps = pad.get_current_caps() or pad.query_caps(None)
    print(f"webrtcsrc pad added: {caps.to_string() if caps else '?'}", flush=True)
    sinkpad = queue.get_static_pad("sink")
    if not sinkpad.is_linked():
        res = pad.link(sinkpad)
        if res != Gst.PadLinkReturn.OK:
            print(f"ERROR: pad link failed: {res.value_nick}", file=sys.stderr, flush=True)


src.connect("pad-added", on_pad_added)


def on_bus(bus, msg):
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
