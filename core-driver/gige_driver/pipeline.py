"""GStreamer pipeline: Aravis-fed appsrc -> tee -> [recorder][raw endpoint][preview],
plus a separate transport appsrc -> shmsink carrying header-framed frames for plugins.

The Aravis "new-buffer" callback is where the hw timestamp + frame_id live, so it:
  - sets the GstBuffer PTS = (timestamp - base) and OFFSET = frame_id,
  - pushes the raw video into the main appsrc (recorder / optional raw shm / preview),
  - and -- rate-limited -- pushes a header-prefixed copy into the transport appsrc for
    out-of-process plugins (wire format in gige_driver.transport).

Why a separate transport appsrc rather than a post-tee transform: shm carries only
bytes (no PTS/meta), so the plugin endpoint needs a custom `application/x-gige-frame`
payload (header + frame). Building that in the feeder -- which already holds the
absolute timestamp + frame_id -- avoids a fragile post-tee buffer rewrite, and rate
limiting becomes a simple time check here.

NOTE (packed formats): assumes 8-bit or 16-bit-aligned data (Mono8 / Mono16 / Bayer*8).
Packed formats (Mono10p/Mono12Packed) need a bit-unpack step not implemented yet.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Optional

import gi
gi.require_version("Gst", "1.0")
gi.require_version("Aravis", "0.8")
from gi.repository import Aravis, GLib, Gst

from . import recorder as rec
from . import transport
from .sidecar import SidecarHeader, SidecarWriter
from .timestamps import TimestampExtractor

log = logging.getLogger(__name__)

_BAYER_MAP = {"RG": "rggb", "GR": "grbg", "GB": "gbrg", "BG": "bggr"}


def parse_pixel_format(pixel_format: str):
    """Return (gst_raw_format, bits_per_pixel, bayer_pattern, packed)."""
    pf = pixel_format or "Mono8"
    bits = 16 if any(tok in pf for tok in ("16", "12", "10")) else 8
    packed = pf.endswith("p") or "Packed" in pf
    bayer = _BAYER_MAP.get(pf[5:7].upper()) if pf.startswith("Bayer") and len(pf) >= 7 else None
    gst_format = "GRAY16_LE" if bits > 8 else "GRAY8"
    return gst_format, bits, bayer, packed


class CapturePipeline:
    def __init__(self, cfg, camera, extractor: TimestampExtractor, sidecar: SidecarWriter):
        self.cfg = cfg
        self.camera = camera
        self.extractor = extractor
        self.sidecar = sidecar
        self.pipeline: Optional[Gst.Pipeline] = None
        self.appsrc: Optional[Gst.Element] = None
        self.transport_src: Optional[Gst.Element] = None
        self.loop: Optional[GLib.MainLoop] = None
        self._base_ts: Optional[int] = None
        self._last_pub_ts: Optional[int] = None
        self._n_pushed = 0
        self._gst_format = "GRAY8"
        self._bits = 8
        self._bayer = None
        self._width = 0
        self._height = 0
        self._image_size = 0
        self._stopping = False

    # ---- build -------------------------------------------------------------
    @staticmethod
    def _shm_size(endpoint, unit_bytes: int) -> int:
        return endpoint.shm_size if endpoint.shm_size > 0 else max(unit_bytes * 8, 1 << 20)

    @staticmethod
    def _ensure_socket_dir(path: str) -> None:
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)

    def build(self) -> str:
        Gst.init(None)
        _x, _y, width, height = self.camera.sensor_geometry()
        pf = self.camera.pixel_format_string()
        self._gst_format, self._bits, self._bayer, packed = parse_pixel_format(pf)
        self._width, self._height = int(width), int(height)
        if packed:
            log.warning("pixel format %s appears PACKED; fed as %s and will misinterpret data. "
                        "Use Mono8/Mono16/Bayer*8 or add an unpack step.", pf, self._gst_format)

        fps = self.cfg.camera.frame_rate
        framerate = f"{int(round(fps))}/1" if fps else "0/1"
        caps = (f"video/x-raw,format={self._gst_format},width={self._width},"
                f"height={self._height},framerate={framerate}")
        self._image_size = self._width * self._height * (2 if self._bits > 8 else 1)
        frame_bytes = self._image_size

        main = [
            f'appsrc name=camsrc is-live=true do-timestamp=false format=time caps="{caps}"',
            "queue max-size-buffers=8 name=src_q",
            "tee name=t",
        ]
        branches = []
        if self.cfg.recording.enabled:
            loc = f"{self.cfg.recording.output_dir.rstrip('/')}/{self.cfg.recording.name_prefix}"
            branches.append("t. ! " + rec.build_recorder_description(self.cfg.recording, self._bits, loc))

        raw = self.cfg.transport.raw_endpoint
        if raw.enabled:
            self._ensure_socket_dir(raw.socket_path)
            shm = self._shm_size(raw, frame_bytes)
            branches.append(
                f"t. ! queue leaky=downstream max-size-buffers=4 ! "
                f"shmsink socket-path={raw.socket_path} shm-size={shm} "
                f"wait-for-connection=false sync=false")
            log.info("raw video endpoint -> %s (%d-byte shm)", raw.socket_path, shm)

        if self.cfg.preview.enabled:
            branches.append(f"t. ! queue leaky=downstream max-size-buffers=4 ! videoconvert ! {self.cfg.preview.sink}")
        else:
            branches.append("t. ! queue leaky=downstream max-size-buffers=4 ! fakesink sync=false")

        chains = [" ! ".join(main) + " " + " ".join(branches)]

        pe = self.cfg.transport.plugin_endpoint
        if pe.enabled:
            self._ensure_socket_dir(pe.socket_path)
            shm = self._shm_size(pe, frame_bytes + transport.HEADER_SIZE)
            chains.append(
                f'appsrc name=transport_src is-live=true do-timestamp=false format=time '
                f'caps="{transport.CAPS}" ! queue max-size-buffers=8 ! '
                f"shmsink socket-path={pe.socket_path} shm-size={shm} "
                f"wait-for-connection=false sync=false")
            log.info("plugin transport endpoint -> %s (%d-byte shm, max_rate=%s)",
                     pe.socket_path, shm, pe.max_rate_hz or "unlimited")

        desc = "   ".join(chains)   # multiple top-level chains in one pipeline
        log.info("pipeline: %s", desc)
        self.pipeline = Gst.parse_launch(desc)
        self.appsrc = self.pipeline.get_by_name("camsrc")
        self.transport_src = self.pipeline.get_by_name("transport_src")
        if self.appsrc is None:
            raise RuntimeError("appsrc 'camsrc' not found after parse_launch")
        return desc

    # ---- the timestamp-extracting feeder ----------------------------------
    def _should_publish(self, ts_ns: int) -> bool:
        rate = self.cfg.transport.plugin_endpoint.max_rate_hz
        if rate <= 0:
            return True
        min_interval = 1_000_000_000.0 / rate
        if self._last_pub_ts is None or (ts_ns - self._last_pub_ts) >= min_interval:
            self._last_pub_ts = ts_ns
            return True
        return False

    def on_new_buffer(self, stream) -> None:
        buf = stream.try_pop_buffer()
        if buf is None:
            return
        if self._stopping:
            stream.push_buffer(buf)   # draining for EOS; stop feeding the pipeline
            return
        try:
            if buf.get_status() != Aravis.BufferStatus.SUCCESS:
                log.debug("drop buffer status=%s", buf.get_status())
                return
            stamp = self.extractor.extract(buf)
            if self._base_ts is None:
                self._base_ts = stamp.timestamp_ns
                self._write_header()
            pts = stamp.timestamp_ns - self._base_ts
            if pts < 0:
                pts = 0

            data = buf.get_data()
            if not data:
                return
            # get_data() returns image+chunks when chunk mode is on; keep only the image
            frame_bytes = bytes(data)[:self._image_size] if self._image_size else bytes(data)

            gbuf = Gst.Buffer.new_wrapped(frame_bytes)
            gbuf.pts = pts
            gbuf.dts = Gst.CLOCK_TIME_NONE
            gbuf.offset = stamp.frame_id
            ret = self.appsrc.emit("push-buffer", gbuf)
            if ret != Gst.FlowReturn.OK:
                log.warning("appsrc push-buffer -> %s", ret)

            # plugin transport endpoint: header + frame, rate-limited
            if self.transport_src is not None and self._should_publish(stamp.timestamp_ns):
                hdr = transport.FrameHeader(
                    timestamp_ns=stamp.timestamp_ns, frame_id=stamp.frame_id,
                    width=self._width, height=self._height,
                    pixfmt=self._gst_format, ts_source=stamp.source.value).pack()
                tbuf = Gst.Buffer.new_wrapped(hdr + frame_bytes)
                tbuf.pts = pts
                tbuf.offset = stamp.frame_id
                self.transport_src.emit("push-buffer", tbuf)

            self.sidecar.add(stamp, pts)
            if self._n_pushed < 5:  # quick eyeball; full per-frame data is in the CSV
                d_cc = (stamp.chunk_ns - stamp.camera_ns) if stamp.chunk_ns is not None else None
                d_sc = (stamp.system_ns - stamp.chunk_ns) if stamp.chunk_ns is not None else None
                log.info("ts[fid=%s] src=%s chunk=%s camera=%s system=%s  chunk-camera=%s system-chunk=%s",
                         stamp.frame_id, stamp.source.value, stamp.chunk_ns, stamp.camera_ns,
                         stamp.system_ns, d_cc, d_sc)
            self._n_pushed += 1
        finally:
            stream.push_buffer(buf)  # recycle the ArvBuffer back into the pool

    def _write_header(self) -> None:
        _x, _y, width, height = self.camera.sensor_geometry()
        self.sidecar.write_header(SidecarHeader(
            created_unix_s=time.time(),
            base_timestamp_ns=int(self._base_ts),
            timestamp_source=self.extractor.active_source.value,
            ptp_synced=self.extractor.ptp_locked,
            pixel_format=self.camera.pixel_format_string(),
            bayer_pattern=self.cfg.recording.bayer_pattern or self._bayer,
            bits_per_pixel=self._bits,
            width=int(width),
            height=int(height),
            tick_frequency_hz=self.camera.tick_frequency_hz,
        ))

    # ---- shutdown ----------------------------------------------------------
    def request_stop(self) -> None:
        """Clean stop: halt acquisition and inject EOS so the muxer finalizes the file.
        The bus EOS handler then quits the loop; a timer is the safety net."""
        if self._stopping:
            return
        self._stopping = True
        log.info("stop requested: stopping acquisition + sending EOS to finalize recording")
        self.camera.stop()
        for src in (self.appsrc, self.transport_src):
            if src is not None:
                src.emit("end-of-stream")
        GLib.timeout_add_seconds(5, self._force_quit)

    def _force_quit(self) -> bool:
        log.warning("EOS did not drain within 5s; forcing stop (recording may be truncated)")
        if self.loop and self.loop.is_running():
            self.loop.quit()
        return False

    # ---- run loop ----------------------------------------------------------
    def run(self) -> None:
        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_bus)

        self.loop = GLib.MainLoop()
        self.pipeline.set_state(Gst.State.PLAYING)

        stream = self.camera.stream
        stream.set_emit_signals(True)
        stream.connect("new-buffer", self.on_new_buffer)
        self.camera.start()
        log.info("running")
        try:
            self.loop.run()
        finally:
            self.shutdown()

    def _on_bus(self, _bus, msg) -> None:
        if msg.type == Gst.MessageType.ERROR:
            err, dbg = msg.parse_error()
            log.error("GStreamer ERROR: %s | %s", err, dbg)
            if self.loop:
                self.loop.quit()
        elif msg.type == Gst.MessageType.EOS:
            log.info("EOS")
            if self.loop:
                self.loop.quit()

    def shutdown(self) -> None:
        log.info("shutting down (pushed %d frames)", self._n_pushed)
        if not self._stopping:   # error/EOS path that didn't go through request_stop
            self.camera.stop()
        if self.pipeline:
            self.pipeline.set_state(Gst.State.NULL)
        self.sidecar.stop()
