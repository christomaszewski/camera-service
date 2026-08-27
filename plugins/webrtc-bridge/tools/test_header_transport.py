#!/usr/bin/env python3
"""Unit tests for header_transport (pure; no gi/GStreamer needed). Run: python3 test_header_transport.py

The packed test vectors mirror the wire contract in core-driver/cam_driver/transport.py -- if that
layout ever changes, these fixtures are the tripwire on the bridge side."""
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from header_transport import (  # noqa: E402
    HEADER_SIZE, HeaderError, PtsTracker, caps_for_frame, parse_header)

_FORMAT = "<4sHHQQHHIBBH"
TS = 1_756_200_000_123_456_789          # an absolute 2026 wall-clock ns


def _hdr(magic=b"CAMF", version=1, header_len=HEADER_SIZE, ts=TS, fid=7,
         w=2448, h=2048, pixfmt=1, src=0, flags=0, pixels=b"\x00" * 16):
    return struct.pack(_FORMAT, magic, version, header_len, ts, fid, w, h, pixfmt, src, flags, 0) \
        + pixels


def test_parse_roundtrip():
    info = parse_header(_hdr())
    assert (info.timestamp_ns, info.frame_id) == (TS, 7)
    assert (info.width, info.height, info.pixfmt) == (2448, 2048, "GRAY8")
    assert info.ts_source == "ptp_chunk"
    assert info.header_len == HEADER_SIZE


def test_parse_accepts_memoryview_and_16bit():
    # the pump hands in a Gst.MapInfo view, not bytes; GRAY16 sensors ride code 2
    info = parse_header(memoryview(_hdr(pixfmt=2, src=2)))
    assert info.pixfmt == "GRAY16_LE"
    assert info.ts_source == "system"


def test_parse_rejects_corruption():
    for bad in (_hdr(magic=b"XXXX"),               # wrong magic
                _hdr(version=2),                   # future version
                _hdr()[:HEADER_SIZE - 1],          # short buffer
                _hdr(pixfmt=99),                   # unknown format code
                _hdr(w=0)):                        # nonsense geometry
        try:
            parse_header(bad)
            raise AssertionError("parse_header accepted corrupt input")
        except HeaderError:
            pass


def test_parse_bounds_header_len():
    # header_len is wire data that becomes a pixel offset -- both directions must be rejected
    for bad_len in (HEADER_SIZE - 1, HEADER_SIZE + 1024):
        try:
            parse_header(_hdr(header_len=bad_len))
            raise AssertionError("parse_header accepted bad header_len")
        except HeaderError:
            pass


def test_caps_bayer_relabel_only_when_wanted_and_8bit():
    info = parse_header(_hdr())                    # GRAY8 mosaic on the wire
    assert caps_for_frame(info, bayer="rggb", debayer=True, fps=25) == \
        "video/x-bayer,format=rggb,width=2448,height=2048,framerate=25/1"
    # debayer off -> raw mosaic preview; no bayer config -> mono
    assert caps_for_frame(info, bayer="rggb", debayer=False) == \
        "video/x-raw,format=GRAY8,width=2448,height=2048"
    assert caps_for_frame(info, bayer=None, debayer=True) == \
        "video/x-raw,format=GRAY8,width=2448,height=2048"
    # a 16-bit sensor can't be a bayer2rgb input: the pattern is ignored
    info16 = parse_header(_hdr(pixfmt=2))
    assert caps_for_frame(info16, bayer="rggb", debayer=True, fps=25) == \
        "video/x-raw,format=GRAY16_LE,width=2448,height=2048,framerate=25/1"


def test_pts_relative_and_monotonic():
    t = PtsTracker(fallback_interval_ns=40_000_000)
    assert t.pts_for(TS) == 0                      # base = first capture stamp
    assert t.pts_for(TS + 40_000_000) == 40_000_000
    assert t.pts_for(TS + 80_000_000) == 80_000_000


def test_pts_rebases_on_clock_reset():
    # camera reconnect resets its clock: PTS must keep ADVANCING by the last observed interval
    t = PtsTracker(fallback_interval_ns=40_000_000)
    t.pts_for(TS)
    t.pts_for(TS + 40_000_000)
    assert t.pts_for(TS - 5_000_000_000) == 80_000_000          # backward stamp -> last + interval
    assert t.pts_for(TS - 5_000_000_000 + 40_000_000) == 120_000_000   # and the new base holds


def _main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print("  ok ", t.__name__)
    print(len(tests), "passed")


if __name__ == "__main__":
    _main()
