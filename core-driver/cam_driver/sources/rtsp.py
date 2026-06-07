"""RTSP capture source (a GstPipelineSource).

RTSP delivers ENCODED video (H.264/H.265/MJPEG over RTP), so it's a dual-output encoded source --
exactly like USB-MJPEG, just a different head: `rtspsrc ! <rtp depay> ! <parser> ! tee`. The
recording stream-copies the delivered bitstream (faithful to what arrived); the decode branch
feeds raw to consumers. All the tee correlation / stamping / dual delivery is the shared base.

Self-configuring: a lightweight `rtspsrc ! parsebin` pre-flight probe (open(), depay+parse only --
no decode) reads the live stream's codec + resolution + framerate, so the config needn't track the
camera (it can switch codec/res out from under us -- e.g. H.265 720p -> H.264 4K). Config is fallback. RTCP -> NTP wall-clock
provenance (add-reference-timestamp-meta, gst>=1.24) + HW decode (nvv4l2decoder via select_decoder)
round it out.
"""
from __future__ import annotations

import logging

import gi
gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst

from ..formats import encoded_info, select_decoder
from ..timestamps import TimestampSource
from .gstbase import GstPipelineSource

log = logging.getLogger(__name__)

# RTP depayloader per codec (rtspsrc -> depay -> parser -> tee).
_RTP_DEPAY = {"H264": "rtph264depay", "H265": "rtph265depay",
              "MJPEG": "rtpjpegdepay", "JPEG": "rtpjpegdepay"}

# rtspsrc's reference-timestamp-meta carries the reconstructed sender clock in the NTP epoch
# (seconds since 1900-01-01); subtract this to get the Unix epoch (1970-01-01) our other stamps use.
_NTP_EPOCH_OFFSET_NS = 2208988800 * 1_000_000_000

# parsed elementary-stream caps name -> our codec string.
_CAPS_CODEC = {"video/x-h264": "h264", "video/x-h265": "h265",
               "image/jpeg": "mjpeg", "video/x-jpeg": "mjpeg"}


def _probe_rtsp(url, protocols="", timeout_s=8):
    """Lightweight live-stream probe -> (codec, width, height, fps), or None on failure.

    `rtspsrc ! parsebin`: parsebin auto-plugs the RTP depayloader + bitstream parser but NO decoder,
    so we read codec + geometry off the PARSED elementary-stream caps without decoding a single frame.
    This is far lighter than gst-discoverer (which auto-plugs a full decoder -- on Jetson that spins up
    NVDEC and waits to decode a keyframe, 2-11s + a second heavy session that contends with the main
    connection). parsebin usually negotiates caps straight from the SDP's sprop parameter sets (no media
    wait at all); worst case it waits for the first in-band SPS. Uses the SAME transport as the real
    connection so a UDP-blocked (Docker NAT) path probes over TCP too."""
    proto = f" protocols={protocols}" if protocols else ""
    try:
        pipeline = Gst.parse_launch(f"rtspsrc name=src location={url} latency=0{proto} ! parsebin name=pb")
    except Exception as e:   # GLib.Error: bad uri / missing element
        log.warning("rtsp probe of %s failed to build: %s", url, e)
        return None
    result = {}
    loop = GLib.MainLoop()

    def read_caps(caps) -> bool:
        if not caps or not caps.get_size():
            return False
        s = caps.get_structure(0)
        codec = _CAPS_CODEC.get(s.get_name())
        okw, w = s.get_int("width")
        okh, h = s.get_int("height")
        if not codec or not (okw and okh and w > 0 and h > 0):
            return False
        okf, fn, fd = s.get_fraction("framerate")
        result.update(codec=codec, w=int(w), h=int(h), fps=(fn / fd if (okf and fd) else 0.0))
        loop.quit()
        return True

    def on_pad(_pb, pad):
        if read_caps(pad.get_current_caps()):    # often already negotiated from the SDP sprop sets
            return
        # not yet: pull data into a fakesink so the parser negotiates, and catch the CAPS event
        sink = Gst.ElementFactory.make("fakesink")
        sink.set_property("sync", False)
        pipeline.add(sink)
        sink.sync_state_with_parent()
        pad.link(sink.get_static_pad("sink"))

        def on_event(_p, info):
            ev = info.get_event()
            if ev.type == Gst.EventType.CAPS and read_caps(ev.parse_caps()):
                return Gst.PadProbeReturn.REMOVE
            return Gst.PadProbeReturn.OK
        pad.add_probe(Gst.PadProbeType.EVENT_DOWNSTREAM, on_event)

    pipeline.get_by_name("pb").connect("pad-added", on_pad)
    bus = pipeline.get_bus()
    bus.add_signal_watch()
    bus.connect("message::error", lambda *_: loop.quit())
    GLib.timeout_add_seconds(int(timeout_s), loop.quit)
    pipeline.set_state(Gst.State.PLAYING)
    loop.run()
    pipeline.set_state(Gst.State.NULL)
    if "codec" in result:
        return result["codec"], result["w"], result["h"], result["fps"]
    log.warning("rtsp probe of %s: no parsable video stream within %ss", url, timeout_s)
    return None


class RtspSource(GstPipelineSource):
    def __init__(self, cfg):   # cfg = config.RtspConfig
        super().__init__()
        self.cfg = cfg
        # Liveness watchdog (base GstPipelineSource): reopen the stream if it stalls / never delivers.
        self._reconnect = bool(getattr(cfg, "reconnect", True))
        self._reconnect_timeout_s = float(getattr(cfg, "reconnect_timeout_s", 5.0))
        # codec/geometry are RESOLVED in open(): a live probe (default) wins over these config
        # fallbacks, and _enc/_depay are computed there once the codec is known.
        self._codec = (cfg.codec or "").lower()
        self._width = int(cfg.width)
        self._height = int(cfg.height)
        self._fps = float(cfg.frame_rate)
        self._enc = None
        self._depay = None
        self._ntp_enabled = False                        # set in _configure_pipeline (gst>=1.24)
        self._ntp_anchor = None                          # (buf_pts, ntp_ns): last frame that had the meta

    def open(self) -> None:
        super().open()
        # Self-configure from the LIVE stream (gst-discoverer): codec/resolution/framerate. Config is
        # only a fallback (probe disabled, or camera unreachable -- in which case the stream fails anyway).
        # This is what lets the camera change codec/res without a config edit.
        if getattr(self.cfg, "probe", True):
            probed = _probe_rtsp(self.cfg.url, getattr(self.cfg, "protocols", ""))
            if probed:
                codec, w, h, fps = probed
                self._codec, self._width, self._height = codec, w, h
                self._fps = fps or float(self.cfg.frame_rate)   # parser may not expose fps (informational)
                log.info("rtsp probe: %s -> codec=%s %dx%d @%.0ffps (parsed from the live stream, no decode)",
                         self.cfg.url, self._codec, self._width, self._height, self._fps)
            else:
                log.warning("rtsp probe failed; falling back to configured codec=%s %dx%d",
                            self._codec, self._width, self._height)
        self._enc = encoded_info(self._codec)
        self._depay = _RTP_DEPAY.get(self._codec.upper())
        if self._enc is None or self._depay is None:
            raise ValueError(f"unsupported rtsp codec {self._codec!r} (known: h264, h265, mjpeg)")
        log.info("rtsp source: %s codec=%s %dx%d -> stream-copy record + decode for consumers",
                 self.cfg.url, self._codec, self._width, self._height)

    def reopen(self) -> None:
        # The rebuilt pipeline restarts the PTS timeline, so drop the NTP extrapolation anchor (it's
        # keyed on the old PTS). The first frame after reconnect re-anchors from its RTCP->NTP meta.
        self._ntp_anchor = None
        super().reopen()

    def _pipeline_desc(self) -> str:
        caps, parser, sw_decoder = self._enc
        decoder, conv = select_decoder(sw_decoder, self._hw_decode_available())
        w, h = self._width, self._height
        log.info("rtsp decode branch: %s ! %s (%s)", decoder, conv,
                 "HW NVDEC" if decoder == "nvv4l2decoder" else "software")
        # Parse in the DECODE branch only (after the tee), not pre-tee: the decoder needs a parser
        # ahead of it, while the encoded branch must hand the recorder the RAW depayed bitstream so the
        # recorder's own parser frames it exactly once. A pre-tee parser would frame it twice (here + the
        # recorder). Mirrors the working UsbSource structure. rtspsrc is named so _configure_pipeline can
        # turn on the RTCP->NTP reference meta (gst>=1.24); the NTP stamp rides the pre-tee buffer, so the
        # stamp probe there gives BOTH the recording and the live consumers the same wall-clock stamp.
        proto = f" protocols={self.cfg.protocols}" if getattr(self.cfg, "protocols", "") else ""
        return (
            f"rtspsrc name=src location={self.cfg.url} latency={int(self.cfg.latency_ms)}{proto} ! "
            f"{self._depay} ! tee name=st "
            # decode branch is BEST-EFFORT (consumers/preview): leaky so a slow/stalled decoder
            # (e.g. nvv4l2decoder waiting for a keyframe at 4K) drops encoded AUs HERE instead of
            # back-pressuring the non-leaky tee and starving the must-not-drop recording (encsink) below.
            f"st. ! queue leaky=downstream max-size-buffers=8 ! {parser} ! {decoder} ! {conv} ! "
            f"video/x-raw,format=I420,width={w},height={h} ! "
            f"appsink name=rawsink emit-signals=true max-buffers=4 drop=true sync=false "
            # encoded branch must-not-drop: it's the faithful recording
            f"st. ! queue ! appsink name=encsink emit-signals=true max-buffers=8 drop=false sync=false"
        )

    def _configure_pipeline(self) -> None:
        """Enable rtspsrc add-reference-timestamp-meta when the running GStreamer supports it (gst>=1.24,
        i.e. JP7) so each buffer carries the camera's RTCP->NTP wall-clock as a GstReferenceTimestampMeta.
        It only ATTACHES metadata -- PTS/pipeline timing is untouched (no ntp-sync re-timing). On gst<1.24
        the property is absent and we silently keep arrival timestamps."""
        src = self._pipeline.get_by_name("src")
        if src is not None and src.find_property("add-reference-timestamp-meta") is not None:
            src.set_property("add-reference-timestamp-meta", True)
            self._ntp_enabled = True
            log.info("rtsp: RTCP->NTP provenance enabled (add-reference-timestamp-meta)")
        else:
            log.info("rtsp: no add-reference-timestamp-meta on this GStreamer (<1.24); arrival timestamps")

    def _extract_capture(self, buf):
        """Read the per-frame wall-clock the camera reports via RTCP, attached by rtspsrc as a
        GstReferenceTimestampMeta (reference 'timestamp/x-ntp', NTP epoch).

        Real cameras drop the meta on the odd frame (~1/s, around RTCP SR boundaries). Once we're
        locked onto the NTP timeline we must NOT fall back to wall-clock arrival for those frames --
        the camera clock is offset from wall-clock, so mixing the two injects large backward jumps.
        Instead extrapolate along the NTP timeline using the buffer PTS delta: rtspsrc puts PTS on the
        same reconstructed RTP/sender timeline as the NTP meta, so the two stay consistent (no sawtooth).
        Only before the FIRST meta (true warm-up) do we fall back to arrival (SYSTEM)."""
        pts = int(buf.pts) if buf.pts != Gst.CLOCK_TIME_NONE else None
        meta = buf.get_reference_timestamp_meta()
        if meta is not None:
            ts = int(meta.timestamp)
            ref = meta.reference.to_string() if meta.reference else ""
            if "x-ntp" in ref:
                ts -= _NTP_EPOCH_OFFSET_NS        # NTP (1900) -> Unix (1970)
            if pts is not None:
                self._ntp_anchor = (pts, ts)
            return ts, TimestampSource.RTP_NTP
        if self._ntp_anchor is not None and pts is not None:
            a_pts, a_ts = self._ntp_anchor       # extrapolate on the NTP timeline via the PTS delta
            return a_ts + (pts - a_pts), TimestampSource.RTP_NTP
        return None, TimestampSource.SYSTEM

    @property
    def active_timestamp_source(self) -> str:
        return TimestampSource.RTP_NTP.value if self._ntp_enabled else TimestampSource.SYSTEM.value

    def geometry(self):
        return (0, 0, self._width, self._height)

    def pixel_format(self) -> str:
        return "I420"

    @property
    def encoded_caps(self):
        return self._enc[0]

    @property
    def encoded_parser(self):
        return self._enc[1]
