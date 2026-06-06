"""Tests for the frame-transport wire format (core <-> out-of-process plugin contract).

Pure logic, runs without GStreamer/Aravis. Run: python3 core-driver/tests/test_transport.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from cam_driver.transport import (  # noqa: E402
    CAPS, HEADER_SIZE, FrameHeader, TransportError, unpack_header,
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
    try:
        FrameHeader(timestamp_ns=0, frame_id=0, width=1, height=1, pixfmt="NV12").pack()
        assert False, "expected TransportError"
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
