"""Validate the plugin transport endpoint.

Reads `application/x-cam-frame` buffers off the core's shm socket, unpacks the
36-byte header (cam_driver.transport), and prints timestamp_ns / frame_id /
geometry per frame -- i.e. exactly what the C++ ros2-bridge will do, but in a few
lines for validation. Same-host consumer; run alongside the core (same container or
shared /dev/shm + socket volume).

Usage: python3 tools/shm_probe.py [--socket /tmp/cam/frames] [--count 10]
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import gi
gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402

from cam_driver import transport  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Probe the cam plugin transport endpoint")
    ap.add_argument("--socket", default="/tmp/cam/frames")
    ap.add_argument("--count", type=int, default=10)
    ap.add_argument("--timeout", type=float, default=10.0, help="seconds to wait per frame")
    args = ap.parse_args()

    Gst.init(None)
    desc = (f"shmsrc socket-path={args.socket} is-live=true ! {transport.CAPS} ! "
            f"appsink name=sink emit-signals=false max-buffers=4 drop=true sync=false")
    print(f"probe: {desc}")
    pipe = Gst.parse_launch(desc)
    sink = pipe.get_by_name("sink")
    pipe.set_state(Gst.State.PLAYING)

    n = 0
    last_fid = None
    timeout_ns = int(args.timeout * Gst.SECOND)
    try:
        while n < args.count:
            sample = sink.emit("try-pull-sample", timeout_ns)
            if sample is None:
                print("timed out waiting for a frame (is the core publishing?)")
                break
            buf = sample.get_buffer()
            ok, mi = buf.map(Gst.MapFlags.READ)
            if not ok:
                continue
            try:
                data = bytes(mi.data)
                hdr = transport.unpack_header(data)
                pixels = len(data) - transport.HEADER_SIZE
                gap = "" if last_fid is None else f" (Δfid={hdr.frame_id - last_fid})"
                last_fid = hdr.frame_id
                print(f"frame_id={hdr.frame_id}{gap} ts={hdr.timestamp_ns} src={hdr.ts_source} "
                      f"{hdr.width}x{hdr.height} {hdr.pixfmt} pixels={pixels}")
            finally:
                buf.unmap(mi)
            n += 1
    finally:
        pipe.set_state(Gst.State.NULL)

    print(f"read {n} frame(s)")
    if n < args.count:
        # The test scripts lean on this as their binding transport assertion: reading fewer
        # frames than asked (producer wedged after one frame, stalled endpoint) must FAIL.
        print(f"FAIL: expected {args.count} frame(s), got {n}")
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:   # long-running probe stopped by the supervisor's SIGINT
        sys.exit(130)
