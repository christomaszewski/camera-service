"""frame-capture's single-band TIFF (cam_driver.capture.tiff): parsed back with a minimal reader, the
image tags, the raw samples, and the EXIF / GPS IFDs and XMP packet it shares with the JPEG.

Run: python3 -m pytest core-driver/tests/test_capture_tiff.py
"""
import os
import struct
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from cam_driver.capture.exif import CameraInfo, build_xmp, exif_ifds  # noqa: E402
from cam_driver.capture.request import GpsFix  # noqa: E402
from cam_driver.capture.tiff import TiffError, build_tiff, tight_samples  # noqa: E402

T = 1_762_610_400_123_456_789
_SIZES = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 7: 1}


def _ifd(f: bytes, off: int) -> dict:
    (n,) = struct.unpack("<H", f[off:off + 2])
    out = {}
    for i in range(n):
        tag, typ, count, raw = struct.unpack("<HHI4s", f[off + 2 + 12 * i:off + 14 + 12 * i])
        size = _SIZES[typ] * count
        data = raw[:size] if size <= 4 else f[struct.unpack("<I", raw)[0]:][:size]
        if typ == 3:
            out[tag] = struct.unpack(f"<{count}H", data)[0]
        elif typ == 4:
            out[tag] = struct.unpack(f"<{count}I", data)[0]
        elif typ == 2:
            out[tag] = data.rstrip(b"\x00").decode()
        elif typ == 5:
            out[tag] = [a / b for a, b in struct.iter_unpack("<II", data)]
        else:
            out[tag] = bytes(data)
    return out


def _tiff(fmt="GRAY16_LE", w=6, h=4, data=None, **kw):
    if data is None:
        data = (np.arange(w * h) + 29315).astype("<u2" if fmt != "GRAY8" else "u1").tobytes()
    gps = GpsFix(lat=-33.5, lon=151.25, alt_m=50.0, alt_ref="msl")
    ifd0, exif, g = exif_ifds(T, w, h, gps, T, 45.0, CameraInfo("FLIR", "Boson 640", 9.1, 12.0),
                              container="tiff")
    return build_tiff(fmt, w, h, data, ifd0, exif, g, kw.get("xmp", build_xmp(yaw=45.0, band_name="LWIR")))


def test_header_image_tags_and_raw_samples():
    f = _tiff()
    assert f[:4] == b"II*\x00"
    ifd0 = _ifd(f, struct.unpack("<I", f[4:8])[0])
    assert (ifd0[256], ifd0[257], ifd0[258], ifd0[259], ifd0[262]) == (6, 4, 16, 1, 1)
    assert (ifd0[277], ifd0[278], ifd0[284], ifd0[339]) == (1, 4, 1, 1)
    off, n = ifd0[273], ifd0[279]
    assert n == 6 * 4 * 2
    assert np.array_equal(np.frombuffer(f[off:off + n], "<u2"), np.arange(24) + 29315)
    assert (ifd0[271], ifd0[272], ifd0[305]) == ("FLIR", "Boson 640", "camera-service frame-capture")
    assert b"http://pix4d.com/camera/1.0/" in ifd0[700] and b"<rdf:li>LWIR</rdf:li>" in ifd0[700]


def test_exif_and_gps_ifds_are_reachable():
    f = _tiff()
    ifd0 = _ifd(f, struct.unpack("<I", f[4:8])[0])
    exif, gps = _ifd(f, ifd0[0x8769]), _ifd(f, ifd0[0x8825])
    assert exif[0x9003] == "2025:11:08 14:00:00" and exif[0x920A] == [pytest.approx(9.1)]
    assert exif[0xA001] == 0xFFFF and 0xA002 not in exif and 0x9101 not in exif    # TIFF, uncalibrated
    assert ifd0[0x011A] == [72.0] and ifd0[0x0128] == 2 and 0x0213 not in ifd0
    assert gps[0x0001] == "S" and gps[0x0003] == "E" and gps[0x0006] == [50.0]
    assert gps[0x0011] == [45.0]


def test_8_bit_planes_padded_rows_and_no_xmp():
    rows = [bytes(range(r * 5, r * 5 + 5)) + b"\x00\x00\x00" for r in range(3)]   # 5 wide, stride 8
    f = _tiff("GRAY8", 5, 3, b"".join(rows), xmp=None)
    ifd0 = _ifd(f, struct.unpack("<I", f[4:8])[0])
    assert ifd0[258] == 8 and f[ifd0[273]:ifd0[273] + ifd0[279]] == bytes(range(15))
    assert 700 not in ifd0
    assert struct.unpack("<I", f[4:8])[0] % 2 == 0               # odd strip padded to a word boundary


def test_big_endian_samples_keep_their_values():
    be = (np.arange(6) + 40000).astype(">u2").tobytes()
    assert tight_samples(be, "GRAY16_BE", 3, 2) == (np.arange(6) + 40000).astype("<u2").tobytes()


def test_refusals():
    with pytest.raises(TiffError, match="mono plane"):
        build_tiff("RGB", 2, 2, bytes(12), {}, {}, None)
    with pytest.raises(TiffError, match="truncated"):
        tight_samples(b"\x00" * 10, "GRAY16_LE", 6, 4)
