#!/usr/bin/env python3
"""Unit tests for format_adapt (pure; no gi/GStreamer needed). Run: python3 test_format_adapt.py"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from format_adapt import adapt_for_input, debayer_enabled  # noqa: E402


def test_bayer_debayers_to_color():
    # the normal CFA case: whatever the pattern, bayer2rgb reads it off the caps
    assert adapt_for_input("video/x-bayer", 2448, 2048, "25/1", True) == ("bayer2rgb", None)
    assert adapt_for_input("video/x-bayer", 640, 480, None, True) == ("bayer2rgb", None)


def test_bayer_without_debayer_relabels_gray8():
    # raw-mosaic preview: byte-identical relabel, geometry/rate carried over
    el, caps = adapt_for_input("video/x-bayer", 2448, 2048, "25/1", False)
    assert el == "capssetter"
    assert caps == "video/x-raw,format=GRAY8,width=2448,height=2048,framerate=25/1"


def test_bayer_relabel_with_partial_geometry():
    # unreadable fields are simply omitted -- capssetter still relabels the media type
    assert adapt_for_input("video/x-bayer", 0, 0, None, False) == \
        ("capssetter", "video/x-raw,format=GRAY8")
    assert adapt_for_input("video/x-bayer", 1920, 1200, None, False) == \
        ("capssetter", "video/x-raw,format=GRAY8,width=1920,height=1200")


def test_raw_passes_through():
    # mono + color raw formats never need the seam, whatever the debayer knob says --
    # this is the fix for the not-negotiated crash loop when env predicted Bayer wrongly
    for fmt_debayer in (True, False):
        assert adapt_for_input("video/x-raw", 640, 512, "25/1", fmt_debayer) is None
        assert adapt_for_input("video/x-raw", 2448, 2048, "25/1", fmt_debayer) is None


def test_debayer_enabled_forms():
    # YAML false arrives Python-cased ("False") through sensor_env's passthrough
    for off in ("0", "false", "False", "FALSE", "no", "off", "OFF"):
        assert not debayer_enabled(off)
    for on in (None, "", "auto", "true", "1", "yes", "anything-else"):
        assert debayer_enabled(on)


def _main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print("  ok ", t.__name__)
    print(len(tests), "passed")


if __name__ == "__main__":
    _main()
