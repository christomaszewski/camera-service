"""Tests for pixel-format parsing + encoder selection (pure; no GStreamer).

Run: python3 core-driver/tests/test_formats.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from cam_driver.formats import (  # noqa: E402
    bytes_per_frame, encoded_info, parse_pixel_format, select_decoder, select_encoder,
)


def test_parse_gige_mono_and_bayer():
    assert parse_pixel_format("Mono8") == ("GRAY8", 8, None, False, False)
    assert parse_pixel_format("Mono16") == ("GRAY16_LE", 16, None, False, False)
    fmt, bits, bayer, packed, color = parse_pixel_format("BayerRG8")
    assert (fmt, bits, bayer, packed, color) == ("GRAY8", 8, "rggb", False, False)
    assert parse_pixel_format("Mono12Packed")[3] is True       # packed flagged


def test_parse_gstreamer_color_passthrough():
    assert parse_pixel_format("I420") == ("I420", 8, None, False, True)
    assert parse_pixel_format("NV12") == ("NV12", 8, None, False, True)
    assert parse_pixel_format("YUY2") == ("YUY2", 8, None, False, True)
    assert parse_pixel_format("RGB") == ("RGB", 8, None, False, True)
    assert parse_pixel_format("GRAY8") == ("GRAY8", 8, None, False, False)   # mono, not color


def test_bytes_per_frame_subsampling():
    assert bytes_per_frame("GRAY8", 100, 100) == 10000
    assert bytes_per_frame("GRAY16_LE", 100, 100) == 20000
    assert bytes_per_frame("I420", 100, 100) == 15000          # 4:2:0 = 1.5x
    assert bytes_per_frame("NV12", 100, 100) == 15000
    assert bytes_per_frame("YUY2", 100, 100) == 20000          # 4:2:2 packed = 2x
    assert bytes_per_frame("RGB", 100, 100) == 30000


def test_select_encoder_auto_color_is_ffv1():
    # the fidelity-critical rule: color -> ffv1 (no NV24 chroma resample)
    assert select_encoder("auto", 8, is_color=True) == "ffv1"
    assert select_encoder("auto", 8, is_color=False) == "hw-hevc-lossless"
    assert select_encoder("auto", 16, is_color=False) == "ffv1"


def test_select_encoder_explicit_is_honored():
    assert select_encoder("x265-lossless", 8, is_color=True) == "x265-lossless"
    assert select_encoder("hw-hevc-lossless", 8, is_color=True) == "hw-hevc-lossless"  # override
    assert select_encoder("bogus", 8) == "hw-hevc-lossless"   # unknown -> auto -> mono8 path


def test_encoded_info():
    caps, parser, decoder = encoded_info("MJPEG")
    assert (caps, parser) == ("image/jpeg", "jpegparse")
    assert encoded_info("H264")[1] == "h264parse"
    assert encoded_info("I420") is None        # raw, not encoded
    assert encoded_info("Mono8") is None


def test_select_encoder_encoded_is_stream_copy():
    assert select_encoder("auto", 8, encoded=True) == "stream-copy"
    assert select_encoder("auto", 8, is_color=True, encoded=True) == "stream-copy"   # encoded wins
    assert select_encoder("ffv1", 8, encoded=True) == "ffv1"      # explicit re-encode honored


def test_select_decoder_software_default():
    # no HW -> per-codec software decoder + CPU videoconvert (dev/x86/JP6-no-NVDEC)
    assert select_decoder("avdec_h264") == ("avdec_h264", "videoconvert")
    assert select_decoder("jpegdec", hw_available=False) == ("jpegdec", "videoconvert")


def test_select_decoder_hw_when_available():
    # HW present -> NVDEC (one element, codec-agnostic) + nvvidconv, regardless of the sw decoder
    assert select_decoder("avdec_h265", hw_available=True) == ("nvv4l2decoder", "nvvidconv")
    assert select_decoder("jpegdec", hw_available=True) == ("nvv4l2decoder", "nvvidconv")


def _main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"{len(tests)} passed")


if __name__ == "__main__":
    _main()
