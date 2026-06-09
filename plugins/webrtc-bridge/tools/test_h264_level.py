#!/usr/bin/env python3
"""Unit tests for h264_level (pure; no gi/GStreamer needed). Run: python3 test_h264_level.py"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from h264_level import h264_level_for, level_covers  # noqa: E402


def test_worked_examples():
    # the brief's worked cases, in GStreamer-canonical level strings ("4" not "4.0").
    assert h264_level_for(1280, 720, 30) == "3.1"
    assert h264_level_for(1920, 1080, 30) == "4"
    assert h264_level_for(2048, 1536, 30) == "5"
    assert h264_level_for(1920, 1080, 60) == "4.2"


def test_low_res_and_4k():
    assert h264_level_for(512, 512, 25) == "3"        # 1024 MBs @ 25600 mbps -> L3.0
    assert h264_level_for(640, 480, 30) == "3"        # 1200 MBs @ 36000 mbps -> L3.0
    assert h264_level_for(3840, 2160, 30) == "5.1"    # 4K30 -> 32400 MBs @ 972000 -> L5.1


def test_fps_pushes_level_via_mbps():
    # same frame size, higher fps -> a higher level through the MaxMBPS gate.
    assert h264_level_for(1280, 720, 30) == "3.1"
    assert h264_level_for(1280, 720, 60) == "3.2"     # 3600 * 60 = 216000 -> L3.2


def test_non_mod16_rounds_up():
    # a non-multiple-of-16 dimension still costs whole macroblocks (ceil), which can cross a boundary.
    assert h264_level_for(1280, 720, 30) == "3.1"     # 80x45 = 3600 MBs == L3.1 exactly
    assert h264_level_for(1280, 721, 30) == "3.2"     # 721 -> 46 MB-rows -> 3680 > 3600 -> L3.2


def test_max_level_clamp():
    # natural need is 5; a 4.2 clamp returns 4.2, and level_covers flags that it does NOT cover it.
    assert h264_level_for(2048, 1536, 30, max_level="4.2") == "4.2"
    assert level_covers("4.2", 2048, 1536, 30) is False
    assert level_covers("5", 2048, 1536, 30) is True
    # a clamp at/above the natural need is a no-op.
    assert h264_level_for(1920, 1080, 30, max_level="5.2") == "4"


def test_optional_bitrate_gate():
    assert h264_level_for(1280, 720, 30) == "3.1"                       # no bitrate -> FS/MBPS only
    assert h264_level_for(1280, 720, 30, bitrate_kbps=20000) == "3.2"   # 3.1 MaxBR 14000 < 20000 -> bump


def test_bad_input_raises():
    for bad in [(0, 720, 30), (1280, 0, 30), (1280, 720, 0), (None, 720, 30)]:
        try:
            h264_level_for(*bad)
            assert False, "expected ValueError for %r" % (bad,)
        except ValueError:
            pass
    try:
        h264_level_for(1280, 720, 30, max_level="4.0")   # not a gst-canonical level string
        assert False, "expected ValueError for max_level='4.0'"
    except ValueError:
        pass


def _main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print("  ok ", t.__name__)
    print(len(tests), "passed")


if __name__ == "__main__":
    _main()
