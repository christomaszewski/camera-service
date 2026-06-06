"""Wire format for the same-host frame transport (shm now; unixfd / Zenoh later).

GStreamer's shmsink/shmsrc transmit ONLY the raw buffer bytes -- PTS/DTS and all
GstMeta are dropped across the process boundary. So we carry per-frame metadata
(absolute capture timestamp, frame id, geometry, provenance) explicitly as a fixed
binary header prepended to each frame's pixel bytes, under the custom caps
`application/x-cam-frame`:

    [ 36-byte FrameHeader ][ raw pixel bytes ]

The core prepends this header on the transport tee branch (after the rate-limit
drop-probe). A consumer (e.g. the C++ ROS2 bridge) reads the header, treats the
remainder as pixels, and stamps its message from `timestamp_ns`.

This header is the CONTRACT between the core and any out-of-process plugin; the C++
bridge must mirror this exact layout. Rules: little-endian; never reorder v1 fields;
bump `version` and read `header_len` for forward compatibility.

Layout (little-endian, fixed 36 bytes for v1 -- struct "<4sHHQQHHIBBH"):
    magic        4s   b"CAMF"
    version      u16  = 1
    header_len   u16  = 36  (offset to pixel data; lets consumers skip unknown v2+ fields)
    timestamp_ns u64  absolute capture time (ns); PTP epoch when locked
    frame_id     u64  camera frame id (GVSP block id or chunk frame id)
    width        u16
    height       u16
    pixfmt       u32  code (PIXFMT map) -> GStreamer raw format
    ts_source    u8   0=ptp_chunk, 1=camera, 2=system  (provenance)
    flags        u8   reserved bitfield
    reserved     u16
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

CAPS = "application/x-cam-frame"
MAGIC = b"CAMF"
VERSION = 1
_FORMAT = "<4sHHQQHHIBBH"
HEADER_SIZE = struct.calcsize(_FORMAT)  # 36
_U64 = 0xFFFFFFFFFFFFFFFF

# pixfmt codes <-> GStreamer raw video formats (keep in sync with the C++ bridge)
_CODE_TO_GST = {1: "GRAY8", 2: "GRAY16_LE", 3: "GRAY16_BE"}
_GST_TO_CODE = {v: k for k, v in _CODE_TO_GST.items()}

# ts_source codes mirror cam_driver.timestamps.TimestampSource values
TS_SOURCE_CODE = {"ptp_chunk": 0, "camera": 1, "system": 2}
TS_SOURCE_NAME = {v: k for k, v in TS_SOURCE_CODE.items()}


class TransportError(ValueError):
    pass


def gst_format_to_code(fmt: str) -> int:
    try:
        return _GST_TO_CODE[fmt]
    except KeyError:
        raise TransportError(f"unsupported transport pixel format {fmt!r}") from None


def code_to_gst_format(code: int) -> str:
    try:
        return _CODE_TO_GST[code]
    except KeyError:
        raise TransportError(f"unknown transport pixfmt code {code}") from None


@dataclass
class FrameHeader:
    timestamp_ns: int
    frame_id: int
    width: int
    height: int
    pixfmt: str                  # GStreamer raw format string, e.g. "GRAY8"
    ts_source: str = "ptp_chunk"
    flags: int = 0
    version: int = VERSION

    def pack(self) -> bytes:
        return struct.pack(
            _FORMAT, MAGIC, self.version, HEADER_SIZE,
            int(self.timestamp_ns) & _U64, int(self.frame_id) & _U64,
            int(self.width), int(self.height), gst_format_to_code(self.pixfmt),
            TS_SOURCE_CODE.get(self.ts_source, 1), int(self.flags) & 0xFF, 0,
        )


def unpack_header(data) -> FrameHeader:
    """Parse the leading header from a transport buffer (data may include pixels)."""
    if len(data) < HEADER_SIZE:
        raise TransportError(f"buffer too small for header: {len(data)} < {HEADER_SIZE}")
    magic, version, header_len, ts, fid, w, h, pixfmt, src, flags, _ = struct.unpack(
        _FORMAT, bytes(data[:HEADER_SIZE]))
    if magic != MAGIC:
        raise TransportError(f"bad magic {magic!r}")
    if version != VERSION:
        raise TransportError(f"unsupported header version {version} (this build: {VERSION})")
    return FrameHeader(
        timestamp_ns=ts, frame_id=fid, width=w, height=h,
        pixfmt=code_to_gst_format(pixfmt),
        ts_source=TS_SOURCE_NAME.get(src, "camera"), flags=flags, version=version,
    )
