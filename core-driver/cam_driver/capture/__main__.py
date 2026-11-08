"""Entry point: `python3 -m cam_driver.capture -c <sensor config> [--plugin frame-capture]`.

Spawned by the per-sensor supervisor for a `plugins:` entry named `frame-capture` with
`isolation: process` (it runs in the core image beside the core). Reads the SAME sensor config as
the core, so it finds the plugin endpoint, the recording directory and the CFA pattern without
anything being configured twice. Exit codes follow main.py: 2 = configuration error.
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading

log = logging.getLogger("frame-capture")


def bayer_hint(cfg, setting: str):
    """The CFA pattern of a mono plane: explicit, `none`, or `auto` -- recording.bayer_pattern, else
    the pattern in the active source's pixel_format (gige / shm); None for everything else."""
    if setting == "none":
        return None
    if setting != "auto":
        return setting
    from cam_driver.formats import parse_pixel_format
    if cfg.recording.bayer_pattern:
        return str(cfg.recording.bayer_pattern).lower()
    block = {"gige": cfg.gige, "shm": cfg.shm}.get(cfg.camera.type)
    if block is None:
        return None
    return parse_pixel_format(getattr(block, "pixel_format", None))[2]


def plugin_params(cfg, name: str) -> dict:
    for p in cfg.plugins:
        if p.name == name and p.enabled:
            return dict(p.params)
    raise ValueError(f"no enabled `plugins:` entry named {name!r} in the sensor config")


def build(config_path: str, plugin: str):
    """Everything wired, nothing started: (service, reader, zenoh adapter). Raises ValueError on a
    configuration problem."""
    from cam_driver.config import load_config, resolve_recording_dir
    from cam_driver.control_zenoh import vehicle_id, zenoh_connect_endpoints
    from .encode import JpegEncoder
    from .frames import FrameRing
    from .reader import TransportReader
    from .service import CaptureService, CaptureSettings, ZenohCapture, capture_key
    from .sinks import FileSink, ZenohStoreSink, run_id_from_recording_dir, store_prefix
    from .timebase import TimeBase, resolve_tai_offset_s

    cfg = load_config(config_path)
    s = CaptureSettings.from_params(plugin_params(cfg, plugin))
    instance = os.environ.get("CAM_INSTANCE") or "camera"
    vehicle = vehicle_id()
    rig_data = os.environ.get("RIG_DATA_DIR", "")
    rec_dir = resolve_recording_dir(cfg.recording.output_dir, rig_data, os.environ.get("CAM_INSTANCE", ""))
    run_id = run_id_from_recording_dir(rec_dir, rig_data)

    sinks, store_sinks = [], []
    if "file" in s.sinks:
        sinks.append(FileSink(s.file_dir if os.path.isabs(s.file_dir) else os.path.join(rec_dir, s.file_dir)))
    if "zenoh" in s.sinks:
        z = ZenohStoreSink(store_prefix(s.store_prefix, vehicle, run_id, instance))
        sinks.append(z)
        store_sinks.append(z)

    timebase = TimeBase(basis=s.time_basis, scale=s.time_scale,
                        tai_offset_ns=resolve_tai_offset_s(s.tai_utc_offset_s) * 1_000_000_000,
                        max_age_ns=int(s.max_latency_ms * 1e6),
                        playback=cfg.camera.type in ("replay", "pcap"))
    service = CaptureService(s, FrameRing(s.buffer_s, int(s.buffer_max_mb * 1024 * 1024)), timebase,
                             JpegEncoder(s.quality, s.max_width, s.mono16), sinks,
                             instance=instance, vehicle=vehicle, run_id=run_id)
    reader = TransportReader(cfg.transport.plugin_endpoint.socket_path, service.on_delivery,
                             prefer=s.transport, bayer_hint=bayer_hint(cfg, s.bayer_pattern))
    service.reader = reader
    control = ZenohCapture(service, capture_key(vehicle, instance),
                           zenoh_connect_endpoints(cfg.control.zenoh_connect), queue_size=s.queue,
                           store_sinks=store_sinks)
    log.info("frame-capture: instance=%s run=%s sinks=%s buffer=%gs time=%s/%s",
             instance, run_id or "-", [x.describe() for x in sinks], s.buffer_s, s.time_basis, s.time_scale)
    return service, reader, control


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="frame-capture plugin (docs/CAPTURE.md)")
    ap.add_argument("-c", "--config", default=os.environ.get("CAM_CONFIG", "config/camera.yaml"))
    ap.add_argument("--plugin", default="frame-capture", help="the plugins: entry to read params from")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    import gi
    gi.require_version("Gst", "1.0")
    from gi.repository import Gst
    Gst.init(None)
    try:
        service, reader, control = build(args.config, args.plugin)
    except (ValueError, OSError) as e:
        log.error("config error: %s", e)
        return 2
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    reader.start()
    control.start()
    stop.wait()
    log.info("frame-capture: stopping")
    control.close()
    reader.stop()
    service.encoder.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
