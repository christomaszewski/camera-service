"""Entry point for the camera core driver (capture + timestamp + record).

Pipeline phase status: P0 (bring-up) + P1 (timestamp spine) + P2 (recorder) + P6 (lifecycle: the
recorder is a per-SESSION pipeline opened / finalized at runtime -- SIGUSR1 / SIGUSR2 here, the zenoh
control plane on top). Transport publish (shm/unixfd) and WebRTC are wired in as later phases via the
tee attach point in pipeline.py. The capture frontend is selected by `source.type` (default gige);
see cam_driver.sources.
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import sys

from gi.repository import GLib   # Gst.parse_launch failures surface as GLib.Error

# CameraError is GigE/Aravis-specific (camera.py loads the Aravis GI namespace at import). Import it
# defensively so a USB/RTSP-only deployment doesn't require Aravis installed -- the placeholder is only
# ever used when Aravis is absent, in which case no GigE source can run anyway.
try:
    from cam_driver.camera import CameraError
except (ImportError, ValueError):
    class CameraError(Exception):
        pass

from cam_driver.config import lifecycle_state_file, load_config, resolve_recording_dir
from cam_driver.control_zenoh import ZenohControl, lifecycle_key, vehicle_id, zenoh_connect_endpoints
from cam_driver.lifecycle import ACTIVE, Lifecycle
from cam_driver.pipeline import CapturePipeline
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

    try:
        cfg = load_config(args.config)
    except (ValueError, OSError) as e:
        # A config typo (e.g. `height: 1080p`) or an unreadable file: fail fast + legibly HERE,
        # naming the offending field, instead of crashing deep in a source -- where it reads as a
        # downstream transport/bridge failure (a dead core never serves its socket).
        log.error("config error: %s", e)
        return 2
    # Recording dir from the deploy env: rig sets RIG_DATA_DIR (absolute host data root, bind-mounted at
    # the same path) to keep recordings OFF the repo; cam-up sets CAM_INSTANCE to namespace per sensor.
    # When the root carries rig's run registry, this also PINS the open run (current -> runs/<id>) --
    # resolved once, here at process start, so a registry rotation can never move a live recorder.
    # A bare run / an ABSOLUTE (pinned) output_dir is unaffected; a RELATIVE one becomes a subdir
    # under the managed recordings root (see docker-compose.yml's `recordings` bind).
    cfg.recording.output_dir = resolve_recording_dir(
        cfg.recording.output_dir, os.environ.get("RIG_DATA_DIR", ""), os.environ.get("CAM_INSTANCE", ""))
    # The per-run prefix stamp lives in the recording SESSION (pipeline.activate): the boot session,
    # every later activate and a restart each get their own, so nothing ever overwrites a run.
    log.info("config: source=%s frame_rate=%s recording=%s->%s/%s-* encoder=%s boot_state=%s",
             cfg.camera.type, cfg.camera.frame_rate, cfg.recording.enabled,
             cfg.recording.output_dir, cfg.recording.name_prefix, cfg.recording.encoder,
             cfg.control.initial_state)

    # The source owns the frontend: device + timestamp policy + feeder (here: GigE/Aravis,
    # incl. chunk/PTP setup). Everything downstream (pipeline) is source-agnostic.
    try:
        source = make_source(cfg)
        source.open()
        source.configure()
    except (CameraError, ValueError) as e:
        log.error("%s", e)
        return 2

    pipe = CapturePipeline(cfg, source)
    try:
        pipe.build()
    except (GLib.Error, RuntimeError, OSError) as e:
        # An unbuildable pipeline (a GStreamer element this host doesn't have and nothing left to
        # fall back to, an unparseable preview sink, an un-creatable socket dir): fail with ONE
        # legible line + a non-zero exit. Uncaught, it's a raw traceback that compose restart-loops
        # with no hint of which element/path is missing.
        log.error("pipeline build failed: %s", e)
        return 2

    # Lifecycle policy over the pipeline's sessions: boot state (config / crash-resume), legal
    # transitions, the state descriptor. SIGUSR1/2 are the zero-dependency local control; the zenoh
    # control plane (docs/LIFECYCLE.md) rides the same object.
    instance = os.environ.get("CAM_INSTANCE") or "camera"
    lifecycle = Lifecycle(
        pipe, recording_enabled=cfg.recording.enabled, initial_state=cfg.control.initial_state,
        state_file=lifecycle_state_file(cfg), resume=cfg.control.resume_state,
        instance=instance, segment_seconds=cfg.recording.segment_seconds)
    boot_state, _reason = lifecycle.resolve_boot_state()
    # With no control plane, nothing could ever re-activate a recorder that died: keep today's
    # non-zero exit for that shape (disk full must not look clean).
    pipe.session_error_fatal = (boot_state == ACTIVE and not cfg.control.enabled)
    control = ZenohControl(lifecycle, lifecycle_key(vehicle_id(), instance),
                           connect=zenoh_connect_endpoints(cfg.control.zenoh_connect),
                           enabled=cfg.control.enabled)

    def _on_playing() -> bool:
        if not lifecycle.boot():
            return False
        control.start()    # presence only once PLAYING + the lifecycle is initialised; retries until reachable
        return True

    def _stop(_signum, _frame):
        log.info("signal received, stopping")
        lifecycle.forget()      # a DELIBERATE stop: the next boot follows the config, not the last command
        control.close()         # withdraw presence now, not after the drain
        pipe.request_stop()

    def _run_transition(transition):
        r = lifecycle.request(transition)
        log.info("lifecycle: %s (signal) -> ok=%s state=%s%s", transition, r.get("ok"), r.get("state"),
                 f" error={r['error']}" if r.get("error") else "")
        return False   # one-shot idle callback

    def _transition(transition):
        # Python signal handlers run between bytecodes on the main thread; the transition itself runs
        # on the GLib loop (GLib.idle_add), where the pipeline's session hooks are safe.
        def handler(_signum, _frame):
            GLib.idle_add(_run_transition, transition)
        return handler

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGUSR1, _transition("activate"))
    signal.signal(signal.SIGUSR2, _transition("deactivate"))

    pipe.run(on_playing=_on_playing)
    control.close()   # the error/EOS paths that never went through _stop
    if pipe.had_error:
        log.error("exited after a pipeline error")   # disk full / encoder failure / fatal source change
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
