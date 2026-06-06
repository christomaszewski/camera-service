#!/usr/bin/env python3
"""WebRTC bridge launcher: run the GStreamer pipeline AND, once it is streaming, advertise this stream
over Zenoh for fleet discovery.

It replaces a bare `gst-launch-1.0` so the advertiser shares the bridge PROCESS — the Zenoh liveliness
token then lives exactly as long as this process: a crash/kill auto-withdraws presence, a graceful
SIGINT/SIGTERM undeclares it. run.sh builds the pipeline string (naming the source `cam_src` and the
sink `cam_webrtcsink`) and passes it in CAM_PIPELINE, so this owns the SAME pipeline gst-launch would.

Separation of concerns (so the generic half is liftable by the next producer):
  - zenoh_advertiser.StreamAdvertiser : GENERIC  — session + liveliness token + descriptor queryable.
  - this file                         : WebRTC   — BUILDS the abstract descriptor from env + negotiated
                                                   caps and ties advertise()/close() to PLAYING / shutdown.

Discovery is additive + best-effort: CAM_ADVERTISE=0 disables it; any Zenoh error is logged and the
video keeps flowing. CAM_LAUNCHER=gst-launch (handled in run.sh) bypasses this entirely.
"""
import json
import logging
import os
import signal
import socket
import sys

import gi
gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from zenoh_advertiser import StreamAdvertiser

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("bridge_stream")


def _env(name, default=None):
    v = os.environ.get(name)
    return v if v not in (None, "") else default


def _truthy(v):
    return str(v).strip().lower() in ("1", "true", "yes", "on")


# VIDEO_CAPS hint -> descriptor codec (a HINT only; WebRTC negotiates the real codec in SDP).
_CODEC_FROM_CAPS = {"video/x-h264": "h264", "video/x-h265": "h265",
                    "video/x-vp8": "vp8", "video/x-vp9": "vp9", "video/x-av1": "av1"}


def vehicle_id():
    return _env("VEHICLE_ID", socket.gethostname())


def sensor_id():
    return _env("CAM_INSTANCE", "camera")


def producer_id():
    return _env("CAM_PRODUCER_ID", "{}-{}".format(vehicle_id(), sensor_id()))


def signalling_url():
    url = _env("CAM_SIGNALLING_URL")
    if url:
        return url
    scheme = _env("CAM_SIGNALLING_SCHEME", "ws")            # bundled signalling server is plain ws (no --cert)
    host = _env("CAM_SIGNALLING_HOST", socket.gethostname())
    port = _env("SIGNALLING_PORT", "8443")
    return "{}://{}:{}".format(scheme, host, port)


def zenoh_connect():
    # Unset -> the vehicle's local zenohd; explicitly empty -> scout only.
    raw = os.environ.get("ZENOH_CONNECT", "tcp/localhost:7447")
    return [e.strip() for e in raw.split(",") if e.strip()]


def base_descriptor():
    """Fields the bridge knows from config alone (dims/format/fps are filled from caps at PLAYING)."""
    d = {
        "schema_version": 1,
        "id": sensor_id(),                                  # matches the key's <sensor_id> segment
        "role": _env("CAM_STREAM_ROLE", sensor_id()),      # human label; config-supplied, default = id
        "producer": "camera-service",
        "protocol": _env("CAM_SIGNALLING_PROTOCOL", "gstwebrtc-api"),
        "signalling": signalling_url(),
        "producer_id": producer_id(),                       # == webrtcsink meta.name (selector on a shared server)
    }
    codec = _CODEC_FROM_CAPS.get((_env("VIDEO_CAPS", "") or "").split(",")[0].strip())
    if codec:                                               # omit unless a codec is actually pinned
        d["codec"] = codec
    topic = _env("CAM_ROS_TOPIC")                          # OPTIONAL config-supplied linkage
    if topic:
        d["ros_topic"] = topic if topic.startswith("/") else "/" + topic
    rec = _env("CAM_RECORDING_GLOB")                       # OPTIONAL config-supplied linkage
    if rec:
        d["recording"] = rec
    return d


def fill_dims_from_caps(d, src):
    """Populate width/height/fps/pixel_format from the negotiated SOURCE caps — accurate on BOTH the JP6
    raw-shm path (caps set from config) and the JP7 unixfd path (geometry self-described by the stream,
    not in env). Falls back to CAM_WIDTH/HEIGHT/FPS/FORMAT when the caps can't be read."""
    w = h = fps = pix = None
    try:
        pad = src.get_static_pad("src") if src is not None else None
        caps = pad.get_current_caps() if pad is not None else None
        if caps is not None and caps.get_size() > 0:
            st = caps.get_structure(0)
            ok, val = st.get_int("width"); w = val if ok else None
            ok, val = st.get_int("height"); h = val if ok else None
            ok, num, den = st.get_fraction("framerate")
            if ok and den and num:
                fps = round(num / den)
            fmt = st.get_string("format")
            if st.get_name() == "video/x-bayer" and fmt:
                pix = "bayer_" + fmt + "8"                  # core Bayer is 8-bit
            elif fmt:
                pix = fmt                                   # GRAY8 / GRAY16_LE / ...
    except Exception as e:
        log.debug("caps read failed: %s", e)

    def envint(name):
        try:
            return int(_env(name))
        except (TypeError, ValueError):
            return None

    d["width"] = w or envint("CAM_WIDTH")
    d["height"] = h or envint("CAM_HEIGHT")
    d["fps"] = fps or envint("CAM_FPS")
    if not pix:
        bayer = _env("CAM_BAYER")
        pix = ("bayer_" + bayer + "8") if bayer else _env("CAM_FORMAT", "GRAY8")
    d["pixel_format"] = pix
    for k in ("width", "height", "fps"):                    # omit what we can't substantiate
        if d.get(k) is None:
            d.pop(k, None)
    return d


class Bridge:
    def __init__(self):
        self.loop = GLib.MainLoop()
        self.pipeline = None
        self.advertiser = None
        self._advertised = False
        self._stopping = False

    def build(self):
        Gst.init(None)
        desc = _env("CAM_PIPELINE")
        if not desc:
            log.error("CAM_PIPELINE not set; nothing to run")
            return False
        log.info("pipeline: %s", desc)
        self.pipeline = Gst.parse_launch(desc)
        # webrtcsink meta.name == producer_id, so discovery + signalling line up (one server, many producers).
        sink = self.pipeline.get_by_name("cam_webrtcsink")
        if sink is not None:
            try:
                pid = producer_id()
                sink.set_property("meta", Gst.Structure.new_from_string("meta,name=" + pid))
                log.info("webrtcsink meta name=%s", pid)
            except Exception as e:
                log.warning("could not set webrtcsink meta: %s", e)
        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_message)
        return True

    def run(self):
        for sig in (signal.SIGINT, signal.SIGTERM):
            GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, sig, self._on_signal)
        self.pipeline.set_state(Gst.State.PLAYING)
        try:
            self.loop.run()
        finally:
            self._teardown()

    def _on_message(self, _bus, msg):
        t = msg.type
        if t == Gst.MessageType.STATE_CHANGED and not self._advertised and isinstance(msg.src, Gst.Pipeline):
            _old, new, _pending = msg.parse_state_changed()
            if new == Gst.State.PLAYING:
                self._advertise()
        elif t == Gst.MessageType.EOS:
            log.info("EOS; stopping")
            self.loop.quit()
        elif t == Gst.MessageType.ERROR:
            err, dbg = msg.parse_error()
            log.error("pipeline error: %s (%s)", err, dbg)
            self.loop.quit()
        return True

    def _advertise(self):
        self._advertised = True                              # one-shot, even if advertising fails
        if not _truthy(_env("CAM_ADVERTISE", "1")):
            log.info("CAM_ADVERTISE=0; discovery disabled")
            return
        try:
            d = fill_dims_from_caps(base_descriptor(), self.pipeline.get_by_name("cam_src"))
            key = "fleet/{}/media/{}".format(vehicle_id(), sensor_id())
            self.advertiser = StreamAdvertiser(key, d, connect=zenoh_connect(), enabled=True)
            self.advertiser.advertise()
            log.info("descriptor: %s", json.dumps(d))
        except Exception as e:                               # discovery must never take down the video path
            log.warning("advertise step failed (%s); streaming continues", e)

    def _on_signal(self):
        if not self._stopping:
            self._stopping = True
            log.info("signal received; shutting down")
            self.loop.quit()
        return GLib.SOURCE_REMOVE

    def _teardown(self):
        if self.advertiser is not None:
            self.advertiser.close()
        if self.pipeline is not None:
            self.pipeline.set_state(Gst.State.NULL)


def main():
    bridge = Bridge()
    if not bridge.build():
        return 1
    bridge.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
