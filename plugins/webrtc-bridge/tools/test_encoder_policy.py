#!/usr/bin/env python3
"""Unit tests for encoder_policy (pure; no gi/GStreamer needed). Run: python3 test_encoder_policy.py"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from encoder_policy import keyframe_interval_frames, live_encoder_props  # noqa: E402


def test_gop_is_seconds_times_fps_rounded():
    assert keyframe_interval_frames(10, 2.0) == 20
    assert keyframe_interval_frames(7.5, 2.0) == 15
    assert keyframe_interval_frames(24, 2.0) == 48
    assert keyframe_interval_frames(30, 0.5) == 15


def test_gop_unknown_fps_assumes_a_conservative_preview_rate():
    # None / 0 / garbage fps -> 10 fps assumed: the SHORT gop is the safe error
    assert keyframe_interval_frames(None, 2.0) == 20
    assert keyframe_interval_frames(0, 2.0) == 20
    assert keyframe_interval_frames("x", 2.0) == 20
    # and a nonsensical seconds value falls back to the default 2 s rather than a degenerate gop
    assert keyframe_interval_frames(10, -1) == 20
    assert keyframe_interval_frames(0.1, 2.0) == 1     # never below one frame


def test_x264_gets_a_cheap_preset_and_a_short_gop():
    props = dict(live_encoder_props("x264enc", 10))
    assert props["speed-preset"] == "ultrafast"
    assert props["key-int-max"] == 20
    # both knobs honored; the preset is normalized to the enum nick form
    props = dict(live_encoder_props("x264enc", 10, keyframe_s="4", x264_preset=" UltraFast "))
    assert props == {"speed-preset": "ultrafast", "key-int-max": 40}


def test_nvenc_gets_only_the_gop_under_its_own_name():
    assert live_encoder_props("nvv4l2h264enc", 10) == [("iframeinterval", 20), ("idrinterval", 20)]
    assert live_encoder_props("openh264enc", 10) == [("gop-size", 20)]


def test_keyframe_zero_leaves_the_encoder_default_gop():
    assert live_encoder_props("x264enc", 10, keyframe_s=0) == [("speed-preset", "ultrafast")]
    assert live_encoder_props("nvv4l2h264enc", 10, keyframe_s="0") == []
    # a garbage env value is not an error: back to the 2 s default
    assert live_encoder_props("nvv4l2h264enc", 10, keyframe_s="two") == [("iframeinterval", 20), ("idrinterval", 20)]


def test_unknown_encoder_is_a_no_op():
    assert live_encoder_props("vp8enc", 10) == []
    assert live_encoder_props("?", None) == []


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
