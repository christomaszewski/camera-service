"""Tests for the frame-transport wire format (core <-> out-of-process plugin contract).

Pure logic, runs without GStreamer/Aravis. Run: python3 core-driver/tests/test_transport.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from cam_driver.transport import (  # noqa: E402
    CAPS, HEADER_SIZE, TS_SOURCE_CODE, FrameHeader, TransportError, unpack_header,
)


def test_roundtrip_ptp():
    h = FrameHeader(timestamp_ns=1_700_000_000_123_456_789, frame_id=42,
                    width=512, height=512, pixfmt="GRAY8", ts_source="ptp_chunk")
    raw = h.pack()
    assert len(raw) == HEADER_SIZE == 36
    out = unpack_header(raw)
    assert out.timestamp_ns == h.timestamp_ns      # full 64-bit PTP-epoch ns survives
    assert out.frame_id == 42
    assert (out.width, out.height) == (512, 512)
    assert out.pixfmt == "GRAY8"
    assert out.ts_source == "ptp_chunk"


def test_header_then_pixels():
    h = FrameHeader(timestamp_ns=5, frame_id=1, width=2, height=2, pixfmt="GRAY8")
    pixels = bytes([10, 20, 30, 40])
    blob = h.pack() + pixels
    parsed = unpack_header(blob)                    # parses header, ignores trailing pixels
    assert blob[HEADER_SIZE:] == pixels             # consumer slices pixels off after the header
    assert parsed.width * parsed.height == len(pixels)


def test_16bit_and_system_source():
    h = FrameHeader(timestamp_ns=9, frame_id=2, width=4, height=4,
                    pixfmt="GRAY16_LE", ts_source="system")
    out = unpack_header(h.pack())
    assert out.pixfmt == "GRAY16_LE"
    assert out.ts_source == "system"


def test_color_pixfmt_roundtrip():
    # color formats now have header codes so the core can carry a color source without crashing
    for fmt in ("I420", "NV12", "YUY2", "RGB"):
        out = unpack_header(FrameHeader(timestamp_ns=7, frame_id=3, width=8, height=8, pixfmt=fmt).pack())
        assert out.pixfmt == fmt


def test_extended_color_pixfmt_codes_stable():
    # additive codes 9+ at their exact wire values; 1-8 must NOT shift (C++ bridges hard-code them)
    for fmt, code in (("NV24", 9), ("YV12", 10), ("UYVY", 11),
                      ("RGBA", 12), ("BGRA", 13), ("RGBx", 14), ("BGRx", 15)):
        raw = FrameHeader(timestamp_ns=11, frame_id=5, width=8, height=8, pixfmt=fmt).pack()
        assert raw[28:32] == bytes([code, 0, 0, 0])   # pixfmt u32 LE (offset 28 in the v1 header)
        assert unpack_header(raw).pixfmt == fmt


def test_every_source_raw_format_is_carriable():
    # The invariant that broke once: every raw format parse_pixel_format can hand a source
    # (formats._GST_RAW) must pack on the shm transport, or _on_frame raises per-frame.
    from cam_driver.formats import _GST_RAW
    for fmt in sorted(_GST_RAW):
        out = unpack_header(FrameHeader(timestamp_ns=1, frame_id=1, width=2, height=2,
                                        pixfmt=fmt).pack())
        assert out.pixfmt == fmt


def test_ts_source_codes_stable():
    # additive provenance rungs; 0-2 must NOT shift (the C++ bridges hard-code them)
    assert TS_SOURCE_CODE == {"ptp_chunk": 0, "camera": 1, "system": 2, "sof": 3, "rtp_ntp": 4}


def test_new_ts_sources_roundtrip():
    # usb SOF + rtsp RTCP->NTP provenance survive pack/unpack at their wire codes
    for src, code in (("sof", 3), ("rtp_ntp", 4)):
        raw = FrameHeader(timestamp_ns=123, frame_id=4, width=8, height=8,
                          pixfmt="I420", ts_source=src).pack()
        assert raw[32] == code          # ts_source byte (offset 32 in the 36-byte v1 header)
        assert unpack_header(raw).ts_source == src


def test_bad_magic():
    try:
        unpack_header(b"XXXX" + bytes(HEADER_SIZE - 4))
        assert False, "expected TransportError"
    except TransportError:
        pass


def test_truncated():
    try:
        unpack_header(b"CAMF")
        assert False, "expected TransportError"
    except TransportError:
        pass


def test_unsupported_pixfmt_rejected():
    # NV21: a real GStreamer format we deliberately don't carry; "BOGUS": not a format at all
    for fmt in ("NV21", "BOGUS"):
        try:
            FrameHeader(timestamp_ns=0, frame_id=0, width=1, height=1, pixfmt=fmt).pack()
            assert False, f"expected TransportError for {fmt}"
        except TransportError:
            pass


def test_caps_constant():
    assert CAPS == "application/x-cam-frame"


def _main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"{len(tests)} passed")


if __name__ == "__main__":
    _main()
