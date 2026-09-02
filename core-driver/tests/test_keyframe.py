"""Tests for the stream-copy keyframe gate (cam_driver.keyframe) -- pure byte logic, no GStreamer.

A recording session that opens mid-stream on an H.264/H.265 source must start on a sync point, or
its first segment begins on P-frames no decoder can render (a parser does not drop pre-IDR data; the
muxer writes what it is given). The gate reads the delivered bytes + the caps string the source
already hands on_encoded.

Run: python3 core-driver/tests/test_keyframe.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from cam_driver.keyframe import is_sync_point, parse_caps_kind  # noqa: E402

H264 = "video/x-h264"
H265 = "video/x-h265"

# H.264 NAL headers: forbidden_zero(1) | nal_ref_idc(2) | nal_unit_type(5)
IDR = bytes([0x65, 0x88, 0x84, 0x00])      # type 5
NONIDR = bytes([0x41, 0x9a, 0x00])         # type 1
SPS = bytes([0x67, 0x42, 0x00, 0x1e])      # type 7
PPS = bytes([0x68, 0xce, 0x38, 0x80])      # type 8
# H.265 NAL headers: forbidden_zero(1) | nal_unit_type(6) | layer_id(6) | tid(3)
H265_IDR_W_RADL = bytes([19 << 1, 0x01, 0xaf])
H265_CRA = bytes([21 << 1, 0x01, 0xaf])
H265_TRAIL_R = bytes([1 << 1, 0x01, 0xaf])


def _annexb(*nals, short=False):
    sc = b"\x00\x00\x01" if short else b"\x00\x00\x00\x01"
    return b"".join(sc + n for n in nals)


def _prefixed(*nals, size=4):
    return b"".join(len(n).to_bytes(size, "big") + n for n in nals)


def test_h264_bytestream_idr_is_a_sync_point():
    assert is_sync_point(H264, _annexb(SPS, PPS, IDR), "byte-stream")


def test_h264_bytestream_non_idr_is_not():
    assert not is_sync_point(H264, _annexb(NONIDR), "byte-stream")
    assert not is_sync_point(H264, _annexb(SPS, PPS, NONIDR), "byte-stream"), \
        "parameter sets alone are not a keyframe"


def test_h264_three_byte_start_code():
    assert is_sync_point(H264, _annexb(IDR, short=True), "byte-stream")


def test_h264_avc_length_prefixed():
    assert is_sync_point(H264, _prefixed(SPS, IDR), "avc", 4)
    assert not is_sync_point(H264, _prefixed(NONIDR), "avc", 4)
    assert is_sync_point(H264, _prefixed(NONIDR, IDR, size=2), "avc", 2), \
        "the NAL length size from the codec_data must be honoured"


def test_h265_random_access_points():
    assert is_sync_point(H265, _annexb(H265_IDR_W_RADL), "byte-stream")
    assert is_sync_point(H265, _annexb(H265_CRA), "byte-stream")
    assert not is_sync_point(H265, _annexb(H265_TRAIL_R), "byte-stream")
    assert is_sync_point(H265, _prefixed(H265_TRAIL_R, H265_IDR_W_RADL), "hvc1", 4)


def test_jpeg_every_frame_qualifies():
    assert is_sync_point("image/jpeg", b"\xff\xd8\xff\xe0")


def test_unknown_media_passes_through():
    # Refusing would silently record NOTHING for a codec the gate merely doesn't know.
    assert is_sync_point("video/x-vp9", b"\x00\x01")
    assert is_sync_point(None, b"\x00\x01")


def test_empty_or_truncated_h26x_is_not_a_sync_point():
    assert not is_sync_point(H264, b"", "byte-stream")
    assert not is_sync_point(H264, b"\x00\x00\x01", "byte-stream")        # start code, no header
    assert not is_sync_point(H264, b"\x00\x00\x00\x10" + IDR, "avc", 4)   # length overruns the data
    assert not is_sync_point(H265, b"", "byte-stream")


def test_parse_caps_kind_reads_media_format_and_nal_length():
    media, sf, nal = parse_caps_kind(
        "video/x-h264, stream-format=(string)avc, alignment=(string)au, "
        "codec_data=(buffer)01640028fee1001a67640028acd94, width=(int)1920")
    assert (media, sf, nal) == (H264, "avc", 3), "avcC lengthSizeMinusOne=2 -> 3-byte NAL lengths"
    media, sf, nal = parse_caps_kind("video/x-h264, stream-format=(string)byte-stream")
    assert (media, sf, nal) == (H264, "byte-stream", 4)
    hvcc = "00" * 21 + "01" + "00" * 2
    assert parse_caps_kind(f"video/x-h265, stream-format=(string)hvc1, codec_data=(buffer){hvcc}") \
        == (H265, "hvc1", 2)
    assert parse_caps_kind("image/jpeg, width=(int)512, height=(int)512") == ("image/jpeg", None, 4)
    assert parse_caps_kind(None) == (None, None, 4)
    assert parse_caps_kind("") == (None, None, 4)


def _main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"{len(tests)} passed")


if __name__ == "__main__":
    _main()
