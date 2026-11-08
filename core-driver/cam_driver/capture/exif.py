"""EXIF and XMP for captures (stdlib only), the JPEG splice that adds them without re-encoding, and
the IFD layout the 16-bit TIFF writer shares (tiff.py).

What a capture carries (docs/CAPTURE.md "EXIF and XMP"):
  IFD0       Software, DateTime, X/YResolution + ResolutionUnit (+ YCbCrPositioning in a JPEG);
             Make / Model when configured (photogrammetry tools key their camera databases on them)
  Exif IFD   ExifVersion, FlashpixVersion, ColorSpace, DateTimeOriginal + SubSecTimeOriginal +
             OffsetTimeOriginal (the frame's UTC capture time); in a JPEG ComponentsConfiguration and
             PixelXDimension / PixelYDimension; FocalLength, FocalPlaneX/YResolution (+ unit),
             FocalLengthIn35mmFilm when the focal length and pixel pitch are configured
  GPS IFD    (with a fix) GPSVersionID, latitude / longitude (+ refs), GPSAltitude (+ ref) -- above mean
             sea level by default, ellipsoidal when asked --, GPSTimeStamp / GPSDateStamp, GPSMapDatum
             WGS-84, GPSProcessingMethod, GPSSpeed / GPSTrack with a velocity, GPSImgDirection with a yaw,
             GPSHPositioningError with an accuracy
  XMP        Pix4D's Camera namespace (http://pix4d.com/camera/1.0/): Yaw / Pitch / Roll,
             GPSXYAccuracy / GPSZAccuracy, IMU*Accuracy, BandName / CentralWavelength / WavelengthFWHM

IFD entries are kept as typed values and packed in either byte order: the JPEG's EXIF is big-endian
("MM"), the TIFF little-endian ("II"). Every value is word-aligned.
"""
from __future__ import annotations

import math
import struct
import time
from dataclasses import dataclass
from typing import Optional
from xml.sax.saxutils import escape, quoteattr

from .request import GpsFix

SOFTWARE = "camera-service frame-capture"
ALT_MODES = ("msl", "ellipsoid")
PIX4D_NS = "http://pix4d.com/camera/1.0/"

_TYPES = {"byte": 1, "ascii": 2, "short": 3, "long": 4, "rational": 5, "undefined": 7}
_SIZES = {"byte": 1, "ascii": 1, "short": 2, "long": 4, "rational": 8, "undefined": 1}
_EXIF_POINTER, _GPS_POINTER = 0x8769, 0x8825
_EXIF_HEADER = b"Exif\x00\x00"
_XMP_HEADER = b"http://ns.adobe.com/xap/1.0/\x00"
_U32 = 0xFFFFFFFF
_DIAG_35MM = 43.2666            # the diagonal of a 36 x 24 mm frame


class ExifError(ValueError):
    pass


@dataclass(frozen=True)
class CameraInfo:
    """The camera as photogrammetry tools want it described. `pixel_pitch_um` is at the sensor's
    native resolution; a downscaled image's focal-plane resolution is scaled to match."""
    make: Optional[str] = None
    model: Optional[str] = None
    focal_length_mm: Optional[float] = None
    pixel_pitch_um: Optional[float] = None


# ---- typed values ----------------------------------------------------------------------------
def _ascii(s: str):
    return ("ascii", s)


def _short(*v):
    return ("short", list(v))


def _long(*v):
    return ("long", list(v))


def _bytes(seq):
    return ("byte", bytes(seq))


def _undefined(b: bytes):
    return ("undefined", bytes(b))


def _rationals(*pairs):
    for num, den in pairs:
        if not (0 <= num <= _U32 and 0 < den <= _U32):
            raise ExifError(f"rational out of range: {num}/{den}")
    return ("rational", list(pairs))


def _rat(x: float, den: int):
    return int(round(abs(x) * den)), den


def _dms(deg: float):
    a = abs(deg)
    d = int(a)
    m = int((a - d) * 60)
    s = (a - d - m / 60.0) * 3600.0
    return _rationals((d, 1), (m, 1), _rat(max(0.0, s), 1_000_000))


def _pack(value, bo: str):
    kind, v = value
    if kind == "ascii":
        b = v.encode("ascii") + b"\x00"
        return _TYPES[kind], len(b), b
    if kind in ("byte", "undefined"):
        return _TYPES[kind], len(v), v
    if kind == "short":
        return _TYPES[kind], len(v), struct.pack(f"{bo}{len(v)}H", *v)
    if kind == "long":
        return _TYPES[kind], len(v), struct.pack(f"{bo}{len(v)}I", *(x & _U32 for x in v))
    if kind == "rational":
        return _TYPES[kind], len(v), b"".join(struct.pack(f"{bo}II", n, d) for n, d in v)
    raise ExifError(f"unknown value kind {kind!r}")


def _ifd(entries: dict, base: int, bo: str) -> bytes:
    """One IFD at offset `base` (relative to the TIFF header), its out-of-line values after it."""
    n = len(entries)
    size = 2 + 12 * n + 4
    head = bytearray(struct.pack(f"{bo}H", n))
    data = bytearray()
    for tag in sorted(entries):
        typ, count, payload = _pack(entries[tag], bo)
        if len(payload) <= 4:
            head += struct.pack(f"{bo}HHI", tag, typ, count) + payload.ljust(4, b"\x00")
        else:
            head += struct.pack(f"{bo}HHII", tag, typ, count, base + size + len(data))
            data += payload
            if len(data) % 2:
                data += b"\x00"
    head += struct.pack(f"{bo}I", 0)
    return bytes(head + data)


def layout(ifd0: dict, exif: dict, gps: Optional[dict], base: int, bo: str) -> bytes:
    """IFD0 at `base` (even), then the Exif IFD, then the GPS IFD, with IFD0's pointers filled in.
    An IFD's size does not depend on the pointer VALUES, so it is laid out once with placeholders."""
    if base % 2:
        raise ExifError("IFDs must start on a word boundary")
    ifd0 = dict(ifd0)
    ifd0[_EXIF_POINTER] = _long(0)
    if gps is not None:
        ifd0[_GPS_POINTER] = _long(0)
    exif_off = base + len(_ifd(ifd0, base, bo))
    exif_bytes = _ifd(exif, exif_off, bo)
    ifd0[_EXIF_POINTER] = _long(exif_off)
    tail = exif_bytes
    if gps is not None:
        gps_off = exif_off + len(exif_bytes)
        ifd0[_GPS_POINTER] = _long(gps_off)
        tail += _ifd(gps, gps_off, bo)
    return _ifd(ifd0, base, bo) + tail


# ---- the tag sets ---------------------------------------------------------------------------
def _split_ns(utc_ns: int):
    secs, frac_ns = divmod(int(utc_ns), 1_000_000_000)
    return time.gmtime(secs), frac_ns


def exif_ifds(capture_utc_ns: int, width: int, height: int, gps: Optional[GpsFix] = None,
              gps_utc_ns: Optional[int] = None, yaw_deg: Optional[float] = None,
              camera: Optional[CameraInfo] = None, scale: float = 1.0, alt_mode: str = "msl",
              software: str = SOFTWARE, container: str = "jpeg"):
    """(ifd0, exif, gps-or-None) entry dicts for one image of `width` x `height`; `scale` is that
    size over the sensor's native size (a downscaled JPEG), for the focal-plane resolution.
    `container` (jpeg | tiff) selects the tags the EXIF spec requires for that file type."""
    if container not in ("jpeg", "tiff"):
        raise ExifError(f"container: expected jpeg or tiff, got {container!r}")
    if alt_mode not in ALT_MODES:
        raise ExifError(f"exif_altitude: expected one of {ALT_MODES}, got {alt_mode!r}")
    camera = camera or CameraInfo()
    tm, frac_ns = _split_ns(capture_utc_ns)
    stamp = time.strftime("%Y:%m:%d %H:%M:%S", tm)
    ifd0 = {0x0131: _ascii(software), 0x0132: _ascii(stamp),
            0x011A: _rationals((72, 1)), 0x011B: _rationals((72, 1)), 0x0128: _short(2)}   # 72 dpi
    if container == "jpeg":
        ifd0[0x0213] = _short(1)                        # YCbCrPositioning: centred
    if camera.make:
        ifd0[0x010F] = _ascii(camera.make)
    if camera.model:
        ifd0[0x0110] = _ascii(camera.model)
    exif = {
        0x9000: _undefined(b"0232"),
        0x9003: _ascii(stamp),
        0x9011: _ascii("+00:00"),                       # OffsetTimeOriginal: the time is UTC
        0x9291: _ascii(f"{frac_ns // 1000:06d}"),       # SubSecTimeOriginal (microseconds)
        0xA000: _undefined(b"0100"),                    # FlashpixVersion
        # ColorSpace: sRGB for the 8-bit JPEG; "uncalibrated" for raw (e.g. radiometric) TIFF samples
        0xA001: _short(1 if container == "jpeg" else 0xFFFF),
    }
    if container == "jpeg":
        exif[0x9101] = _undefined(b"\x01\x02\x03\x00")   # ComponentsConfiguration: Y Cb Cr
        exif[0xA002] = _long(int(width))                # PixelX/YDimension: JPEG only (TIFF has its own)
        exif[0xA003] = _long(int(height))
    if camera.focal_length_mm:
        exif[0x920A] = _rationals(_rat(camera.focal_length_mm, 1000))
    if camera.pixel_pitch_um:
        px_per_cm = 1e4 / camera.pixel_pitch_um * scale
        exif[0xA20E] = _rationals(_rat(px_per_cm, 1000))
        exif[0xA20F] = _rationals(_rat(px_per_cm, 1000))
        exif[0xA210] = _short(3)                        # FocalPlaneResolutionUnit: centimetres
        if camera.focal_length_mm:
            diag_mm = math.hypot(width / scale, height / scale) * camera.pixel_pitch_um / 1000.0
            exif[0xA405] = _short(max(1, min(65535, int(round(camera.focal_length_mm * _DIAG_35MM / diag_mm)))))
    gps_ifd = None
    if gps is not None:
        gtm, gfrac = _split_ns(gps_utc_ns if gps_utc_ns is not None else capture_utc_ns)
        gps_ifd = {
            0x0000: _bytes((2, 3, 0, 0)),
            0x0001: _ascii("N" if gps.lat >= 0 else "S"),
            0x0002: _dms(gps.lat),
            0x0003: _ascii("E" if gps.lon >= 0 else "W"),
            0x0004: _dms(gps.lon),
            0x0007: _rationals((gtm.tm_hour, 1), (gtm.tm_min, 1), _rat(gtm.tm_sec + gfrac / 1e9, 1000)),
            0x0012: _ascii("WGS-84"),
            0x001B: _undefined(b"ASCII\x00\x00\x00GPS"),     # GPSProcessingMethod (an EXIF-listed value)
            0x001D: _ascii(time.strftime("%Y:%m:%d", gtm)),
        }
        alt = gps.msl_alt_m() if alt_mode == "msl" else gps.ellipsoid_alt_m()
        if alt is not None:
            gps_ifd[0x0005] = _bytes((0 if alt >= 0 else 1,))
            gps_ifd[0x0006] = _rationals(_rat(alt, 1000))
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
    return ifd0, exif, gps_ifd


def build_exif(capture_utc_ns: int, width: int, height: int, gps: Optional[GpsFix] = None,
               gps_utc_ns: Optional[int] = None, yaw_deg: Optional[float] = None,
               software: str = SOFTWARE, camera: Optional[CameraInfo] = None, scale: float = 1.0,
               alt_mode: str = "msl") -> bytes:
    """The complete EXIF APP1 segment (marker included) for one JPEG."""
    ifd0, exif, gps_ifd = exif_ifds(capture_utc_ns, width, height, gps, gps_utc_ns, yaw_deg,
                                    camera, scale, alt_mode, software)
    tiff = b"MM" + struct.pack(">HI", 42, 8) + layout(ifd0, exif, gps_ifd, 8, ">")
    body = _EXIF_HEADER + tiff
    if len(body) + 2 > 0xFFFF:
        raise ExifError(f"EXIF too large for one APP1 segment ({len(body)} bytes)")
    return b"\xFF\xE1" + struct.pack(">H", len(body) + 2) + body


# ---- XMP (Pix4D's Camera namespace) -----------------------------------------------------------
def _num(x: float) -> str:
    s = f"{float(x):.6f}".rstrip("0").rstrip(".")
    return "0" if s in ("", "-0") else s


def build_xmp(yaw: Optional[float] = None, pitch: Optional[float] = None, roll: Optional[float] = None,
              xy_acc: Optional[float] = None, z_acc: Optional[float] = None,
              yaw_acc: Optional[float] = None, pitch_acc: Optional[float] = None,
              roll_acc: Optional[float] = None, band_name: Optional[str] = None,
              central_wavelength_nm: Optional[float] = None,
              wavelength_fwhm_nm: Optional[float] = None) -> Optional[bytes]:
    """An XMP packet with the Pix4D Camera tags that have a value, or None when none has. Angles in
    degrees in Pix4D's convention (pitch 0 = nadir, yaw 0 = image top towards north), accuracies in
    metres / degrees; the band tags are ordered lists (rdf:Seq), one entry per band."""
    attrs = [(name, v) for name, v in (
        ("Yaw", None if yaw is None else yaw % 360.0), ("Pitch", pitch), ("Roll", roll),
        ("GPSXYAccuracy", xy_acc), ("GPSZAccuracy", z_acc), ("IMUYawAccuracy", yaw_acc),
        ("IMUPitchAccuracy", pitch_acc), ("IMURollAccuracy", roll_acc)) if v is not None]
    seqs = [(name, v) for name, v in (
        ("BandName", band_name),
        ("CentralWavelength", None if central_wavelength_nm is None else _num(central_wavelength_nm)),
        ("WavelengthFWHM", None if wavelength_fwhm_nm is None else _num(wavelength_fwhm_nm))) if v is not None]
    if not attrs and not seqs:
        return None
    attr_xml = "".join(f"\n    Camera:{name}={quoteattr(_num(v))}" for name, v in attrs)
    seq_xml = "".join(f"\n   <Camera:{name}><rdf:Seq><rdf:li>{escape(str(v))}</rdf:li></rdf:Seq></Camera:{name}>"
                      for name, v in seqs)
    xml = ('<?xpacket begin="﻿" id="W5M0MpCehiHzreSzNTczkc9d"?>\n'
           '<x:xmpmeta xmlns:x="adobe:ns:meta/">\n'
           ' <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">\n'
           f'  <rdf:Description rdf:about=""\n    xmlns:Camera="{PIX4D_NS}"{attr_xml}>{seq_xml}\n'
           '  </rdf:Description>\n'
           ' </rdf:RDF>\n'
           '</x:xmpmeta>\n'
           '<?xpacket end="w"?>')
    return xml.encode("utf-8")


def xmp_segment(packet: bytes) -> bytes:
    body = _XMP_HEADER + packet
    if len(body) + 2 > 0xFFFF:
        raise ExifError(f"XMP too large for one APP1 segment ({len(body)} bytes)")
    return b"\xFF\xE1" + struct.pack(">H", len(body) + 2) + body


# ---- JPEG splice ----------------------------------------------------------------------------
def insert_segments(jpeg: bytes, segments) -> bytes:
    """`jpeg` with `segments` (APP1s) placed after the leading JFIF APP0 (or right after SOI),
    replacing any Exif or XMP APP1 already present. The entropy-coded image is copied untouched."""
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
        elif not (marker == 0xE1 and (seg[4:10] == _EXIF_HEADER or seg[4:4 + len(_XMP_HEADER)] == _XMP_HEADER)):
            others.append(seg)
        pos += 2 + seglen
    return b"\xFF\xD8" + b"".join(jfif) + b"".join(segments) + b"".join(others) + jpeg[pos:]


def insert_exif(jpeg: bytes, app1: bytes, xmp: Optional[bytes] = None) -> bytes:
    """`jpeg` with the EXIF `app1` (and an XMP packet, when given) spliced in; see insert_segments."""
    return insert_segments(jpeg, [app1] + ([xmp_segment(xmp)] if xmp else []))
