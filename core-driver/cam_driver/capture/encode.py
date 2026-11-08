"""Frame -> JPEG (docs/CAPTURE.md "Image").

JPEG is 8-bit, so frames are brought to 8 bits first:
  * 16-bit mono (GRAY16_LE/BE, e.g. a thermal core's Y16) -> GRAY8 by `mono16`: `stretch` (default;
    the 0.5-99.5 percentile range mapped to 0-255 -- a raw thermal frame's narrow band would
    otherwise be black) or `shift` (the high byte, a fixed linear map);
  * a CFA mosaic (8-bit, or 16-bit scaled as above) -> demosaiced by `bayer2rgb`;
  * everything else -> `videoconvert` into whatever `jpegenc` takes.
Optionally downscaled to `max_width`. One small pipeline per input shape is built on first use and
reused (appsrc -> ... -> jpegenc -> appsink, no main loop needed); captures are infrequent, so the
CPU `jpegenc` is used everywhere (a 640x480 encode is ~20 ms on an Orin core).
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Optional, Tuple

import gi
gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402

from .frames import Frame

log = logging.getLogger(__name__)

MONO16_MODES = ("stretch", "shift")
_PACKED_BPP = {"GRAY8": 1, "GRAY16_LE": 2, "GRAY16_BE": 2, "RGB": 3, "BGR": 3,
               "RGBA": 4, "BGRA": 4, "RGBx": 4, "BGRx": 4}


class EncodeError(RuntimeError):
    pass


@dataclass(frozen=True)
class Encoded:
    jpeg: bytes
    width: int
    height: int


def to_8bit(data: bytes, fmt: str, width: int, height: int, mode: str = "stretch") -> bytes:
    """A 16-bit mono plane as tightly packed 8-bit samples (also used for a 16-bit CFA mosaic, where
    one global map keeps the colour balance)."""
    import numpy as np
    dtype = "<u2" if fmt == "GRAY16_LE" else ">u2"
    stride = len(data) // (2 * height) if height else width
    arr = np.frombuffer(data, dtype=dtype, count=stride * height).reshape(height, stride)[:, :width]
    if mode == "shift":
        return (arr >> 8).astype(np.uint8).tobytes()
    lo, hi = np.percentile(arr, (0.5, 99.5))
    if hi <= lo:
        hi = lo + 1
    scaled = (arr.astype(np.float32) - lo) * (255.0 / (hi - lo))
    return np.clip(scaled, 0, 255).astype(np.uint8).tobytes()


def _round4(n: int) -> int:
    return (n + 3) & ~3


def pad_rows(data: bytes, width: int, height: int, bpp: int) -> bytes:
    """GStreamer assumes rows padded to 4 bytes for packed single-plane video (and bayer2rgb does
    too); a tightly packed frame with an odd row size is repacked, anything else passes through."""
    row = width * bpp
    stride = _round4(row)
    if stride == row or len(data) != row * height:
        return data
    pad = b"\x00" * (stride - row)
    return b"".join(data[y * row:(y + 1) * row] + pad for y in range(height))


def scaled_size(width: int, height: int, max_width: int) -> Tuple[int, int]:
    if not max_width or width <= max_width:
        return width, height
    h = max(2, int(round(height * max_width / width)) & ~1)
    return max_width & ~1, h


class JpegEncoder:
    def __init__(self, quality: int = 90, max_width: int = 0, mono16: str = "stretch",
                 timeout_s: float = 5.0):
        if not 1 <= int(quality) <= 100:
            raise ValueError(f"quality: expected 1..100, got {quality!r}")
        if int(max_width) < 0:
            raise ValueError(f"max_width: must be >= 0, got {max_width!r}")
        if mono16 not in MONO16_MODES:
            raise ValueError(f"mono16: expected one of {MONO16_MODES}, got {mono16!r}")
        self.quality = int(quality)
        self.max_width = int(max_width)
        self.mono16 = mono16
        self.timeout_ns = int(timeout_s * Gst.SECOND)
        self._pipes = {}
        self._lock = threading.Lock()
        self._pts = 0

    def _input(self, frame: Frame):
        """(caps, data, width, height) to push: 16-bit made 8-bit, rows padded, mosaic labelled."""
        fmt, data, w, h = frame.fmt, frame.data, frame.width, frame.height
        if fmt in ("GRAY16_LE", "GRAY16_BE"):
            data, fmt = to_8bit(data, fmt, w, h, self.mono16), "GRAY8"
        if frame.bayer:
            if fmt != "GRAY8":
                raise EncodeError(f"a {frame.bayer} mosaic must be 8 or 16 bit mono, got {fmt}")
            return (f"video/x-bayer,format={frame.bayer},width={w},height={h},framerate=0/1",
                    pad_rows(data, w, h, 1), w, h)
        if fmt in _PACKED_BPP:
            data = pad_rows(data, w, h, _PACKED_BPP[fmt])
        return f"video/x-raw,format={fmt},width={w},height={h},framerate=0/1", data, w, h

    def _pipeline(self, caps: str, w: int, h: int):
        ow, oh = scaled_size(w, h, self.max_width)
        key = (caps, ow, oh)
        if key in self._pipes:
            return self._pipes[key], ow, oh
        chain = ["bayer2rgb", "videoconvert"] if caps.startswith("video/x-bayer") else ["videoconvert"]
        if (ow, oh) != (w, h):
            chain += ["videoscale", f"video/x-raw,width={ow},height={oh}"]
        desc = (f'appsrc name=src is-live=false format=time caps="{caps}" ! '
                + " ! ".join(chain)
                + f" ! jpegenc quality={self.quality} ! appsink name=sink sync=false max-buffers=1")
        pipe = Gst.parse_launch(desc)
        src, sink = pipe.get_by_name("src"), pipe.get_by_name("sink")
        if pipe.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            raise EncodeError(f"could not start the encoder pipeline: {desc}")
        log.info("encoder: %s -> %dx%d jpeg q=%d", caps.split(",framerate", 1)[0], ow, oh, self.quality)
        self._pipes[key] = (pipe, src, sink)
        return self._pipes[key], ow, oh

    def _bus_error(self, pipe) -> Optional[str]:
        msg = pipe.get_bus().pop_filtered(Gst.MessageType.ERROR)
        if msg is None:
            return None
        err, dbg = msg.parse_error()
        return f"{err.message} ({dbg})" if dbg else err.message

    def encode(self, frame: Frame) -> Encoded:
        with self._lock:
            caps, data, w, h = self._input(frame)
            (pipe, src, sink), ow, oh = self._pipeline(caps, w, h)
            buf = Gst.Buffer.new_wrapped(bytes(data))
            buf.pts = self._pts
            self._pts += Gst.MSECOND
            if src.emit("push-buffer", buf) != Gst.FlowReturn.OK:
                self._drop(caps, ow, oh)
                raise EncodeError(self._bus_error(pipe) or "encoder refused the frame")
            sample = sink.emit("try-pull-sample", self.timeout_ns)
            if sample is None:
                err = self._bus_error(pipe)
                self._drop(caps, ow, oh)
                raise EncodeError(err or "encoder timed out")
            out = sample.get_buffer()
            return Encoded(out.extract_dup(0, out.get_size()), ow, oh)

    def _drop(self, caps: str, ow: int, oh: int) -> None:
        entry = self._pipes.pop((caps, ow, oh), None)
        if entry is not None:
            entry[0].set_state(Gst.State.NULL)

    def close(self) -> None:
        with self._lock:
            for pipe, _src, _sink in self._pipes.values():
                pipe.set_state(Gst.State.NULL)
            self._pipes.clear()
