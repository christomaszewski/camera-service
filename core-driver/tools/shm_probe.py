"""Validate the plugin transport endpoint.

Reads the core's unixfd or headered-shm endpoint and prints timestamps, frame IDs and
geometry, as a small equivalent of the C++ ROS bridge. The default selects the available
socket; --transport and --socket can select a particular producer explicitly.
Run alongside the core in the same container or with a shared socket volume and /dev/shm.

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
    ap.add_argument("--socket", help="defaults to the available /tmp/cam/{unixfd,frames} endpoint")
    ap.add_argument("--transport", choices=("auto", "shm", "unixfd"), default="auto")
    ap.add_argument("--count", type=int, default=10)
    ap.add_argument("--timeout", type=float, default=10.0, help="seconds to wait per frame")
    args = ap.parse_args()

    Gst.init(None)
    socket = args.socket or ("/tmp/cam/unixfd" if os.path.exists("/tmp/cam/unixfd") else "/tmp/cam/frames")
    unixfd = args.transport == "unixfd" or (args.transport == "auto" and os.path.basename(socket) == "unixfd")
    source = (f"unixfdsrc socket-path={socket}" if unixfd else
              f"shmsrc socket-path={socket} is-live=true ! {transport.CAPS}")
    desc = source + " ! appsink name=sink emit-signals=false max-buffers=4 drop=true sync=false"
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
            if unixfd:
                assert buf.offset != Gst.BUFFER_OFFSET_NONE and buf.offset_end > 0
                print(f"frame_id={buf.offset} ts={buf.offset_end} "
                      f"{sample.get_caps().to_string()} pixels={buf.get_size()}")
                n += 1
                continue
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
