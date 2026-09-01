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
import faulthandler
import json
import logging
import os
import signal
import socket
import sys
import time

import gi
gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from zenoh_advertiser import StreamAdvertiser
from h264_level import h264_level_for, level_covers, LEVELS as H264_LEVELS
from format_adapt import adapt_for_input, debayer_enabled
from scale_plan import fit_within, parse_max_size, scale_caps
from header_transport import (HeaderError, PtsTracker, TS_SOURCE_SHORT,
                              caps_for_frame, parse_header)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("bridge_stream")

# A crash inside a native GStreamer element (a platform-injected encoder is the prime suspect on
# Jetson) otherwise kills the container with NOTHING in docker logs -- the restart loop is then
# indistinguishable from a config error. This prints the C-level fault + Python stacks to stderr.
faulthandler.enable()


def _env(name, default=None):
    v = os.environ.get(name)
    return v if v not in (None, "") else default


def _truthy(v):
    return str(v).strip().lower() in ("1", "true", "yes", "on")


_H264_PROFILES = ("constrained-baseline", "high")


def webrtc_profile():
    """H.264 encode profile (config knob CAM_WEBRTC_PROFILE) -- effectively ALWAYS constrained-baseline.
    webrtcsink hard-pins profile=constrained-baseline on its internal parser filter whenever it encodes
    raw input (utils.rs parser_caps(force_profile=true); initial discovery passes output_caps=ANY --
    unchanged through gst-plugins-rs 0.15), so a stream in any OTHER profile fails caps negotiation
    inside webrtcsink. Verified on-device: forcing nvv4l2h264enc's `profile` property to High made
    h264parse re-expose profile=high behind that filter -> not-negotiated -> discovery died with
    "No caps found for stream video_0". So `high` warns + falls back; the knob survives for the day
    upstream honors a requested profile. Unknown values warn + fall back likewise."""
    p = (_env("CAM_WEBRTC_PROFILE", "constrained-baseline") or "").strip().lower()
    if p == "high":
        log.warning("CAM_WEBRTC_PROFILE=high: this webrtcsink forces constrained-baseline for raw input "
                    "at codec discovery; using constrained-baseline (encoder choice -- x264enc vs NVENC "
                    "-- is unaffected)")
    elif p not in _H264_PROFILES:
        log.warning("CAM_WEBRTC_PROFILE=%r not in %s; using constrained-baseline", p, list(_H264_PROFILES))
    return "constrained-baseline"


def webrtc_max_level():
    """Safety CLAMP (CAM_WEBRTC_MAX_LEVEL) on the AUTO-derived H.264 level -- NOT the level itself (a
    manual level is the exact footgun that caused the fixed-3.1 black tile). Default 5.2 (the H.264 max,
    effectively no clamp); unknown -> 5.2."""
    lvl = (_env("CAM_WEBRTC_MAX_LEVEL", "5.2") or "").strip()
    if lvl not in H264_LEVELS:
        log.warning("CAM_WEBRTC_MAX_LEVEL=%r is not a valid level; using 5.2", lvl)
        return "5.2"
    return lvl


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
        if caps is None and src is not None and src.find_property("caps") is not None:
            # appsrc (the pump paths): the property is set before the first push, so it can be
            # ahead of the pad's current caps when the advert runs off the first frame.
            caps = src.get_property("caps")
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


def _is_h264(codec_name, caps):
    """H.264? The signal's codec arg form varies by webrtcsink build, so accept either the name arg
    (contains 'h264') or a caps whose structure is video/x-h264."""
    if codec_name and "h264" in str(codec_name).lower():
        return True
    if caps is not None and caps.get_size() > 0 and caps.get_structure(0).get_name() == "video/x-h264":
        return True
    return False


def _wh_fps(caps):
    """(width, height, fps) from a video/x-raw caps, or None. fps defaults to 30 when unset/zero."""
    if caps is None or caps.get_size() == 0:
        return None
    st = caps.get_structure(0)
    okw, w = st.get_int("width")
    okh, h = st.get_int("height")
    if not (okw and okh):
        return None
    fps = 30
    okf, num, den = st.get_fraction("framerate")
    if okf and den and num:
        fps = max(1, round(num / den))
    return (w, h, fps)


def _negotiated_caps(el, which):
    """Current (negotiated) caps of element `el`'s `which` ('src'|'sink') pad. webrtcsink uses REQUEST
    sink pads, so iterate them rather than get_static_pad('sink')."""
    if el is None:
        return None
    if which == "src":
        pad = el.get_static_pad("src")
        return pad.get_current_caps() if pad is not None else None
    it = el.iterate_sink_pads()
    while True:
        res, pad = it.next()
        if res == Gst.IteratorResult.OK:
            caps = pad.get_current_caps()
            if caps is not None and caps.get_size() > 0:
                return caps
        elif res == Gst.IteratorResult.RESYNC:
            it.resync()
        else:
            return None


def _configure_live_encoder(encoder):
    """Force B-frames OFF (real-time) + a low-latency tune, DEFENSIVELY across encoders (x264enc /
    nvv4l2h264enc / openh264enc): only sets properties that exist, so it's a no-op on a non-H.264
    encoder. Deliberately does NOT touch the encoder's `profile`: the bitstream profile is negotiated
    from the downstream caps (webrtcsink's parser filter pins constrained-baseline -- see
    webrtc_profile), and forcing a DIFFERENT profile on the element makes the SPS contradict those
    caps (h264parse re-parses the real profile) -> not-negotiated -> webrtcsink discovery dies.
    Hit exactly this on-device the first time nvv4l2h264enc was actually reachable."""
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
        except Exception as e:                       # noqa: BLE001
            log.debug("encoder %s: set %s=%r failed: %s", name, prop, val, e)
            return False

    for p in ("bframes", "num-B-Frames", "max-bframes", "b-frames"):   # name varies by encoder
        if setp(p, 0):
            break
    setp("b-adapt", False)                            # x264enc: don't auto-insert B-frames
    if name == "x264enc":
        try:
            encoder.set_property("tune", "zerolatency")    # flags enum, set by nick
            done.append("tune=zerolatency")
        except Exception as e:                        # noqa: BLE001
            log.debug("x264enc tune set failed: %s", e)
    elif name == "nvv4l2h264enc":
        setp("maxperf-enable", True)
        setp("insert-sps-pps", True)                  # mid-stream joiners get SPS/PPS at every IDR
    log.info("encoder-setup: %s -> %s", name, ", ".join(done) or "(no matching low-latency props)")


class Bridge:
    def __init__(self):
        self.loop = GLib.MainLoop()
        self.pipeline = None
        self.advertiser = None
        self._advertised = False        # the advertise sequence has been ENTERED (one-shot on PLAYING)
        self._advertise_retry_s = 5     # re-attempt cadence until the zenoh router accepts the token
        self._stopping = False
        self._h264_caps_str = None      # cached forced H.264 output caps (profile + derived level)
        # 16->8 preview normalize pump (CAM_WEBRTC_NORMALIZE; run.sh splits the pipeline at
        # norm_in/norm_out when the knob is on -- see thermal_preview.py for the stretch itself).
        self.norm = None                # PercentileStretch (built lazily when the elements exist)
        self.norm_out = None            # the appsrc we push stretched frames into
        self._norm_ready = False        # out-caps configured from the first input sample
        self._norm_passthrough = False  # input wasn't GRAY16: forward unmodified
        self._norm_dtype = None         # numpy dtype per the input caps ('<u2' / '>u2')
        self._norm_w = self._norm_h = 0
        self._fmt_adapted = False       # the fmt_tap seam has been resolved (one-shot)
        # Encode-side downscale (CAM_WEBRTC_MAX_SIZE; run.sh inserts fmt_scale -> scale_caps):
        # the bound, and the target the capsfilter currently pins (None = passthrough).
        self._max_size = None
        self._scale_target = None
        # Headered-shm pump (JP6 plugin endpoint; run.sh splits the pipeline at hdr_in/hdr_out):
        # strip the 36-byte CAMF header, stamp caps from it, re-attach capture time as offset_end.
        self.hdr_out = None             # the appsrc we push de-headered frames into
        self._hdr_pts = PtsTracker()    # absolute capture ns -> relative monotonic PTS
        self._hdr_caps_key = None       # (w, h, pixfmt) the current hdr_out caps were built from
        self._hdr_warn_last = 0.0       # throttle for per-frame header errors (monotonic s)
        # Capture->now latency (works wherever buffers carry the absolute capture ns in offset_end:
        # unixfd natively, headered shm via the pump; n/a on shm-raw). Sample lists are drained by
        # the heartbeat; the EMA feeds the optional burned-in overlay (lat_overlay element).
        self._lat_rx = []               # capture->bridge-ingress ms (headered pump only)
        self._lat_enc = []              # capture->webrtcsink-input ms (probe on its sink pad)
        self._lat_ema = None            # smoothed capture->enc ms, for the overlay text
        self._lat_wall = 0.0            # monotonic s of the last enc sample (overlay staleness)
        self._ts_source = None          # provenance of the capture stamp (headered shm only)
        # diagnostics (see the "diagnostics" section below)
        self._error = False             # bus ERROR seen -> non-zero exit (legible in docker ps)
        self._src_kind = "shmsrc"       # shmsrc | unixfdsrc, for targeted no-data hints
        self._rx_frames = 0             # buffers seen on cam_src's src pad (the heartbeat counter)
        self._rx_bytes = 0
        self._status_iv = 0             # heartbeat period (CAM_WEBRTC_STATUS; 0 = off)
        self._status_prev = 0
        self._status_zero = 0           # consecutive heartbeats with zero frames received
        self._consumers = 0             # webrtcsink consumers currently connected
        self._warn_last = {}            # bus-warning throttle: message -> monotonic s

    def build(self):
        Gst.init(None)
        desc = _env("CAM_PIPELINE")
        if not desc:
            log.error("CAM_PIPELINE not set; nothing to run")
            return False
        log.info("pipeline: %s", desc)
        try:
            self.pipeline = Gst.parse_launch(desc)
        except GLib.Error as e:
            log.error("pipeline parse failed: %s -- a required element is missing from this image "
                      "or the description is malformed (check with gst-inspect-1.0)", e)
            return False
        self._src_kind = "unixfdsrc" if "unixfdsrc" in desc else "shmsrc"
        self._log_encoder_inventory()
        norm_in = self.pipeline.get_by_name("norm_in")
        self.norm_out = self.pipeline.get_by_name("norm_out")
        if norm_in is not None and self.norm_out is not None:
            from thermal_preview import PercentileStretch, parse_window  # needs numpy (in the image)
            spec = _env("CAM_WEBRTC_NORMALIZE", "auto")
            try:
                lo, hi = parse_window(spec)
            except ValueError as e:
                log.warning("CAM_WEBRTC_NORMALIZE=%r unparseable (%s); using the 1:99 default", spec, e)
                lo, hi = parse_window("auto")
            self.norm = PercentileStretch(lo, hi)
            norm_in.connect("new-sample", self._on_norm_sample)
            norm_in.connect("eos", self._on_norm_eos)
            log.info("preview normalize: percentile window %g:%g (EMA-smoothed), 16->8 before encode",
                     lo, hi)
        # Headered-shm pump (JP6 plugin endpoint): run.sh splits the pipeline at hdr_in/hdr_out;
        # _on_hdr_sample strips the CAMF header, stamps hdr_out's caps from it (geometry/format
        # self-describe -- env is not consulted for them), and re-attaches PTS/frame_id/capture-ns.
        hdr_in = self.pipeline.get_by_name("hdr_in")
        self.hdr_out = self.pipeline.get_by_name("hdr_out")
        if hdr_in is not None and self.hdr_out is not None:
            hdr_in.connect("new-sample", self._on_hdr_sample)
            hdr_in.connect("eos", self._on_hdr_eos)
            log.info("header pump: consuming the core's shm+header plugin endpoint "
                     "(self-describing geometry; capture timestamps -> latency metrics)")
        # Self-describing transport (unixfd): run.sh leaves an `identity name=fmt_tap` seam instead
        # of an env-hardwired bayer2rgb; resolve it from the FIRST caps the stream actually carries
        # (see adapt_for_input). The probe sits on the tap's SINK pad, so it runs -- on the streaming
        # thread, serialized BEFORE the caps event reaches anything downstream of the tap -- early
        # enough to splice the right element in ahead of negotiation.
        tap = self.pipeline.get_by_name("fmt_tap")
        if tap is not None:
            pad = tap.get_static_pad("sink")
            pad.add_probe(Gst.PadProbeType.EVENT_DOWNSTREAM, self._on_fmt_caps, tap)
        # Encode-side downscale (CAM_WEBRTC_MAX_SIZE): run.sh inserts `videoscale name=fmt_scale !
        # capsfilter name=scale_caps` (caps ANY = passthrough) right after the format seam; the
        # target is planned from the caps the stream ACTUALLY carries by a probe on the scaler's
        # sink pad -- it runs ahead of the scaler's own negotiation, so the pinned size is in place
        # before anything downstream (webrtcsink's codec discovery included) ever sees a geometry.
        scaler = self.pipeline.get_by_name("fmt_scale")
        if scaler is not None:
            spec = _env("CAM_WEBRTC_MAX_SIZE")
            try:
                self._max_size = parse_max_size(spec)
            except ValueError as e:
                log.warning("CAM_WEBRTC_MAX_SIZE=%r ignored (%s); streaming at source resolution",
                            spec, e)
            if self._max_size is not None:
                scaler.get_static_pad("sink").add_probe(Gst.PadProbeType.EVENT_DOWNSTREAM,
                                                        self._on_scale_caps)
                log.info("scale: bounding the encode geometry to %dx%d (CAM_WEBRTC_MAX_SIZE=%s)",
                         self._max_size[0], self._max_size[1], spec)
        # webrtcsink meta.name == producer_id, so discovery + signalling line up (one server, many producers).
        sink = self.pipeline.get_by_name("cam_webrtcsink")
        if sink is not None:
            try:
                pid = producer_id()
                sink.set_property("meta", Gst.Structure.new_from_string("meta,name=" + pid))
                log.info("webrtcsink meta name=%s", pid)
            except Exception as e:
                log.warning("could not set webrtcsink meta: %s", e)
            # H.264: pin the profile + the derived MINIMUM level on the encoder output, per
            # consumer, so the payloader's profile-level-id matches the actual stream (no more fixed
            # 42e01f -> out-of-level black tile). encoder-setup also forces B-frames off for live.
            try:
                sink.connect("request-encoded-filter", self._on_request_encoded_filter)
                sink.connect("encoder-setup", self._on_encoder_setup)
            except Exception as e:
                log.warning("could not connect webrtcsink encoder signals: %s", e)
            # Viewer visibility for the status heartbeat (signature varies by build -> defensive).
            for sig_name, handler in (("consumer-added", self._on_consumer_added),
                                      ("consumer-removed", self._on_consumer_removed)):
                try:
                    sink.connect(sig_name, handler)
                except Exception as e:                # noqa: BLE001
                    log.debug("no %s signal on this webrtcsink: %s", sig_name, e)
            # capture->encode latency probe: buffers reaching webrtcsink still carry the absolute
            # capture ns in offset_end (unixfd natively; headered shm via the pump; basetransform
            # elements -- bayer2rgb/videoconvert/textoverlay -- copy offsets through). The sink pad
            # is a request pad, but parse_launch has already linked it, so it exists here.
            it = sink.iterate_sink_pads()
            while True:
                res, spad = it.next()
                if res == Gst.IteratorResult.OK:
                    spad.add_probe(Gst.PadProbeType.BUFFER, self._on_enc_buffer)
                    break
                if res == Gst.IteratorResult.RESYNC:
                    it.resync()
                else:
                    break
        # Burned-in latency overlay (CAM_WEBRTC_LATENCY_OVERLAY; run.sh inserts the element):
        # refresh its text from the smoothed capture->enc latency a couple of times a second.
        if self.pipeline.get_by_name("lat_overlay") is not None:
            GLib.timeout_add(500, self._overlay_tick)
        # Heartbeat plumbing: count every buffer the source hands downstream, and log a periodic
        # status line -- the difference between "no data ever arrives" (core/socket side), "data
        # flows then the pipeline dies" (encoder/discovery), and "streams fine, nobody connected"
        # then reads straight out of docker logs.
        src = self.pipeline.get_by_name("cam_src")
        pad = src.get_static_pad("src") if src is not None else None
        if pad is not None:
            pad.add_probe(Gst.PadProbeType.BUFFER, self._on_rx_buffer)
        self._status_iv = self._status_interval()
        if self._status_iv:
            GLib.timeout_add_seconds(self._status_iv, self._status_tick)
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

    # ---- diagnostics ---------------------------------------------------------
    _INVENTORY = ("nvv4l2h264enc", "nvh264enc", "x264enc", "openh264enc", "vp8enc", "rtpgccbwe")

    def _log_encoder_inventory(self):
        """One-shot startup line: which encoders (+ the congestion-control element) this container
        actually has, at what rank -- so "which encoder will webrtcsink pick, and why" is
        answerable from the log alone. Follows with a pointed warning when GST_PLUGIN_FEATURE_RANK
        names an element the registry doesn't have: on Jetson that means the platform injection
        (CSV on JP6, CDI on JP7) didn't deliver it or it blacklisted at scan, and webrtcsink will
        quietly use the next-ranked encoder instead."""
        reg = Gst.Registry.get()
        parts = []
        for name in self._INVENTORY:
            f = reg.lookup_feature(name)
            parts.append("{}={}".format(name, "rank {}".format(int(f.get_rank())) if f else "ABSENT"))
        log.info("element inventory: %s", "  ".join(parts))
        for spec in (os.environ.get("GST_PLUGIN_FEATURE_RANK") or "").split(","):
            el = spec.split(":", 1)[0].strip()
            if el and reg.lookup_feature(el) is None:
                log.warning("GST_PLUGIN_FEATURE_RANK names %r but it is NOT in the registry "
                            "(platform injection missing, or blacklisted at scan -- check "
                            "`gst-inspect-1.0 %s` inside this container); webrtcsink will use the "
                            "next-ranked encoder", el, el)

    @staticmethod
    def _status_interval():
        raw = str(_env("CAM_WEBRTC_STATUS", "10") or "").strip().lower()
        if raw in ("", "0", "off", "false", "no"):
            return 0
        try:
            return max(int(raw), 1)
        except ValueError:
            log.warning("CAM_WEBRTC_STATUS=%r is not a seconds interval; using 10", raw)
            return 10

    def _on_rx_buffer(self, _pad, info):
        buf = info.get_buffer()
        self._rx_frames += 1
        if buf is not None:
            self._rx_bytes += buf.get_size()
            # unixfd buffers already carry capture ns in offset_end at the source pad (the headered
            # pump adds its ingress sample itself -- its cam_src buffers are still [header][pixels]).
            lat = self._capture_lat_ms(buf.offset_end)
            if lat is not None:
                self._lat_rx.append(lat)
        return Gst.PadProbeReturn.OK

    # ---- capture->now latency (offset_end = absolute capture ns; unixfd + headered shm) --------
    @staticmethod
    def _capture_lat_ms(offset_end):
        """now - capture, in ms -- or None when offset_end isn't a plausible absolute wall-clock ns
        (CLOCK_TIME_NONE / unset on shm-raw; also guards a wildly skewed sender clock rather than
        reporting a nonsense number). Window: 2001-09 <= stamp <= now+60s. NOTE: capture stamps are
        PTP-epoch when the camera is locked -- an unsynced host clock shows up here as a constant
        offset, which is exactly what the reading is for."""
        if offset_end is None:
            return None
        now = time.time_ns()
        if not (10**18 <= offset_end <= now + 60_000_000_000):
            return None
        return (now - offset_end) / 1e6

    def _on_enc_buffer(self, _pad, info):
        """BUFFER probe on webrtcsink's sink pad: per-frame capture->encoder-input latency. This
        brackets everything upstream of the encoder (core + transport hop + queue/debayer/convert);
        the WebRTC network/receiver legs are a per-consumer matter it cannot see."""
        buf = info.get_buffer()
        if buf is not None:
            lat = self._capture_lat_ms(buf.offset_end)
            if lat is not None:
                self._lat_enc.append(lat)
                self._lat_ema = lat if self._lat_ema is None else 0.85 * self._lat_ema + 0.15 * lat
                self._lat_wall = time.monotonic()
        return Gst.PadProbeReturn.OK

    def _overlay_tick(self):
        """Refresh the burned-in overlay text (2 Hz). "lat --" when no stamped buffer has passed
        recently (shm-raw, or the stream stalled) -- the overlay must never show a stale number."""
        if self._stopping:
            return False
        overlay = self.pipeline.get_by_name("lat_overlay") if self.pipeline is not None else None
        if overlay is None:
            return False
        if self._lat_ema is not None and time.monotonic() - self._lat_wall < 3.0:
            src = TS_SOURCE_SHORT.get(self._ts_source)
            text = "lat {:.0f} ms{}".format(self._lat_ema, " ({})".format(src) if src else "")
        else:
            text = "lat --"
        try:
            overlay.set_property("text", text)
        except Exception as e:                        # noqa: BLE001 -- never take down the video
            log.debug("overlay text set failed: %s", e)
        return True

    @staticmethod
    def _lat_fmt(label, samples):
        """' lat[label]=p50/p95ms(n)' for one drained sample list, '' when empty."""
        if not samples:
            return ""
        s = sorted(samples)
        p50 = s[len(s) // 2]
        p95 = s[min(len(s) - 1, int(round(0.95 * (len(s) - 1))))]
        return " lat[{}]=p50 {:.0f}/p95 {:.0f}ms(n={})".format(label, p50, p95, len(s))

    def _status_tick(self):
        """The status heartbeat (every CAM_WEBRTC_STATUS seconds; default 10, 0 = off)."""
        if self._stopping:
            return False
        delta = self._rx_frames - self._status_prev
        self._status_prev = self._rx_frames
        src = self.pipeline.get_by_name("cam_src") if self.pipeline is not None else None
        pad = src.get_static_pad("src") if src is not None else None
        caps = pad.get_current_caps() if pad is not None else None
        capstr = caps.to_string() if caps is not None else None
        if capstr is None and pad is not None:
            # shmsrc never stamps current caps on its pad (the capsfilter downstream holds them);
            # the allowed-caps intersection is the next-best truth for the heartbeat.
            allowed = pad.get_allowed_caps()
            if allowed is not None and not allowed.is_any() and not allowed.is_empty():
                capstr = allowed.to_string()
        # capture->now latency over this interval (empty on shm-raw -- no capture stamps survive).
        # cap->rx = capture -> bridge ingress; cap->enc = capture -> webrtcsink input, so
        # (enc - rx) is the bridge's own queue/debayer/convert residency.
        lat_rx, self._lat_rx = self._lat_rx, []
        lat_enc, self._lat_enc = self._lat_enc, []
        lat_seg = self._lat_fmt("cap->rx", lat_rx) + self._lat_fmt("cap->enc", lat_enc)
        if lat_seg and self._ts_source:
            lat_seg += " ts_src=" + self._ts_source
        _ok, state, pending = self.pipeline.get_state(0)
        log.info("status: state=%s%s in=%d frames (+%d/%ds, %.1f MB) caps=%s consumers=%d%s",
                 Gst.Element.state_get_name(state),
                 "->" + Gst.Element.state_get_name(pending)
                 if pending != Gst.State.VOID_PENDING else "",
                 self._rx_frames, delta, self._status_iv, self._rx_bytes / 1e6,
                 capstr if capstr is not None else "(none yet)", self._consumers, lat_seg)
        if self._rx_frames == 0:
            self._status_zero += 1
            if self._status_zero == 2:            # ~2 intervals with nothing: say where to look
                if self._src_kind == "unixfdsrc":
                    log.warning("no frames from the core yet: is the core up with "
                                "transport.plugin_endpoint.enabled: true on a gst>=1.24 (JP7) "
                                "core, and does CAM_TRANSPORT_SOCKET match its socket?")
                elif self.hdr_out is not None:
                    log.warning("no frames from the core yet: is the core up with "
                                "transport.plugin_endpoint.enabled: true on a gst<1.24 (JP6) "
                                "core, and does CAM_TRANSPORT_SOCKET match "
                                "plugin_endpoint.socket_path? (a gst>=1.24 core serves unixfd "
                                "there instead -- use CAM_TRANSPORT=unixfd)")
                else:
                    log.warning("no frames from the core yet: is the core up with "
                                "transport.raw_endpoint.enabled: true, and does CAM_SHM_SOCKET "
                                "match raw_endpoint.socket_path? (CAM_TRANSPORT=shm-raw reads "
                                "the raw shm endpoint)")
        else:
            self._status_zero = 0
        return True

    def _on_consumer_added(self, _sink, peer_id, _webrtcbin):
        self._consumers += 1
        log.info("webrtc consumer connected: %s (%d total)", peer_id, self._consumers)

    def _on_consumer_removed(self, _sink, peer_id, _webrtcbin):
        self._consumers = max(0, self._consumers - 1)
        log.info("webrtc consumer left: %s (%d total)", peer_id, self._consumers)

    @property
    def had_error(self):
        """True when the run ended on a pipeline ERROR -> main() exits non-zero, so a restart loop
        shows exit code 1 in docker ps instead of looking like a clean stop."""
        return self._error

    def _on_message(self, _bus, msg):
        t = msg.type
        if t == Gst.MessageType.STATE_CHANGED and not self._advertised:
            _old, new, _pending = msg.parse_state_changed()
            # Trigger on the SOURCE element (cam_src) reaching PLAYING, NOT the pipeline's aggregate
            # PLAYING. A CAM_WEBRTC_NORMALIZE split pipeline (the appsink->appsrc 16->8 bridge) is two
            # state-change islands whose aggregate async transition never completes, so the pipeline
            # NEVER posts a pipeline-level STATE_CHANGED to PLAYING -- the advert keyed on that never
            # fired (the stream ran fine, but no discovery tile). The source element DOES reach PLAYING
            # in every pipeline variant once capture is live. Verified in cam-dev: for the split
            # pipeline the pipeline-level PLAYING msg is never seen while the cam_src one reliably is.
            # cam_src is the run.sh source name (shmsrc on JP6 / unixfdsrc on JP7); single-chain
            # cameras post it too, so this is universal. EXCEPT the headered-shm pump: there
            # cam_src's caps are application/x-cam-frame and the REAL video caps only exist once
            # the pump has parsed the first header -- advertising here would stamp the descriptor
            # from stale env defaults, so the pump triggers the advert itself (first frame).
            if new == Gst.State.PLAYING and msg.src is not None \
                    and msg.src.get_name() == "cam_src" and self.hdr_out is None:
                self._advertise()
        elif t == Gst.MessageType.EOS:
            log.info("EOS; stopping")
            self.loop.quit()
        elif t == Gst.MessageType.WARNING:
            # Failures often announce themselves as warnings first (encoder fallbacks, missing
            # rtpgccbwe, allocator trouble); surface them, throttled per distinct message.
            err, dbg = msg.parse_warning()
            key = str(err)
            now = time.monotonic()
            if now - self._warn_last.get(key, 0.0) >= 10.0:
                self._warn_last[key] = now
                log.warning("pipeline warning from %s: %s (%s)",
                            msg.src.get_name() if msg.src is not None else "?", err, dbg)
        elif t == Gst.MessageType.ERROR:
            err, dbg = msg.parse_error()
            self._error = True
            log.error("pipeline error from %s: %s (%s)",
                      msg.src.get_name() if msg.src is not None else "?", err, dbg)
            self.loop.quit()
        return True

    def _advertise(self):
        self._advertised = True                              # enter once (on the PLAYING transition)
        if not _truthy(_env("CAM_ADVERTISE", "1")):
            log.info("CAM_ADVERTISE=0; discovery disabled")
            return
        try:
            # On the headered-shm path cam_src's pad says only application/x-cam-frame; the pump's
            # appsrc (hdr_out) carries the real video caps the header described.
            src = self.pipeline.get_by_name("hdr_out") or self.pipeline.get_by_name("cam_src")
            d = fill_dims_from_caps(base_descriptor(), src)
            if self._max_size is not None and d.get("width") and d.get("height"):
                # The viewer receives the DOWNSCALED geometry (CAM_WEBRTC_MAX_SIZE): the same plan
                # the fmt_scale probe applies, computed here from the source dims because the advert
                # fires before the first caps have reached the scaler.
                target = fit_within(d["width"], d["height"], *self._max_size)
                if target is not None:
                    d["width"], d["height"] = target
            key = "fleet/{}/media/{}".format(vehicle_id(), sensor_id())
            self.advertiser = StreamAdvertiser(key, d, connect=zenoh_connect(), enabled=True)
            log.info("descriptor: %s", json.dumps(d))
        except Exception as e:                               # discovery must never take down the video path
            log.warning("advertise setup failed (%s); streaming continues", e)
            return
        # The zenoh router (infra) can come up AFTER this bridge reaches PLAYING -- compose depends_on
        # doesn't wait for readiness, and a cold rack boot brings everything up together. advertise()
        # was one-shot, so a router-not-ready-yet race left the stream PERMANENTLY undiscovered (token
        # never declared -> the dashboard's history-backed subscriber has nothing to replay). Retry on
        # a timer until the token lands -- mirrors run.sh's existing wait-loop for the core's socket.
        if not self._try_advertise():
            log.info("advertise: zenoh not reachable yet; retrying every %ds until the token lands",
                     self._advertise_retry_s)
            GLib.timeout_add_seconds(self._advertise_retry_s, self._retry_advertise)

    def _try_advertise(self) -> bool:
        """One attempt to declare the liveliness token + queryable. StreamAdvertiser.advertise() is
        idempotent and self-closes a failed session, so calling it again on the next tick cleanly
        re-opens. Never raises (discovery must not take down the video path)."""
        try:
            return bool(self.advertiser and self.advertiser.advertise())
        except Exception as e:                               # noqa: BLE001
            log.debug("advertise attempt failed: %s", e)
            return False

    def _retry_advertise(self) -> bool:
        if self._stopping or self._try_advertise():
            if self.advertiser and self.advertiser.active:
                log.info("advertise: token declared on retry; stream now discoverable")
            return False                                     # stop the timer
        return True                                          # keep retrying

    def _encode_geometry(self):
        """(w, h, fps, source) actually fed to webrtcsink's encoder. Prefer webrtcsink's negotiated
        INPUT caps (authoritative -- correct on JP7 unixfd where env geometry is unset, and after the
        CAM_WEBRTC_MAX_SIZE downscale on the branch); fall back to the source caps, then CAM_WIDTH/HEIGHT/FPS (valid
        on the JP6 raw-shm path, where the config IS the geometry)."""
        for el_name, which in (("cam_webrtcsink", "sink"), ("hdr_out", "src"), ("cam_src", "src")):
            whf = _wh_fps(_negotiated_caps(self.pipeline.get_by_name(el_name), which))
            if whf:
                return whf + (el_name,)

        def _i(n):
            try:
                return int(_env(n))
            except (TypeError, ValueError):
                return None
        w, h = _i("CAM_WIDTH"), _i("CAM_HEIGHT")
        if w and h:
            return (w, h, _i("CAM_FPS") or 30, "env")
        return None

    def _h264_output_caps(self):
        """The forced H.264 output caps string (constrained-baseline + level derived from the encode
        resolution), computed once and cached. Returns None -- leaving webrtcsink's defaults -- when the
        geometry can't yet be read (retried on the next call, so a later consumer still gets it)."""
        if self._h264_caps_str:
            return self._h264_caps_str
        geo = self._encode_geometry()
        if geo is None:
            log.warning("h264: cannot determine the encode resolution yet; leaving webrtcsink defaults "
                        "(advertised profile-level-id may not match the stream)")
            return None
        w, h, fps, src = geo
        profile, maxlvl = webrtc_profile(), webrtc_max_level()
        try:
            level = h264_level_for(w, h, fps, max_level=maxlvl)
        except ValueError as e:
            log.warning("h264 level math failed (%s); leaving webrtcsink defaults", e)
            return None
        if not level_covers(level, w, h, fps):
            log.warning("h264: %dx%d@%d needs a level above the clamp %s -- the stream may not decode; "
                        "lower the resolution or raise CAM_WEBRTC_MAX_LEVEL", w, h, fps, maxlvl)
        # Pin BOTH fields: the derived LEVEL, so the payloader's profile-level-id matches the stream
        # (the fixed-42e01f black-tile fix), and profile=constrained-baseline -- the same profile
        # webrtcsink itself forces on its parser filter for raw input (see webrtc_profile), so this pin
        # can never conflict. Verified-safe on x264enc AND nvv4l2h264enc (the v4l2 encoder negotiates
        # its profile/level V4L2 controls straight from these downstream caps).
        self._h264_caps_str = "video/x-h264,profile={},level=(string){}".format(profile, level)
        log.info("h264 encode: profile=%s level=%s for %dx%d@%d (from %s) -> caps %s",
                 profile, level, w, h, fps, src, self._h264_caps_str)
        return self._h264_caps_str

    def _on_request_encoded_filter(self, _sink, consumer_id, codec_name, caps):
        """webrtcsink: a filter inserted AFTER the encoder, BEFORE the payloader. For H.264 we pin
        constrained-baseline + the derived level here, so the payloader emits a
        matching profile-level-id. See webrtc_profile for why the profile is always constrained-baseline."""
        log.debug("request-encoded-filter: consumer=%r codec=%r caps=%s",
                  consumer_id, codec_name, caps.to_string() if caps is not None else None)
        # Apply during BOTH discovery (consumer_id None) AND per-consumer: discovery builds the SDP from
        # this chain's output caps, so the pin must be present there for the advertised profile-level-id
        # to match the stream. Discovery feeds the encoder the REAL negotiated input caps, so the level
        # we derive from that same resolution matches.
        if not _is_h264(codec_name, caps):
            return None
        caps_str = self._h264_output_caps()
        if not caps_str:
            return None
        cf = Gst.ElementFactory.make("capsfilter", None)
        cf.set_property("caps", Gst.Caps.from_string(caps_str))
        log.info("request-encoded-filter[%s]: %s", consumer_id, caps_str)
        return cf

    def _on_encoder_setup(self, _sink, consumer_id, codec_name, encoder):
        """webrtcsink: configure the per-consumer encoder -- force B-frames OFF + low-latency for live.
        Return False so webrtcsink still layers its own bitrate / congestion-control defaults on top."""
        try:
            _configure_live_encoder(encoder)
        except Exception as e:                        # noqa: BLE001 -- never break the video path
            log.warning("encoder-setup failed: %s", e)
        return False

    # ---- input format adapter (fmt_tap: adapt to the caps the stream ACTUALLY carries) ---------
    def _on_fmt_caps(self, _pad, info, tap):
        """Pad probe on fmt_tap's sink pad: on the first CAPS event, splice in whatever the real
        input format needs (bayer2rgb / a GRAY8 relabel / nothing -- adapt_for_input). Runs on the
        source's streaming thread while the sticky caps event is still upstream of the tap, so the
        splice is complete before negotiation (let alone data) reaches the spliced-in element.

        The seam is UNLINKED at build (run.sh emits the tap and the rest of the pipeline as two
        separate chains, the same trick as the normalize split): the source negotiates before it
        ever sends a caps event, and with the tap statically linked to videoconvert that
        pre-caps negotiation would already have died not-negotiated on a Bayer stream (identity
        proxies the caps query to videoconvert, which takes no video/x-bayer). Dangling, the tap
        answers with its ANY template; this probe then completes the graph -- through the right
        element -- and the caps event flows on into it. The first buffer follows the event on
        this same thread, so the link always exists before data does."""
        ev = info.get_event()
        if ev is None or ev.type != Gst.EventType.CAPS:
            return Gst.PadProbeReturn.OK
        if self._fmt_adapted:
            return Gst.PadProbeReturn.REMOVE
        self._fmt_adapted = True
        # The head of the post-seam chain, in the order run.sh emits them: the downscaler
        # (fmt_scale) when CAM_WEBRTC_MAX_SIZE is set, else the normalize pump's appsink (norm_in --
        # fmt_next is then the post-pump chain, fed by norm_out, never by the tap), else videoconvert.
        nxt = None
        for head in ("fmt_scale", "norm_in", "fmt_next"):
            nxt = self.pipeline.get_by_name(head)
            if nxt is not None:
                break
        if nxt is None:                          # no downstream head to hand the stream to (bug)
            log.error("format adapter: no fmt_scale/norm_in/fmt_next element; pipeline is incomplete")
            return Gst.PadProbeReturn.REMOVE
        srcpad, nxtpad = tap.get_static_pad("src"), nxt.get_static_pad("sink")

        def _link(a, b):
            ret = a.link(b)
            if ret != Gst.PadLinkReturn.OK:
                raise RuntimeError("link {} -> {}: {}".format(a.get_name(), b.get_name(), ret))

        try:
            caps = ev.parse_caps()
            st = caps.get_structure(0)
            okf, num, den = st.get_fraction("framerate")
            fr = "{}/{}".format(num, den) if (okf and den) else None
            okw, w = st.get_int("width")
            okh, h = st.get_int("height")
            plan = adapt_for_input(st.get_name(), w if okw else 0, h if okh else 0, fr,
                                   debayer_enabled(_env("CAM_WEBRTC_DEBAYER", "auto")))
            el = None
            if plan is not None:
                factory, el_caps = plan
                el = Gst.ElementFactory.make(factory, "fmt_adapt")
                if el is None:                   # missing element must degrade, never kill the video
                    log.warning("format adapter: element %r unavailable; passing %s through",
                                factory, caps.to_string())
                elif el_caps is not None:        # capssetter relabel: full replacement caps
                    el.set_property("caps", Gst.Caps.from_string(el_caps))
                    el.set_property("join", False)
                    el.set_property("replace", True)
            if el is not None:
                self.pipeline.add(el)
                el.sync_state_with_parent()
                _link(srcpad, el.get_static_pad("sink"))
                _link(el.get_static_pad("src"), nxtpad)
                log.info("format adapter: input %s -> inserted %s%s", caps.to_string(), factory,
                         " (relabel to {})".format(el_caps) if el_caps else " (debayer to color)")
            else:
                _link(srcpad, nxtpad)
                log.info("format adapter: input %s passes through", caps.to_string())
        except Exception as e:                   # noqa: BLE001 -- adapter failure = passthrough
            log.warning("format adapter failed (%s); linking straight through", e)
            if not srcpad.is_linked():
                try:
                    _link(srcpad, nxtpad)
                except Exception as e2:          # noqa: BLE001
                    log.error("format adapter: fallback link failed (%s); stream cannot start", e2)
        return Gst.PadProbeReturn.REMOVE

    # ---- encode-side downscale (fmt_scale: bound the geometry fed to the encoder) --------------
    def _on_scale_caps(self, _pad, info):
        """Pad probe on fmt_scale's sink pad: on every CAPS event, pin scale_caps to the geometry
        that fits CAM_WEBRTC_MAX_SIZE (fit_within: aspect kept, even dims, never upscaled), or leave
        it ANY when the frame already fits, which keeps videoscale in passthrough. Runs on the
        streaming thread BEFORE the scaler negotiates the new caps, so the pinned size is what it
        (and everything downstream) fixates on. Kept installed rather than one-shot: a reconnect at
        another geometry (the header pump re-stamps caps) re-plans. Changing the capsfilter posts a
        RECONFIGURE upstream -- the normal caps-change path; it settles on the same caps."""
        ev = info.get_event()
        if ev is None or ev.type != Gst.EventType.CAPS:
            return Gst.PadProbeReturn.OK
        try:
            caps = ev.parse_caps()
            st = caps.get_structure(0)
            okw, w = st.get_int("width")
            okh, h = st.get_int("height")
            if not (okw and okh):
                log.warning("scale: input caps %s carry no geometry; passing through", caps.to_string())
                return Gst.PadProbeReturn.OK
            target = fit_within(w, h, *self._max_size)
            if target == self._scale_target:
                return Gst.PadProbeReturn.OK
            cf = self.pipeline.get_by_name("scale_caps")
            if target is None:
                if self._scale_target is not None:       # was pinned for an earlier geometry
                    cf.set_property("caps", Gst.Caps.new_any())
                log.info("scale: %dx%d already fits %dx%d; videoscale passes through",
                         w, h, self._max_size[0], self._max_size[1])
            else:
                cf.set_property("caps", Gst.Caps.from_string(scale_caps(*target)))
                log.info("scale: %dx%d -> %dx%d (CAM_WEBRTC_MAX_SIZE bound %dx%d); convert/encode "
                         "now run on %.0f%% of the source pixels", w, h, target[0], target[1],
                         self._max_size[0], self._max_size[1],
                         100.0 * target[0] * target[1] / (w * h))
            self._scale_target = target
        except Exception as e:                   # noqa: BLE001 -- never break the video path
            log.warning("scale plan failed (%s); passing through at source resolution", e)
        return Gst.PadProbeReturn.OK

    # ---- 16->8 preview normalize pump (norm_in appsink -> stretch -> norm_out appsrc) ----------
    def _norm_configure(self, caps):
        """One-shot: read the INPUT caps off the first sample and set the matching OUTPUT caps.
        GRAY16_LE/BE -> stretched GRAY8 at the same geometry/rate; anything else passes through
        unchanged (the knob was set on a non-16-bit camera -- warn, don't break the preview)."""
        st = caps.get_structure(0)
        fmt = st.get_string("format") or ""
        ok_w, w = st.get_int("width")
        ok_h, h = st.get_int("height")
        if fmt in ("GRAY16_LE", "GRAY16_BE") and ok_w and ok_h:
            self._norm_dtype = "<u2" if fmt == "GRAY16_LE" else ">u2"
            self._norm_w, self._norm_h = w, h
            out = caps.copy()
            out.set_value("format", "GRAY8")
            log.info("normalize: %s %dx%d -> GRAY8 (percentile stretch)", fmt, w, h)
        else:
            self._norm_passthrough = True
            out = caps
            log.warning("normalize requested but input caps are %s; passing through unmodified",
                        caps.to_string())
        self.norm_out.set_property("caps", out)
        self._norm_ready = True

    def _on_norm_sample(self, sink):
        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.OK
        buf = sample.get_buffer()
        if not self._norm_ready:
            try:
                self._norm_configure(sample.get_caps())
            except Exception as e:                       # noqa: BLE001 -- never kill the stream thread
                log.warning("normalize: caps configure failed (%s); passing through", e)
                self._norm_passthrough = True
                self._norm_ready = True
        ok, mi = buf.map(Gst.MapFlags.READ)
        if not ok:
            return Gst.FlowReturn.OK
        try:
            if self._norm_passthrough:
                out_bytes = bytes(mi.data)
            else:
                import numpy as np
                n = self._norm_w * self._norm_h
                arr = np.frombuffer(mi.data, dtype=self._norm_dtype, count=-1)
                if arr.size < n:                          # torn/short frame: drop it, stay alive
                    log.warning("normalize: short frame (%d px < %dx%d); dropped",
                                arr.size, self._norm_w, self._norm_h)
                    return Gst.FlowReturn.OK
                out_bytes = self.norm(arr[:n].reshape(self._norm_h, self._norm_w)).tobytes()
        finally:
            buf.unmap(mi)
        obuf = Gst.Buffer.new_wrapped(out_bytes)
        obuf.pts, obuf.dts, obuf.duration = buf.pts, buf.dts, buf.duration
        # keep frame_id/capture-ns riding across the pump (the latency probe reads offset_end)
        obuf.offset, obuf.offset_end = buf.offset, buf.offset_end
        ret = self.norm_out.emit("push-buffer", obuf)
        if ret != Gst.FlowReturn.OK:
            log.warning("normalize: push-buffer -> %s", ret)
        return Gst.FlowReturn.OK

    def _on_norm_eos(self, _sink):
        if self.norm_out is not None:
            self.norm_out.emit("end-of-stream")           # propagate EOS across the pump

    # ---- headered-shm pump (hdr_in appsink -> strip CAMF header, restamp -> hdr_out appsrc) ----
    def _hdr_warn(self, msg):
        now = time.monotonic()
        if now - self._hdr_warn_last >= 10.0:             # per-frame path: throttle, never spam
            self._hdr_warn_last = now
            log.warning("header pump: %s", msg)

    def _on_hdr_sample(self, sink):
        """Per frame: parse the 36-byte header, hand the PIXEL bytes on as a zero-copy sub-buffer
        (copy_region shares the memory), and re-attach what shm dropped: a relative monotonic PTS
        derived from the capture stamp, offset=frame_id, offset_end=absolute capture ns (the unixfd
        convention -- everything downstream, latency probes included, is transport-agnostic).
        A corrupt frame is dropped, throttled-warned, and the stream stays up."""
        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.OK
        buf = sample.get_buffer()
        ok, mi = buf.map(Gst.MapFlags.READ)
        if not ok:
            return Gst.FlowReturn.OK
        try:
            info = parse_header(mi.data)
        except HeaderError as e:
            self._hdr_warn("dropped frame ({})".format(e))
            return Gst.FlowReturn.OK
        finally:
            buf.unmap(mi)
        payload = buf.get_size() - info.header_len
        if payload <= 0:
            self._hdr_warn("dropped frame (no pixel bytes after the header)")
            return Gst.FlowReturn.OK
        # Geometry/format come from the HEADER (self-describing); env only contributes the CFA
        # pattern + rate hint. One caps set per format, re-stamped if the camera changes mid-stream
        # (rare -- reconnect at another ROI/format; the fmt_tap splice is one-shot, so a Bayer<->mono
        # flip after start still needs a bridge restart, which the warning says).
        key = (info.width, info.height, info.pixfmt)
        if key != self._hdr_caps_key:
            try:
                fps = int(_env("CAM_FPS") or 0) or None
            except ValueError:
                fps = None
            caps_s = caps_for_frame(info, bayer=_env("CAM_BAYER"),
                                    debayer=debayer_enabled(_env("CAM_WEBRTC_DEBAYER", "auto")),
                                    fps=fps)
            self.hdr_out.set_property("caps", Gst.Caps.from_string(caps_s))
            if self._hdr_caps_key is None:
                log.info("header pump: stream is %s (ts_source=%s)", caps_s, info.ts_source)
                if not self._advertised:
                    # the pump path advertises HERE, not on cam_src PLAYING (see _on_message):
                    # hdr_out now carries the true caps, so the descriptor gets real geometry.
                    GLib.idle_add(self._advertise)
            else:
                log.warning("header pump: input format changed mid-stream (%s); if the preview "
                            "breaks, restart the bridge (the format seam resolves once)", caps_s)
            self._hdr_caps_key = key
        obuf = buf.copy_region(Gst.BufferCopyFlags.MEMORY, info.header_len, payload)
        obuf.pts = self._hdr_pts.pts_for(info.timestamp_ns)
        obuf.dts = Gst.CLOCK_TIME_NONE
        obuf.duration = Gst.CLOCK_TIME_NONE
        obuf.offset = info.frame_id
        obuf.offset_end = info.timestamp_ns
        self._ts_source = info.ts_source
        lat = self._capture_lat_ms(info.timestamp_ns)
        if lat is not None:
            self._lat_rx.append(lat)
        ret = self.hdr_out.emit("push-buffer", obuf)
        if ret != Gst.FlowReturn.OK:
            self._hdr_warn("push-buffer -> {}".format(ret))
        return Gst.FlowReturn.OK

    def _on_hdr_eos(self, _sink):
        if self.hdr_out is not None:
            self.hdr_out.emit("end-of-stream")            # propagate EOS across the pump

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
    return 1 if bridge.had_error else 0


if __name__ == "__main__":
    sys.exit(main())
