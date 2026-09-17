"""Pure helpers for the ros2-source writer -- no ROS, no GStreamer imports, so they test on any host.

  * ROS image encodings -> GStreamer caps for the convert pipeline's input.
  * The instance's PINNED format (its `shm:` block, Aravis-style Mono8/BayerRG8 or a GStreamer raw
    name) -> the output caps and the header pixfmt.
  * The 36-byte `application/x-cam-frame` header for `framing: header`. This MIRRORS
    core-driver/cam_driver/transport.py field for field (the core reads it); the cross-check lives in
    core-driver/tests/test_ros2_source.py, which packs both and compares bytes.
  * Row unpadding: a ROS Image may carry `step` > width * bytes_per_pixel; the transport carries
    tightly packed rows.
"""
from __future__ import annotations

import struct

# ---- the header (transport.py mirror) ----------------------------------------------------------
HEADER_FORMAT = "<4sHHQQHHIBBH"
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)   # 36
MAGIC = b"CAMF"
VERSION = 1
HEADER_CAPS = "application/x-cam-frame"
PIXFMT_CODE = {"GRAY8": 1, "GRAY16_LE": 2, "GRAY16_BE": 3, "I420": 4, "NV12": 5, "YUY2": 6, "RGB": 7,
               "BGR": 8, "NV24": 9, "YV12": 10, "UYVY": 11, "RGBA": 12, "BGRA": 13, "RGBx": 14, "BGRx": 15}
TS_SOURCE_CODE = {"ptp_chunk": 0, "camera": 1, "system": 2, "sof": 3, "rtp_ntp": 4}
_U64 = 0xFFFFFFFFFFFFFFFF


def pack_header(timestamp_ns: int, frame_id: int, width: int, height: int, gst_format: str,
                ts_source: str = "camera") -> bytes:
    try:
        code = PIXFMT_CODE[gst_format]
    except KeyError:
        raise ValueError(f"{gst_format!r} has no transport pixfmt code (framing: header cannot carry it)") from None
    return struct.pack(HEADER_FORMAT, MAGIC, VERSION, HEADER_SIZE, int(timestamp_ns) & _U64, int(frame_id) & _U64,
                       int(width), int(height), code, TS_SOURCE_CODE.get(ts_source, 1), 0, 0)


# ---- formats ------------------------------------------------------------------------------------
# ROS encoding -> (media type, GStreamer format or Bayer pattern, bytes per pixel or None for planar)
ENCODINGS = {
    "rgb8": ("video/x-raw", "RGB", 3), "bgr8": ("video/x-raw", "BGR", 3),
    "rgba8": ("video/x-raw", "RGBA", 4), "bgra8": ("video/x-raw", "BGRA", 4),
    "mono8": ("video/x-raw", "GRAY8", 1), "8UC1": ("video/x-raw", "GRAY8", 1),
    "mono16": ("video/x-raw", "GRAY16_LE", 2), "16UC1": ("video/x-raw", "GRAY16_LE", 2),
    "yuv422_yuy2": ("video/x-raw", "YUY2", 2), "yuyv": ("video/x-raw", "YUY2", 2),
    "yuv422": ("video/x-raw", "UYVY", 2), "uyvy": ("video/x-raw", "UYVY", 2),
    "nv12": ("video/x-raw", "NV12", None), "nv24": ("video/x-raw", "NV24", None),
    "bayer_rggb8": ("video/x-bayer", "rggb", 1), "bayer_bggr8": ("video/x-bayer", "bggr", 1),
    "bayer_gbrg8": ("video/x-bayer", "gbrg", 1), "bayer_grbg8": ("video/x-bayer", "grbg", 1),
}
# Aravis-style Bayer pixel formats (the core's `shm.pixel_format`) -> CFA pattern; mirrors formats.py
BAYER_PATTERN = {"RG": "rggb", "GR": "grbg", "GB": "gbrg", "BG": "bggr"}
GST_RAW = set(PIXFMT_CODE)


def frame_bytes(gst_format: str, width: int, height: int) -> int:
    """Tightly packed frame size (mirrors formats.bytes_per_frame)."""
    px = int(width) * int(height)
    if gst_format in ("GRAY16_LE", "GRAY16_BE"):
        return px * 2
    if gst_format in ("I420", "NV12", "YV12"):
        return px * 3 // 2
    if gst_format in ("YUY2", "UYVY"):
        return px * 2
    if gst_format in ("NV24", "RGB", "BGR"):
        return px * 3
    if gst_format in ("RGBA", "BGRA", "RGBx", "BGRx"):
        return px * 4
    return px   # GRAY8


class Encoding:
    """What one ROS encoding is on the wire, with the message's endianness applied."""

    def __init__(self, encoding: str, is_bigendian: bool = False):
        enc = str(encoding or "").strip()
        try:
            media, fmt, bpp = ENCODINGS[enc.lower() if enc.lower() in ENCODINGS else enc]
        except KeyError:
            raise ValueError(f"ROS image encoding {enc!r} is not supported "
                             f"(one of {', '.join(sorted(ENCODINGS))})") from None
        if fmt == "GRAY16_LE" and is_bigendian:
            fmt = "GRAY16_BE"
        self.encoding = enc
        self.media = media          # video/x-raw | video/x-bayer
        self.format = fmt           # GStreamer format, or the CFA pattern for video/x-bayer
        self.bytes_per_pixel = bpp  # None = planar (no row unpadding)
        self.bayer = media == "video/x-bayer"

    @property
    def raw_format(self) -> str:
        """The GStreamer raw format of the BYTES: a Bayer mosaic is GRAY8 bytes."""
        return "GRAY8" if self.bayer else self.format

    def caps(self, width: int, height: int, fps: float) -> str:
        return f"{self.media},format={self.format},width={int(width)},height={int(height)},framerate={_fps(fps)}"


class Pinned:
    """The instance's pinned `shm:` format: what every frame must be on the socket."""

    def __init__(self, pixel_format: str, width: int, height: int, fps: float):
        pf = str(pixel_format or "RGB").strip()
        self.pixel_format, self.width, self.height, self.fps = pf, int(width), int(height), float(fps or 10.0)
        self.bayer = None
        if pf in GST_RAW:
            self.format = pf
        elif pf.startswith("Bayer") and len(pf) >= 7 and pf[5:7].upper() in BAYER_PATTERN and pf.endswith("8"):
            self.format, self.bayer = "GRAY8", BAYER_PATTERN[pf[5:7].upper()]
        elif pf.startswith("Mono"):
            self.format = "GRAY16_LE" if any(t in pf for t in ("16", "12", "10")) else "GRAY8"
        else:
            raise ValueError(f"pinned pixel_format {pf!r}: use a GStreamer raw format (RGB, GRAY8, NV12, ...) "
                             f"or Mono8/Mono16/Bayer{{RG,GR,GB,BG}}8")
        self.frame_bytes = frame_bytes(self.format, self.width, self.height)

    def raw_caps(self) -> str:
        """`video/x-raw` caps of the bytes (the convert pipeline's output; shm/header framings)."""
        return f"video/x-raw,format={self.format},width={self.width},height={self.height},framerate={_fps(self.fps)}"

    def unixfd_caps(self) -> str:
        """Self-describing caps for unixfd: a Bayer mosaic is tagged as such (the core checks the pattern)."""
        if self.bayer:
            return f"video/x-bayer,format={self.bayer},width={self.width},height={self.height},framerate={_fps(self.fps)}"
        return self.raw_caps()

    def matches(self, enc: Encoding, width: int, height: int) -> bool:
        """True when a message's bytes ARE the pinned frame (no conversion needed)."""
        if (int(width), int(height)) != (self.width, self.height):
            return False
        if self.bayer:
            return enc.bayer and enc.format == self.bayer
        return not enc.bayer and enc.format == self.format


def _fps(fps: float) -> str:
    f = float(fps or 10.0)
    if abs(f - round(f)) < 1e-6:
        return f"{int(round(f))}/1"
    return f"{int(round(f * 1000))}/1000"


def unpad_rows(data, step: int, row_bytes: int, height: int) -> bytes:
    """Tightly pack rows when the message's `step` carries padding. Returns `data` as bytes otherwise."""
    step, row_bytes, height = int(step), int(row_bytes), int(height)
    if step <= row_bytes or row_bytes <= 0:
        return bytes(data)
    mv = memoryview(data)
    return b"".join(bytes(mv[r * step: r * step + row_bytes]) for r in range(height))


def stamp_ns(sec: int, nanosec: int) -> int:
    return int(sec) * 1_000_000_000 + int(nanosec)
