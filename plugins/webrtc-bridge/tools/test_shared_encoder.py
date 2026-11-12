#!/usr/bin/env python3
"""Unit tests for shared_encoder (pure; no gi/GStreamer needed). Run: python3 test_shared_encoder.py"""
import contextlib
import io
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import shared_encoder  # noqa: E402
from shared_encoder import (BITRATE_DEFAULT, QUEUE, KeyframeLimiter, chain, codec_for, encoder_profile,  # noqa: E402
                            hardware_level, level_caps, parse_bitrate, parse_profile, pin_caps, select_encoder,
                            wanted)

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


def _ranked(names, **ranks):
    return {name: ranks.get(name, 256) for name in names}


def test_element_ranks_decide_between_hardware_and_software_as_they_do_per_viewer():
    # L4T's own ranks: the hardware encoder one above x264enc
    l4t = _ranked(_JETSON, nvv4l2h264enc=257, nvv4l2h265enc=257)
    assert select_encoder("h264", l4t) == "nvv4l2h264enc" and select_encoder("h265", l4t) == "nvv4l2h265enc"
    # GST_PLUGIN_FEATURE_RANK=nvv4l2h264enc:MAX, the deployed sensors' setting
    assert select_encoder("h264", {**l4t, "nvv4l2h264enc": 2**31 - 1}) == "nvv4l2h264enc"
    # NVENC kept for the recorder: ranked away, x264enc ranked up, or both
    assert select_encoder("h264", {**l4t, "nvv4l2h264enc": 0}) == "x264enc"
    assert select_encoder("h264", {**l4t, "x264enc": 2**31 - 1}) == "x264enc"
    assert select_encoder("h264", {**l4t, "nvv4l2h264enc": 63}) == "x264enc"        # below MARGINAL: unused
    assert select_encoder("h264", {**l4t, "nvv4l2h264enc": 256}) == "nvv4l2h264enc"  # a tie stays on hardware
    # an encoder ranked NONE is no encoder at all
    assert select_encoder("h264", {**l4t, "nvv4l2h264enc": 0, "x264enc": 0}) is None
    assert select_encoder("h265", {**l4t, "nvv4l2h265enc": 0}) is None
    # x264enc ranked away leaves the hardware one, whatever its rank relative to nothing
    assert select_encoder("h264", {**l4t, "x264enc": 0, "nvv4l2h264enc": 64}) == "nvv4l2h264enc"
    assert select_encoder("h264", _ranked(_DEV, x264enc=0)) is None


def _main(have, **env):
    """shared_encoder.main() in a container that has `have`, with `env` set: (exit code, stdout, stderr)."""
    names = ("VIDEO_CAPS", "CAM_WEBRTC_BITRATE", "CAM_WEBRTC_START_BITRATE", "CAM_WEBRTC_X264_PRESET")
    clean = {k: v for k, v in os.environ.items() if k not in names}
    out, err = io.StringIO(), io.StringIO()
    with patch.dict(os.environ, {**clean, **env}, clear=True), patch.object(shared_encoder, "_available", lambda: have), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = shared_encoder.main()
    return code, out.getvalue().strip(), err.getvalue().strip()


def test_the_command_prints_the_fragment_for_this_container_or_says_why_not():
    """run.sh takes the LAST line of stdout as the fragment and treats a non-zero exit as "encode per
    viewer"; anything said to a person goes to stderr."""
    l4t = _ranked(_JETSON, nvv4l2h264enc=257, nvv4l2h265enc=257)
    code, out, err = _main(l4t, CAM_WEBRTC_BITRATE="3000000")
    assert code == 0 and err == "" and "\n" not in out
    assert out == chain("h264", "nvv4l2h264enc", 3000000)
    code, out, err = _main(l4t, VIDEO_CAPS="video/x-h265", CAM_WEBRTC_START_BITRATE="2500000")
    assert code == 0 and out == chain("h265", "nvv4l2h265enc", 2500000)
    # software, with its preset; nothing configured -> the default bitrate
    code, out, err = _main(_ranked(_DEV), CAM_WEBRTC_X264_PRESET="superfast")
    assert code == 0 and out == chain("h264", "x264enc", BITRATE_DEFAULT, "superfast") and err == ""
    # ranked away from the hardware encoder: it says so, and still prints only the fragment on stdout
    code, out, err = _main({**l4t, "nvv4l2h264enc": 0})
    assert code == 0 and out == chain("h264", "x264enc", BITRATE_DEFAULT)
    assert "x264enc (rank 256) instead of nvv4l2h264enc (rank 0)" in err and "GST_PLUGIN_FEATURE_RANK" in err
    # the ways it cannot: a codec that is not shared, no encoder, a missing parser or capssetter
    code, out, err = _main(l4t, VIDEO_CAPS="video/x-vp8")
    assert code == 3 and out == "" and "is not H.264 or H.265" in err
    code, out, err = _main(_ranked(_DEV), VIDEO_CAPS="video/x-h265")
    assert code == 3 and out == "" and "no h265 encoder" in err and "needs the Jetson hardware encoder" in err
    code, out, err = _main(_ranked(_DEV - {"capssetter", "h264parse"}))
    assert code == 3 and out == "" and "missing capssetter, h264parse" in err
    code, out, err = _main({})
    assert code == 3 and "no h264 encoder" in err


def test_bitrate_falls_back_to_the_start_bitrate_then_the_default():
    assert parse_bitrate("6000000", "2000000") == 6000000
    assert parse_bitrate(None, "2000000") == parse_bitrate("", "2000000") == parse_bitrate("0", "2000000") == 2000000
    assert parse_bitrate(None, None) == parse_bitrate("x", "-5") == BITRATE_DEFAULT


def test_profile_is_honoured_in_this_mode():
    # `baseline` is the constrained one: what WebRTC negotiates under the name and what the encoders emit
    assert [parse_profile(v) for v in (None, "", "High", "main", "baseline", "constrained-baseline", "high10")] == \
        ["constrained-baseline", "constrained-baseline", "high", "main", "constrained-baseline",
         "constrained-baseline", "constrained-baseline"]


def test_the_profile_pinned_is_one_the_encoder_will_produce():
    """x264enc at ultrafast has no tool above constrained-baseline and labels its caps with what it was
    asked for anyway; the parser then announces the real profile, the second announcement webrtcsink refuses."""
    for asked in ("constrained-baseline", "baseline", "main", "high", None):
        assert encoder_profile("x264enc", asked, "ultrafast") == "constrained-baseline"
        assert encoder_profile("x264enc", asked, None) == encoder_profile("x264enc", asked, " UltraFast ") == "constrained-baseline"
    for preset in ("superfast", "veryfast", "medium"):
        assert [encoder_profile("x264enc", p, preset) for p in ("main", "high", "baseline", None)] == \
            ["main", "high", "constrained-baseline", "constrained-baseline"]
    # the hardware encoders produce what they are asked for (and have no preset to speak of)
    assert [encoder_profile("nvv4l2h264enc", p, None) for p in ("main", "high", "baseline")] == \
        ["main", "high", "constrained-baseline"]


def test_pin_caps_gives_each_encoder_what_it_leaves_out():
    # NVENC leaves profile and level to downstream: both pinned, or the parser announces twice
    assert pin_caps("nvv4l2h264enc", "h264", "high", "4.1") == \
        "video/x-h264,stream-format=byte-stream,alignment=au,profile=high,level=(string)4.1"
    assert pin_caps("nvv4l2h264enc", "h264", None, "5.1").endswith("profile=constrained-baseline,level=(string)5.1")
    # before the geometry is known there is no level to pin yet
    assert pin_caps("nvv4l2h264enc", "h264") == "video/x-h264,stream-format=byte-stream,alignment=au,profile=constrained-baseline"
    # x264enc computes and announces its own level: pinning one could only contradict it
    assert pin_caps("x264enc", "h264", "main", "4") == "video/x-h264,stream-format=byte-stream,alignment=au,profile=main"
    assert pin_caps("nvv4l2h265enc", "h265", "high", "4") == "video/x-h265,stream-format=byte-stream,alignment=au"


def test_the_level_pinned_for_the_hardware_encoder_is_the_one_it_will_write():
    """Asked for a level, nvv4l2h264enc announces it -- and writes the one its bitrate needs, never less
    than 2.1. The parser then announces the real one, and webrtcsink refuses a second announcement."""
    # measured on an R39 Orin: (asked, bit/s) -> written
    for asked, bitrate, written in (("1.1", 150_000, "2.1"), ("1.3", 500_000, "2.1"), ("2.1", 6_000_000, "3"),
                                    ("3", 20_000_000, "3.2"), ("4", 30_000_000, "4.1")):
        assert hardware_level(asked, bitrate) == written, (asked, bitrate)
    # and the cases that went through unchanged
    for asked, bitrate in (("2.1", 2_000_000), ("3", 2_000_000), ("3.1", 2_000_000), ("4", 4_000_000), ("5", 8_000_000)):
        assert hardware_level(asked, bitrate) == asked, (asked, bitrate)
    # a limit is inclusive; the next level up takes over one bit later
    assert hardware_level("3", 10_000_000) == "3" and hardware_level("3", 10_000_001) == "3.1"
    assert hardware_level("1", 0) == hardware_level("1b", None) == "2.1"                  # the floor
    assert hardware_level("5.2", 10**9) == "5.2"                                          # nothing above
    assert hardware_level("6.2", 1) == "6.2" and hardware_level(None, 1) is None          # not this table's
    # never below what was asked: the geometry needs that much whatever the bitrate
    assert all(hardware_level(lvl, 1) in ("2.1", lvl) for lvl in shared_encoder.LEVELS)


def test_a_whole_number_level_is_pinned_in_both_of_the_hardware_encoders_spellings():
    """Asked for level 3, nvv4l2h264enc encodes level 3 and announces "3.0" (4 comes back as "4"): a
    filter pinned to "3" alone refused it, and every SD-sized preview died not-negotiated."""
    base = "video/x-h264,stream-format=byte-stream,alignment=au,profile=constrained-baseline,level=(string)"
    for level in ("1", "2", "3", "4", "5", "6"):
        assert pin_caps("nvv4l2h264enc", "h264", None, level) == base + "{ %s, %s.0 }" % (level, level)
    for level in ("1b", "1.1", "2.2", "3.1", "4.2", "5.1"):
        assert pin_caps("nvv4l2h264enc", "h264", None, level) == base + level
    # ...and the parser is handed the one spelling it will itself announce after reading the SPS
    assert level_caps("h264", "3") == "video/x-h264,level=(string)3"
    assert level_caps("h264", "3.1") == "video/x-h264,level=(string)3.1"
    assert level_caps("h264") == level_caps("h264", None) == "video/x-h264"        # nothing known: no change


def test_chain_shape_per_encoder():
    nv = chain("h264", "nvv4l2h264enc", 4_000_000)
    assert nv.startswith("queue name=shared_enc_q leaky=downstream max-size-buffers=2 ! nvvidconv ! "
                         "video/x-raw(memory:NVMM),format=NV12 ! nvv4l2h264enc name=shared_enc bitrate=4000000 ! ")
    assert 'capsfilter name=shared_enc_caps caps="video/x-h264,stream-format=byte-stream,alignment=au,profile=constrained-baseline" ! ' \
           'capssetter name=shared_enc_level join=true caps="video/x-h264" ! ' \
           'h264parse config-interval=-1 ! capssetter join=true caps="video/x-h264,coded-picture-structure=(string)frame,' \
           'chroma-format=(string)4:2:0,bit-depth-luma=(uint)8,bit-depth-chroma=(uint)8"' in nv
    sw = chain("h264", "x264enc", 2_500_000, "superfast")
    assert sw.startswith(QUEUE + " ! x264enc name=shared_enc bitrate=2500 tune=zerolatency speed-preset=superfast ! ")
    assert "nvvidconv" not in sw and "h264parse config-interval=-1 ! capssetter" in sw
    assert "shared_enc_level" not in sw            # x264enc spells its level as the parser does
    assert " ! x264enc name=shared_enc bitrate=1 tune=zerolatency speed-preset=ultrafast ! " in chain("h264", "x264enc", 100, None)
    h265 = chain("h265", "nvv4l2h265enc", 3_000_000)
    assert "nvv4l2h265enc name=shared_enc bitrate=3000000" in h265
    assert 'caps="video/x-h265,stream-format=byte-stream,alignment=au" ! h265parse config-interval=-1 ! ' \
           'capssetter join=true caps="video/x-h265,' in h265
    assert "shared_enc_level" not in h265
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
