"""FLIR Boson health provider: the camera's own state -- FPA temperature, flat-field-correction
(FFC) state, frame counter, serial/part number -- over its serial COMMAND channel. docs/HEALTH.md
"boson".

Over USB a Boson is a composite device: a UVC camera (the v4l2 node the pipeline streams from) AND a
CDC-ACM serial port (`/dev/ttyACM*`, stably `/dev/serial/by-id/usb-FLIR_Boson_<sn>-if00`) carrying
FLIR's command protocol. The two are independent interfaces, so this polls the serial port while the
video streams untouched. Configured on the source: usb.control_protocol: flir-boson +
usb.control_device.

The wire protocol (FLIR's serial framing, as used by the Boson SDK):

    0x8E | channel(1)=0 | sequence(4) | function id(4) | status(4) | payload | CRC-16(2) | 0xAE

big-endian; a request's status field is 0xFFFFFFFF, a reply's is the return code (0 = success).
CRC-16/AUG-CCITT (poly 0x1021, init 0x1D0F) over channel..payload, before escaping. Between the
start and end markers the bytes 0x8E / 0x9E / 0xAE are escaped as 0x9E followed by the byte - 0x0D,
so a frame is always delimited by its markers.

stdlib only (os/termios/select): no pyserial in the image, and the unit tests drive a pty.
"""
from __future__ import annotations

import binascii
import errno
import logging
import os
import select
import struct
import threading
import time
from typing import Callable, List, Optional

from .health import OK, Report

log = logging.getLogger(__name__)

START, END, ESC = 0x8E, 0xAE, 0x9E
_ESCAPES = {0x8E: 0x81, 0x9E: 0x91, 0xAE: 0xA1}
CRC_INIT = 0x1D0F
REQUEST_STATUS = 0xFFFFFFFF
BAUD = 921600          # ignored by a USB CDC-ACM port, set anyway (a UART-attached Boson needs it)

# Function ids (FLIR Boson SDK). Payloads are big-endian.
FN_CAMERA_SN = 0x00050002      # uint32
FN_PART_NUMBER = 0x00050004    # 20 bytes ASCII, NUL-padded
FN_FFC_STATE = 0x0005000C      # uint16 enum, FFC_STATES
FN_FPA_TEMP = 0x00050030       # int16, degrees C x 10
FN_LAST_FFC_FRAME = 0x0005005D  # uint32, frame count at the last FFC
FN_FRAME_COUNT = 0x00020002    # uint32, frames since power-on

FFC_STATES = {0: "none", 1: "imminent", 2: "in_progress", 3: "complete"}
RETURN_CODES = {0x0161: "bad command id", 0x0162: "bad payload", 0x0170: "unspecified error",
                0x017D: "insufficient bytes", 0x017E: "excess bytes", 0x017F: "buffer overflow",
                0x0203: "range error"}


class BosonError(RuntimeError):
    """A command the camera answered badly (or not at all). The port itself is still usable."""


# ---- framing (pure) ---------------------------------------------------------------
def crc16(data: bytes) -> int:
    return binascii.crc_hqx(bytes(data), CRC_INIT)


def escape(body: bytes) -> bytes:
    out = bytearray()
    for b in body:
        if b in _ESCAPES:
            out += bytes((ESC, _ESCAPES[b]))
        else:
            out.append(b)
    return bytes(out)


def unescape(body: bytes) -> bytes:
    out, it = bytearray(), iter(body)
    for b in it:
        if b == ESC:
            nxt = next(it, None)
            if nxt is None:
                raise BosonError("frame ends inside an escape")
            out.append((nxt + 0x0D) & 0xFF)
        else:
            out.append(b)
    return bytes(out)


def encode_frame(seq: int, function_id: int, status: int = REQUEST_STATUS, payload: bytes = b"") -> bytes:
    body = struct.pack(">BIII", 0, seq & 0xFFFFFFFF, function_id, status) + bytes(payload)
    body += struct.pack(">H", crc16(body))
    return bytes((START,)) + escape(body) + bytes((END,))


def decode_frame(frame: bytes):
    """(seq, function_id, status, payload) of one frame, markers included. Raises BosonError."""
    if len(frame) < 2 or frame[0] != START or frame[-1] != END:
        raise BosonError("not a frame")
    body = unescape(frame[1:-1])
    if len(body) < 15:
        raise BosonError(f"short frame ({len(body)} bytes)")
    (crc,) = struct.unpack(">H", body[-2:])
    if crc != crc16(body[:-2]):
        raise BosonError("CRC mismatch")
    channel, seq, fid, status = struct.unpack(">BIII", body[:13])
    return seq, fid, status, body[13:-2]


class FrameSplitter:
    """Bytes in, whole frames out. Garbage before a start marker is dropped."""

    def __init__(self):
        self._buf = bytearray()

    def feed(self, data: bytes) -> List[bytes]:
        self._buf += data
        frames = []
        while True:
            s = self._buf.find(START)
            if s < 0:
                self._buf.clear()
                return frames
            e = self._buf.find(END, s + 1)
            if e < 0:
                del self._buf[:s]
                return frames
            frames.append(bytes(self._buf[s:e + 1]))
            del self._buf[:e + 1]


# ---- the port -------------------------------------------------------------------------
class BosonPort:
    """One open command channel. Not thread-safe: the health thread is its only user."""

    def __init__(self, path: str, timeout_s: float = 0.5):
        self.path = path
        self.timeout_s = timeout_s
        self._fd: Optional[int] = None
        self._seq = 0

    def open(self) -> None:
        import termios   # Linux/macOS stdlib; imported here so the pure framing imports anywhere
        import tty
        fd = os.open(self.path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        try:
            tty.setraw(fd)
            attrs = termios.tcgetattr(fd)
            speed = getattr(termios, f"B{BAUD}", None)
            if speed is not None:
                attrs[4] = attrs[5] = speed
            termios.tcsetattr(fd, termios.TCSANOW, attrs)
        except termios.error:
            pass   # a pty / odd driver: the framing doesn't depend on line settings
        self._fd = fd

    @property
    def is_open(self) -> bool:
        return self._fd is not None

    def close(self) -> None:
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None

    def command(self, function_id: int, payload: bytes = b"") -> bytes:
        """Send one command, return the reply payload. BosonError on a bad/absent reply; OSError
        when the device itself is gone (the caller closes and reopens)."""
        if self._fd is None:
            raise OSError(errno.EBADF, "port not open")
        self._drain()
        self._seq = (self._seq + 1) & 0xFFFFFFFF
        seq = self._seq
        frame = encode_frame(seq, function_id, payload=payload)
        view = memoryview(frame)
        while view:
            n = os.write(self._fd, view)
            view = view[n:]
        splitter = FrameSplitter()
        deadline = time.monotonic() + self.timeout_s
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise BosonError(f"no reply to 0x{function_id:08X} within {self.timeout_s:g}s")
            r, _, _ = select.select([self._fd], [], [], left)
            if not r:
                continue
            data = os.read(self._fd, 4096)
            if not data:
                raise OSError(errno.EIO, "port closed")
            for f in splitter.feed(data):
                try:
                    rseq, rfid, status, body = decode_frame(f)
                except BosonError as e:
                    log.debug("boson: dropped a frame: %s", e)
                    continue
                if rseq != seq or rfid != function_id:
                    continue   # a late reply to an earlier, timed-out command
                if status != 0:
                    raise BosonError(f"0x{function_id:08X}: camera returned 0x{status:04X} "
                                     f"({RETURN_CODES.get(status, 'unknown')})")
                return body

    def _drain(self) -> None:
        """Discard anything unread (a late reply) so it can't be taken for this command's."""
        while True:
            r, _, _ = select.select([self._fd], [], [], 0)
            if not r:
                return
            try:
                if not os.read(self._fd, 4096):
                    return
            except BlockingIOError:
                return


def _unpack(fmt: str, body: bytes):
    need = struct.calcsize(fmt)
    if len(body) < need:
        raise BosonError(f"reply too short ({len(body)} < {need} bytes)")
    return struct.unpack(fmt, body[:need])[0]


# ---- the provider ------------------------------------------------------------------------
class BosonProvider:
    name = "boson"
    components = ("camera",)

    def __init__(self, device: str, *, port_factory: Callable[[str], BosonPort] = BosonPort,
                 stop_event: Optional[threading.Event] = None):
        self.device = device
        self._port_factory = port_factory
        self._port: Optional[BosonPort] = None
        self._hardware_id = ""
        self._stop = stop_event or threading.Event()

    def poll(self) -> List[Report]:
        port = self._ensure_port()
        values, errors = {}, []
        reads = (
            ("temp.sensor_c", FN_FPA_TEMP, lambda b: _unpack(">h", b) / 10.0),   # FPA = the imager
            ("ffc.state", FN_FFC_STATE, lambda b: FFC_STATES.get(_unpack(">H", b), "unknown")),
            ("frame_count", FN_FRAME_COUNT, lambda b: _unpack(">I", b)),
            ("ffc.last_frame", FN_LAST_FFC_FRAME, lambda b: _unpack(">I", b)),
        )
        for key, fid, parse in reads:
            if self._stop.is_set():
                break
            try:
                values[key] = parse(port.command(fid))
            except BosonError as e:
                errors.append(str(e))
            except OSError:
                self._close()   # the device went away (unplug): reopen on a later poll
                raise
        if reads and len(errors) >= len(reads):
            self._close()   # a port that answers nothing is as good as gone; reopen next time
            raise BosonError(f"camera not answering on {self.device} ({errors[0]})")
        if isinstance(values.get("frame_count"), int) and isinstance(values.get("ffc.last_frame"), int):
            values["ffc.frames_since"] = values["frame_count"] - values["ffc.last_frame"]
        if errors:
            values["read_errors"] = len(errors)
            log.debug("boson: read errors: %s", "; ".join(errors))
        msgs = []
        if isinstance(values.get("temp.sensor_c"), float):
            msgs.append(f"{values['temp.sensor_c']:g} C")
        if values.get("ffc.state") in ("imminent", "in_progress"):
            # Normal operation (the shutter closes for ~a second, the image freezes): OK, but said,
            # so a viewer knows why the picture is frozen right now.
            msgs.append(f"FFC {values['ffc.state'].replace('_', ' ')}")
        return [Report("camera", OK, "; ".join(msgs) or "OK", hardware_id=self._hardware_id, values=values)]

    def _ensure_port(self) -> BosonPort:
        if self._port is not None and self._port.is_open:
            return self._port
        port = self._port_factory(self.device)
        port.open()   # OSError (absent/stale node, permissions) -> the monitor holds, then STALE
        try:
            self._hardware_id = self._identify(port)
        except OSError:
            port.close()
            raise
        self._port = port
        log.info("health: boson command channel open on %s (%s)", self.device, self._hardware_id or "unidentified")
        return port

    @staticmethod
    def _identify(port: BosonPort) -> str:
        """"FLIR Boson <part number> sn <serial>" -- read once per open, best-effort."""
        parts = ["FLIR Boson"]
        try:
            pn = port.command(FN_PART_NUMBER).split(b"\0", 1)[0].decode("ascii", "replace").strip()
            if pn:
                parts.append(pn)
        except BosonError as e:
            log.debug("boson: part number unreadable: %s", e)
        try:
            parts.append(f"sn {_unpack('>I', port.command(FN_CAMERA_SN))}")
        except BosonError as e:
            log.debug("boson: serial number unreadable: %s", e)
        return " ".join(parts)

    def _close(self) -> None:
        if self._port is not None:
            self._port.close()
        self._port = None

    def close(self) -> None:
        self._close()
