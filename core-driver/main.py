"""Entry point for the camera core driver (capture + timestamp + record).

Pipeline phase status: P0 (bring-up) + P1 (timestamp spine) + P2 (recorder).
Transport publish (shm/unixfd) and WebRTC are wired in as later phases via the tee
attach point in pipeline.py. The capture frontend is selected by `source.type`
(default gige); see cam_driver.sources.
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import sys

# CameraError is GigE/Aravis-specific (camera.py loads the Aravis GI namespace at import). Import it
# defensively so a USB/RTSP-only deployment doesn't require Aravis installed -- the placeholder is only
# ever used when Aravis is absent, in which case no GigE source can run anyway.
try:
    from cam_driver.camera import CameraError
except (ImportError, ValueError):
    class CameraError(Exception):
        pass

from cam_driver.config import load_config, resolve_recording_dir
from cam_driver.pipeline import CapturePipeline
from cam_driver.sidecar import SidecarWriter
from cam_driver.sources import make_source


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="camera core driver")
    ap.add_argument("-c", "--config", default=os.environ.get("CAM_CONFIG", "config/camera.yaml"))
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    log = logging.getLogger("cam")

    cfg = load_config(args.config)
    # Recording dir from the deploy env: rig sets RIG_DATA_DIR (absolute host data root, bind-mounted at
    # the same path) to keep recordings OFF the repo; cam-up sets CAM_INSTANCE to namespace per sensor.
    # A bare run / a pinned output_dir is unaffected (see docker-compose.yml's `recordings` bind).
    cfg.recording.output_dir = resolve_recording_dir(
        cfg.recording.output_dir, os.environ.get("RIG_DATA_DIR", ""), os.environ.get("CAM_INSTANCE", ""))
    log.info("config: source=%s frame_rate=%s recording=%s->%s encoder=%s",
             cfg.camera.type, cfg.camera.frame_rate, cfg.recording.enabled,
             cfg.recording.output_dir, cfg.recording.encoder)

    # The source owns the frontend: device + timestamp policy + feeder (here: GigE/Aravis,
    # incl. chunk/PTP setup). Everything downstream (pipeline) is source-agnostic.
    try:
        source = make_source(cfg)
        source.open()
        source.configure()
    except (CameraError, ValueError) as e:
        log.error("%s", e)
        return 2

    sidecar = SidecarWriter(os.path.join(cfg.recording.output_dir, cfg.recording.name_prefix))
    sidecar.start()

    pipe = CapturePipeline(cfg, source, sidecar)
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
