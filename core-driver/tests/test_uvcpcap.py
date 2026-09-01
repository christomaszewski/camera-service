"""Tests for usbmon parsing + UVC frame reassembly (pure; no GStreamer).

Byte-exact round trips against the synthetic captures from uvcpcap_fixture: every
builder returns the exact (ts_ns, frame_bytes) list iter_frames must reproduce.

Run: python3 core-driver/tests/test_uvcpcap.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from cam_driver.pcapio import PcapFormatError  # noqa: E402
from cam_driver.uvcpcap import UvcFrameExtractor, iter_frames, probe  # noqa: E402
from uvcpcap_fixture import (  # noqa: E402
    PcapWriter, build_bulk_capture, build_mjpeg_capture, build_y16_capture,
)

_TMP = tempfile.mkdtemp(prefix="uvcpcap_test_")
_W, _H = 64, 8
_SIZE = _W * _H * 2


def _path(data: bytes, name: str) -> str:
    p = os.path.join(_TMP, name)
    with open(p, "wb") as f:
        f.write(data)
    return p


def _y16(name, **kw):
    blob, expected = build_y16_capture(width=_W, height=_H, **kw)
    return _path(blob, name), expected


def test_y16_roundtrip_all_container_formats():
    # Same scenario across every container flavor: exact bytes AND exact timestamps.
    for fmt in ("pcap", "pcap-ns", "pcapng", "pcapng-us", "linktype189", "pcap-be"):
        p, expected = _y16(f"y16-{fmt}.pcap", fmt=fmt)
        got = list(iter_frames(p, bus=1, dev=5, ep=1, expected_frame_size=_SIZE))
        assert got == expected, f"fmt={fmt}: {len(got)} frames vs {len(expected)}"
        assert len(got) == 4    # 6 frames - 1 ERR - 1 truncated


def test_extractor_stats_and_recovery():
    p, expected = _y16("y16-stats.pcap")
    ex = UvcFrameExtractor(_path(open(p, "rb").read(), "y16-stats2.pcap"),
                           bus=1, dev=5, ep=1, expected_frame_size=_SIZE)
    got = list(ex)
    assert got == expected
    assert ex.stats.err_frames == 1        # the ERR-bit frame was dropped...
    assert ex.stats.truncated >= 1         # ...and the snaplen-cut one (counted per iso packet)
    assert ex.stats.size_drops == 1        # the leading mid-frame partial, rejected by size
    assert ex.stats.frames_ok == len(expected)
    # neighbors of the poisoned frames are byte-exact (asserted by got == expected)


def test_two_pass_determinism():
    p, expected = _y16("y16-twopass.pcap")
    ex = UvcFrameExtractor(p, bus=1, dev=5, ep=1, expected_frame_size=_SIZE)
    assert list(ex) == list(ex) == expected   # loop replay re-iterates the same extractor


def test_padded_headers_tolerated_not_strict():
    p, expected = _y16("y16-padded.pcap", hlen=12, err_frame=None, truncated_frame=None)
    got = list(iter_frames(p, bus=1, dev=5, ep=1, expected_frame_size=_SIZE))
    assert got == expected
    strict = UvcFrameExtractor(p, bus=1, dev=5, ep=1, expected_frame_size=_SIZE, strict=True)
    try:
        frames = list(strict)
        raise AssertionError(f"strict mode accepted padded headers ({len(frames)} frames)")
    except ValueError as e:
        assert "bad_header" in str(e)


def test_probe_ranks_video_over_noise():
    p, _ = _y16("y16-probe.pcap")
    pr = probe(p)
    assert pr.best is not None
    assert (pr.best.bus, pr.best.dev, pr.best.ep) == (1, 5, 1)
    assert pr.best.uvc_score >= 0.9
    # noise streams (HID interrupt) are visible but never selected
    assert any(s.xfer == "intr" for s in pr.streams)


def test_probe_negotiated_and_described():
    p, _ = _y16("y16-neg.pcap")
    best = probe(p).best
    assert best.negotiated is not None
    assert best.negotiated.max_video_frame_size == _SIZE
    assert best.negotiated.fps is not None and 59.0 < best.negotiated.fps < 61.0
    assert best.described == ("GRAY16_LE", _W, _H)     # from the captured enumeration


def test_probe_without_enumeration():
    p, expected = _y16("y16-noenum.pcap", include_enumeration=False, include_commit=False)
    best = probe(p).best
    assert best is not None and best.described is None and best.negotiated is None
    got = list(iter_frames(p, bus=1, dev=5, ep=1, expected_frame_size=_SIZE))
    assert got == expected


def test_autoselect_matches_explicit():
    p, expected = _y16("y16-auto.pcap")
    assert list(iter_frames(p, expected_frame_size=_SIZE)) == expected
    # partial pin: device only; bus/ep filled from the probe
    assert list(iter_frames(p, dev=5, expected_frame_size=_SIZE)) == expected


def test_wrong_size_is_legible():
    p, _ = _y16("y16-size.pcap")
    try:
        list(iter_frames(p, bus=1, dev=5, ep=1, expected_frame_size=_SIZE + 4096))
        raise AssertionError("expected ValueError")
    except ValueError as e:
        assert str(_SIZE) in str(e) and str(_SIZE + 4096) in str(e)   # names both sizes


def test_wrong_endpoint_is_legible():
    p, _ = _y16("y16-ep.pcap")
    try:
        list(iter_frames(p, bus=1, dev=5, ep=4, expected_frame_size=_SIZE))
        raise AssertionError("expected ValueError")
    except ValueError as e:
        assert "no IN data" in str(e)
    try:
        list(iter_frames(p, dev=9, expected_frame_size=_SIZE))   # pinned device absent
        raise AssertionError("expected ValueError")
    except ValueError as e:
        assert "streams seen" in str(e)


def test_network_capture_is_legible():
    w = PcapWriter(linktype=1)    # an Ethernet capture, not usbmon
    w.add(1_700_000_000_000_000_000, b"\x00" * 60)
    p = _path(w.getvalue(), "eth.pcap")
    try:
        probe(p)
        raise AssertionError("expected PcapFormatError")
    except PcapFormatError as e:
        assert "usbmon" in str(e)


def test_mjpeg_roundtrip_and_padding_strip():
    blob, expected = build_mjpeg_capture()
    p = _path(blob, "mjpeg.pcap")
    got = list(iter_frames(p, bus=1, dev=6, ep=2, mjpeg=True))
    assert got == expected                       # frame 2 had wire padding; parser stripped it
    assert all(f.startswith(b"\xff\xd8") and f.endswith(b"\xff\xd9") for _, f in got)


def test_bulk_payload_spanning_urbs():
    blob, expected = build_bulk_capture()
    p = _path(blob, "bulk.pcap")
    got = list(iter_frames(p, bus=1, dev=7, ep=3, expected_frame_size=32 * 4 * 2))
    assert got == expected


def test_bulk_zlp_terminated_payloads():
    # payload length an exact multiple of the URB size: a zero-length completion ends it
    blob, expected = build_bulk_capture(urb_size=129, zlp=True)   # 2B header + 256B = 2*129
    p = _path(blob, "bulk-zlp.pcap")
    got = list(iter_frames(p, bus=1, dev=7, ep=3, expected_frame_size=32 * 4 * 2))
    assert got == expected


def test_mjpeg_frame_with_a_missed_iso_packet_is_dropped_not_merged():
    # A missed/errored iso packet (nonzero descriptor status) is lost DATA. A raw frame with a hole
    # is caught by the size check, but an MJPEG one still starts with SOI and ends with EOI -- so
    # the extractor must treat the packet like a snaplen cut and skip the frame, or a JPEG with a
    # hole gets stream-copied into the recording as a valid frame.
    blob, expected = build_mjpeg_capture(errored_frame=1)
    p = _path(blob, "mjpeg-lost.pcap")
    ex = UvcFrameExtractor(p, bus=1, dev=6, ep=2, mjpeg=True)
    got = list(ex)
    assert got == expected and len(got) == 4          # frame 1 gone, its neighbours byte-exact
    assert ex.stats.truncated >= 1                    # the lost packet is counted with the cuts


def test_wholly_lost_frame_does_not_swallow_the_next():
    # usbmon drops EVERY urb of frame 5: frame 6 repeats frame 4's FID and must still emit
    p, expected = _y16("y16-omit.pcap", omit_frame=5, err_frame=None, truncated_frame=None,
                       leading_partial=False)
    got = list(iter_frames(p, bus=1, dev=5, ep=1, expected_frame_size=_SIZE))
    assert got == expected and len(got) == 5


def test_zero_based_capture_timestamps():
    # relative-time captures legitimately start at ts 0 (editcap -t): stats must not
    # treat 0 as "unset"
    p, expected = _y16("y16-zero.pcap", base_ns=0, err_frame=None, truncated_frame=None,
                       include_enumeration=False, include_commit=False, leading_partial=False)
    ex = UvcFrameExtractor(p, bus=1, dev=5, ep=1, expected_frame_size=_SIZE)
    got = list(ex)
    assert got == expected
    assert ex.stats.first_ts_ns == expected[0][0] == 0
    assert ex.stats.last_ts_ns >= expected[-1][0]   # last URB of the last frame


def _main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"{len(tests)} passed")


if __name__ == "__main__":
    _main()
