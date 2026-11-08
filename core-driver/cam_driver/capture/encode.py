"""Frame -> JPEG (docs/CAPTURE.md "Image").

JPEG is 8-bit, so frames are brought to 8 bits first:
  * 16-bit mono (GRAY16_LE/BE, e.g. a thermal core's Y16) -> GRAY8 by `mono16`: `stretch` (default;
    the 0.5-99.5 percentile range mapped to 0-255 -- a raw thermal frame's narrow band would
    otherwise be black) or `shift` (the high byte, a fixed linear map);
  * a CFA mosaic (8-bit, or 16-bit scaled as above) -> demosaiced by `bayer2rgb`;
  * everything else -> `videoconvert` into whatever the encoder takes.
Optionally downscaled to `max_width`. One small pipeline per input shape is built on first use and
reused (appsrc -> ... -> encoder -> appsink, no main loop needed).

The encoder (`jpeg_encoder`): `auto` uses the Jetson hardware JPEG encoder (`nvjpegenc`, fed
system-memory I420 -- its most widely documented input) when the element exists AND a startup probe
encodes a test frame; otherwise the CPU `jpegenc`. `hw` asks for the hardware encoder (degrading to
the CPU one with a warning, like the recorder's NVENC -> FFV1), `cpu` never touches it. One encoder
is chosen per process: NVIDIA's libjpeg replacement and the stock libjpeg have been reported to
clash when both are loaded, so the CPU encoder is only loaded beside the hardware one when the
hardware one has already failed. A hardware failure at runtime retries that frame on the CPU and,
if the CPU succeeds, stays on the CPU for the life of the process (the descriptor says why).
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Optional, Tuple

import gi
gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # noqa: E402

from ..formats import bytes_per_frame
from .frames import Frame

log = logging.getLogger(__name__)

MONO16_MODES = ("stretch", "shift")
JPEG_ENCODERS = ("auto", "hw", "cpu")
HW_ELEMENT = "nvjpegenc"
CPU_ELEMENT = "jpegenc"
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
                 timeout_s: float = 5.0, encoder: str = "auto", hw_element: str = HW_ELEMENT):
        if not 1 <= int(quality) <= 100:
            raise ValueError(f"quality: expected 1..100, got {quality!r}")
        if int(max_width) < 0:
            raise ValueError(f"max_width: must be >= 0, got {max_width!r}")
        if mono16 not in MONO16_MODES:
            raise ValueError(f"mono16: expected one of {MONO16_MODES}, got {mono16!r}")
        if encoder not in JPEG_ENCODERS:
            raise ValueError(f"jpeg_encoder: expected one of {JPEG_ENCODERS}, got {encoder!r}")
        self.quality = int(quality)
        self.max_width = int(max_width)
        self.mono16 = mono16
        self.timeout_ns = int(timeout_s * Gst.SECOND)
        self.requested = encoder
        self.hw_element = hw_element      # injectable so the hardware path is testable off-Jetson
        self.fallback: Optional[str] = None
        self._pipes = {}
        self._lock = threading.Lock()
        self._pts = 0
        self._hw = False
        with self._lock:
            self._hw = self._choose()

    @property
    def hardware(self) -> bool:
        return self._hw

    @property
    def backend(self) -> str:
        return self.hw_element if self._hw else CPU_ELEMENT

    def describe(self) -> dict:
        return {"backend": self.backend, "hardware": self.hardware, "requested": self.requested,
                "fallback": self.fallback, "quality": self.quality, "max_width": self.max_width}

    def _choose(self) -> bool:
        """True when the hardware encoder is present and encodes a probe frame."""
        if self.requested == "cpu":
            return False
        if Gst.ElementFactory.find(self.hw_element) is None:
            if self.requested == "hw":
                self.fallback = f"{self.hw_element} is not available on this host"
                log.warning("encoder: hardware JPEG requested but %s; using %s", self.fallback, CPU_ELEMENT)
            else:
                log.info("encoder: %s not available; using %s", self.hw_element, CPU_ELEMENT)
            return False
        self._hw = True
        try:
            self._encode(self._input(Frame(0, 0, 0, "probe", 0, None, "GRAY8", 16, 16, None, bytes(range(256)))))
        except Exception as e:   # noqa: BLE001 -- any probe failure means: do not use it
            self._close_pipes()
            self.fallback = f"{self.hw_element} probe failed: {e}"
            log.warning("encoder: %s; using %s", self.fallback, CPU_ELEMENT)
            return False
        log.info("encoder: hardware JPEG (%s)", self.hw_element)
        return True

    def _input(self, frame: Frame):
        """(caps, data, width, height) to push: 16-bit made 8-bit, rows padded, mosaic labelled.
        A frame shorter than its geometry is refused here: videoconvert does not always notice."""
        fmt, data, w, h = frame.fmt, frame.data, frame.width, frame.height
        need = bytes_per_frame(fmt, w, h)
        if w <= 0 or h <= 0 or len(data) < need:
            raise EncodeError(f"truncated frame: {len(data)} bytes for {fmt} {w}x{h} (needs {need})")
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
        hw = self.hardware
        ow, oh = scaled_size(w, h, self.max_width)
        scale = (ow, oh) != (w, h)
        # I420 for the hardware encoder wants even sides: drop the last column / row of an odd frame
        # (no resampling, and the principal point does not move). A downscale is already even.
        crop = hw and not scale and bool(w % 2 or h % 2)
        if crop:
            ow, oh = w - w % 2, h - h % 2
        key = (hw, caps, ow, oh)
        if key in self._pipes:
            pipe, src, sink = self._pipes[key]
            return (pipe, src, sink), ow, oh
        chain = ["bayer2rgb"] if caps.startswith("video/x-bayer") else []
        if crop:
            chain.append(f"videocrop right={w % 2} bottom={h % 2}")
        chain.append("videoconvert")
        out_caps = ["format=I420"] if hw else []
        if scale:
            chain.append("videoscale")
            out_caps.append(f"width={ow},height={oh}")
        if out_caps:
            chain.append("video/x-raw," + ",".join(out_caps))
        desc = (f'appsrc name=src is-live=false format=time caps="{caps}" ! '
                + " ! ".join(chain)
                + f" ! {self.backend} quality={self.quality} ! appsink name=sink sync=false max-buffers=1")
        pipe = Gst.parse_launch(desc)
        src, sink = pipe.get_by_name("src"), pipe.get_by_name("sink")
        if pipe.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            pipe.set_state(Gst.State.NULL)
            raise EncodeError(f"could not start the encoder pipeline: {desc}")
        log.info("encoder: %s -> %dx%d jpeg q=%d (%s)", caps.split(",framerate", 1)[0], ow, oh,
                 self.quality, self.backend)
        self._pipes[key] = (pipe, src, sink)
        return (pipe, src, sink), ow, oh

    def _bus_error(self, pipe) -> Optional[str]:
        msg = pipe.get_bus().pop_filtered(Gst.MessageType.ERROR)
        if msg is None:
            return None
        err, dbg = msg.parse_error()
        return f"{err.message} ({dbg})" if dbg else err.message

    def _encode(self, inp) -> Encoded:
        caps, data, w, h = inp
        try:
            (pipe, src, sink), ow, oh = self._pipeline(caps, w, h)
        except GLib.Error as e:
            raise EncodeError(f"could not build the {self.backend} pipeline: {e.message}") from None
        buf = Gst.Buffer.new_wrapped(bytes(data))
        buf.pts = self._pts
        self._pts += Gst.MSECOND
        if src.emit("push-buffer", buf) != Gst.FlowReturn.OK:
            err = self._bus_error(pipe)
            self._drop(pipe)
            raise EncodeError(err or "encoder refused the frame")
        sample = sink.emit("try-pull-sample", self.timeout_ns)
        if sample is None:
            err = self._bus_error(pipe)
            self._drop(pipe)
            raise EncodeError(err or "encoder timed out")
        out = sample.get_buffer()
        return Encoded(out.extract_dup(0, out.get_size()), ow, oh)

    def encode(self, frame: Frame) -> Encoded:
        with self._lock:
            inp = self._input(frame)          # a malformed frame is the frame's fault, on any encoder
            if not self.hardware:
                return self._encode(inp)
            try:
                return self._encode(inp)
            except EncodeError as hw_err:
                self._hw = False
                try:
                    out = self._encode(inp)
                except EncodeError:
                    self._hw = True           # the CPU cannot encode it either: the frame, not the encoder
                    raise
                self.fallback = f"{self.hw_element} failed at runtime: {hw_err}"
                log.warning("encoder: %s; using %s from now on", self.fallback, CPU_ELEMENT)
                self._close_pipes(hw=True)
                return out

    def _drop(self, pipe) -> None:
        for key, entry in list(self._pipes.items()):
            if entry[0] is pipe:
                del self._pipes[key]
        pipe.set_state(Gst.State.NULL)

    def _close_pipes(self, hw: Optional[bool] = None) -> None:
        for key, entry in list(self._pipes.items()):
            if hw is None or key[0] == hw:
                entry[0].set_state(Gst.State.NULL)
                del self._pipes[key]

    def close(self) -> None:
        with self._lock:
            self._close_pipes()
