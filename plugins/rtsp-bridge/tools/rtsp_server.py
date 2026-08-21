#!/usr/bin/env python3
"""rtsp-bridge launcher: an in-process GstRtspServer serving the core's frames, plus Zenoh discovery.

run.sh resolves the transport and exports the SOURCE half of the pipeline (CAM_SRC_CHAIN, ending in
video/x-raw,format=I420 -- split at pump_in/pump_out when the frame pump applies); this composes the
encode half (rtsp_launch), mounts ONE shared on-demand media factory, and advertises the stream.

Lifecycle (deliberately on-demand, no always-on knob):
  * The media pipeline -- including the socket source -- is constructed on the first client's
    DESCRIBE and torn down after the last client leaves: zero encode cost with nobody watching.
  * Core restart self-heals WITHOUT the exit-and-restart convention the long-lived bridges use: the
    dying source unprepares the media and drops the clients; the NEXT client constructs fresh media
    whose source reconnects (unixfdsink broadcasts to any client; shmsink accepts new connects).
    The server process itself only exits on signals or a failed startup.

Discovery mirrors bridge_stream.py, with one difference: a SERVER is connectable from bind time, so
the advert fires on server.attach() (not on a PLAYING transition that on-demand media won't reach
until someone connects). Same liveliness semantics: the token lives exactly as long as this process.
"""
import json
import logging
import os
import signal
import socket
import sys

import gi
gi.require_version("Gst", "1.0")
gi.require_version("GstRtspServer", "1.0")
gi.require_version("GstRtsp", "1.0")
from gi.repository import GLib, Gst, GstRtsp, GstRtspServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rtsp_launch
from frame_pump import FramePump
from thermal_preview import parse_window
from zenoh_advertiser import StreamAdvertiser

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("rtsp_server")

_LOWER_TRANS = {"udp": GstRtsp.RTSPLowerTrans.UDP,
                "udp-mcast": GstRtsp.RTSPLowerTrans.UDP_MCAST,
                "tcp": GstRtsp.RTSPLowerTrans.TCP}


def _env(name, default=None):
    v = os.environ.get(name)
    return v if v not in (None, "") else default


def _truthy(v):
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def _envint(name, default=None):
    try:
        return int(_env(name))
    except (TypeError, ValueError):
        return default


def vehicle_id():
    return _env("VEHICLE_ID", socket.gethostname())


def sensor_id():
    return _env("CAM_INSTANCE", "camera")


def stream_id():
    # When webrtc-bridge shares the sensor, sensor_env auto-sets CAM_RTSP_STREAM_ID=<name>-rtsp so
    # the two streams don't collide on one fleet/<vehicle>/media/<id> key.
    return _env("CAM_RTSP_STREAM_ID", sensor_id())


def producer_id():
    return _env("CAM_PRODUCER_ID", "{}-{}".format(vehicle_id(), stream_id()))


def zenoh_connect():
    raw = os.environ.get("ZENOH_CONNECT", "tcp/localhost:7447")
    return [e.strip() for e in raw.split(",") if e.strip()]


def normalize_window():
    """CAM_RTSP_NORMALIZE (as resolved by run.sh: '' = off) -> (lo, hi) window or None."""
    spec = _env("CAM_RTSP_NORMALIZE_RESOLVED", "")
    if not spec:
        return None
    try:
        return parse_window(spec)
    except ValueError as e:
        log.warning("CAM_RTSP_NORMALIZE=%r unparseable (%s); using the 1:99 default", spec, e)
        return parse_window("auto")


def descriptor(codec, url):
    """The docs/DISCOVERY.md stream descriptor, env-only (on-demand media has no live caps to read
    at advert time; omit what we can't substantiate)."""
    d = {
        "schema_version": 1,
        "id": stream_id(),
        "role": _env("CAM_STREAM_ROLE", sensor_id()),
        "producer": "camera-service",
        "protocol": "rtsp",
        "signalling": url,                                # the spec's "where to connect" field
        "producer_id": producer_id(),
        "codec": codec,                                   # we encode it ourselves, so this is known
    }
    for key, envn in (("width", "CAM_WIDTH"), ("height", "CAM_HEIGHT"), ("fps", "CAM_FPS")):
        v = _envint(envn)
        if v:
            d[key] = v
    bayer = _env("CAM_BAYER")
    d["pixel_format"] = ("bayer_" + bayer + "8") if bayer else _env("CAM_FORMAT", "GRAY8")
    topic = _env("CAM_ROS_TOPIC")
    if topic:
        d["ros_topic"] = topic if topic.startswith("/") else "/" + topic
    rec = _env("CAM_RECORDING_GLOB")
    if rec:
        d["recording"] = rec
    return d


def configure_encoder(encoder, keyint):
    """Defensive per-media encoder pass (the setp pattern from bridge_stream): only sets properties
    that exist, so it no-ops across encoders/BSP versions. The CPU encoders are fully tuned in the
    launch string (their properties are stable); this mainly covers the nvv4l2 extras that vary."""
    fac = encoder.get_factory()
    name = fac.get_name() if fac is not None else "?"
    done = []

    def setp(prop, val):
        if encoder.find_property(prop) is None:
            return False
        try:
            encoder.set_property(prop, val)
            done.append("%s=%s" % (prop, val))
            return True
        except Exception as e:                            # noqa: BLE001
            log.debug("encoder %s: set %s=%r failed: %s", name, prop, val, e)
            return False

    if name.startswith("nvv4l2"):
        setp("maxperf-enable", True)
        setp("insert-sps-pps", True)                      # (+VPS on the h265 encoder) at every IDR
        setp("insert-vui", True)
        setp("idrinterval", keyint)
    for p in ("num-B-Frames", "max-bframes"):             # nvv4l2 B-frame knobs (x26x handled in-string)
        if setp(p, 0):
            break
    log.info("encoder-setup: %s -> %s", name, ", ".join(done) or "(nothing to adjust)")


class RtspBridge:
    def __init__(self):
        self.loop = GLib.MainLoop()
        self.server = None
        self.advertiser = None
        self._stopping = False
        self._advertise_retry_s = 5
        self._media_seq = 0
        # resolved config
        self.codec = None
        self.encoder = None
        self.keyint = _envint("CAM_RTSP_KEYINT", 30)
        self.port = _envint("CAM_RTSP_PORT", 8554)
        self.mount = rtsp_launch.normalize_mount_path(_env("CAM_RTSP_PATH"), sensor_id())
        self.pump_mode = _truthy(_env("CAM_PUMP", "0"))
        self.strip_header = _env("CAM_TRANSPORT_RESOLVED", "shm") == "shm"
        self.norm_window = normalize_window()
        self.debayer = _truthy(_env("CAM_RTSP_DEBAYER_RESOLVED", "0"))

    # ---- pipeline description ----------------------------------------------------------------
    def _resolve_codec_encoder(self):
        try:
            self.codec = rtsp_launch.normalize_codec(_env("CAM_RTSP_CODEC", "h264"))
        except ValueError as e:
            log.warning("%s; using h264", e)
            self.codec = "h264"
        have = lambda n: Gst.ElementFactory.find(n) is not None  # noqa: E731
        pinned = _env("CAM_RTSP_ENCODER", "auto")
        enc = rtsp_launch.pick_encoder(self.codec, pinned, have)
        if not have(enc):
            log.error("encoder %r not available in this container "
                      "(CAM_RTSP_ENCODER=%s codec=%s); cannot serve", enc, pinned, self.codec)
            return False
        if enc == rtsp_launch.CODECS[self.codec]["nv"] and not have("nvvidconv"):
            # The NVMM hop rides the same platform grant as the encoder; a half-granted container
            # (encoder injected, converter not) can't run the NVENC chain.
            log.warning("%s present but nvvidconv is not; falling back to %s",
                        enc, rtsp_launch.CODECS[self.codec]["cpu"])
            enc = rtsp_launch.CODECS[self.codec]["cpu"]
            if not have(enc):
                log.error("fallback encoder %r not available either; cannot serve", enc)
                return False
        self.encoder = enc
        return True

    def build(self):
        Gst.init(None)
        src_chain = _env("CAM_SRC_CHAIN")
        if not src_chain:
            log.error("CAM_SRC_CHAIN not set (run via run.sh); nothing to serve")
            return False
        if not self._resolve_codec_encoder():
            return False
        bitrate = _envint("CAM_RTSP_BITRATE", 4000000)
        enc_block = rtsp_launch.encoder_block(self.codec, self.encoder, bitrate, self.keyint)
        launch = rtsp_launch.build_factory_launch(src_chain, self.codec, enc_block)
        log.info("media launch: %s", launch)

        self.server = GstRtspServer.RTSPServer()
        self.server.set_service(str(self.port))
        factory = GstRtspServer.RTSPMediaFactory()
        factory.set_launch(launch)
        # ONE shared media per mount: every client attaches to the same pipeline (the core's
        # endpoint is consumed once, encoded once); constructed on the first DESCRIBE, torn down
        # after the last TEARDOWN/timeout.
        factory.set_shared(True)
        factory.set_latency(_envint("CAM_RTSP_LATENCY", 200))
        proto_spec = _env("CAM_RTSP_PROTOCOLS")
        if proto_spec:
            try:
                toks = rtsp_launch.parse_protocols(proto_spec)
                flags = GstRtsp.RTSPLowerTrans(0)
                for t in toks:
                    flags |= _LOWER_TRANS[t]
                factory.set_protocols(flags)
                log.info("transports restricted to %s", ",".join(toks))
            except ValueError as e:
                log.warning("%s; leaving the server default (udp+tcp)", e)
        factory.connect("media-configure", self._on_media_configure)
        self.server.get_mount_points().add_factory(self.mount, factory)
        return True

    # ---- per-media wiring ----------------------------------------------------------------------
    def _on_media_configure(self, _factory, media):
        self._media_seq += 1
        seq = self._media_seq
        log.info("media #%d constructed (first client connected)", seq)
        element = media.get_element()
        enc = element.get_by_name("cam_enc")
        if enc is not None:
            try:
                configure_encoder(enc, self.keyint)
            except Exception as e:                        # noqa: BLE001 -- never break the media
                log.warning("encoder-setup failed: %s", e)
        pump_in = element.get_by_name("pump_in")
        pump_out = element.get_by_name("pump_out")
        if pump_in is not None and pump_out is not None:
            env_geom = None
            if _envint("CAM_WIDTH") and _envint("CAM_HEIGHT"):
                env_geom = (_envint("CAM_WIDTH"), _envint("CAM_HEIGHT"))
            try:
                # The signal connections hold the pump for the media's lifetime; the attribute is
                # belt-and-suspenders (and handy in a debugger).
                media._cam_pump = FramePump(
                    pump_in, pump_out, strip_header=self.strip_header,
                    normalize=self.norm_window, bayer=_env("CAM_BAYER", ""),
                    debayer=self.debayer, fps=_envint("CAM_FPS", 25) or 25,
                    env_geometry=env_geom)
            except Exception as e:                        # noqa: BLE001
                log.error("frame pump wiring failed: %s", e)
        elif self.pump_mode:
            log.error("pump elements missing from the media (run.sh/CAM_SRC_CHAIN mismatch)")
        media.connect("new-state", self._on_media_state, seq)
        media.connect("unprepared", self._on_media_unprepared, seq)

    def _on_media_state(self, _media, state, seq):
        log.info("media #%d -> %s", seq, Gst.Element.state_get_name(state))

    def _on_media_unprepared(self, _media, seq):
        # Normal after the last client leaves (on-demand teardown); also the path a core restart
        # takes (source ERROR -> unprepare -> clients dropped). Either way the next DESCRIBE
        # constructs fresh media that reconnects, so the server itself stays up.
        log.info("media #%d unprepared (last client left, or the source went away -- the next "
                 "client reconnects)", seq)

    # ---- discovery -----------------------------------------------------------------------------
    def advertise(self):
        if not _truthy(_env("CAM_ADVERTISE", "1")):
            log.info("CAM_ADVERTISE=0; discovery disabled")
            return
        url = _env("CAM_RTSP_URL") or rtsp_launch.rtsp_url(
            _env("CAM_RTSP_HOST", socket.gethostname()), self.port, self.mount)
        try:
            d = descriptor(self.codec, url)
            key = "fleet/{}/media/{}".format(vehicle_id(), stream_id())
            self.advertiser = StreamAdvertiser(key, d, connect=zenoh_connect(), enabled=True)
            log.info("descriptor: %s", json.dumps(d))
        except Exception as e:                            # discovery must never take down the server
            log.warning("advertise setup failed (%s); serving continues", e)
            return
        if not self._try_advertise():
            log.info("advertise: zenoh not reachable yet; retrying every %ds until the token lands",
                     self._advertise_retry_s)
            GLib.timeout_add_seconds(self._advertise_retry_s, self._retry_advertise)

    def _try_advertise(self):
        try:
            return bool(self.advertiser and self.advertiser.advertise())
        except Exception as e:                            # noqa: BLE001
            log.debug("advertise attempt failed: %s", e)
            return False

    def _retry_advertise(self):
        if self._stopping or self._try_advertise():
            if self.advertiser and self.advertiser.active:
                log.info("advertise: token declared on retry; stream now discoverable")
            return False
        return True

    # ---- lifecycle -----------------------------------------------------------------------------
    def run(self):
        sid = self.server.attach(None)
        if sid == 0:
            log.error("server.attach failed (port %s in use?)", self.port)
            return 1
        log.info("serving rtsp://0.0.0.0:%d%s (codec=%s encoder=%s %s, shared on-demand media)",
                 self.port, self.mount, self.codec, self.encoder,
                 "pump+header" if self.strip_header and self.pump_mode
                 else ("pump" if self.pump_mode else "linear"))
        self.advertise()
        for sig in (signal.SIGINT, signal.SIGTERM):
            GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, sig, self._on_signal)
        try:
            self.loop.run()
        finally:
            if self.advertiser is not None:
                self.advertiser.close()
        return 0

    def _on_signal(self):
        if not self._stopping:
            self._stopping = True
            log.info("signal received; shutting down")
            self.loop.quit()
        return GLib.SOURCE_REMOVE


def main():
    bridge = RtspBridge()
    if not bridge.build():
        return 1
    return bridge.run()


if __name__ == "__main__":
    sys.exit(main())
