"""Reading the core's plugin endpoint (plugins/README.md "The contract") into Delivered frames.

  * JP7 / GStreamer >= 1.24: `unixfdsrc` at <socket dir>/unixfd -- native caps (video/x-raw GRAY8/16
    or colour, video/x-bayer for an 8-bit mosaic), frame_id in buffer.offset, the absolute capture
    stamp in buffer.offset_end. No timestamp source on this path.
  * JP6: `shmsrc` at transport.plugin_endpoint.socket_path, each buffer = the 36-byte header
    (cam_driver.transport) + pixels. The header carries the timestamp source.

`auto` picks whichever socket exists, the same rule as tools/shm_probe.py. The reader runs on its
own thread with no main loop (appsink pulls), copies each frame out of the transport (see frames),
and survives the core restarting: an error or EOS tears the pipeline down and it re-dials.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

import gi
gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402

from .. import transport

log = logging.getLogger(__name__)

TRANSPORTS = ("auto", "unixfd", "shm")
_BAYER_PATTERNS = ("rggb", "grbg", "gbrg", "bggr")


@dataclass(frozen=True)
class Delivered:
    frame_id: int
    stamp_ns: int
    source: Optional[str]
    fmt: str
    width: int
    height: int
    bayer: Optional[str]
    data: bytes
    arrival_ns: int


def unixfd_socket(plugin_socket: str) -> str:
    """Where the core binds its unixfd endpoint: beside the configured plugin socket."""
    return os.path.join(os.path.dirname(plugin_socket) or "/tmp/cam", "unixfd")


def select_endpoint(plugin_socket: str, prefer: str = "auto"):
    """(kind, path) of the endpoint to dial, or (None, None) when none exists yet."""
    ufd = unixfd_socket(plugin_socket)
    if prefer in ("auto", "unixfd") and os.path.exists(ufd):
        return "unixfd", ufd
    if prefer in ("auto", "shm") and os.path.exists(plugin_socket):
        return "shm", plugin_socket
    return None, None


def from_header_bytes(header: bytes, pixels: bytes, bayer_hint: Optional[str], arrival_ns: int) -> Delivered:
    """A JP6 shm frame. The header has no CFA field, so a mono plane is a mosaic when the sensor
    config says so (`bayer_hint`)."""
    hdr = transport.unpack_header(header)
    bayer = bayer_hint if hdr.pixfmt in ("GRAY8", "GRAY16_LE", "GRAY16_BE") else None
    return Delivered(hdr.frame_id, hdr.timestamp_ns, hdr.ts_source, hdr.pixfmt, hdr.width, hdr.height,
                     bayer, pixels, arrival_ns)


def from_caps(structure_name: str, fmt: str, width: int, height: int, frame_id: int, stamp_ns: int,
              data: bytes, bayer_hint: Optional[str], arrival_ns: int) -> Delivered:
    """A JP7 unixfd frame from its caps fields and buffer offsets. An 8-bit mosaic is self-describing
    (video/x-bayer); a >8-bit one rides as GRAY16 and needs the hint, like the header path."""
    if structure_name == "video/x-bayer":
        if fmt not in _BAYER_PATTERNS:
            raise transport.TransportError(f"unsupported bayer caps format {fmt!r}")
        return Delivered(frame_id, stamp_ns, None, "GRAY8", width, height, fmt, data, arrival_ns)
    bayer = bayer_hint if fmt in ("GRAY16_LE", "GRAY16_BE") else None
    return Delivered(frame_id, stamp_ns, None, fmt, width, height, bayer, data, arrival_ns)


class TransportReader:
    def __init__(self, plugin_socket: str, on_frame: Callable[[Delivered], None], prefer: str = "auto",
                 bayer_hint: Optional[str] = None, retry_s: float = 1.0, pull_timeout_s: float = 0.5,
                 clock: Callable[[], int] = time.time_ns):
        if prefer not in TRANSPORTS:
            raise ValueError(f"transport: expected one of {TRANSPORTS}, got {prefer!r}")
        self.plugin_socket = plugin_socket
        self.on_frame = on_frame
        self.prefer = prefer
        self.bayer_hint = bayer_hint
        self.retry_s = retry_s
        self.pull_timeout_ns = int(pull_timeout_s * Gst.SECOND)
        self._clock = clock
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.kind: Optional[str] = None
        self.connected = False
        self.frames = 0
        self.errors = 0
        self.last_error: Optional[str] = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="capture-reader", daemon=True)
        self._thread.start()

    def stop(self, timeout_s: float = 3.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout_s)

    def describe(self) -> dict:
        return {"transport": self.kind, "connected": self.connected, "frames": self.frames,
                "errors": self.errors, "last_error": self.last_error}

    def _run(self) -> None:
        while not self._stop.is_set():
            kind, path = select_endpoint(self.plugin_socket, self.prefer)
            if kind is None:
                self._stop.wait(self.retry_s)
                continue
            try:
                self._session(kind, path)
            except Exception as e:   # noqa: BLE001 -- a bad frame or pipeline must never end the reader
                self.errors += 1
                self.last_error = str(e)
                log.warning("reader: %s endpoint %s failed: %s", kind, path, e)
            self.connected = False
            self._stop.wait(self.retry_s)

    def _session(self, kind: str, path: str) -> None:
        if kind == "unixfd":
            src = f"unixfdsrc socket-path={path}"
        else:
            src = f"shmsrc socket-path={path} is-live=true ! {transport.CAPS}"
        pipe = Gst.parse_launch(src + " ! appsink name=sink emit-signals=false max-buffers=4 drop=true sync=false")
        sink = pipe.get_by_name("sink")
        bus = pipe.get_bus()
        try:
            if pipe.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
                raise RuntimeError("could not start the transport pipeline")
            self.kind, self.connected = kind, True
            log.info("reader: attached to %s endpoint %s", kind, path)
            while not self._stop.is_set():
                sample = sink.emit("try-pull-sample", self.pull_timeout_ns)
                if sample is not None:
                    try:
                        self._deliver(kind, sample)
                    except Exception as e:   # noqa: BLE001 -- one bad frame is dropped, not the endpoint
                        self.errors += 1
                        if str(e) != self.last_error:
                            log.warning("reader: dropped a frame: %s", e)
                        self.last_error = str(e)
                msg = bus.pop_filtered(Gst.MessageType.ERROR | Gst.MessageType.EOS)
                if msg is not None:
                    if msg.type == Gst.MessageType.ERROR:
                        err, _dbg = msg.parse_error()
                        raise RuntimeError(err.message)
                    log.info("reader: %s endpoint ended (EOS); re-dialing", kind)
                    return
                if sample is None and sink.get_property("eos"):
                    return
        finally:
            pipe.set_state(Gst.State.NULL)

    def _deliver(self, kind: str, sample) -> None:
        arrival = self._clock()
        buf = sample.get_buffer()
        size = buf.get_size()
        if kind == "unixfd":
            s = sample.get_caps().get_structure(0)
            d = from_caps(s.get_name(), s.get_string("format") or "", s.get_value("width"),
                          s.get_value("height"), int(buf.offset), int(buf.offset_end),
                          buf.extract_dup(0, size), self.bayer_hint, arrival)
        else:
            if size < transport.HEADER_SIZE:
                raise transport.TransportError(f"short transport buffer ({size} bytes)")
            d = from_header_bytes(buf.extract_dup(0, transport.HEADER_SIZE),
                                  buf.extract_dup(transport.HEADER_SIZE, size - transport.HEADER_SIZE),
                                  self.bayer_hint, arrival)
        self.frames += 1
        self.on_frame(d)
