"""A small EXIF writer (stdlib only) and the JPEG splice that adds it without re-encoding.

What a capture's JPEG carries (docs/CAPTURE.md "EXIF"):
  IFD0       Software, DateTime
  Exif IFD   ExifVersion, DateTimeOriginal + SubSecTimeOriginal + OffsetTimeOriginal (the frame's
             UTC capture time), PixelXDimension / PixelYDimension
  GPS IFD    (only with a fix) GPSVersionID, latitude / longitude (+ refs), GPSAltitude (+ ref) when a
             height above MEAN SEA LEVEL is known, GPSTimeStamp / GPSDateStamp, GPSMapDatum WGS-84,
             GPSSpeed / GPSTrack when the fix has a velocity, GPSImgDirection when a yaw is given,
             GPSHPositioningError when an accuracy is given

EXIF has no tag for pitch or roll and expects altitude above sea level; the JSON sidecar keeps the
full-precision fix, the ellipsoid height and the orientation. Written big-endian ("MM"), every
value word-aligned, one APP1 segment placed after the JFIF APP0 (replacing any Exif APP1 already
there).
"""
from __future__ import annotations

import math
import struct
import time
from typing import Optional

from .request import GpsFix

SOFTWARE = "camera-service frame-capture"

_BYTE, _ASCII, _SHORT, _LONG, _RATIONAL, _UNDEFINED = 1, 2, 3, 4, 5, 7
_EXIF_POINTER, _GPS_POINTER = 0x8769, 0x8825
_EXIF_HEADER = b"Exif\x00\x00"
_U32 = 0xFFFFFFFF


class ExifError(ValueError):
    pass


# ---- value encoders: (type, count, payload) ------------------------------------------------
def _ascii(s: str):
    b = s.encode("ascii") + b"\x00"
    return _ASCII, len(b), b


def _bytes(seq):
    return _BYTE, len(seq), bytes(seq)


def _short(v: int):
    return _SHORT, 1, struct.pack(">H", v)


def _long(v: int):
    return _LONG, 1, struct.pack(">I", v & _U32)


def _undefined(b: bytes):
    return _UNDEFINED, len(b), b


def _rationals(*pairs):
    out = b""
    for num, den in pairs:
        if not (0 <= num <= _U32 and 0 < den <= _U32):
            raise ExifError(f"rational out of range: {num}/{den}")
        out += struct.pack(">II", num, den)
    return _RATIONAL, len(pairs), out


def _rat(x: float, den: int):
    return int(round(abs(x) * den)), den


def _dms(deg: float):
    a = abs(deg)
    d = int(a)
    m = int((a - d) * 60)
    s = (a - d - m / 60.0) * 3600.0
    return _rationals((d, 1), (m, 1), _rat(max(0.0, s), 1_000_000))


# ---- TIFF assembly -----------------------------------------------------------------------
def _ifd(entries: dict, base: int) -> bytes:
    """One IFD at offset `base` (relative to the TIFF header), its out-of-line values after it."""
    n = len(entries)
    size = 2 + 12 * n + 4
    head = bytearray(struct.pack(">H", n))
    data = bytearray()
    for tag in sorted(entries):
        typ, count, payload = entries[tag]
        if len(payload) <= 4:
            head += struct.pack(">HHI", tag, typ, count) + payload.ljust(4, b"\x00")
        else:
            head += struct.pack(">HHII", tag, typ, count, base + size + len(data))
            data += payload
            if len(data) % 2:
                data += b"\x00"
    head += struct.pack(">I", 0)
    return bytes(head + data)


def _split_ns(utc_ns: int):
    secs, frac_ns = divmod(int(utc_ns), 1_000_000_000)
    return time.gmtime(secs), frac_ns


def build_exif(capture_utc_ns: int, width: int, height: int, gps: Optional[GpsFix] = None,
               gps_utc_ns: Optional[int] = None, yaw_deg: Optional[float] = None,
               software: str = SOFTWARE) -> bytes:
    """The complete APP1 segment (marker included) for one capture."""
    tm, frac_ns = _split_ns(capture_utc_ns)
    stamp = time.strftime("%Y:%m:%d %H:%M:%S", tm)
    ifd0 = {0x0131: _ascii(software), 0x0132: _ascii(stamp)}
    exif = {
        0x9000: _undefined(b"0232"),
        0x9003: _ascii(stamp),
        0x9011: _ascii("+00:00"),                       # OffsetTimeOriginal: the time is UTC
        0x9291: _ascii(f"{frac_ns // 1000:06d}"),       # SubSecTimeOriginal (microseconds)
        0xA002: _long(int(width)),
        0xA003: _long(int(height)),
    }
    gps_ifd = None
    if gps is not None:
        gtm, gfrac = _split_ns(gps_utc_ns if gps_utc_ns is not None else capture_utc_ns)
        gps_ifd = {
            0x0000: _bytes((2, 3, 0, 0)),
            0x0001: _ascii("N" if gps.lat >= 0 else "S"),
            0x0002: _dms(gps.lat),
            0x0003: _ascii("E" if gps.lon >= 0 else "W"),
            0x0004: _dms(gps.lon),
            0x0007: _rationals((gtm.tm_hour, 1), (gtm.tm_min, 1),
                               _rat(gtm.tm_sec + gfrac / 1e9, 1000)),
            0x0012: _ascii("WGS-84"),
            0x001D: _ascii(time.strftime("%Y:%m:%d", gtm)),
        }
        msl = gps.msl_alt_m()
        if msl is not None:
            gps_ifd[0x0005] = _bytes((0 if msl >= 0 else 1,))
            gps_ifd[0x0006] = _rationals(_rat(msl, 1000))
        if gps.vel_enu is not None:
            ve, vn, _vu = gps.vel_enu
            speed = math.hypot(ve, vn)
            gps_ifd[0x000C] = _ascii("K")
            gps_ifd[0x000D] = _rationals(_rat(speed * 3.6, 1000))
            if speed > 0.05:                            # a course is undefined at a standstill
                gps_ifd[0x000E] = _ascii("T")
                gps_ifd[0x000F] = _rationals(_rat(math.degrees(math.atan2(ve, vn)) % 360.0, 100))
        if gps.h_acc_m is not None:
            gps_ifd[0x001F] = _rationals(_rat(gps.h_acc_m, 1000))
    if yaw_deg is not None:
        gps_ifd = gps_ifd if gps_ifd is not None else {0x0000: _bytes((2, 3, 0, 0))}
        gps_ifd[0x0010] = _ascii("T")
        gps_ifd[0x0011] = _rationals(_rat(yaw_deg % 360.0, 100))

    # IFD0's size does not depend on the pointer VALUES, so lay it out once with placeholders,
    # place the sub-IFDs after it, then rebuild IFD0 with the real offsets.
    ifd0[_EXIF_POINTER] = _long(0)
    if gps_ifd is not None:
        ifd0[_GPS_POINTER] = _long(0)
    exif_off = 8 + len(_ifd(ifd0, 8))
    exif_bytes = _ifd(exif, exif_off)
    ifd0[_EXIF_POINTER] = _long(exif_off)
    tail = exif_bytes
    if gps_ifd is not None:
        gps_off = exif_off + len(exif_bytes)
        ifd0[_GPS_POINTER] = _long(gps_off)
        tail += _ifd(gps_ifd, gps_off)
    tiff = b"MM" + struct.pack(">HI", 42, 8) + _ifd(ifd0, 8) + tail
    body = _EXIF_HEADER + tiff
    if len(body) + 2 > 0xFFFF:
        raise ExifError(f"EXIF too large for one APP1 segment ({len(body)} bytes)")
    return b"\xFF\xE1" + struct.pack(">H", len(body) + 2) + body


def insert_exif(jpeg: bytes, app1: bytes) -> bytes:
    """`jpeg` with `app1` placed after the leading JFIF APP0 (or right after SOI), replacing any
    Exif APP1 already present. The entropy-coded image is copied untouched."""
    if len(jpeg) < 4 or jpeg[:2] != b"\xFF\xD8":
        raise ExifError("not a JPEG (no SOI marker)")
    pos = 2
    jfif, others = [], []
    while pos + 4 <= len(jpeg) and jpeg[pos] == 0xFF:
        marker = jpeg[pos + 1]
        if not (0xE0 <= marker <= 0xEF or marker == 0xFE):
            break
        (seglen,) = struct.unpack(">H", jpeg[pos + 2:pos + 4])
        if seglen < 2 or pos + 2 + seglen > len(jpeg):
            raise ExifError("truncated JPEG segment")
        seg = jpeg[pos:pos + 2 + seglen]
        if marker == 0xE0 and not others and seg[4:9] == b"JFIF\x00":
            jfif.append(seg)
        elif not (marker == 0xE1 and seg[4:10] == _EXIF_HEADER):
            others.append(seg)
        pos += 2 + seglen
    return b"\xFF\xD8" + b"".join(jfif) + app1 + b"".join(others) + jpeg[pos:]
