"""USB / v4l2 capture source.

RAW formats (GRAY8/I420/NV12/...):  [src] ! caps ! appsink -> on_frame.

ENCODED formats (MJPEG/H.264/H.265 -- a UVC cam, or the RTSP source later) DUAL-OUTPUT, so the
recording stays faithful (stream-copy, no re-encode) AND live consumers still get raw frames:

    [src] ! enc_caps ! tee
        tee. ! parse ! decoder ! videoconvert ! I420 ! appsink(raw)  -> on_frame   (consumers)
        tee. ! appsink(encoded)                                      -> on_encoded (stream-copy recorder)

A pad probe on the PRE-TEE stream stamps each frame exactly once, in order, keyed by buffer PTS,
so both branches deliver the SAME FrameStamp for a given frame -- decoder reorder (H.264 B-frames)
is handled because the lookup is by PTS, not arrival order. The decoder runs continuously in the
branch (proper streaming decode, not per-frame). HW decode (nvv4l2decoder) is a hardware
refinement; here we use software decoders. The encoded branch must-not-drop (it's the faithful
log); the raw branch is best-effort (consumers).
"""
from __future__ import annotations

import logging
import time
from collections import OrderedDict
from typing import Optional

import gi
gi.require_version("Gst", "1.0")
from gi.repository import Gst

from ..formats import encoded_info
from ..timestamps import FrameStamp, TimestampSource
from .base import OnFrame, Source

log = logging.getLogger(__name__)

# Fake encoders (videotestsrc raw -> encoded) so the encoded path is CI-testable without a device.
_FAKE_ENCODER = {
    "MJPEG": "jpegenc", "JPEG": "jpegenc",
    "H264": "x264enc tune=zerolatency key-int-max=15 speed-preset=ultrafast",
    "H265": "x265enc",
}


class UsbSource(Source):
    def __init__(self, cfg):   # cfg = config.UsbConfig
        self.cfg = cfg
        self._enc = encoded_info(cfg.pixel_format)   # None if raw; else (caps, parser, decoder)
        self._pipeline: Optional[Gst.Pipeline] = None
        self._rawsink: Optional[Gst.Element] = None
        self._encsink: Optional[Gst.Element] = None
        self._on_frame: Optional[OnFrame] = None
        self._on_encoded: Optional[OnFrame] = None
        self._frame_id = 0
        self._stamps: "OrderedDict[int, FrameStamp]" = OrderedDict()  # pts -> stamp (bounded)

    # ---- lifecycle ---------------------------------------------------------
    def open(self) -> None:
        Gst.init(None)
        kind = "FAKE (videotestsrc)" if self.cfg.fake else f"device={self.cfg.device} (v4l2)"
        mode = " [encoded -> stream-copy record + decode for consumers]" if self._enc else " [raw]"
        log.info("usb source: %s format=%s%s", kind, self.cfg.pixel_format, mode)

    def configure(self) -> None:
        fps = int(round(self.cfg.frame_rate or 30.0))
        w, h = int(self.cfg.width), int(self.cfg.height)
        sink_opts = "emit-signals=true max-buffers=4 drop=true sync=false"
        if self._enc:
            caps, parser, decoder = self._enc
            if self.cfg.fake:
                enc = _FAKE_ENCODER.get(self.cfg.pixel_format.upper(), "jpegenc")
                head = (f"videotestsrc is-live=true do-timestamp=true ! "
                        f"video/x-raw,width={w},height={h},framerate={fps}/1 ! {enc}")
            else:
                head = f"v4l2src device={self.cfg.device} do-timestamp=true"
            desc = (
                f"{head} ! {caps} ! tee name=st "
                f"st. ! queue ! {parser} ! {decoder} ! videoconvert ! "
                f"video/x-raw,format=I420,width={w},height={h} ! appsink name=rawsink {sink_opts} "
                # encoded branch must-not-drop: it's the faithful recording
                f"st. ! queue ! appsink name=encsink emit-signals=true max-buffers=8 drop=false sync=false"
            )
        else:
            caps = f"video/x-raw,format={self.cfg.pixel_format},width={w},height={h},framerate={fps}/1"
            head = ("videotestsrc is-live=true do-timestamp=true" if self.cfg.fake
                    else f"v4l2src device={self.cfg.device} do-timestamp=true")
            desc = f"{head} ! {caps} ! appsink name=rawsink {sink_opts}"
        log.info("usb source pipeline: %s", desc)
        self._pipeline = Gst.parse_launch(desc)
        self._rawsink = self._pipeline.get_by_name("rawsink")
        self._encsink = self._pipeline.get_by_name("encsink")   # None for a raw source
        if self._rawsink is None:
            raise RuntimeError("usb source 'rawsink' not found")
        if self._enc:
            tee = self._pipeline.get_by_name("st")
            tee.get_static_pad("sink").add_probe(Gst.PadProbeType.BUFFER, self._stamp_probe)

    def start(self, on_frame: OnFrame, on_encoded: OnFrame = None) -> None:
        self._on_frame = on_frame
        self._on_encoded = on_encoded
        self._rawsink.connect("new-sample", self._on_raw)
        if self._encsink is not None:
            self._encsink.connect("new-sample", self._on_enc)
        self._pipeline.set_state(Gst.State.PLAYING)

    def stop(self) -> None:
        if self._pipeline is not None:
            self._pipeline.set_state(Gst.State.NULL)

    # ---- correlation: stamp each frame once, pre-tee, keyed by PTS ----------
    def _new_stamp(self, pts_hint: int) -> FrameStamp:
        now = time.time_ns()   # arrival (CLOCK_REALTIME) -> wall-clock recording base; SOF policy = step 4
        st = FrameStamp(frame_id=self._frame_id, timestamp_ns=now, source=TimestampSource.SYSTEM,
                        system_ns=now, camera_ns=pts_hint, chunk_ns=None)
        self._frame_id += 1
        return st

    def _stamp_probe(self, pad, info) -> Gst.PadProbeReturn:
        buf = info.get_buffer()
        pts = int(buf.pts) if buf.pts != Gst.CLOCK_TIME_NONE else time.time_ns()
        self._stamps[pts] = self._new_stamp(pts)
        while len(self._stamps) > 240:
            self._stamps.popitem(last=False)   # bounded; evict oldest
        return Gst.PadProbeReturn.OK

    def _stamp_for(self, buf) -> FrameStamp:
        """Look up the pre-tee stamp by buffer PTS (both branches share it); fall back if evicted."""
        pts = int(buf.pts) if buf.pts != Gst.CLOCK_TIME_NONE else None
        st = self._stamps.get(pts) if pts is not None else None
        return st if st is not None else self._new_stamp(pts or time.time_ns())

    def _on_raw(self, sink) -> Gst.FlowReturn:
        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.OK
        buf = sample.get_buffer()
        stamp = self._stamp_for(buf) if self._enc else self._new_stamp(
            int(buf.pts) if buf.pts != Gst.CLOCK_TIME_NONE else time.time_ns())
        data = buf.extract_dup(0, buf.get_size())
        if self._on_frame is not None:
            self._on_frame(stamp, data)
        return Gst.FlowReturn.OK

    def _on_enc(self, sink) -> Gst.FlowReturn:
        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.OK
        buf = sample.get_buffer()
        stamp = self._stamp_for(buf)
        data = buf.extract_dup(0, buf.get_size())
        if self._on_encoded is not None:
            self._on_encoded(stamp, data)
        return Gst.FlowReturn.OK

    # ---- introspection -----------------------------------------------------
    def geometry(self):
        return (0, 0, int(self.cfg.width), int(self.cfg.height))

    def pixel_format(self) -> str:
        # encoded sources decode to I420 for the raw consumer path; the recorder uses encoded_caps
        return "I420" if self._enc else self.cfg.pixel_format

    @property
    def encoded_caps(self):
        return self._enc[0] if self._enc else None

    @property
    def encoded_parser(self):
        return self._enc[1] if self._enc else None

    @property
    def active_timestamp_source(self) -> str:
        return TimestampSource.SYSTEM.value
