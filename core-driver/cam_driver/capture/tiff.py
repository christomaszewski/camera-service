"""A single-band TIFF writer (stdlib + numpy) for radiometric captures (docs/CAPTURE.md "TIFF").

JPEG is 8-bit, so a 16-bit thermal frame loses its values on the way to a JPEG. The `tiff` format
keeps them: the camera's own 16-bit samples, unscaled, in a baseline little-endian ("II") grayscale
TIFF -- one uncompressed strip, BitsPerSample 16 (or 8 for an 8-bit mono plane), SampleFormat
unsigned -- with the same EXIF and GPS IFDs as the JPEG (exif.layout) and the XMP packet in tag 700.
What the numbers mean is the camera's business: raw counts, or temperature for a radiometric core
in a linear mode (FLIR's TLinear modes are kelvin x 100 at high gain; check the core's documentation).

Layout: header, pixel strip at offset 8, then IFD0, the Exif IFD and the GPS IFD. Row padding a
GStreamer buffer may carry is removed; big-endian samples are swapped to the file's byte order.
"""
from __future__ import annotations

import struct
from typing import Optional

from .exif import _long, _short, layout

TIFF_FORMATS = {"GRAY8": (8, "u1"), "GRAY16_LE": (16, "<u2"), "GRAY16_BE": (16, ">u2")}


class TiffError(ValueError):
    pass


def tight_samples(data: bytes, fmt: str, width: int, height: int) -> bytes:
    """The plane's samples as tightly packed little-endian rows."""
    import numpy as np
    bits, dtype = TIFF_FORMATS[fmt]
    bpp = bits // 8
    row = width * bpp
    if height <= 0 or width <= 0 or len(data) < row * height:
        raise TiffError(f"truncated frame: {len(data)} bytes for {fmt} {width}x{height}")
    stride = len(data) // height if len(data) % height == 0 else row
    arr = np.frombuffer(data, dtype=np.uint8, count=stride * height).reshape(height, stride)[:, :row]
    samples = np.ascontiguousarray(arr).view(dtype).reshape(height, width)
    if bits == 16 and samples.dtype.byteorder == ">":
        samples = samples.byteswap().view(samples.dtype.newbyteorder("<"))   # same values, file byte order
    return samples.tobytes()


def build_tiff(fmt: str, width: int, height: int, data: bytes, ifd0: dict, exif: dict,
               gps: Optional[dict], xmp: Optional[bytes] = None) -> bytes:
    """A complete TIFF file. `ifd0`/`exif`/`gps` are exif.exif_ifds' entries; the image tags are
    added to IFD0 here."""
    if fmt not in TIFF_FORMATS:
        raise TiffError(f"tiff needs a mono plane ({', '.join(TIFF_FORMATS)}), got {fmt}")
    bits, _ = TIFF_FORMATS[fmt]
    pixels = tight_samples(data, fmt, width, height)
    strip_off = 8
    ifd_off = strip_off + len(pixels) + (len(pixels) % 2)
    tags = dict(ifd0)
    tags.update({
        256: _long(width), 257: _long(height), 258: _short(bits), 259: _short(1),   # no compression
        262: _short(1),                                                            # BlackIsZero
        273: _long(strip_off), 277: _short(1), 278: _long(height), 279: _long(len(pixels)),
        284: _short(1), 339: _short(1),                                            # chunky, unsigned
    })
    if xmp:
        tags[700] = ("byte", bytes(xmp))
    return (b"II" + struct.pack("<HI", 42, ifd_off) + pixels + b"\x00" * (len(pixels) % 2)
            + layout(tags, exif, gps, ifd_off, "<"))
