#!/usr/bin/env python3
"""Measure core CPU for a paced replay with an attached transport consumer and recording off.

Run the same script/input/image against two code checkouts for a before/after comparison.
Input recordings are only read; transport sockets live in a temporary directory.
"""
import argparse
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import gi
gi.require_version("Gst", "1.0")
from gi.repository import Gst

from cam_driver.config import parse_config
from cam_driver.pipeline import CapturePipeline
from cam_driver.sources.replay import ReplaySource


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("run", help="recorded session prefix or run directory")
    ap.add_argument("--seconds", type=float, default=10.0, help="window length in source seconds")
    ap.add_argument("--speed", type=float, default=1.0)
    ap.add_argument("--max-rate-hz", type=float, default=0.0)
    ap.add_argument("--legacy-transport-copy", action="store_true",
                    help="disable replay shared allocation/native pooling for an A/B baseline")
    args = ap.parse_args()
    with tempfile.TemporaryDirectory() as tmp:
        cfg = parse_config({
            "camera": {"type": "replay"},
            "replay": {"path": args.run, "speed": args.speed},
            "playback": {"to_s": args.seconds, "on_finish": "exit"},
            "recording": {"enabled": False}, "preview": {"enabled": False},
            "control": {"enabled": False},
            "transport": {"plugin_endpoint": {"enabled": True, "socket_path": tmp + "/frames",
                                               "max_rate_hz": args.max_rate_hz},
                          "raw_endpoint": {"enabled": False}}})
        source = ReplaySource(cfg.replay, cfg.playback)
        source.open()
        source.configure()
        pipe = CapturePipeline(cfg, source)
        pipe.build()
        if args.legacy_transport_copy:
            pipe._unixfd_pool = False
        consumer = None
        received = 0

        def handoff(*_):
            nonlocal received
            received += 1

        def on_playing():
            nonlocal consumer
            endpoint = (f'unixfdsrc socket-path="{pipe._unixfd_path}"' if pipe._have_unixfd else
                        f'shmsrc socket-path="{tmp}/frames" is-live=true do-timestamp=true')
            consumer = Gst.parse_launch(endpoint + " ! fakesink name=sink sync=false signal-handoffs=true")
            consumer.get_by_name("sink").connect("handoff", handoff)
            consumer.set_state(Gst.State.PLAYING)
            time.sleep(0.1)
            return True

        cpu, wall = time.process_time(), time.monotonic()
        try:
            pipe.run(on_playing=on_playing)
        finally:
            if consumer is not None:
                consumer.set_state(Gst.State.NULL)
        cpu, wall = time.process_time() - cpu, time.monotonic() - wall
        print(json.dumps({"gstreamer": Gst.version_string(), "frames": pipe.drops.frames,
                          "received": received, "cpu_seconds": round(cpu, 4),
                          "wall_seconds": round(wall, 4), "cpu_percent_one_core": round(100 * cpu / wall, 2),
                          "transport": "unixfd" if pipe._have_unixfd else "shm",
                          "shared_memory_transport": pipe._unixfd_pool,
                          "health": pipe.drops.summary()}, sort_keys=True))
        if pipe.had_error or not received or pipe.drops.publish_drops or pipe.drops.enqueue_failures:
            raise SystemExit("benchmark failed or dropped frames; do not compare this sample")


if __name__ == "__main__":
    main()
