#!/usr/bin/env python3
"""WebRTC lab producer: a stripped `webrtcsink` bridge for answering the W2 / W3 / W5 questions on a Jetson.

Not the product bridge (plugins/webrtc-bridge/tools/bridge_stream.py): it carries only what the experiments
need, and prints the sink's own view of every session every 2 s. Run inside the webrtc-bridge image, with a
signalling server already listening on LAB_PORT:

    gst-webrtc-signalling-server --host 0.0.0.0 --port 8450 &
    LAB_MODE=shared python3 docs/encoding-optimization/scripts/webrtc_lab.py

  LAB_HEAD          gst fragment producing raw video (default: the core's unixfd endpoint, to I420)
  LAB_MODE          per-viewer  raw into webrtcsink, one encoder per session (the shipped shape)
                    shared      ONE encoder + parser ahead of webrtcsink; every session shares it (W3)
  LAB_CODEC         h264 | h265
  LAB_ENCODER       nv | sw     nvv4l2h26Xenc, or x264enc / x265enc (shared mode)
  LAB_BITRATE       bit/s of the shared encoder (default 4000000)
  LAB_GOP           keyframe interval in frames (default 60)
  LAB_FORCE_IDR     1 = turn force-key-unit requests into the encoder's `force-IDR` action, rate limited
  LAB_IDR_MIN_S     minimum spacing of those forced IDRs (default 1.0)
  LAB_SINK_PROPS    extra webrtcsink properties, e.g. "min-bitrate=500000"
  LAB_MAX_SESSIONS  reaper, cap policy: end the oldest sessions beyond this many (0 = off)
  LAB_STALE_S       reaper, liveness policy: end a session whose receiver feedback has not changed for this
                    many seconds (0 = off)
  LAB_STATS         1 = print each session's raw stats structure once (field discovery)
  LAB_DIFF          1 = print which stats fields changed since the previous tick (what freezes in a blackout)
  LAB_PORT          signalling port (default 8443)

What it established on an R39 AGX Orin is written up in ../RESULTS.md.
"""
import os
import re
import sys
import time
import traceback

import gi
gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # noqa: E402

Gst.init(None)
E = os.environ.get
PORT = E("LAB_PORT", "8443")
MODE = E("LAB_MODE", "per-viewer")
CODEC = E("LAB_CODEC", "h264")
ENC = E("LAB_ENCODER", "nv")
BITRATE = int(E("LAB_BITRATE", "4000000"))
GOP = int(E("LAB_GOP", "60"))
MAX_SESS = int(E("LAB_MAX_SESSIONS", "0"))
STALE_S = float(E("LAB_STALE_S", "0"))
IDR_MIN_S = float(E("LAB_IDR_MIN_S", "1.0"))
HEAD = E("LAB_HEAD", "unixfdsrc name=cam_src socket-path=/tmp/cam/unixfd ! queue leaky=downstream max-size-buffers=2 ! "
                     "videoconvert ! video/x-raw,format=I420")
T0 = time.monotonic()


def log(msg):
    print(f"[lab {time.monotonic() - T0:7.1f}] {msg}", flush=True)


def shared_encoder():
    """One encoder + parser for every viewer. Two things webrtcsink needs from a pre-encoded input:

    * COMPLETE caps the first time. h26Xparse announces caps when the encoder's arrive and again once it has
      parsed the SPS (profile/level, then chroma-format, bit depths, coded-picture-structure). webrtcsink
      refuses the second announcement ("Renegotiation is not supported") and the pipeline dies
      not-negotiated. Pinning profile + level after the encoder and declaring the remaining fields with
      `capssetter` makes the second announcement identical to the first, so it is never sent.
    * A keyframe when a viewer joins or reports loss -- see the force-IDR hook below."""
    extra = E("LAB_ENC_EXTRA", "")
    done = ('capssetter join=true caps="video/x-%s,coded-picture-structure=(string)frame,'
            'chroma-format=(string)4:2:0,bit-depth-luma=(uint)8,bit-depth-chroma=(uint)8"' % CODEC)
    if CODEC == "h264":
        pin = E("LAB_ENC_CAPS", "video/x-h264,profile=high,level=(string)4,stream-format=byte-stream,alignment=au")
        if ENC == "nv":
            return ("nvvidconv ! video/x-raw(memory:NVMM),format=NV12 ! "
                    f"nvv4l2h264enc name=shared_enc bitrate={BITRATE} iframeinterval={GOP} idrinterval={GOP} "
                    f"insert-sps-pps=true maxperf-enable=true num-B-Frames=0 {extra} ! {pin} ! "
                    f"h264parse config-interval=-1 ! {done}")
        return (f"x264enc name=shared_enc tune=zerolatency speed-preset=ultrafast bitrate={BITRATE // 1000} "
                f"key-int-max={GOP} bframes=0 {extra} ! {pin} ! h264parse config-interval=-1 ! {done}")
    if ENC == "nv":
        return ("nvvidconv ! video/x-raw(memory:NVMM),format=NV12 ! "
                f"nvv4l2h265enc name=shared_enc bitrate={BITRATE} iframeinterval={GOP} idrinterval={GOP} "
                f"insert-sps-pps=true maxperf-enable=true {extra} ! h265parse config-interval=-1 ! {done}")
    return (f"x265enc name=shared_enc tune=zerolatency speed-preset=ultrafast bitrate={BITRATE // 1000} "
            f"key-int-max={GOP} {extra} ! h265parse config-interval=-1 ! {done}")


sink = f"webrtcsink name=ws signaller::uri=ws://127.0.0.1:{PORT} " + E("LAB_SINK_PROPS", "")
desc = f"{HEAD} ! {shared_encoder()} ! {sink}" if MODE == "shared" else f"{HEAD} ! {sink} video-caps=video/x-{CODEC}"
log(f"mode={MODE} codec={CODEC} enc={ENC}")
log("pipeline: " + desc)
pipeline = Gst.parse_launch(desc)
ws = pipeline.get_by_name("ws")
ws.set_property("meta", Gst.Structure.new_from_string("meta,name=lab"))
signaller = ws.get_property("signaller")
shared = pipeline.get_by_name("shared_enc")
sessions = {}      # session id -> {first seen, feedback key, when it last changed, previous flat stats}
state = {"added": 0, "removed": 0, "fku": 0, "idr": 0, "idr_last": 0.0, "idr_pending": False,
         "key": 0, "delta": 0, "bytes": 0, "dumped": set()}


def on_added(_s, peer, _bin):
    state["added"] += 1
    log(f"consumer-added {peer}")


def on_removed(_s, peer, _bin):
    state["removed"] += 1
    log(f"consumer-removed {peer}")


ws.connect("consumer-added", on_added)
ws.connect("consumer-removed", on_removed)


def on_encoder_setup(_s, consumer_id, codec, enc):
    """per-viewer mode: the same low-latency / short-GOP settings the product bridge applies."""
    fac = enc.get_factory()
    log(f"encoder-setup consumer={consumer_id} codec={codec} encoder={fac.get_name() if fac else '?'}")
    for prop, val in (("iframeinterval", GOP), ("idrinterval", GOP), ("insert-sps-pps", True),
                      ("maxperf-enable", True), ("key-int-max", GOP)):
        if enc.find_property(prop) is not None:
            try:
                enc.set_property(prop, val)
            except Exception:   # noqa: BLE001
                pass
    return False


ws.connect_after("encoder-setup", on_encoder_setup)

if shared is not None:
    def emit_idr():
        state["idr_pending"] = False
        state["idr_last"] = time.monotonic()
        state["idr"] += 1
        try:
            shared.emit("force-IDR")
        except Exception as e:   # noqa: BLE001 -- the software encoders act on the event itself
            log(f"force-IDR not available on this encoder: {e}")
        return False

    def on_up_event(_pad, info):
        """webrtcsink asks for a keyframe (GstForceKeyUnit, upstream) when a session's ICE completes and on
        every PLI/FIR. nvv4l2h26Xenc does not act on the event, so a joining viewer waits for the next
        periodic IDR; its `force-IDR` action does the job. Rate limited: one join sends dozens of requests,
        and with a shared encoder every IDR goes to every viewer."""
        ev = info.get_event()
        if ev is not None and ev.type == Gst.EventType.CUSTOM_UPSTREAM:
            st = ev.get_structure()
            if st is not None and st.get_name() == "GstForceKeyUnit":
                state["fku"] += 1
                if E("LAB_FORCE_IDR") == "1" and not state["idr_pending"]:
                    state["idr_pending"] = True
                    wait = max(0.0, IDR_MIN_S - (time.monotonic() - state["idr_last"]))
                    GLib.timeout_add(int(wait * 1000), emit_idr)
        return Gst.PadProbeReturn.OK

    def on_enc_buf(_pad, info):
        b = info.get_buffer()
        if b is not None:
            state["bytes"] += b.get_size()
            state["delta" if b.has_flags(Gst.BufferFlags.DELTA_UNIT) else "key"] += 1
        return Gst.PadProbeReturn.OK

    pad = shared.get_static_pad("src")
    pad.add_probe(Gst.PadProbeType.EVENT_UPSTREAM, on_up_event)
    pad.add_probe(Gst.PadProbeType.BUFFER, on_enc_buf)


def flatten(st, path="", out=None):
    """Every scalar of a (nested) stats structure as {path: str(value)}; the sample timestamps left out."""
    out = {} if out is None else out
    for i in range(st.n_fields()):
        f = st.nth_field_name(i)
        if f == "timestamp":
            continue
        try:
            v = st.get_value(f)
        except Exception:   # noqa: BLE001 -- PyGObject cannot marshal every field type (GstValueArray)
            continue
        if isinstance(v, Gst.Structure):
            flatten(v, f"{path}/{f}" if path else f, out)
        else:
            out[f"{path}/{f}"] = str(v)
    return out


def short(key):
    parts = key.split("/")
    head = parts[0].split("_")[0].replace("rtp-", "").replace("-stream-stats", "").replace("ice-candidate-", "")
    return head + ":" + "/".join(parts[1:]).replace("gst-rtpsource-stats/", "src.").replace("gst-twcc-stats/", "twcc.")


def receiver_key(flat):
    """What only moves while the RECEIVER is talking to us: the transport-wide congestion-control feedback
    on the candidate pair (changes at every sample) and the RTCP receiver-report block of the remote
    source (about every 5 s). Sender-side counters (bytes/packets sent, SR fields) keep moving into a dead
    link and must not be part of it."""
    return tuple(sorted((k, v) for k, v in flat.items()
                        if "gst-twcc-stats/" in k or "/rb-" in k or k.endswith("/round-trip-time")))


def end_session(sid, why):
    """Both signals, in this order. `end-session` (an action) has the signaller tell the server, which tells
    the viewer; on its own it leaves the session in the sink. `session-ended` is what the signaller emits
    when the far side ends a session, and the sink's handler for it is what removes the session and releases
    its encoder -- measured: emitting only the first leaves `get-sessions` unchanged."""
    log(f"REAP {sid}: {why}")
    for sig in ("end-session", "session-ended"):
        try:
            signaller.emit(sig, sid)
        except Exception as e:   # noqa: BLE001
            log(f"  {sig} failed: {e}")


cpu = {"t": os.times(), "w": time.monotonic()}


def tick():
    now = time.monotonic()
    ids = list(ws.emit("get-sessions"))
    stats = ws.get_property("stats")
    t = os.times()
    pct = ((t.user - cpu["t"].user) + (t.system - cpu["t"].system)) / (now - cpu["w"]) * 100
    cpu["t"], cpu["w"] = t, now
    extra = ""
    if shared is not None:
        extra = (f" shared-enc key={state['key']} delta={state['delta']} fku-events={state['fku']} "
                 f"forced-idr={state['idr']} out={state['bytes'] / 1e6:.1f}MB")
    log(f"sessions={len(ids)} added={state['added']} removed={state['removed']} cpu={pct:5.1f}%{extra}")
    for sid in ids:
        s = sessions.setdefault(sid, {"first": now, "key": None, "key_at": now, "flat": {}})
        ss = stats.get_value(sid) if stats is not None and stats.has_field(sid) else None
        if ss is None:
            continue
        if E("LAB_STATS") == "1" and sid not in state["dumped"] and now - s["first"] > 6:
            state["dumped"].add(sid)
            log("  RAW STATS " + ss.to_string()[:6000])
        flat = flatten(ss)
        if E("LAB_DIFF") == "1":
            changed = sorted(short(k) for k in flat if s["flat"].get(k) != flat[k])
            log(f"  {sid[:8]} changed[{len(changed)}]: " + " ".join(changed)[:1500])
        s["flat"] = flat
        key = receiver_key(flat)
        if key != s["key"]:
            s["key"], s["key_at"] = key, now
        quiet = now - s["key_at"]
        # the encoder's congestion-controlled target lives in an array PyGObject cannot marshal: read the text
        raw = ss.to_string().replace("\\", "")
        m = re.search(r"consumer-stats.*", raw)
        target = re.search(r"bitrate=\(\w+\)(\d+)", m.group(0)) if m else None
        fec = re.search(r"fec-percentage=\(\w+\)(\d+)", m.group(0)) if m else None
        tw = {k.rsplit("/", 1)[-1]: v for k, v in flat.items() if "gst-twcc-stats/" in k}
        log(f"  {sid[:8]} age={now - s['first']:5.1f}s feedback-quiet={quiet:4.1f}s "
            f"enc-bitrate={target.group(1) if target else '?'} fec={fec.group(1) if fec else '?'} "
            f"twcc-loss={tw.get('packet-loss-pct', '?')[:5]} twcc-recv-bps={tw.get('bitrate-recv', '?')}")
        if STALE_S and quiet > STALE_S and now - s["first"] > STALE_S:
            end_session(sid, f"no receiver feedback for {quiet:.0f}s")
    for sid in [k for k in sessions if k not in ids]:
        del sessions[sid]
    if MAX_SESS and len(ids) > MAX_SESS:
        for sid in sorted(ids, key=lambda k: sessions.get(k, {"first": now})["first"])[:len(ids) - MAX_SESS]:
            end_session(sid, f"cap {MAX_SESS} exceeded")


def safe_tick():
    try:
        tick()
    except Exception:   # noqa: BLE001 -- a bench tool must keep printing
        log("tick failed: " + traceback.format_exc()[-600:])
    return True


def on_bus(_bus, msg):
    if msg.type == Gst.MessageType.ERROR:
        err, dbg = msg.parse_error()
        log(f"ERROR from {msg.src.get_name() if msg.src else '?'}: {err.message} ({(dbg or '').splitlines()[0][:200]})")
        loop.quit()
    elif msg.type == Gst.MessageType.WARNING:
        log(f"WARNING from {msg.src.get_name() if msg.src else '?'}: {msg.parse_warning()[0].message}")
    return True


bus = pipeline.get_bus()
bus.add_signal_watch()
bus.connect("message", on_bus)
loop = GLib.MainLoop()
GLib.timeout_add_seconds(2, safe_tick)
pipeline.set_state(Gst.State.PLAYING)
try:
    loop.run()
except KeyboardInterrupt:
    pass
finally:
    pipeline.set_state(Gst.State.NULL)
sys.exit(0)
