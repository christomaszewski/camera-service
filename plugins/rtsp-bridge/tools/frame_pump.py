"""The rtsp-bridge frame pump: an appsink -> appsrc bridge inside each RTSP media pipeline.

One mechanism serves both special input paths (run.sh splits CAM_SRC_CHAIN at pump_in/pump_out
whenever either applies; a plain JP7 stream never constructs a pump):

  * JP6 header endpoint (application/x-cam-frame): every buffer is [36-byte header][pixels]. The
    pump validates the header, strips it (zero-copy buffer region when no stretch is needed), sets
    the output caps from the header's geometry/pixfmt, and restamps PTS from the header's PTP
    capture time -- shm transmits bytes only, so this is where timing and format re-enter the
    pipeline (same contract the C++ ros2/ros1 header bridges implement).
  * 16-bit normalize (CAM_RTSP_NORMALIZE, both platforms): GRAY16 frames are percentile-stretched
    to a visible GRAY8 (thermal_preview.PercentileStretch) before the 8-bit convert -- videoconvert
    alone keeps the TOP byte, near-black for LSB-aligned radiometric cameras.

The pump lives inside ON-DEMAND shared RTSP media: it is constructed per media in the factory's
media-configure handler and dies with the media. It must never stall or kill the streaming thread
-- anomalies drop the frame (warn once) and EOS is propagated across the split.

Pure decision logic (caps derivation, PTS policy, frame-size math) is gi-free at module top level
so test_frame_pump.py runs anywhere; only the FramePump class touches GStreamer.
"""
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cam_header

log = logging.getLogger("frame_pump")

CLOCK_TIME_NONE = 2 ** 64 - 1
_GRAY16 = ("GRAY16_LE", "GRAY16_BE")

# bytes per frame for the formats the core's header endpoint can carry (transport._CODE_TO_GST).
_BPP = {"GRAY8": 1, "GRAY16_LE": 2, "GRAY16_BE": 2, "RGB": 3, "BGR": 3,
        "RGBA": 4, "BGRA": 4, "RGBx": 4, "BGRx": 4, "YUY2": 2, "UYVY": 2, "NV24": 3}
_PLANAR_420 = ("I420", "NV12", "YV12")


def frame_bytes(fmt, width, height):
    """Minimum pixel-payload size for a frame, or 0 for an unknown format (skip the size check)."""
    if fmt in _PLANAR_420:
        return (width * height * 3) // 2
    return width * height * _BPP.get(fmt, 0)


def want_stretch(fmt, normalize, debayer_active):
    """Stretch only 16-bit mono, only when the knob is on, never on the (8-bit) debayer path."""
    return normalize is not None and not debayer_active and fmt in _GRAY16


def out_caps_str(fmt, width, height, fps=0, bayer="", debayer=False, stretch=False):
    """Caps the pump emits. The header endpoint publishes a Bayer mosaic as GRAY8 (the header codes
    carry no CFA pattern), so on the debayer path the config-supplied pattern (CAM_BAYER) relabels
    it video/x-bayer for the downstream bayer2rgb -- the same trick webrtc's JP6 branch uses."""
    if bayer and debayer and fmt == "GRAY8":
        media, ofmt = "video/x-bayer", bayer
    else:
        media, ofmt = "video/x-raw", ("GRAY8" if (stretch and fmt in _GRAY16) else fmt)
    caps = "{},format={},width={},height={}".format(media, ofmt, width, height)
    if fps:
        caps += ",framerate={}/1".format(int(fps))
    return caps


def next_pts(state, ts_ns, arrival_pts, duration_ns):
    """PTS policy for header-mode frames -> (pts, fell_back_now).

    Prefer the header's capture clock (smooth PTP-derived spacing, relative to the first frame).
    The FIRST anomaly -- absent, zero, or non-increasing timestamp (a PTP re-sync step) -- switches
    PERMANENTLY to arrival time (shmsrc do-timestamp) so the stream never oscillates between time
    bases; monotonicity is enforced across the switch. `state` is a plain dict owned by the caller.
    """
    fell_back_now = False
    if not state.get("fallback"):
        if ts_ns and ts_ns > 0:
            first = state.get("first")
            if first is None:
                state["first"], state["last"] = ts_ns, 0
                return 0, False
            pts = ts_ns - first
            if pts > state["last"]:
                state["last"] = pts
                return pts, False
        state["fallback"] = True
        fell_back_now = True
    step = duration_ns or 40 * 10 ** 6                    # unknown rate: assume 25 fps spacing
    if arrival_pts is not None and arrival_pts != CLOCK_TIME_NONE:
        pts = arrival_pts
    else:
        pts = state.get("synth", state.get("last", 0) + step)
        state["synth"] = pts + step
    if pts <= state.get("last", -1):                      # keep monotonic across the base switch
        pts = state["last"] + step
    state["last"] = pts
    return pts, fell_back_now


class FramePump:
    """Wire pump_in (appsink) to pump_out (appsrc) inside one RTSP media pipeline.

    strip_header: JP6 header-endpoint mode (parse + strip cam_header, restamp from PTP time).
    normalize:    (lo, hi) percentile window, or None = off (thermal_preview.parse_window output).
    Callers must have done gi.require_version("Gst", ...) before constructing.
    """

    def __init__(self, pump_in, pump_out, *, strip_header=False, normalize=None,
                 bayer="", debayer=False, fps=0, env_geometry=None):
        from gi.repository import Gst
        self._Gst = Gst
        self._out = pump_out
        self._strip = bool(strip_header)
        self._normalize = normalize
        self._bayer, self._debayer = bayer or "", bool(debayer)
        self._fps = int(fps or 0)
        self._env_geometry = env_geometry                 # (w, h) or None; cross-checked, not trusted
        self._dur = (10 ** 9 // self._fps) if self._fps else None
        self._configured = False
        self._passthrough = False
        self._stretch = None                              # PercentileStretch once configured
        self._np = None
        self._dtype = None
        self._w = self._h = 0
        self._fmt = ""
        self._min_bytes = 0
        self._pts_state = {}
        self._warned = set()
        pump_in.connect("new-sample", self._on_sample)
        pump_in.connect("eos", self._on_eos)

    def _warn_once(self, key, msg, *args):
        if key not in self._warned:
            self._warned.add(key)
            log.warning(msg, *args)

    # ---- configuration (one-shot, from the first frame) -------------------------------------
    def _make_stretch(self):
        import numpy as np
        from thermal_preview import PercentileStretch
        self._np = np
        lo, hi = self._normalize
        log.info("normalize: percentile window %g:%g (EMA-smoothed), 16->8 before encode", lo, hi)
        return PercentileStretch(lo, hi)

    def _configure_header(self, hdr):
        self._w, self._h, self._fmt = hdr.width, hdr.height, hdr.pixfmt
        if self._env_geometry and self._env_geometry != (hdr.width, hdr.height):
            self._warn_once("geom", "header geometry %dx%d != configured %sx%s; trusting the header",
                            hdr.width, hdr.height, *self._env_geometry)
        stretch = want_stretch(hdr.pixfmt, self._normalize, self._bayer and self._debayer)
        if self._normalize is not None and not stretch:
            self._warn_once("norm", "normalize requested but input is %s; passing through", hdr.pixfmt)
        if stretch:
            self._stretch = self._make_stretch()
            self._dtype = "<u2" if hdr.pixfmt == "GRAY16_LE" else ">u2"
        self._min_bytes = frame_bytes(hdr.pixfmt, hdr.width, hdr.height)
        caps = out_caps_str(hdr.pixfmt, hdr.width, hdr.height, fps=self._fps,
                            bayer=self._bayer, debayer=self._debayer, stretch=bool(self._stretch))
        self._out.set_property("caps", self._Gst.Caps.from_string(caps))
        log.info("pump: header endpoint %s %dx%d (ts_source=%s) -> %s",
                 hdr.pixfmt, hdr.width, hdr.height, hdr.ts_source, caps)
        self._configured = True

    def _configure_caps(self, caps):
        """JP7 normalize mode: derive everything from the negotiated input caps (self-describing
        unixfd stream). Non-GRAY16 input -> passthrough (knob set on the wrong camera; warn only)."""
        st = caps.get_structure(0)
        fmt = st.get_string("format") or ""
        ok_w, w = st.get_int("width")
        ok_h, h = st.get_int("height")
        if fmt in _GRAY16 and ok_w and ok_h:
            self._w, self._h, self._fmt = w, h, fmt
            self._dtype = "<u2" if fmt == "GRAY16_LE" else ">u2"
            self._stretch = self._make_stretch()
            out = caps.copy()
            out.set_value("format", "GRAY8")
            log.info("pump: %s %dx%d -> GRAY8 (percentile stretch)", fmt, w, h)
        else:
            self._passthrough = True
            out = caps
            self._warn_once("norm", "normalize requested but input caps are %s; passing through",
                            caps.to_string())
        self._out.set_property("caps", out)
        self._configured = True

    # ---- per-frame -------------------------------------------------------------------------
    def _on_sample(self, sink):
        Gst = self._Gst
        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.OK
        try:
            obuf = self._pump_header(sample) if self._strip else self._pump_caps(sample)
        except Exception as e:                            # noqa: BLE001 -- never kill the stream thread
            self._warn_once("pump-err", "pump error (%s); dropping frames of this shape", e)
            obuf = None
        if obuf is not None:
            ret = self._out.emit("push-buffer", obuf)
            if ret != Gst.FlowReturn.OK and ret != Gst.FlowReturn.FLUSHING:
                self._warn_once("push", "pump: push-buffer -> %s", ret)
        return Gst.FlowReturn.OK

    def _pump_header(self, sample):
        Gst = self._Gst
        buf = sample.get_buffer()
        size = buf.get_size()
        hsz = cam_header.HEADER_SIZE
        ok, mi = buf.map(Gst.MapFlags.READ)
        if not ok:
            return None
        try:
            hdr = cam_header.unpack_header(mi.data)       # raises TransportError on bad magic/version
            if not self._configured:
                self._configure_header(hdr)
            if self._min_bytes and size - hsz < self._min_bytes:
                self._warn_once("short", "short frame (%d payload < %d expected); dropped",
                                size - hsz, self._min_bytes)
                return None
            if self._stretch is not None:
                np = self._np
                arr = np.frombuffer(mi.data, dtype=self._dtype, count=self._w * self._h, offset=hsz)
                out_bytes = self._stretch(arr.reshape(self._h, self._w)).tobytes()
                obuf = Gst.Buffer.new_wrapped(out_bytes)
            else:
                obuf = None                               # sliced after unmap (no copy)
        finally:
            buf.unmap(mi)
        if obuf is None:
            # Zero-copy strip: a region buffer referencing the same memory, minus the header.
            obuf = buf.copy_region(Gst.BufferCopyFlags.MEMORY, hsz, size - hsz)
        pts, fell_back = next_pts(self._pts_state, hdr.timestamp_ns, buf.pts, self._dur)
        if fell_back:
            log.warning("pump: header capture clock absent/non-monotonic; using arrival time from "
                        "here on (PTP re-sync or no PTP lock)")
        obuf.pts = pts
        if self._dur:
            obuf.duration = self._dur
        obuf.offset = hdr.frame_id                        # mirror the unixfd metadata convention
        obuf.offset_end = hdr.timestamp_ns
        return obuf

    def _pump_caps(self, sample):
        Gst = self._Gst
        buf = sample.get_buffer()
        if not self._configured:
            self._configure_caps(sample.get_caps())
        if self._passthrough:
            return buf                                    # forward as-is (refcounted; PTS intact)
        ok, mi = buf.map(Gst.MapFlags.READ)
        if not ok:
            return None
        try:
            np = self._np
            n = self._w * self._h
            arr = np.frombuffer(mi.data, dtype=self._dtype, count=-1)
            if arr.size < n:
                self._warn_once("short", "short frame (%d px < %dx%d); dropped",
                                arr.size, self._w, self._h)
                return None
            out_bytes = self._stretch(arr[:n].reshape(self._h, self._w)).tobytes()
        finally:
            buf.unmap(mi)
        obuf = Gst.Buffer.new_wrapped(out_bytes)
        obuf.pts, obuf.dts, obuf.duration = buf.pts, buf.dts, buf.duration
        return obuf

    def _on_eos(self, _sink):
        self._out.emit("end-of-stream")                   # propagate EOS across the split
