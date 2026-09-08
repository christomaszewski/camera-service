"""The shm INPUT source end to end (docs/TRANSPORT.md "Input"): a real shmsink writer in-process,
the real shmsrc reader, a fake consumer at the seam the pipeline sits on. Covers both framings,
the writer being absent / restarting, and the config-mismatch stop.

Needs GStreamer (gi): run inside the dev container --
  docker run --rm -v "$PWD:/repo" -w /repo/core-driver cam-dev python3 tests/test_shm_source.py
"""
import os
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import gi  # noqa: E402
gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # noqa: E402

from cam_driver import transport  # noqa: E402
from cam_driver.config import parse_config  # noqa: E402
from cam_driver.sources.base import SourceConfigChanged  # noqa: E402
from cam_driver.sources.factory import make_source  # noqa: E402
from cam_driver.timestamps import TimestampSource  # noqa: E402

Gst.init(None)

W, H = 64, 48
RGB_BYTES = W * H * 3


def _socket() -> str:
    return os.path.join(tempfile.mkdtemp(), "in")


class _Writer:
    """A plain shmsink writer: videotestsrc raw (any pipeline's shape) or an appsrc for header frames."""

    def __init__(self, socket: str, *, header: bool = False, width: int = W, height: int = H):
        size = width * height * 3 + (transport.HEADER_SIZE if header else 0)
        if header:
            desc = (f'appsrc name=src is-live=true format=time caps="{transport.CAPS}" ! '
                    f'shmsink socket-path="{socket}" shm-size={size * 8} wait-for-connection=false sync=false')
        else:
            desc = (f"videotestsrc is-live=true pattern=smpte ! video/x-raw,format=RGB,width={width},height={height},"
                    f'framerate=20/1 ! shmsink socket-path="{socket}" shm-size={size * 8} wait-for-connection=false sync=false')
        self.pipeline = Gst.parse_launch(desc)
        self.src = self.pipeline.get_by_name("src")
        self.pipeline.set_state(Gst.State.PLAYING)

    def push_header_frame(self, frame_id: int, ts_ns: int, *, width: int = W, height: int = H,
                          pixfmt: str = "RGB", ts_source: str = "system", fill: int = 7) -> None:
        hdr = transport.FrameHeader(timestamp_ns=ts_ns, frame_id=frame_id, width=width, height=height,
                                    pixfmt=pixfmt, ts_source=ts_source).pack()
        payload = hdr + bytes([fill]) * (width * height * 3)
        buf = Gst.Buffer.new_wrapped(payload)
        buf.pts = frame_id * 50_000_000
        assert self.src.emit("push-buffer", buf) == Gst.FlowReturn.OK

    def stop(self) -> None:
        self.pipeline.set_state(Gst.State.NULL)


class _Consumer:
    def __init__(self):
        self.frames = []
        self._lock = threading.Lock()

    def on_frame(self, st, data):
        with self._lock:
            self.frames.append((st, bytes(data)))

    def count(self) -> int:
        with self._lock:
            return len(self.frames)


def _pump(until, timeout_s: float = 10.0) -> None:
    ctx = GLib.MainContext.default()
    t0 = time.monotonic()
    while not until():
        while ctx.iteration(False):
            pass
        if time.monotonic() - t0 > timeout_s:
            raise AssertionError(f"timed out after {timeout_s}s")
        time.sleep(0.005)


def _source(socket: str, **shm):
    cfg = parse_config({"camera": {"type": "shm", "reconnect_timeout_s": 0.5},
                        "shm": {"socket_path": socket, "pixel_format": "RGB", "width": W, "height": H,
                                "frame_rate": 20, **shm}})
    src = make_source(cfg)
    assert type(src).__name__ == "ShmSource"
    src.open()
    src.configure()
    return src


def test_raw_frames_from_any_shmsink_arrive_with_arrival_stamps_and_minted_ids():
    socket = _socket()
    writer = _Writer(socket)
    src = _source(socket)
    c = _Consumer()
    src.start(c.on_frame)
    _pump(lambda: c.count() >= 5)
    stamps = [st for st, _ in c.frames[:5]]
    assert [st.frame_id for st in stamps] == [0, 1, 2, 3, 4]                     # minted, contiguous
    assert all(st.source == TimestampSource.SYSTEM for st in stamps)             # arrival provenance
    assert all(len(d) == RGB_BYTES for _, d in c.frames[:5])
    assert stamps[1].timestamp_ns > stamps[0].timestamp_ns
    assert src.active_timestamp_source == "system" and src.geometry() == (0, 0, W, H)
    assert src.is_disconnected() is False and src.reconnect_enabled
    src.stop()
    writer.stop()


def test_header_frames_carry_the_writers_stamp_id_and_provenance():
    socket = _socket()
    writer = _Writer(socket, header=True)
    src = _source(socket, framing="header")
    c = _Consumer()
    src.start(c.on_frame)
    t0 = 1_700_000_000_000_000_000
    for i in range(3):
        writer.push_header_frame(100 + i, t0 + i * 50_000_000, ts_source="ptp_chunk", fill=i)
        time.sleep(0.05)
    _pump(lambda: c.count() >= 3)
    stamps = [st for st, _ in c.frames[:3]]
    assert [st.frame_id for st in stamps] == [100, 101, 102]
    assert [st.timestamp_ns for st in stamps] == [t0, t0 + 50_000_000, t0 + 100_000_000]
    assert all(st.source == TimestampSource.PTP_CHUNK for st in stamps)
    assert [d[0] for _, d in c.frames[:3]] == [0, 1, 2] and all(len(d) == RGB_BYTES for _, d in c.frames[:3])
    assert src.active_timestamp_source == "ptp_chunk"
    src.stop()
    writer.stop()


def test_an_absent_writer_is_a_cheap_retry_and_a_restarted_one_a_reconnect():
    socket = _socket()
    src = _source(socket)                       # nothing owns the socket yet
    c = _Consumer()
    src.start(c.on_frame)
    _pump(lambda: src.is_disconnected(), timeout_s=5)          # the reader's ERROR flips liveness
    try:
        src.reopen()
    except FileNotFoundError as e:
        assert "writer" in str(e)                               # the backoff loop retries cheaply
    else:
        raise AssertionError("reopen with no socket must raise FileNotFoundError")
    writer = _Writer(socket)
    src.reopen()                                                # the socket is there now
    src.start(c.on_frame)
    _pump(lambda: c.count() >= 3)
    writer.stop()                                               # the writer goes away...
    n = c.count()
    _pump(lambda: src.is_disconnected(), timeout_s=8)          # ...starvation or ERROR: disconnected
    writer = _Writer(socket)                                    # ...and comes back
    src.reopen()
    src.start(c.on_frame)
    _pump(lambda: c.count() >= n + 3)
    assert c.frames[-1][0].frame_id > c.frames[n - 1][0].frame_id   # ids keep counting across the reconnect
    src.stop()
    writer.stop()


def test_a_frame_that_does_not_match_the_pinned_config_stops_legibly():
    socket = _socket()
    writer = _Writer(socket, width=32, height=24)               # the writer sends a smaller raw frame
    src = _source(socket)                                        # config pins 64x48
    c = _Consumer()
    src.start(c.on_frame)
    _pump(lambda: src.is_disconnected(), timeout_s=5)
    assert c.count() == 0
    try:
        src.reopen()
    except SourceConfigChanged as e:
        assert "2304 bytes" in str(e) and "9216" in str(e) and "shm:" in str(e)
    else:
        raise AssertionError("a size mismatch must be a SourceConfigChanged")
    src.stop()
    writer.stop()
    socket = _socket()
    writer = _Writer(socket, header=True)
    src = _source(socket, framing="header")
    c = _Consumer()
    src.start(c.on_frame)
    writer.push_header_frame(1, 1_000, width=32, height=24)    # a header frame of another geometry
    _pump(lambda: src.is_disconnected(), timeout_s=5)
    try:
        src.reopen()
    except SourceConfigChanged as e:
        assert "32x24" in str(e) and "64x48" in str(e)
    else:
        raise AssertionError("a header geometry mismatch must be a SourceConfigChanged")
    src.stop()
    writer.stop()


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("ok  ", name)
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print("FAIL", name, "->", repr(exc))
    print(f"{len([n for n in globals() if n.startswith('test_')]) - failures} passed")
    sys.exit(1 if failures else 0)
