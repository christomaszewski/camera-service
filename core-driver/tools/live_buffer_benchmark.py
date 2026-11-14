#!/usr/bin/env python3
"""Compare live bytes/owned-buffer delivery with a real transport consumer and recording off.

Runs a paced fake USB source through CapturePipeline. CPU includes videotestsrc, the core,
and same-process consumer callbacks; results describe this host with simulated capture.
Sockets live in a temporary directory. Any missing frame or pipeline error invalidates the run.
"""
import argparse
import json
import os
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import gi
gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst

from cam_driver.config import parse_config
from cam_driver.pipeline import CapturePipeline
from cam_driver.sources.usb import UsbSource


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--legacy-bytes", action="store_true", help="use start() instead of start_buffers()")
    ap.add_argument("--seconds", type=float, default=5.0)
    ap.add_argument("--width", type=int, default=2448)
    ap.add_argument("--height", type=int, default=2048)
    ap.add_argument("--format", default="GRAY8",
                    choices=("GRAY8", "GRAY16_LE", "I420", "NV12", "RGB", "BGR", "RGBA", "BGRA", "YUY2", "UYVY"))
    ap.add_argument("--fps", type=int, default=24)
    ap.add_argument("--raw-endpoint", action="store_true", help="also attach a raw shm consumer")
    ap.add_argument("--preview", action="store_true", help="also enable the local preview with a fakesink")
    args = ap.parse_args()
    if min(args.seconds, args.width, args.height, args.fps) <= 0:
        ap.error("seconds, width, height and fps must be positive")
    expected = max(1, round(args.seconds * args.fps))

    class FiniteUsb(UsbSource):
        def _pipeline_desc(self):
            return super()._pipeline_desc().replace("videotestsrc is-live=true",
                                                   f"videotestsrc num-buffers={expected} is-live=true")

    with tempfile.TemporaryDirectory(prefix="cam-live-bench-") as tmp:
        cfg = parse_config({
            "camera": {"type": "usb", "frame_rate": args.fps},
            "usb": {"fake": True, "pixel_format": args.format, "width": args.width,
                    "height": args.height, "frame_rate": args.fps},
            "recording": {"enabled": False}, "control": {"enabled": False},
            "preview": {"enabled": args.preview,
                        "sink": "fakesink name=bench_preview sync=false async=false signal-handoffs=true"},
            "transport": {"plugin_endpoint": {"enabled": True, "socket_path": tmp + "/frames"},
                          "raw_endpoint": {"enabled": args.raw_endpoint, "socket_path": tmp + "/raw"}}})
        source = FiniteUsb(cfg.usb)
        source.open()
        source.configure()
        pipe = CapturePipeline(cfg, source)
        pipe.build()
        if args.legacy_bytes:
            source.start_buffers = None
        consumers, watched, errors = [], [], []
        received = {"plugin": 0}
        condition = threading.Condition()
        start = None
        timeout_id = None
        force_quit = pipe._force_quit

        def failed_drain():
            errors.append("main pipeline EOS did not drain; shutdown was forced")
            return force_quit()

        pipe._force_quit = failed_drain

        def handoff(_sink, _buffer, _pad, name):
            with condition:
                received[name] += 1
                condition.notify_all()

        def watch(pipeline, name, stop_on_eos=False):
            bus = pipeline.get_bus()
            bus.add_signal_watch()
            watched.append(bus)

            def on_message(_bus, message):
                if message.type == Gst.MessageType.ERROR:
                    error, detail = message.parse_error()
                    errors.append(f"{name}: {error}: {detail}")
                    pipe.request_stop()
                elif stop_on_eos and message.type == Gst.MessageType.EOS:
                    pipe.request_stop()

            bus.connect("message", on_message)

        def attach(endpoint, name):
            received[name] = 0
            consumer = Gst.parse_launch(endpoint + " ! fakesink name=sink sync=false async=false signal-handoffs=true")
            consumers.append(consumer)
            consumer.get_by_name("sink").connect("handoff", handoff, name)
            watch(consumer, name)
            consumer.set_state(Gst.State.PLAYING)
            result, state, _ = consumer.get_state(2 * Gst.SECOND)
            if result == Gst.StateChangeReturn.FAILURE or state != Gst.State.PLAYING:
                raise RuntimeError(f"{name} consumer did not connect")

        def timed_out():
            nonlocal timeout_id
            timeout_id = None
            errors.append("capture did not finish within its time budget")
            pipe.request_stop()
            return False

        def on_playing():
            nonlocal start, timeout_id
            endpoint = (f'unixfdsrc socket-path="{pipe._unixfd_path}"' if pipe._have_unixfd else
                        f'shmsrc socket-path="{tmp}/frames" is-live=true do-timestamp=true')
            attach(endpoint, "plugin")
            if args.raw_endpoint:
                attach(f'shmsrc socket-path="{tmp}/raw" is-live=true do-timestamp=true', "raw")
            if args.preview:
                received["preview"] = 0
                pipe.pipeline.get_by_name("bench_preview").connect("handoff", handoff, "preview")
            watch(source._pipeline, "source", stop_on_eos=True)
            timeout_id = GLib.timeout_add(int((args.seconds + 10) * 1000), timed_out)
            start = time.process_time(), time.monotonic()
            return True

        try:
            pipe.run(on_playing=on_playing)
            with condition:
                condition.wait_for(lambda: all(n >= expected for n in received.values()), timeout=2)
            cpu, wall = time.process_time() - start[0], time.monotonic() - start[1]
        finally:
            if timeout_id is not None:
                GLib.source_remove(timeout_id)
            for consumer in consumers:
                consumer.set_state(Gst.State.NULL)
            for bus in watched:
                bus.remove_signal_watch()
            pipe.shutdown()
        health = pipe.drops.summary()
        invalid = (pipe.had_error or errors or health["frames"] != expected
                   or any(n != expected for n in received.values())
                   or any(health[key] for key in ("source_gaps", "frames_missing", "enqueue_failures", "publish_drops")))
        print(json.dumps({"gstreamer": Gst.version_string(), "mode": "bytes" if args.legacy_bytes else "owned-buffer",
                          "width": args.width, "height": args.height, "format": args.format, "fps": args.fps,
                          "expected_frames": expected, "received": received, "health": health,
                          "cpu_seconds": round(cpu, 6), "wall_seconds": round(wall, 6),
                          "cpu_ms_per_frame": round(1000 * cpu / expected, 4),
                          "cpu_percent_one_core": round(100 * cpu / wall, 2),
                          "transport": "unixfd" if pipe._have_unixfd else "shm",
                          "native_transport_pool": pipe._unixfd_pool, "valid": not invalid,
                          "errors": errors}, sort_keys=True))
        if invalid:
            raise SystemExit("benchmark failed or dropped frames; do not compare this sample")


if __name__ == "__main__":
    main()
