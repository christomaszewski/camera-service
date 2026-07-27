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


def _bridge_src(*parts):
    """Read a C++ bridge source, or None when plugins/ isn't reachable -- a vendored launch surface
    doesn't ship it, and the dev container mounts core-driver/ alone. Returning None (rather than
    calling pytest.skip) keeps these runnable by the standalone `python3 tests/test_transport.py`
    path too, which has no pytest."""
    import os
    p = os.path.join(os.path.dirname(__file__), "..", "..", "plugins", *parts)
    if not os.path.exists(p):
        return None
    with open(p) as f:
        return f.read()


def test_header_bridges_cover_every_pixfmt_code():
    # The 36-byte header's pixfmt table is hand-mirrored into TWO C++ switch statements. Nothing
    # links them, so this asserts every code has a `case N:` in both -- an unhandled code makes the
    # bridge drop 100% of frames behind a throttled warning, with no ERROR to trip the restart policy.
    from cam_driver.transport import _CODE_TO_GST
    for parts in (("ros2-bridge", "src", "cam_header_bridge.cpp"),
                  ("ros1-bridge", "src", "cam_ros1_bridge.cpp")):
        src = _bridge_src(*parts)
        if src is None:
            continue
        missing = [c for c in sorted(_CODE_TO_GST) if f"case {c}:" not in src]
        assert not missing, f"{parts[0]} pixfmt_info is missing wire code(s) {missing}"


def test_unixfd_bridge_covers_every_raw_format():
    # The JP7 transport is self-describing, so the unixfd bridge matches on the CAPS FORMAT STRING
    # rather than a wire code -- same duplication, different spelling. It shipped covering only 8 of
    # the 15 formats formats._GST_RAW admits, so UYVY/YV12/NV24/RGBA/BGRA/RGBx/BGRx published nothing
    # on JP7 while working on JP6. Bayer is not here: it rides as video/x-bayer, checked below.
    from cam_driver.formats import _GST_RAW
    src = _bridge_src("ros2-bridge", "src", "cam_unixfd_bridge.cpp")
    if src is None:
        return
    missing = [f for f in sorted(_GST_RAW) if f'"{f}"' not in src]
    assert not missing, f"cam_unixfd_bridge caps_to_meta is missing format(s) {missing}"


def test_unixfd_bridge_covers_every_bayer_pattern():
    # 8-bit Bayer rides the unixfd transport as video/x-bayer,<pattern> (pipeline.py), so the four
    # patterns formats._BAYER_MAP can emit must all be recognized too.
    from cam_driver.formats import _BAYER_MAP
    src = _bridge_src("ros2-bridge", "src", "cam_unixfd_bridge.cpp")
    if src is None:
        return
    missing = [p for p in sorted(set(_BAYER_MAP.values())) if f'"{p}"' not in src]
    assert not missing, f"cam_unixfd_bridge caps_to_meta is missing Bayer pattern(s) {missing}"


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
