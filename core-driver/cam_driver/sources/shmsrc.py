"""Shared-memory INPUT source (a GstPipelineSource): frames from another process's shmsink.

Two framings, one socket (docs/TRANSPORT.md "Input"):

  raw     -- ANY GStreamer pipeline's `video/x-raw` on a plain shmsink: a simulator's render, a
             point-cloud preview, another instance's raw endpoint. shm carries bytes only (no caps,
             no timestamps), so the caps are PINNED in config and every frame is stamped on
             arrival with a minted frame id -- exactly the USB/RTSP posture.
  header  -- the service's own transport (`application/x-cam-frame`, transport.FrameHeader) from a
             writer that owns real per-frame timestamps: the stamp, frame id and provenance come
             from the header; geometry/format are CHECKED against the pinned config.

The WRITER owns the socket; this source is the client. It waits for the socket to appear, and a
writer restart is a reconnect, not a fault: a bus ERROR or data starvation flips is_disconnected()
and the pipeline's backoff loop reopens (reopen() raises FileNotFoundError while the socket is
absent -- a cheap retry, no half-built pipeline). A frame that does not match the pinned config
(size, or a header's geometry/format) is a CONFIG mismatch a reopen cannot fix: reopen() raises
SourceConfigChanged and the process exits legibly for a restart with the right config.
"""
from __future__ import annotations

import logging
import os
import time

import gi
gi.require_version("Gst", "1.0")
from gi.repository import Gst

from .. import transport
from ..formats import bytes_per_frame, parse_pixel_format
from ..timestamps import FrameStamp, TimestampSource
from .base import SourceConfigChanged
from .gstbase import GstPipelineSource

log = logging.getLogger(__name__)


class ShmSource(GstPipelineSource):
    def __init__(self, cfg):   # cfg = config.ShmConfig
        super().__init__()
        self.cfg = cfg
        self._header = cfg.framing == "header"
        self._gst_format = parse_pixel_format(cfg.pixel_format)[0]
        self._expected = bytes_per_frame(self._gst_format, cfg.width, cfg.height)
        self._reconnect = bool(getattr(cfg, "reconnect", True))
        self._reconnect_timeout_s = float(getattr(cfg, "reconnect_timeout_s", 5.0))
        self._mismatch: str | None = None      # a config mismatch, surfaced by reopen() as fatal
        self._errored = False                  # the reader pipeline posted ERROR (socket gone/absent)
        self._bad_headers = 0
        self._last_source = TimestampSource.SYSTEM
        self._delivered = 0

    # ---- lifecycle ---------------------------------------------------------
    def open(self) -> None:
        super().open()
        present = os.path.exists(self.cfg.socket_path)
        log.info("shm source: %s (%s framing, %s %dx%d @ %s fps, %d bytes/frame)%s",
                 self.cfg.socket_path, self.cfg.framing, self.cfg.pixel_format, self.cfg.width,
                 self.cfg.height, f"{self.cfg.frame_rate:g}", self._expected,
                 "" if present else " -- socket not present yet: waiting for the writer")

    def configure(self) -> None:
        self._errored = False
        super().configure()
        bus = self._pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message::error", self._on_bus_error)

    def _on_bus_error(self, _bus, msg) -> None:
        err, dbg = msg.parse_error()
        log.warning("shm reader error: %s (%s) -- the writer is gone or not up yet; reconnecting",
                    err, (dbg or "").splitlines()[0][:120] if dbg else "")
        self._errored = True

    def stop(self) -> None:
        if self._pipeline is not None:
            try:
                self._pipeline.get_bus().remove_signal_watch()
            except Exception:   # noqa: BLE001
                pass
        super().stop()

    def reopen(self) -> None:
        if self._mismatch:
            raise SourceConfigChanged(self._mismatch)
        if not os.path.exists(self.cfg.socket_path):
            raise FileNotFoundError(f"shm socket {self.cfg.socket_path} not present -- is the writer up? "
                                    f"(it owns the socket; this source is the client)")
        super().reopen()

    # ---- reader pipeline ---------------------------------------------------
    def _pipeline_desc(self) -> str:
        src = f'shmsrc socket-path="{self.cfg.socket_path}" is-live=true do-timestamp=true'
        raw_sink = "appsink name=rawsink emit-signals=true max-buffers=4 drop=true sync=false"
        if self._header:
            return f"{src} ! {transport.CAPS} ! {raw_sink}"
        fps = max(1, int(round(self.cfg.frame_rate or 10.0)))
        caps = f"video/x-raw,format={self._gst_format},width={self.cfg.width},height={self.cfg.height},framerate={fps}/1"
        return f"{src} ! {caps} ! {raw_sink}"

    # ---- liveness ----------------------------------------------------------
    def is_disconnected(self) -> bool:
        if self._mismatch is not None:
            return True   # reopen() turns it into the legible fatal
        if self._errored and self._started:
            return True
        return super().is_disconnected()

    # ---- delivery ----------------------------------------------------------
    def _fail_config(self, what: str) -> None:
        if self._mismatch is None:
            self._mismatch = (f"shm input does not match the pinned config: {what} -- fix `shm:` "
                              f"(pixel_format/width/height) to what the writer sends")
            log.error("%s", self._mismatch)

    def _on_raw(self, sink) -> Gst.FlowReturn:
        sample = sink.emit("pull-sample")
        if sample is None or self._mismatch is not None:
            return Gst.FlowReturn.OK
        buf = sample.get_buffer()
        data = buf.extract_dup(0, buf.get_size())
        now = time.time_ns()
        if self._header:
            try:
                hdr = transport.unpack_header(data)
            except transport.TransportError as e:
                self._bad_headers += 1
                if self._bad_headers in (1, 100, 10_000):
                    log.warning("shm input: unparseable header (%s) -- %d so far; is the writer sending "
                                "application/x-cam-frame, or should framing be `raw`?", e, self._bad_headers)
                return Gst.FlowReturn.OK
            if (hdr.width, hdr.height) != (self.cfg.width, self.cfg.height) or hdr.pixfmt != self._gst_format:
                self._fail_config(f"the writer sends {hdr.pixfmt} {hdr.width}x{hdr.height}, config pins "
                                  f"{self._gst_format} {self.cfg.width}x{self.cfg.height}")
                return Gst.FlowReturn.OK
            pixels = data[transport.HEADER_SIZE:]
            if len(pixels) != self._expected:
                self._fail_config(f"a header frame carries {len(pixels)} pixel bytes, "
                                  f"{self._gst_format} {self.cfg.width}x{self.cfg.height} is {self._expected}")
                return Gst.FlowReturn.OK
            try:
                source = TimestampSource(hdr.ts_source)
            except ValueError:
                source = TimestampSource.SYSTEM
            self._last_source = source
            stamp = FrameStamp(frame_id=int(hdr.frame_id), timestamp_ns=int(hdr.timestamp_ns), source=source,
                               system_ns=now, camera_ns=int(hdr.timestamp_ns), chunk_ns=None)
            self._last_data_ns = now
        else:
            if len(data) != self._expected:
                self._fail_config(f"a raw frame is {len(data)} bytes, {self._gst_format} "
                                  f"{self.cfg.width}x{self.cfg.height} is {self._expected}")
                return Gst.FlowReturn.OK
            pixels = data
            stamp = self._new_stamp(buf)   # arrival, minted id: the USB/RTSP posture
        self._delivered += 1
        self._raw_delivered += 1
        if self._on_frame is not None:
            self._on_frame(stamp, pixels)
        return Gst.FlowReturn.OK

    # ---- introspection -----------------------------------------------------
    def geometry(self):
        return (0, 0, int(self.cfg.width), int(self.cfg.height))

    def pixel_format(self) -> str:
        return self.cfg.pixel_format

    @property
    def delivered_frame_rate(self):
        return float(self.cfg.frame_rate) if self.cfg.frame_rate else None

    @property
    def active_timestamp_source(self) -> str:
        return self._last_source.value if self._header else TimestampSource.SYSTEM.value
