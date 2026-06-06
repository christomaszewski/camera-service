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

from ..formats import encoded_info
from .gstbase import GstPipelineSource

log = logging.getLogger(__name__)

# RTP depayloader per codec (rtspsrc -> depay -> parser -> tee).
_RTP_DEPAY = {"H264": "rtph264depay", "H265": "rtph265depay",
              "MJPEG": "rtpjpegdepay", "JPEG": "rtpjpegdepay"}


class RtspSource(GstPipelineSource):
    def __init__(self, cfg):   # cfg = config.RtspConfig
        super().__init__()
        self.cfg = cfg
        self._enc = encoded_info(cfg.codec)              # (caps, parser, decoder); RTSP is always encoded
        self._depay = _RTP_DEPAY.get((cfg.codec or "").upper())
        if self._enc is None or self._depay is None:
            raise ValueError(f"unsupported rtsp codec {cfg.codec!r} (known: h264, h265, mjpeg)")

    def open(self) -> None:
        super().open()
        log.info("rtsp source: %s codec=%s -> stream-copy record + decode for consumers",
                 self.cfg.url, self.cfg.codec)

    def _pipeline_desc(self) -> str:
        caps, parser, decoder = self._enc
        w, h = int(self.cfg.width), int(self.cfg.height)
        # Parse in the DECODE branch only (after the tee), not pre-tee: the decoder needs a parser
        # ahead of it, while the encoded branch must hand the recorder the RAW depayed bitstream so the
        # recorder's own parser frames it exactly once. A pre-tee parser would frame it twice (here + the
        # recorder). Mirrors the working UsbSource structure.
        return (
            f"rtspsrc location={self.cfg.url} latency={int(self.cfg.latency_ms)} ! "
            f"{self._depay} ! tee name=st "
            f"st. ! queue ! {parser} ! {decoder} ! videoconvert ! "
            f"video/x-raw,format=I420,width={w},height={h} ! "
            f"appsink name=rawsink emit-signals=true max-buffers=4 drop=true sync=false "
            # encoded branch must-not-drop: it's the faithful recording
            f"st. ! queue ! appsink name=encsink emit-signals=true max-buffers=8 drop=false sync=false"
        )

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
