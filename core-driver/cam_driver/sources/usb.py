"""USB / v4l2 capture source (a GstPipelineSource).

RAW formats (GRAY8/I420/NV12/...):  [src] ! caps ! appsink(rawsink) -> on_frame.

ENCODED formats (MJPEG/H.264/H.265 from a UVC cam) DUAL-OUTPUT via the base's tee machinery, so
the recording stays faithful (stream-copy) AND live consumers get decoded raw:

    [src] ! enc_caps ! tee name=st
        st. ! parse ! decoder ! videoconvert ! I420 ! appsink(rawsink)  -> on_frame   (consumers)
        st. ! appsink(encsink)                                          -> on_encoded (stream-copy)

`fake` mode (videotestsrc, + an encoder for the encoded case) makes both paths CI-testable with no
device. HW decode (nvv4l2decoder) + dynamic caps negotiation + the SOF timestamp policy are
hardware/follow-up items.
"""
from __future__ import annotations

import logging
import time

import gi
gi.require_version("Gst", "1.0")
from gi.repository import Gst

from ..formats import encoded_info, select_decoder
from ..timestamps import TimestampSource
from .gstbase import GstPipelineSource

log = logging.getLogger(__name__)

# Fake encoders (videotestsrc raw -> encoded) so the encoded path is CI-testable without a device.
_FAKE_ENCODER = {
    "MJPEG": "jpegenc", "JPEG": "jpegenc",
    "H264": "x264enc tune=zerolatency key-int-max=15 speed-preset=ultrafast",
    "H265": "x265enc",
}


class UsbSource(GstPipelineSource):
    def __init__(self, cfg):   # cfg = config.UsbConfig
        super().__init__()
        self.cfg = cfg
        self._enc = encoded_info(cfg.pixel_format)   # None if raw; else (caps, parser, decoder)
        # SOF = the v4l2 DRIVER per-frame timestamp (do-timestamp=false). Opt-in + real-device only
        # (the fake videotestsrc has no driver clock); per-frame we still fall back to arrival on a bad ts.
        self._sof = bool(getattr(cfg, "sof_timestamps", False)) and not cfg.fake

    def open(self) -> None:
        super().open()
        kind = "FAKE (videotestsrc)" if self.cfg.fake else f"device={self.cfg.device} (v4l2)"
        mode = " [encoded -> stream-copy record + decode for consumers]" if self._enc else " [raw]"
        log.info("usb source: %s format=%s%s ts=%s", kind, self.cfg.pixel_format, mode,
                 "sof (v4l2 driver)" if self._sof else "arrival")

    def _pipeline_desc(self) -> str:
        fps = int(round(self.cfg.frame_rate or 30.0))
        w, h = int(self.cfg.width), int(self.cfg.height)
        raw_sink = "appsink name=rawsink emit-signals=true max-buffers=4 drop=true sync=false"
        v4l2_dots = "false" if self._sof else "true"   # false => buf.pts carries the v4l2 driver ts (SOF)
        if self._enc:
            caps, parser, sw_decoder = self._enc
            decoder, conv = select_decoder(sw_decoder, self._hw_decode_available())
            log.info("usb decode branch: %s ! %s (%s)", decoder, conv,
                     "HW NVDEC" if conv == "nvvidconv" else "software")
            if self.cfg.fake:
                enc = _FAKE_ENCODER.get(self.cfg.pixel_format.upper(), "jpegenc")
                head = (f"videotestsrc is-live=true do-timestamp=true ! "
                        f"video/x-raw,width={w},height={h},framerate={fps}/1 ! {enc}")
                tee_caps = caps                 # jpegenc output: bare media type is fine
            else:
                # real v4l2: pin width/height/framerate so the device negotiates the exact encoded mode
                # (a UVC cam offers many MJPEG resolutions; bare "image/jpeg" would let it pick any).
                head = f"v4l2src device={self.cfg.device} do-timestamp={v4l2_dots}"
                tee_caps = f"{caps},width={w},height={h},framerate={fps}/1"
            return (
                f"{head} ! {tee_caps} ! tee name=st "
                f"st. ! queue ! {parser} ! {decoder} ! {conv} ! "
                f"video/x-raw,format=I420,width={w},height={h} ! {raw_sink} "
                # encoded branch must-not-drop: it's the faithful recording
                f"st. ! queue ! appsink name=encsink emit-signals=true max-buffers=8 drop=false sync=false"
            )
        caps = f"video/x-raw,format={self.cfg.pixel_format},width={w},height={h},framerate={fps}/1"
        head = ("videotestsrc is-live=true do-timestamp=true" if self.cfg.fake
                else f"v4l2src device={self.cfg.device} do-timestamp={v4l2_dots}")
        return f"{head} ! {caps} ! {raw_sink}"

    # ---- introspection -----------------------------------------------------
    def geometry(self):
        return (0, 0, int(self.cfg.width), int(self.cfg.height))

    def pixel_format(self) -> str:
        # encoded sources decode to I420 for the consumer path; the recorder uses encoded_caps
        return "I420" if self._enc else self.cfg.pixel_format

    @property
    def encoded_caps(self):
        return self._enc[0] if self._enc else None

    @property
    def encoded_parser(self):
        return self._enc[1] if self._enc else None

    # ---- timestamp provenance ----------------------------------------------
    def _extract_capture(self, buf):
        """SOF: lift the v4l2 driver's per-frame timestamp (buf.pts, since do-timestamp=false) into the
        wall-clock epoch. Only when enabled; sanity-check against arrival so a driver that reports zeros
        or garbage falls back to arrival. A cam that stamps at start-of-exposure beats arrival; one that
        stamps at dequeue ~= arrival (no gain, but still the driver's ts)."""
        if not self._sof:
            return None, TimestampSource.SYSTEM
        pts = int(buf.pts) if buf.pts != Gst.CLOCK_TIME_NONE else None
        sof = self._pts_to_realtime(pts) if pts is not None else None
        now = time.time_ns()
        if sof is not None and -50_000_000 <= now - sof <= 2_000_000_000:  # plausible capture time
            return sof, TimestampSource.SOF
        return None, TimestampSource.SYSTEM

    @property
    def active_timestamp_source(self) -> str:
        return TimestampSource.SOF.value if self._sof else TimestampSource.SYSTEM.value
