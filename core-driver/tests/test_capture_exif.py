"""frame-capture EXIF (cam_driver.capture.exif): the APP1 segment parses back to the values written,
the splice keeps the image untouched and the segment order readers expect.

A minimal TIFF reader lives here so the test needs nothing beyond the stdlib (the output was also
checked against Pillow and exiftool when it was written). Run:
python3 -m pytest core-driver/tests/test_capture_exif.py
"""
import os
import struct
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from cam_driver.capture.exif import ExifError, build_exif, insert_exif  # noqa: E402
from cam_driver.capture.request import GpsFix  # noqa: E402

T = 1_762_610_400_123_456_789           # 2025-11-08 14:00:00.123456789 UTC
_SIZES = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 7: 1}


def _read_ifd(tiff: bytes, off: int) -> dict:
    (n,) = struct.unpack(">H", tiff[off:off + 2])
    out = {}
    for i in range(n):
        tag, typ, count, raw = struct.unpack(">HHI4s", tiff[off + 2 + 12 * i: off + 14 + 12 * i])
        size = _SIZES[typ] * count
        data = raw[:size] if size <= 4 else tiff[struct.unpack(">I", raw)[0]:][:size]
        if typ == 2:
            val = data.rstrip(b"\x00").decode()
        elif typ == 5:
            val = [a / b for a, b in struct.iter_unpack(">II", data)]
        elif typ == 4:
            val = struct.unpack(">I", data)[0]
        elif typ == 3:
            val = struct.unpack(">H", data)[0]
        else:
            val = bytes(data)
        out[tag] = val
    return out


def _parse(app1: bytes):
    assert app1[:2] == b"\xFF\xE1" and struct.unpack(">H", app1[2:4])[0] == len(app1) - 2
    assert app1[4:10] == b"Exif\x00\x00"
    tiff = app1[10:]
    assert tiff[:4] == b"MM\x00\x2a"
    ifd0 = _read_ifd(tiff, struct.unpack(">I", tiff[4:8])[0])
    exif = _read_ifd(tiff, ifd0[0x8769])
    gps = _read_ifd(tiff, ifd0[0x8825]) if 0x8825 in ifd0 else None
    return ifd0, exif, gps


def _dms(vals):
    d, m, s = vals
    return d + m / 60 + s / 3600


def test_capture_time_and_size_without_gps():
    ifd0, exif, gps = _parse(build_exif(T, 640, 480))
    assert ifd0[0x0131] == "camera-service frame-capture" and ifd0[0x0132] == "2025:11:08 14:00:00"
    assert exif[0x9003] == "2025:11:08 14:00:00" and exif[0x9291] == "123456"
    assert exif[0x9011] == "+00:00" and exif[0x9000] == b"0232"
    assert (exif[0xA002], exif[0xA003]) == (640, 480)
    assert gps is None


def test_a_full_fix_round_trips():
    fix = GpsFix(lat=40.4433012, lon=-79.9436045, alt_m=276.3, geoid_sep_m=-33.9,
                 vel_enu=(3.0, 4.0, 0.0), h_acc_m=0.02)
    _ifd0, _exif, gps = _parse(build_exif(T, 64, 48, gps=fix, gps_utc_ns=T + 5_000_000, yaw_deg=-10.0))
    assert gps[0x0000] == b"\x02\x03\x00\x00"
    assert gps[0x0001] == "N" and _dms(gps[0x0002]) == pytest.approx(40.4433012, abs=1e-9)
    assert gps[0x0003] == "W" and _dms(gps[0x0004]) == pytest.approx(79.9436045, abs=1e-9)
    assert gps[0x0005] == b"\x00" and gps[0x0006] == [pytest.approx(310.2)]
    assert gps[0x0007] == [14.0, 0.0, pytest.approx(0.128)] and gps[0x001D] == "2025:11:08"
    assert gps[0x0012] == "WGS-84"
    assert gps[0x000C] == "K" and gps[0x000D] == [pytest.approx(18.0)]
    assert gps[0x000E] == "T" and gps[0x000F] == [pytest.approx(36.87)]
    assert gps[0x0010] == "T" and gps[0x0011] == [pytest.approx(350.0)]
    assert gps[0x001F] == [pytest.approx(0.02)]


def test_southern_eastern_below_sea_level_and_standstill():
    fix = GpsFix(lat=-33.5, lon=151.25, alt_m=-12.0, alt_ref="msl", vel_enu=(0.0, 0.0, 0.0))
    _ifd0, _exif, gps = _parse(build_exif(T, 8, 8, gps=fix))
    assert gps[0x0001] == "S" and gps[0x0003] == "E"
    assert gps[0x0005] == b"\x01" and gps[0x0006] == [pytest.approx(12.0)]
    assert 0x000F not in gps and gps[0x000D] == [0.0]          # no course at a standstill


def test_ellipsoid_height_without_a_geoid_separation_is_left_out():
    _ifd0, _exif, gps = _parse(build_exif(T, 8, 8, gps=GpsFix(lat=1.0, lon=2.0, alt_m=100.0)))
    assert 0x0005 not in gps and 0x0006 not in gps


def test_a_yaw_without_a_fix_still_writes_a_direction():
    _ifd0, _exif, gps = _parse(build_exif(T, 8, 8, yaw_deg=90.0))
    assert gps[0x0011] == [90.0] and 0x0002 not in gps


_JFIF = b"\xFF\xE0" + struct.pack(">H", 16) + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
_BODY = b"\xFF\xDB\x00\x04\x00\x00\xFF\xDA\x00\x02scan-data\xFF\xD9"


def test_the_splice_goes_after_jfif_and_keeps_the_image_bytes():
    jpeg = b"\xFF\xD8" + _JFIF + _BODY
    app1 = build_exif(T, 8, 8)
    out = insert_exif(jpeg, app1)
    assert out == b"\xFF\xD8" + _JFIF + app1 + _BODY


def test_an_existing_exif_segment_is_replaced_and_other_segments_kept():
    old = build_exif(T - 10**9, 8, 8)
    com = b"\xFF\xFE\x00\x05hi!"
    jpeg = b"\xFF\xD8" + old + com + _BODY
    app1 = build_exif(T, 8, 8)
    assert insert_exif(jpeg, app1) == b"\xFF\xD8" + app1 + com + _BODY


def test_not_a_jpeg_or_a_truncated_segment_is_refused():
    with pytest.raises(ExifError, match="SOI"):
        insert_exif(b"\x89PNG....", build_exif(T, 8, 8))
    with pytest.raises(ExifError, match="truncated"):
        insert_exif(b"\xFF\xD8\xFF\xE0\x00\x40JFIF", build_exif(T, 8, 8))
