"""RTSP capture source (a GstPipelineSource).

RTSP delivers ENCODED video (H.264/H.265/MJPEG over RTP), so it's a dual-output encoded source --
exactly like USB-MJPEG, just a different head: `rtspsrc ! <rtp depay> ! <parser> ! tee`. The
recording stream-copies the delivered bitstream (faithful to what arrived); the decode branch
feeds raw to consumers. All the tee correlation / stamping / dual delivery is the shared base.

First cut: configured geometry (must match the stream) + arrival timestamps. Follow-ups (shared
with USB): dynamic caps negotiation from the stream, and RTP/RTCP -> NTP wall-clock provenance
(rtspsrc ntp-sync / rtpjitterbuffer add-reference-timestamp-meta). HW decode (nvv4l2decoder) is
a hardware refinement.
"""
from __future__ import annotations

import logging

import gi
gi.require_version("Gst", "1.0")
from gi.repository import Gst

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


class RtspSource(GstPipelineSource):
    def __init__(self, cfg):   # cfg = config.RtspConfig
        super().__init__()
        self.cfg = cfg
        self._enc = encoded_info(cfg.codec)              # (caps, parser, decoder); RTSP is always encoded
        self._depay = _RTP_DEPAY.get((cfg.codec or "").upper())
        self._ntp_enabled = False                        # set in _configure_pipeline (gst>=1.24)
        self._ntp_anchor = None                          # (buf_pts, ntp_ns): last frame that had the meta
        if self._enc is None or self._depay is None:
            raise ValueError(f"unsupported rtsp codec {cfg.codec!r} (known: h264, h265, mjpeg)")

    def open(self) -> None:
        super().open()
        log.info("rtsp source: %s codec=%s -> stream-copy record + decode for consumers",
                 self.cfg.url, self.cfg.codec)

    def _pipeline_desc(self) -> str:
        caps, parser, sw_decoder = self._enc
        decoder, conv = select_decoder(sw_decoder, self._hw_decode_available())
        w, h = int(self.cfg.width), int(self.cfg.height)
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
            f"st. ! queue ! {parser} ! {decoder} ! {conv} ! "
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
        return (0, 0, int(self.cfg.width), int(self.cfg.height))

    def pixel_format(self) -> str:
        return "I420"

    @property
    def encoded_caps(self):
        return self._enc[0]

    @property
    def encoded_parser(self):
        return self._enc[1]
