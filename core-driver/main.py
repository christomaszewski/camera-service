"""Entry point for the GigE Vision core driver (capture + timestamp + record).

Pipeline phase status: P0 (bring-up) + P1 (timestamp spine) + P2 (recorder).
Transport publish (shm) and WebRTC are wired in as later phases via the tee
attach point in pipeline.py.
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import sys

from cam_driver.camera import CameraError, GigECamera
from cam_driver.config import load_config
from cam_driver.pipeline import CapturePipeline
from cam_driver.sidecar import SidecarWriter
from cam_driver.timestamps import TimestampExtractor, TimestampSource


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="GigE Vision core driver")
    ap.add_argument("-c", "--config", default=os.environ.get("CAM_CONFIG", "config/camera.yaml"))
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    log = logging.getLogger("cam")

    cfg = load_config(args.config)
    log.info("config: camera=%s pixel_format=%s ts=%s encoder=%s",
             cfg.camera.camera_id or "<first>", cfg.camera.pixel_format,
             cfg.camera.timestamp_source, cfg.recording.encoder)

    cam = GigECamera(cfg.camera)
    try:
        cam.open()
        cam.configure()
    except CameraError as e:
        log.error("%s", e)
        return 2

    want_ptp = cfg.camera.timestamp_source == "ptp_chunk"
    chunks_ok = cam.enable_chunks() if want_ptp else False
    ptp_ok = cam.enable_ptp(cfg.camera.ptp_lock_timeout_s) if (want_ptp and cfg.camera.ptp_enable) else False

    try:
        prefer = TimestampSource(cfg.camera.timestamp_source)
    except ValueError:
        log.warning("invalid timestamp_source %r; defaulting to ptp_chunk", cfg.camera.timestamp_source)
        prefer = TimestampSource.PTP_CHUNK

    extractor = TimestampExtractor(
        chunk_parser=cam.chunk_parser,
        chunk_timestamp_name=cfg.camera.chunk_timestamp_name,
        chunk_frame_id_name=cfg.camera.chunk_frame_id_name,
        prefer=prefer,
        tick_frequency_hz=cam.tick_frequency_hz,
    )
    extractor.resolve_active_source(ptp_locked=ptp_ok, chunks_enabled=chunks_ok)

    sidecar = SidecarWriter(os.path.join(cfg.recording.output_dir, cfg.recording.name_prefix))
    sidecar.start()

    cam.create_stream(cfg.camera.n_stream_buffers)

    pipe = CapturePipeline(cfg, cam, extractor, sidecar)
    pipe.build()

    def _stop(_signum, _frame):
        log.info("signal received, stopping")
        pipe.request_stop()

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    pipe.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
