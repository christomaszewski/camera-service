#!/usr/bin/env python3
"""Unit tests for format_adapt (pure; no gi/GStreamer needed). Run: python3 test_format_adapt.py"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from format_adapt import adapt_for_input, bin_plan, debayer_enabled, debayer_full  # noqa: E402


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


def test_full_is_a_debayer_mode_not_an_off_form():
    for full in ("full", "Full", " FULL "):
        assert debayer_full(full) and debayer_enabled(full)
    for other in (None, "", "auto", "true", "false", "0", "fully"):
        assert not debayer_full(other)


def test_a_preview_reduced_to_half_size_or_less_is_binned_and_streams_at_the_planned_size():
    # the 5 MP camera: half size is 1224x1024
    assert bin_plan(2448, 2048, (1280, 1280), "rggb") == (1224, 1024)     # near half: the half frame as it is
    assert bin_plan(2448, 2048, (1224, 1224), "rggb") == (1224, 1024)     # exactly half
    assert bin_plan(2448, 2048, (640, 640), "rggb") == (640, 534)         # below half: binned, then scaled
    assert bin_plan(2448, 2048, (1280, 720), "rggb") == (860, 720)
    assert bin_plan(512, 512, (256, 256), "grbg") == (256, 256)
    for pattern in ("rggb", "bggr", "grbg", "gbrg", "RGGB"):
        assert bin_plan(1920, 1200, (960, 960), pattern) == (960, 600)


def test_a_bound_well_above_half_size_keeps_the_full_debayer():
    assert bin_plan(2448, 2048, (1920, 1920), "rggb") is None             # asked for 1920: 1224 is not it
    assert bin_plan(2448, 2048, (1400, 1400), "rggb") is None
    assert bin_plan(2448, 2048, (1360, 1360), "rggb") == (1224, 1024)     # 1224 is within 10% of 1360
    assert bin_plan(2448, 2048, (1361, 1361), "rggb") is None
    # each axis is held to it: a wide box that the height binds
    assert bin_plan(2448, 2048, (4000, 1200), "rggb") is None


def test_nothing_is_binned_without_a_bound_a_reduction_or_a_known_pattern():
    assert bin_plan(2448, 2048, None, "rggb") is None                     # CAM_WEBRTC_MAX_SIZE off
    assert bin_plan(640, 480, (1280, 1280), "rggb") is None               # already fits: nothing is scaled
    assert bin_plan(2448, 2048, (1280, 1280), None) is None
    assert bin_plan(2448, 2048, (1280, 1280), "rgb") is None
    assert bin_plan(2, 2, (1, 1), "rggb") is None and bin_plan(3, 9, (2, 2), "rggb") is None


def test_a_half_size_frame_with_an_odd_edge_is_only_used_when_the_scaler_follows():
    # 2052x2048 halves to 1026x1024 (even: streamed as it is); 2050x2048 to 1025x1024 (odd)
    assert bin_plan(2052, 2048, (1100, 1100), "rggb") == (1026, 1024)
    assert bin_plan(2050, 2048, (1100, 1100), "rggb") is None             # would stream 1025 wide
    assert bin_plan(2050, 2048, (640, 640), "rggb") == (640, 638)         # the scaler evens it out
    assert bin_plan(2448, 2046, (1280, 1280), "rggb") is None             # 1023 high


def _main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print("  ok ", t.__name__)
    print(len(tests), "passed")


if __name__ == "__main__":
    _main()
