"""Aravis camera setup: discovery, feature configuration, PTP, chunk mode, stream.

Key facts baked in here (see project research):
  * FLIR's on-camera compression is proprietary and Aravis can't decode it ->
    force ImageCompressionMode=Off.
  * FLIR exposes PTP via the legacy GevIEEE1588* feature names, not the SFNC
    Ptp* names -> we try both.
  * Requires Aravis >= 0.8.23 (prefer >= 0.8.32) for FLIR extended-chunk payloads.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

import gi
gi.require_version("Aravis", "0.8")
from gi.repository import Aravis, GLib

log = logging.getLogger(__name__)


class CameraError(RuntimeError):
    pass


class GigECamera:
    def __init__(self, cfg):
        self.cfg = cfg
        self.camera: Optional[Aravis.Camera] = None
        self.device = None
        self.stream = None
        self.chunk_parser = None
        self.tick_frequency_hz: int = 0
        self.ptp_locked: bool = False
        self.chunks_enabled: bool = False

    # ---- feature helpers ---------------------------------------------------
    def _has(self, feature: str) -> bool:
        try:
            return self.device.get_feature(feature) is not None
        except GLib.Error:
            return False

    def _set_bool(self, feature: str, value: bool) -> bool:
        try:
            self.device.set_boolean_feature_value(feature, value)
            return True
        except GLib.Error as e:
            log.warning("set %s=%s failed: %s", feature, value, e)
            return False

    def _set_str(self, feature: str, value: str) -> bool:
        try:
            self.device.set_string_feature_value(feature, value)
            return True
        except GLib.Error as e:
            log.warning("set %s=%s failed: %s", feature, value, e)
            return False

    # ---- lifecycle ---------------------------------------------------------
    def open(self) -> None:
        if getattr(self.cfg, "fake", False):
            Aravis.enable_interface("Fake")
            log.info("Aravis 'Fake' interface enabled (no hardware/network needed)")
        Aravis.update_device_list()
        n = Aravis.get_n_devices()
        log.info("Aravis sees %d device(s)", n)
        for i in range(n):
            log.info("  [%d] %s", i, Aravis.get_device_id(i))
        cam_id = self.cfg.camera_id or ("Fake_1" if getattr(self.cfg, "fake", False) else None)
        try:
            self.camera = Aravis.Camera.new(cam_id)
        except GLib.Error as e:
            raise CameraError(f"failed to open camera {cam_id!r}: {e}") from e
        self.device = self.camera.get_device()
        log.info("opened %s %s (sn %s)",
                 self.camera.get_vendor_name(), self.camera.get_model_name(),
                 self.camera.get_device_serial_number())

    def configure(self) -> None:
        c, cfg = self.camera, self.cfg

        # Aravis can't decode the camera's proprietary compression -> ensure it's off.
        if self._has("ImageCompressionMode"):
            self._set_str("ImageCompressionMode", "Off")

        if cfg.pixel_format:
            try:
                c.set_pixel_format_from_string(cfg.pixel_format)
            except GLib.Error as e:
                log.warning("pixel format %s rejected: %s", cfg.pixel_format, e)

        if cfg.roi:
            try:
                c.set_region(cfg.roi.x, cfg.roi.y, cfg.roi.width, cfg.roi.height)
            except GLib.Error as e:
                log.warning("ROI rejected: %s", e)

        if cfg.frame_rate:
            try:
                c.set_frame_rate(cfg.frame_rate)
            except GLib.Error as e:
                log.warning("frame rate rejected: %s", e)

        # GigE link tuning (no-op / skip on USB3 devices)
        try:
            if c.is_gv_device():
                if cfg.packet_size and cfg.packet_size > 0:
                    c.gv_set_packet_size(cfg.packet_size)
                else:
                    c.gv_auto_packet_size()
                if cfg.packet_delay is not None:
                    c.gv_set_packet_delay(cfg.packet_delay)
                log.info("GigE packet size=%s delay=%s", c.gv_get_packet_size(), cfg.packet_delay)
        except GLib.Error as e:
            log.warning("GigE packet tuning issue: %s", e)

        try:
            self.tick_frequency_hz = int(self.device.get_integer_feature_value("GevTimestampTickFrequency"))
        except GLib.Error:
            self.tick_frequency_hz = 0
        log.info("GevTimestampTickFrequency = %s Hz", self.tick_frequency_hz)

    def enable_chunks(self) -> bool:
        try:
            self.camera.set_chunk_mode(True)
            self.camera.set_chunks(self.cfg.chunk_selectors or "Timestamp,FrameID")
            self.chunk_parser = self.camera.create_chunk_parser()
            self.chunks_enabled = True
            log.info("chunk mode enabled: %s", self.cfg.chunk_selectors)
        except GLib.Error as e:
            log.warning("could not enable chunk mode: %s", e)
            self.chunks_enabled = False
        return self.chunks_enabled

    def enable_ptp(self, timeout_s: float = 10.0) -> bool:
        """Enable IEEE-1588 and wait for the camera to slave to the grandmaster."""
        enable_feature = next((f for f in ("GevIEEE1588", "PtpEnable") if self._has(f)), None)
        if not enable_feature:
            log.warning("no PTP feature found on this camera")
            return False

        if enable_feature == "GevIEEE1588":
            self._set_bool("GevIEEE1588", True)
            if self._has("GevIEEE1588Mode"):
                self._set_str("GevIEEE1588Mode", self.cfg.ptp_mode or "SlaveOnly")
            status_feature = "GevIEEE1588Status"
        else:
            self._set_bool("PtpEnable", True)
            status_feature = "PtpStatus"

        locked = {"Slave", "Master"}
        deadline = time.monotonic() + timeout_s
        last = None
        while time.monotonic() < deadline:
            try:
                status = self.device.get_string_feature_value(status_feature)
            except GLib.Error as e:
                log.warning("could not read %s: %s", status_feature, e)
                break
            if status != last:
                log.info("PTP %s = %s", status_feature, status)
                last = status
            if status in locked:
                self.ptp_locked = True
                return True
            time.sleep(0.25)
        log.warning("PTP did not lock within %.1fs (last %s=%s)", timeout_s, status_feature, last)
        return False

    def create_stream(self, n_buffers: int = 20):
        self.stream = self.camera.create_stream(None, None)
        if self.stream is None:
            raise CameraError("failed to create Aravis stream")
        payload = self.camera.get_payload()
        for _ in range(max(2, n_buffers)):
            self.stream.push_buffer(Aravis.Buffer.new_allocate(payload))
        log.info("stream created, payload=%d bytes, %d buffers queued", payload, n_buffers)
        return self.stream

    def start(self) -> None:
        self.camera.set_acquisition_mode(Aravis.AcquisitionMode.CONTINUOUS)
        self.camera.start_acquisition()
        log.info("acquisition started")

    def stop(self) -> None:
        try:
            if self.camera:
                self.camera.stop_acquisition()
        except GLib.Error as e:
            log.warning("stop_acquisition: %s", e)

    # ---- introspection -----------------------------------------------------
    def sensor_geometry(self):
        return self.camera.get_region()  # (x, y, width, height)

    def pixel_format_string(self) -> str:
        try:
            return self.camera.get_pixel_format_as_string()
        except GLib.Error:
            return self.cfg.pixel_format or "Mono8"
