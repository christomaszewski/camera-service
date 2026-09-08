"""The ros2-source writer's pure helpers (plugins/ros2-source/cam_ros2_source/framing.py) -- no ROS,
no GStreamer: the encoding table, the pinned-format decisions, row unpadding, and THE HEADER, which
the plugin packs on its own (it runs without cam_driver) and the core reads with cam_driver.transport.
Both pack the same frame here and the bytes must match.

Run: python3 core-driver/tests/test_ros2_source.py   (or pytest core-driver/tests)
"""
import importlib.util
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from cam_driver import transport  # noqa: E402
from cam_driver.formats import bytes_per_frame, parse_pixel_format  # noqa: E402

_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "plugins", "ros2-source", "cam_ros2_source", "framing.py")
_spec = importlib.util.spec_from_file_location("ros2_source_framing", _PATH)
F = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(F)


def test_header_matches_the_core_transport_byte_for_byte():
    for fmt, w, h, src in (("RGB", 640, 480, "camera"), ("GRAY8", 320, 240, "system"), ("GRAY16_LE", 8, 8, "camera"),
                           ("NV12", 1920, 1080, "ptp_chunk"), ("BGRA", 2, 2, "sof")):
        ts, fid = 1_788_872_267_123_456_789, 42
        ours = F.pack_header(ts, fid, w, h, fmt, src)
        core = transport.FrameHeader(timestamp_ns=ts, frame_id=fid, width=w, height=h, pixfmt=fmt, ts_source=src).pack()
        assert ours == core, (fmt, ours.hex(), core.hex())
        assert len(ours) == transport.HEADER_SIZE == F.HEADER_SIZE == 36
        back = transport.unpack_header(ours + b"\0" * 4)
        assert (back.timestamp_ns, back.frame_id, back.width, back.height, back.pixfmt, back.ts_source) == (ts, fid, w, h, fmt, src)
    assert F.PIXFMT_CODE == {v: k for k, v in transport._CODE_TO_GST.items()}
    assert F.TS_SOURCE_CODE == transport.TS_SOURCE_CODE
    assert F.HEADER_CAPS == transport.CAPS


def test_frame_bytes_agrees_with_the_core_for_every_transport_format():
    for fmt in F.PIXFMT_CODE:
        assert F.frame_bytes(fmt, 64, 48) == bytes_per_frame(fmt, 64, 48), fmt


def test_encodings_map_to_caps_and_bytes():
    rgb = F.Encoding("rgb8")
    assert (rgb.media, rgb.format, rgb.bytes_per_pixel, rgb.bayer, rgb.raw_format) == ("video/x-raw", "RGB", 3, False, "RGB")
    assert rgb.caps(640, 480, 15) == "video/x-raw,format=RGB,width=640,height=480,framerate=15/1"
    assert F.Encoding("mono16", is_bigendian=True).format == "GRAY16_BE"
    assert F.Encoding("mono16").format == "GRAY16_LE"
    bay = F.Encoding("bayer_rggb8")
    assert (bay.media, bay.format, bay.bayer, bay.raw_format) == ("video/x-bayer", "rggb", True, "GRAY8")
    assert F.Encoding("nv12").bytes_per_pixel is None
    assert F.Encoding("8UC1").format == "GRAY8"
    try:
        F.Encoding("32FC1")
    except ValueError as e:
        assert "32FC1" in str(e) and "not supported" in str(e)
    else:
        raise AssertionError("32FC1 accepted")


def test_pinned_format_decisions_mirror_the_core_parser():
    for pf in ("RGB", "GRAY8", "NV12", "Mono8", "Mono16", "BayerRG8", "BayerGB8"):
        p = F.Pinned(pf, 640, 480, 10)
        gst, _bits, bayer, _packed, _color = parse_pixel_format(pf)
        assert (p.format, p.bayer) == (gst, bayer), pf
        assert p.frame_bytes == bytes_per_frame(gst, 640, 480)
    p = F.Pinned("BayerRG8", 640, 480, 12.5)
    assert p.unixfd_caps() == "video/x-bayer,format=rggb,width=640,height=480,framerate=12500/1000"
    assert p.raw_caps() == "video/x-raw,format=GRAY8,width=640,height=480,framerate=12500/1000"
    # as-is when the bytes already are the pinned frame; convert otherwise
    rgb = F.Pinned("RGB", 640, 480, 10)
    assert rgb.matches(F.Encoding("rgb8"), 640, 480)
    assert not rgb.matches(F.Encoding("bgr8"), 640, 480)
    assert not rgb.matches(F.Encoding("rgb8"), 320, 240)
    assert p.matches(F.Encoding("bayer_rggb8"), 640, 480)
    assert not p.matches(F.Encoding("bayer_bggr8"), 640, 480)
    assert not p.matches(F.Encoding("mono8"), 640, 480)
    try:
        F.Pinned("YUV9", 1, 1, 1)
    except ValueError as e:
        assert "YUV9" in str(e)
    else:
        raise AssertionError("bad pinned format accepted")


def test_unpad_rows_strips_step_padding_only_when_present():
    rows = [bytes([r] * 6) + b"PP" for r in range(3)]          # 2 px * 3 B + 2 B padding per row
    padded = b"".join(rows)
    assert F.unpad_rows(padded, step=8, row_bytes=6, height=3) == b"".join(bytes([r] * 6) for r in range(3))
    tight = b"".join(bytes([r] * 6) for r in range(3))
    assert F.unpad_rows(tight, step=6, row_bytes=6, height=3) == tight
    assert F.unpad_rows(memoryview(tight), step=0, row_bytes=6, height=3) == tight
    assert F.stamp_ns(3, 5) == 3_000_000_005


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
