"""USB / v4l2 capture source (step 3: the seam's second implementation).

Unlike the GVSP source (Aravis, not a GStreamer element), v4l2 *is* a GStreamer element,
so this source runs its OWN small pipeline -- `v4l2src` (real) or `videotestsrc` (fake) !
caps ! appsink -- and the appsink "new-sample" callback is the feeder: pull the buffer,
build a FrameStamp (timestamp + a frame counter + provenance), and hand the raw bytes to
the pipeline's on_frame. That appsink -> on_frame -> shared-appsrc bridge is the one copy
accepted in the frame-producer seam, so every source shares one
FrameStamp -> PTS -> sidecar path.

Step 3 is intentionally minimal: raw GRAY8 at a configured geometry, an arrival timestamp,
and a `fake` (videotestsrc) mode so it's CI-testable without a real device. DEFERRED to
step 4: MJPEG/H.264 HW-decode, YUYV/NV12 color + caps negotiation, the SOF-vs-arrival
timestamp policy, /dev/v4l/by-id device mgmt + hotplug/reconnect, and the recorder
stream-copy mode for already-compressed delivery.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

import gi
gi.require_version("Gst", "1.0")
from gi.repository import Gst

from ..timestamps import FrameStamp, TimestampSource
from .base import OnFrame, Source

log = logging.getLogger(__name__)


class UsbSource(Source):
    def __init__(self, cfg):   # cfg = config.UsbConfig
        self.cfg = cfg
        self._pipeline: Optional[Gst.Pipeline] = None
        self._appsink: Optional[Gst.Element] = None
        self._on_frame: Optional[OnFrame] = None
        self._frame_id = 0

    # ---- lifecycle ---------------------------------------------------------
    def open(self) -> None:
        Gst.init(None)
        # No persistent device handle: v4l2src opens the device when the pipeline plays.
        # (Real-device existence/caps probing is a step-4 concern.)
        log.info("usb source: %s", "FAKE (videotestsrc), no device"
                 if self.cfg.fake else f"device={self.cfg.device} (v4l2)")

    def configure(self) -> None:
        fps = self.cfg.frame_rate or 30.0
        caps = (f"video/x-raw,format={self.cfg.pixel_format},"
                f"width={int(self.cfg.width)},height={int(self.cfg.height)},"
                f"framerate={int(round(fps))}/1")
        head = ("videotestsrc is-live=true do-timestamp=true" if self.cfg.fake
                else f"v4l2src device={self.cfg.device} do-timestamp=true")
        desc = (f"{head} ! {caps} ! "
                f"appsink name=usbsink emit-signals=true max-buffers=4 drop=true sync=false")
        log.info("usb source pipeline: %s", desc)
        self._pipeline = Gst.parse_launch(desc)
        self._appsink = self._pipeline.get_by_name("usbsink")
        if self._appsink is None:
            raise RuntimeError("usb source appsink 'usbsink' not found")

    def start(self, on_frame: OnFrame) -> None:
        self._on_frame = on_frame
        self._appsink.connect("new-sample", self._on_sample)
        self._pipeline.set_state(Gst.State.PLAYING)

    def stop(self) -> None:
        if self._pipeline is not None:
            self._pipeline.set_state(Gst.State.NULL)

    # ---- the feeder (appsink "new-sample", on a GStreamer streaming thread) ----
    def _on_sample(self, sink) -> Gst.FlowReturn:
        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.OK
        buf = sample.get_buffer()
        # Step 3 provenance = host arrival (CLOCK_REALTIME), so the recording gets a
        # wall-clock base. The buffer PTS (running-time / kernel SOF when v4l2 provides it)
        # is kept as the camera_ns candidate; the real SOF-vs-arrival policy is step 4.
        now = time.time_ns()
        sof = int(buf.pts) if buf.pts != Gst.CLOCK_TIME_NONE else now
        stamp = FrameStamp(
            frame_id=self._frame_id, timestamp_ns=now, source=TimestampSource.SYSTEM,
            system_ns=now, camera_ns=sof, chunk_ns=None,
        )
        self._frame_id += 1
        frame_bytes = buf.extract_dup(0, buf.get_size())
        if self._on_frame is not None:
            self._on_frame(stamp, frame_bytes)
        return Gst.FlowReturn.OK

    # ---- introspection -----------------------------------------------------
    def geometry(self):
        return (0, 0, int(self.cfg.width), int(self.cfg.height))

    def pixel_format(self) -> str:
        return self.cfg.pixel_format

    @property
    def active_timestamp_source(self) -> str:
        return TimestampSource.SYSTEM.value
