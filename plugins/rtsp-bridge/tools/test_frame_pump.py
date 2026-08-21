#!/usr/bin/env python3
"""Unit tests for frame_pump's gi-free decision logic (caps derivation, PTS policy, frame-size
math) and the stretch stage's numeric behavior via thermal_preview.

Runs standalone (`python3 test_frame_pump.py`) or under pytest -- no GStreamer, no docker.
(numpy needed only for the stretch test; it is in the image and the dev venv.)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import frame_pump as fp


def test_frame_bytes():
    assert fp.frame_bytes("GRAY8", 640, 512) == 640 * 512
    assert fp.frame_bytes("GRAY16_LE", 640, 512) == 640 * 512 * 2
    assert fp.frame_bytes("I420", 640, 512) == 640 * 512 * 3 // 2
    assert fp.frame_bytes("RGB", 4, 4) == 48
    assert fp.frame_bytes("nonsense", 640, 512) == 0     # unknown -> size check skipped


def test_want_stretch():
    win = (1.0, 99.0)
    assert fp.want_stretch("GRAY16_LE", win, False)
    assert fp.want_stretch("GRAY16_BE", win, False)
    assert not fp.want_stretch("GRAY16_LE", None, False)         # knob off
    assert not fp.want_stretch("GRAY8", win, False)              # not 16-bit
    assert not fp.want_stretch("GRAY16_LE", win, True)           # debayer path is 8-bit color


def test_out_caps_mono_and_stretch():
    assert (fp.out_caps_str("GRAY8", 512, 512, fps=25)
            == "video/x-raw,format=GRAY8,width=512,height=512,framerate=25/1")
    assert "format=GRAY8" in fp.out_caps_str("GRAY16_LE", 640, 512, fps=25, stretch=True)
    assert "format=GRAY16_LE" in fp.out_caps_str("GRAY16_LE", 640, 512, fps=25, stretch=False)
    assert "framerate" not in fp.out_caps_str("GRAY8", 512, 512, fps=0)   # unknown rate -> omitted


def test_out_caps_bayer_relabel():
    # The header endpoint publishes a Bayer mosaic as GRAY8; the config pattern relabels it for
    # bayer2rgb -- but only on the debayer path.
    caps = fp.out_caps_str("GRAY8", 512, 512, fps=25, bayer="rggb", debayer=True)
    assert caps.startswith("video/x-bayer,format=rggb,")
    caps = fp.out_caps_str("GRAY8", 512, 512, fps=25, bayer="rggb", debayer=False)
    assert caps.startswith("video/x-raw,format=GRAY8,")


def test_next_pts_header_clock():
    st = {}
    dur = 40_000_000
    pts, fb = fp.next_pts(st, 1_000_000_000_000, None, dur)
    assert (pts, fb) == (0, False)                        # first frame anchors the base
    pts, fb = fp.next_pts(st, 1_000_000_040_000, None, dur)
    assert (pts, fb) == (40_000, False)                   # smooth PTP-derived spacing
    pts, fb = fp.next_pts(st, 1_000_000_100_000, None, dur)
    assert (pts, fb) == (100_000, False)


def test_next_pts_falls_back_permanently():
    st = {}
    dur = 40_000_000
    fp.next_pts(st, 2_000_000_000_000, 0, dur)
    fp.next_pts(st, 2_000_000_040_000, 10, dur)
    # A PTP re-sync step (backwards) -> permanent switch to arrival time, monotonicity kept.
    pts, fb = fp.next_pts(st, 1_999_999_000_000, 500_000, dur)
    assert fb and pts == 500_000
    # Later header times are ignored even if they recover; arrival keeps driving.
    pts, fb = fp.next_pts(st, 2_000_000_080_000, 600_000, dur)
    assert (pts, fb) == (600_000, False)
    # Arrival must stay monotonic too; a stalled arrival clock gets duration-stepped.
    pts, _ = fp.next_pts(st, None, 600_000, dur)
    assert pts == 600_000 + dur


def test_next_pts_no_clocks_at_all():
    st = {}
    dur = 40_000_000
    p1, fb = fp.next_pts(st, 0, fp.CLOCK_TIME_NONE, dur)  # zero ts + no arrival -> synthesized
    assert fb
    p2, _ = fp.next_pts(st, 0, fp.CLOCK_TIME_NONE, dur)
    p3, _ = fp.next_pts(st, 0, fp.CLOCK_TIME_NONE, dur)
    assert p2 - p1 == dur and p3 - p2 == dur


def test_stretch_stage_numeric():
    try:
        import numpy as np
    except ImportError:                                  # host without numpy; the image always has it
        print("  (stretch test skipped: numpy not installed)")
        return
    from thermal_preview import PercentileStretch
    # An LSB-aligned 16-bit ramp (the radiometric case videoconvert renders near-black) must come
    # out spanning the 8-bit range.
    frame = np.linspace(100, 2000, 640 * 512, dtype=np.uint16).reshape(512, 640)
    out = PercentileStretch(1.0, 99.0)(frame)
    assert out.dtype == np.uint8 and out.shape == (512, 640)
    assert int(out.max()) - int(out.min()) > 200


def _main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print("  ok ", t.__name__)
    print(len(tests), "passed")


if __name__ == "__main__":
    _main()
