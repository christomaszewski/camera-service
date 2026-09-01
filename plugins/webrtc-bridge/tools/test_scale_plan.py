#!/usr/bin/env python3
"""Unit tests for scale_plan (pure; no gi/GStreamer needed). Run: python3 test_scale_plan.py"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from scale_plan import fit_within, parse_max_size, scale_caps  # noqa: E402


def test_parse_forms():
    assert parse_max_size("1280") == (1280, 1280)          # N = both axes (longest-edge cap)
    assert parse_max_size("1280x720") == (1280, 720)       # WxH box
    assert parse_max_size(" 1280 X 720 ") == (1280, 720)   # case/whitespace-insensitive
    assert parse_max_size(1280) == (1280, 1280)            # a YAML int arrives str()'d; be lenient


def test_parse_off_forms():
    # YAML false arrives Python-cased ("False") through sensor_env's passthrough
    for off in (None, "", "off", "OFF", "0", "false", "False", "no", "none"):
        assert parse_max_size(off) is None, off


def test_parse_rejects_garbage():
    for bad in ("abc", "1280x", "x720", "1280x720x1", "1x1", "0x0", "-5", "1280.5", "1280*720"):
        try:
            parse_max_size(bad)
        except ValueError:
            continue
        raise AssertionError("accepted {!r}".format(bad))


def test_fit_landscape_5mp():
    # the motivating case: 5MP color into a 1280 longest-edge cap -> width binds, height floors to even
    assert fit_within(2448, 2048, 1280, 1280) == (1280, 1070)
    # a 16:9 box on the same frame -> height binds
    assert fit_within(2448, 2048, 1280, 720) == (860, 720)


def test_fit_exact_and_common():
    assert fit_within(3840, 2160, 1920, 1080) == (1920, 1080)   # exact 2x, both axes bind
    assert fit_within(512, 512, 256, 256) == (256, 256)         # the loopback test scenario
    assert fit_within(1000, 500, 500, 250) == (500, 250)


def test_fit_never_upscales():
    assert fit_within(512, 512, 512, 512) is None               # already fits exactly
    assert fit_within(640, 512, 1280, 1280) is None             # smaller than the box
    assert fit_within(1280, 720, 1280, 1080) is None            # fits one axis, under the other


def test_fit_portrait_and_odd():
    assert fit_within(2048, 2448, 1280, 1280) == (1070, 1280)   # portrait: height binds
    assert fit_within(1920, 1200, 1000, 1000) == (1000, 624)    # 625 floors to even
    assert fit_within(1920, 1080, 1279, 1279) == (1278, 718)    # an odd bound floors to even too
    assert fit_within(4000, 10, 100, 100) == (100, 2)           # degenerate: never below 2


def test_fit_result_is_inside_the_box_and_even():
    for w, h in ((2448, 2048), (1920, 1200), (4096, 3000), (640, 480), (3, 3000)):
        for mw, mh in ((1280, 1280), (1280, 720), (999, 333), (2, 2)):
            got = fit_within(w, h, mw, mh)
            if got is None:
                assert w <= mw and h <= mh
                continue
            ow, oh = got
            assert 2 <= ow <= max(2, mw) and 2 <= oh <= max(2, mh), (w, h, mw, mh, got)
            assert ow % 2 == 0 and oh % 2 == 0, (w, h, mw, mh, got)


def test_fit_rejects_bad_input():
    for w, h in ((0, 100), (100, 0), (-1, 5)):
        try:
            fit_within(w, h, 100, 100)
        except ValueError:
            continue
        raise AssertionError("accepted {}x{}".format(w, h))


def test_caps_string_pins_square_par():
    assert scale_caps(1280, 1070) == "video/x-raw,width=1280,height=1070,pixel-aspect-ratio=1/1"


def _main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print("  ok ", t.__name__)
    print(len(tests), "passed")


if __name__ == "__main__":
    _main()
