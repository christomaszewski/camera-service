"""GVSP / Aravis capture source (the only source today).

Wraps the Aravis device (camera.GigECamera) with the PTP/chunk timestamp policy
(timestamps.TimestampExtractor) and an appsrc-feeder: the Aravis "new-buffer" signal fires
on its receive thread, where we pop the buffer, extract the hardware timestamp + frame id,
strip the appended chunk bytes, and hand (FrameStamp, image_bytes) to the pipeline's
on_frame callback. Reconnect (control-lost / frame-timeout -> reopen) lives here too, since
it's Aravis-specific.

This is the GVSP-specific frontend; everything from the pipeline's appsrc onward is shared.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

import gi
gi.require_version("Aravis", "0.8")
from gi.repository import Aravis

from ..camera import GigECamera
from ..timestamps import TimestampExtractor, TimestampSource
from .base import OnFrame, Source

log = logging.getLogger(__name__)


class GigeSource(Source):
    def __init__(self, cfg):   # cfg = config.CameraConfig
        self.cfg = cfg
        self.camera = GigECamera(cfg)
        self.extractor: Optional[TimestampExtractor] = None
        self._on_frame: Optional[OnFrame] = None
        self._image_size = 0
        self._last_buf_t = 0.0   # monotonic time of the last popped buffer (liveness)

    # ---- lifecycle ---------------------------------------------------------
    def open(self) -> None:
        self.camera.open()

    def configure(self) -> None:
        self.camera.configure()
        want_ptp = self.cfg.timestamp_source == "ptp_chunk"
        chunks_ok = self.camera.enable_chunks() if want_ptp else False
        ptp_ok = (self.camera.enable_ptp(self.cfg.ptp_lock_timeout_s)
                  if (want_ptp and self.cfg.ptp_enable) else False)
        try:
            prefer = TimestampSource(self.cfg.timestamp_source)
        except ValueError:
            log.warning("invalid timestamp_source %r; defaulting to ptp_chunk", self.cfg.timestamp_source)
            prefer = TimestampSource.PTP_CHUNK
        self.extractor = TimestampExtractor(
            chunk_parser=self.camera.chunk_parser,
            chunk_timestamp_name=self.cfg.chunk_timestamp_name,
            chunk_frame_id_name=self.cfg.chunk_frame_id_name,
            prefer=prefer,
            tick_frequency_hz=self.camera.tick_frequency_hz,
        )
        self.extractor.resolve_active_source(ptp_locked=ptp_ok, chunks_enabled=chunks_ok)
        self.camera.create_stream(self.cfg.n_stream_buffers)
        self._image_size = self._compute_image_size()

    def _compute_image_size(self) -> int:
        _x, _y, w, h = self.camera.sensor_geometry()
        pf = self.cfg.pixel_format or "Mono8"
        bytes_pp = 2 if any(tok in pf for tok in ("16", "12", "10")) else 1
        return int(w) * int(h) * bytes_pp

    def start(self, on_frame: OnFrame, on_encoded: OnFrame = None) -> None:
        self._on_frame = on_frame   # gige delivers raw only; on_encoded is unused (not an encoded source)
        stream = self.camera.stream
        stream.set_emit_signals(True)
        stream.connect("new-buffer", self._on_new_buffer)
        self._last_buf_t = time.monotonic()   # reset liveness so the watchdog ignores spin-up
        self.camera.start()

    def stop(self) -> None:
        self.camera.stop()

    # ---- the timestamp-extracting feeder (runs on the Aravis receive thread) ----
    def _on_new_buffer(self, stream) -> None:
        buf = stream.try_pop_buffer()
        if buf is None:
            return
        self._last_buf_t = time.monotonic()   # any buffer (even non-SUCCESS) => the stream is alive
        try:
            if buf.get_status() != Aravis.BufferStatus.SUCCESS:
                log.debug("drop buffer status=%s", buf.get_status())
                return
            stamp = self.extractor.extract(buf)
            data = buf.get_data()
            if not data:
                return
            # get_data() returns image+chunks when chunk mode is on; keep only the image
            frame_bytes = bytes(data)[:self._image_size] if self._image_size else bytes(data)
            if self._on_frame is not None:
                self._on_frame(stamp, frame_bytes)
        finally:
            stream.push_buffer(buf)  # recycle the ArvBuffer back into the pool

    # ---- introspection -----------------------------------------------------
    def geometry(self):
        return self.camera.sensor_geometry()

    def pixel_format(self) -> str:
        return self.camera.pixel_format_string()

    @property
    def tick_frequency_hz(self) -> int:
        return self.camera.tick_frequency_hz

    @property
    def ptp_locked(self) -> bool:
        return self.extractor.ptp_locked if self.extractor else False

    @property
    def active_timestamp_source(self) -> str:
        return self.extractor.active_source.value if self.extractor else self.cfg.timestamp_source

    # ---- reconnect ---------------------------------------------------------
    @property
    def reconnect_enabled(self) -> bool:
        return bool(self.cfg.reconnect)

    def is_disconnected(self) -> bool:
        since = time.monotonic() - self._last_buf_t
        return self.camera.control_lost or since > self.cfg.reconnect_timeout_s

    def reopen(self) -> None:
        """Full re-setup after a disconnect (raises CameraError/GLib.Error if not back yet);
        refresh the extractor's chunk parser to the new device. Caller re-arms via start()."""
        self.camera.reopen(self.cfg.n_stream_buffers)
        self.extractor.set_chunk_parser(self.camera.chunk_parser, self.camera.tick_frequency_hz)
