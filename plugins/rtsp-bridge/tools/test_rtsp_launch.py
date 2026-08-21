#!/usr/bin/env python3
"""Unit tests for rtsp_launch (pure string builders) + the cam_header pack/unpack round-trip.

Runs standalone (`python3 test_rtsp_launch.py`) or under pytest -- no GStreamer, no docker.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cam_header
import rtsp_launch as rl


def test_normalize_codec():
    assert rl.normalize_codec(None) == "h264"
    assert rl.normalize_codec(" H265 ") == "h265"
    try:
        rl.normalize_codec("av1")
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_pick_encoder():
    have_nv = lambda n: n.startswith("nvv4l2")   # noqa: E731
    have_none = lambda n: False                  # noqa: E731
    assert rl.pick_encoder("h264", "auto", have_nv) == "nvv4l2h264enc"
    assert rl.pick_encoder("h264", None, have_none) == "x264enc"
    assert rl.pick_encoder("h265", "", have_none) == "x265enc"
    # An explicit pin wins verbatim, even when the probe would say otherwise.
    assert rl.pick_encoder("h264", "openh264enc", have_nv) == "openh264enc"


def test_encoder_block_cpu_kbps():
    b = rl.encoder_block("h264", "x264enc", 4_000_000, 30)
    assert "bitrate=4000" in b            # x264enc takes kbit/s
    assert "key-int-max=30" in b and "tune=zerolatency" in b
    assert "bframes=0" in b and "b-adapt=false" in b and "name=cam_enc" in b
    b265 = rl.encoder_block("h265", "x265enc", 2_000_000, 15)
    assert "x265enc" in b265 and "bitrate=2000" in b265 and "key-int-max=15" in b265
    assert 'option-string="bframes=0"' in b265


def test_encoder_block_nvenc():
    b = rl.encoder_block("h264", "nvv4l2h264enc", 4_000_000, 30)
    assert "bitrate=4000000" in b         # nvv4l2 takes bit/s
    assert "iframeinterval=30" in b
    assert b.startswith("nvvidconv ! video/x-raw(memory:NVMM),format=I420 ! ")
    b265 = rl.encoder_block("h265", "nvv4l2h265enc", 4_000_000, 30)
    assert "nvv4l2h265enc" in b265 and "nvvidconv" in b265


def test_encoder_block_unknown_pin_is_bare():
    # An unrecognized pinned element gets NO properties in the string (a wrong prop name would be a
    # parse-time hard failure); the defensive media-configure pass handles tuning.
    assert rl.encoder_block("h264", "openh264enc", 4_000_000, 30) == "openh264enc name=cam_enc"


def test_build_factory_launch():
    src = "unixfdsrc name=cam_src socket-path=/tmp/cam/unixfd ! videoconvert ! video/x-raw,format=I420"
    l264 = rl.build_factory_launch(src, "h264", rl.encoder_block("h264", "x264enc", 4_000_000, 30))
    assert l264.startswith("( ") and l264.endswith(" )")
    assert "h264parse ! rtph264pay name=pay0 pt=96 config-interval=-1" in l264
    l265 = rl.build_factory_launch(src, "h265", rl.encoder_block("h265", "x265enc", 4_000_000, 30))
    assert "h265parse ! rtph265pay name=pay0 pt=96 config-interval=-1" in l265


def test_parse_protocols():
    assert rl.parse_protocols(None) is None and rl.parse_protocols("  ") is None
    assert rl.parse_protocols("tcp") == ("tcp",)
    assert rl.parse_protocols("UDP, tcp") == ("udp", "tcp")
    for junk in ("http", "udp;tcp", ","):
        try:
            rl.parse_protocols(junk)
            raise AssertionError(f"expected ValueError for {junk!r}")
        except ValueError:
            pass


def test_mount_path_and_url():
    assert rl.normalize_mount_path(None, "cam_a") == "/cam_a"
    assert rl.normalize_mount_path("front", "cam_a") == "/front"
    assert rl.normalize_mount_path("/front", "cam_a") == "/front"
    assert rl.rtsp_url("vehicle", 8554, "/cam_a") == "rtsp://vehicle:8554/cam_a"


def test_cam_header_roundtrip():
    hdr = cam_header.FrameHeader(timestamp_ns=1_700_000_000_123_456_789, frame_id=42,
                                 width=2448, height=2048, pixfmt="GRAY8", ts_source="ptp_chunk")
    blob = hdr.pack() + b"\x00" * 16          # unpack tolerates trailing pixel bytes
    back = cam_header.unpack_header(blob)
    assert (back.timestamp_ns, back.frame_id, back.width, back.height, back.pixfmt,
            back.ts_source) == (hdr.timestamp_ns, 42, 2448, 2048, "GRAY8", "ptp_chunk")
    try:
        cam_header.unpack_header(b"XXXX" + blob[4:])
        raise AssertionError("expected TransportError on bad magic")
    except cam_header.TransportError:
        pass


def _main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print("  ok ", t.__name__)
    print(len(tests), "passed")


if __name__ == "__main__":
    _main()
