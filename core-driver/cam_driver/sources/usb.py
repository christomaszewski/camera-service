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

from ..formats import encoded_info
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

    def open(self) -> None:
        super().open()
        kind = "FAKE (videotestsrc)" if self.cfg.fake else f"device={self.cfg.device} (v4l2)"
        mode = " [encoded -> stream-copy record + decode for consumers]" if self._enc else " [raw]"
        log.info("usb source: %s format=%s%s", kind, self.cfg.pixel_format, mode)

    def _pipeline_desc(self) -> str:
        fps = int(round(self.cfg.frame_rate or 30.0))
        w, h = int(self.cfg.width), int(self.cfg.height)
        raw_sink = "appsink name=rawsink emit-signals=true max-buffers=4 drop=true sync=false"
        if self._enc:
            caps, parser, decoder = self._enc
            if self.cfg.fake:
                enc = _FAKE_ENCODER.get(self.cfg.pixel_format.upper(), "jpegenc")
                head = (f"videotestsrc is-live=true do-timestamp=true ! "
                        f"video/x-raw,width={w},height={h},framerate={fps}/1 ! {enc}")
                tee_caps = caps                 # jpegenc output: bare media type is fine
            else:
                # real v4l2: pin width/height/framerate so the device negotiates the exact encoded mode
                # (a UVC cam offers many MJPEG resolutions; bare "image/jpeg" would let it pick any).
                head = f"v4l2src device={self.cfg.device} do-timestamp=true"
                tee_caps = f"{caps},width={w},height={h},framerate={fps}/1"
            return (
                f"{head} ! {tee_caps} ! tee name=st "
                f"st. ! queue ! {parser} ! {decoder} ! videoconvert ! "
                f"video/x-raw,format=I420,width={w},height={h} ! {raw_sink} "
                # encoded branch must-not-drop: it's the faithful recording
                f"st. ! queue ! appsink name=encsink emit-signals=true max-buffers=8 drop=false sync=false"
            )
        caps = f"video/x-raw,format={self.cfg.pixel_format},width={w},height={h},framerate={fps}/1"
        head = ("videotestsrc is-live=true do-timestamp=true" if self.cfg.fake
                else f"v4l2src device={self.cfg.device} do-timestamp=true")
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
