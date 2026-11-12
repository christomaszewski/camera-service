#!/usr/bin/env python3
"""Unit tests for shared_encoder (pure; no gi/GStreamer needed). Run: python3 test_shared_encoder.py"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from shared_encoder import (BITRATE_DEFAULT, QUEUE, KeyframeLimiter, chain, codec_for,  # noqa: E402
                            parse_bitrate, parse_profile, pin_caps, select_encoder, wanted)

_JETSON = {"nvvidconv", "nvv4l2h264enc", "nvv4l2h265enc", "x264enc", "h264parse", "h265parse", "capssetter"}
_NANO = {"nvvidconv", "nvv4l2h265enc", "x264enc", "h264parse", "h265parse", "capssetter"}   # no H.264 NVENC
_DEV = {"x264enc", "h264parse", "h265parse", "capssetter"}


def test_mode_and_codec():
    assert wanted("shared") and wanted(" Shared ")
    assert not any(wanted(v) for v in (None, "", "per-viewer", "1", "true"))
    assert codec_for(None) == codec_for("") == "h264"
    assert codec_for("video/x-h264") == codec_for("video/x-h264,profile=high") == "h264"
    assert codec_for("video/x-h265") == "h265"
    assert codec_for("video/x-vp8") is None and codec_for("video/x-av1") is None


def test_the_hardware_encoder_is_shared_when_its_pieces_are_there():
    assert select_encoder("h264", _JETSON) == "nvv4l2h264enc"
    assert select_encoder("h265", _JETSON) == "nvv4l2h265enc"
    assert select_encoder("h264", _NANO) == "x264enc"                    # an Orin Nano has no H.264 NVENC
    assert select_encoder("h264", _JETSON - {"nvvidconv"}) == "x264enc"  # the encoder alone is not enough
    assert select_encoder("h264", _DEV) == "x264enc"
    assert select_encoder("h265", _DEV) is None                          # H.265 is hardware only
    assert select_encoder("h264", set()) is None


def test_bitrate_falls_back_to_the_start_bitrate_then_the_default():
    assert parse_bitrate("6000000", "2000000") == 6000000
    assert parse_bitrate(None, "2000000") == parse_bitrate("", "2000000") == parse_bitrate("0", "2000000") == 2000000
    assert parse_bitrate(None, None) == parse_bitrate("x", "-5") == BITRATE_DEFAULT


def test_profile_is_honoured_in_this_mode():
    assert [parse_profile(v) for v in (None, "", "High", "main", "baseline", "constrained-baseline", "high10")] == \
        ["constrained-baseline", "constrained-baseline", "high", "main", "baseline", "constrained-baseline",
         "constrained-baseline"]


def test_pin_caps_gives_each_encoder_what_it_leaves_out():
    # NVENC leaves profile and level to downstream: both pinned, or the parser announces twice
    assert pin_caps("nvv4l2h264enc", "h264", "high", "4") == \
        "video/x-h264,stream-format=byte-stream,alignment=au,profile=high,level=(string)4"
    assert pin_caps("nvv4l2h264enc", "h264", None, "5.1").endswith("profile=constrained-baseline,level=(string)5.1")
    # before the geometry is known there is no level to pin yet
    assert pin_caps("nvv4l2h264enc", "h264") == "video/x-h264,stream-format=byte-stream,alignment=au,profile=constrained-baseline"
    # x264enc computes and announces its own level: pinning one could only contradict it
    assert pin_caps("x264enc", "h264", "main", "4") == "video/x-h264,stream-format=byte-stream,alignment=au,profile=main"
    assert pin_caps("nvv4l2h265enc", "h265", "high", "4") == "video/x-h265,stream-format=byte-stream,alignment=au"


def test_chain_shape_per_encoder():
    nv = chain("h264", "nvv4l2h264enc", 4_000_000)
    assert nv.startswith("queue name=shared_enc_q leaky=downstream max-size-buffers=2 ! nvvidconv ! "
                         "video/x-raw(memory:NVMM),format=NV12 ! nvv4l2h264enc name=shared_enc bitrate=4000000 ! ")
    assert 'capsfilter name=shared_enc_caps caps="video/x-h264,stream-format=byte-stream,alignment=au,profile=constrained-baseline" ! ' \
           'h264parse config-interval=-1 ! capssetter join=true caps="video/x-h264,coded-picture-structure=(string)frame,' \
           'chroma-format=(string)4:2:0,bit-depth-luma=(uint)8,bit-depth-chroma=(uint)8"' in nv
    sw = chain("h264", "x264enc", 2_500_000, "superfast")
    assert sw.startswith(QUEUE + " ! x264enc name=shared_enc bitrate=2500 tune=zerolatency speed-preset=superfast ! ")
    assert "nvvidconv" not in sw and "h264parse config-interval=-1 ! capssetter" in sw
    assert " ! x264enc name=shared_enc bitrate=1 tune=zerolatency speed-preset=ultrafast ! " in chain("h264", "x264enc", 100, None)
    h265 = chain("h265", "nvv4l2h265enc", 3_000_000)
    assert "nvv4l2h265enc name=shared_enc bitrate=3000000" in h265
    assert 'caps="video/x-h265,stream-format=byte-stream,alignment=au" ! h265parse config-interval=-1 ! ' \
           'capssetter join=true caps="video/x-h265,' in h265
    try:
        chain("h264", "openh264enc", 1)
        raise AssertionError("an encoder with no chain must be refused")
    except ValueError:
        pass


def test_keyframes_one_per_interval_and_no_request_lost():
    k = KeyframeLimiter(1.0)
    assert k.request(10.0) == 0.0                      # the first: now
    assert k.request(10.01) is None                    # already pending
    k.fired(10.02)
    # a join sends dozens: they fold into ONE keyframe at the end of the interval
    assert abs(k.request(10.3) - 0.72) < 1e-9
    assert all(k.request(10.3 + i * 0.01) is None for i in range(40))
    k.fired(11.02)
    assert k.request(15.0) == 0.0                      # long after: now
    k.fired(15.0)
    assert (k.requests, k.forced) == (44, 3)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
    print("ok")
