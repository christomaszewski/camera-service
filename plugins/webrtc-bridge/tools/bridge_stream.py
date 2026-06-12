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
from h264_level import h264_level_for, level_covers, LEVELS as H264_LEVELS

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("bridge_stream")


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
        self._advertised = False
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

    def build(self):
        Gst.init(None)
        desc = _env("CAM_PIPELINE")
        if not desc:
            log.error("CAM_PIPELINE not set; nothing to run")
            return False
        log.info("pipeline: %s", desc)
        self.pipeline = Gst.parse_launch(desc)
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

    def _encode_geometry(self):
        """(w, h, fps, source) actually fed to webrtcsink's encoder. Prefer webrtcsink's negotiated
        INPUT caps (authoritative -- correct on JP7 unixfd where env geometry is unset, and after any
        future videoscale on the branch); fall back to the source caps, then CAM_WIDTH/HEIGHT/FPS (valid
        on the JP6 raw-shm path, where the config IS the geometry)."""
        for el_name, which in (("cam_webrtcsink", "sink"), ("cam_src", "src")):
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
        ret = self.norm_out.emit("push-buffer", obuf)
        if ret != Gst.FlowReturn.OK:
            log.warning("normalize: push-buffer -> %s", ret)
        return Gst.FlowReturn.OK

    def _on_norm_eos(self, _sink):
        if self.norm_out is not None:
            self.norm_out.emit("end-of-stream")           # propagate EOS across the pump

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
