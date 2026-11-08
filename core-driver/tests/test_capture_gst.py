"""frame-capture's GStreamer halves: the JPEG encoder (every input shape decodes back at the expected
size) and the transport reader against real producers on both endpoint kinds -- a headered shmsink
(JP6) and, where the runtime has it, a unixfdsink (JP7) -- including re-dialing after the producer
goes away. Runs on every CI runtime without skipping: the unixfd half is exercised wherever the
element exists and asserted absent elsewhere.

Run: python3 -m pytest core-driver/tests/test_capture_gst.py
"""
import os
import sys
import tempfile
import threading
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import gi  # noqa: E402
gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402

Gst.init(None)

from cam_driver import transport  # noqa: E402
from cam_driver.capture.encode import (CPU_ELEMENT, EncodeError, JpegEncoder, pad_rows,  # noqa: E402
                                       scaled_size, to_8bit)
from cam_driver.capture.frames import Frame  # noqa: E402
from cam_driver.capture.reader import (TransportReader, from_caps, from_header_bytes,  # noqa: E402
                                       select_endpoint, unixfd_socket)

HAVE_UNIXFD = Gst.ElementFactory.find("unixfdsink") is not None


def _frame(fmt, w, h, data, bayer=None):
    return Frame(1, 1, 1, "capture", 1, None, fmt, w, h, bayer, data)


def _decode(jpeg: bytes):
    pipe = Gst.parse_launch('appsrc name=src caps="image/jpeg" ! jpegdec ! appsink name=sink sync=false')
    src, sink = pipe.get_by_name("src"), pipe.get_by_name("sink")
    pipe.set_state(Gst.State.PLAYING)
    src.emit("push-buffer", Gst.Buffer.new_wrapped(jpeg))
    src.emit("end-of-stream")
    sample = sink.emit("try-pull-sample", 5 * Gst.SECOND)
    pipe.set_state(Gst.State.NULL)
    assert sample is not None, "the JPEG did not decode"
    s = sample.get_caps().get_structure(0)
    return s.get_value("width"), s.get_value("height"), s.get_string("format")


# ---- encoder -------------------------------------------------------------------------------
def test_pure_helpers():
    assert pad_rows(b"abc" * 2, 3, 2, 1) == b"abc\x00abc\x00"
    assert pad_rows(b"abcd" * 2, 4, 2, 1) == b"abcd" * 2              # already aligned
    assert pad_rows(b"abc\x00" * 2, 3, 2, 1) == b"abc\x00" * 2        # already padded
    assert scaled_size(1920, 1080, 640) == (640, 360)
    assert scaled_size(640, 480, 0) == (640, 480) and scaled_size(320, 240, 640) == (320, 240)
    import numpy as np
    ramp = np.array([[1000, 2000], [3000, 65535]], dtype="<u2").tobytes()
    assert to_8bit(ramp, "GRAY16_LE", 2, 2, "shift") == bytes([3, 7, 11, 255])
    stretched = to_8bit(ramp, "GRAY16_LE", 2, 2, "stretch")
    assert stretched[0] == 0 and stretched[3] == 255
    flat = np.full((2, 2), 500, dtype=">u2").tobytes()
    assert to_8bit(flat, "GRAY16_BE", 2, 2) == bytes(4)
    # fixed: the same raw value maps to the same grey in every frame, whatever else is in it
    frame_a = np.array([[29000, 30000], [31000, 29000]], dtype="<u2").tobytes()
    frame_b = np.array([[29000, 40000], [20000, 29000]], dtype="<u2").tobytes()
    a = to_8bit(frame_a, "GRAY16_LE", 2, 2, "fixed", (28000, 32000))
    b = to_8bit(frame_b, "GRAY16_LE", 2, 2, "fixed", (28000, 32000))
    assert a[0] == b[0] == 64 and b[1] == 255 and b[2] == 0
    with pytest.raises(ValueError, match="mono16_range"):
        to_8bit(frame_a, "GRAY16_LE", 2, 2, "fixed")
    with pytest.raises(ValueError, match="mono16_range"):
        JpegEncoder(mono16="fixed")


@pytest.mark.parametrize("fmt, w, h, nbytes, bayer, expect_fmt", [
    ("GRAY8", 64, 48, 64 * 48, None, "GRAY8"),
    ("GRAY8", 65, 49, 65 * 49, None, "GRAY8"),                     # odd rows get padded
    ("GRAY16_LE", 32, 24, 32 * 24 * 2, None, "GRAY8"),
    ("GRAY16_BE", 32, 24, 32 * 24 * 2, None, "GRAY8"),
    ("GRAY8", 64, 48, 64 * 48, "rggb", None),                      # demosaiced to colour
    ("GRAY16_LE", 64, 48, 64 * 48 * 2, "bggr", None),
    ("RGB", 33, 21, 33 * 21 * 3, None, None),
    ("I420", 64, 48, 64 * 48 * 3 // 2, None, None),
])
def test_every_input_shape_encodes_to_a_decodable_jpeg(fmt, w, h, nbytes, bayer, expect_fmt):
    enc = JpegEncoder(quality=80)
    try:
        out = enc.encode(_frame(fmt, w, h, bytes(range(256)) * (nbytes // 256) + bytes(nbytes % 256), bayer))
        assert out.jpeg[:2] == b"\xFF\xD8" and (out.width, out.height) == (w, h)
        dw, dh, dfmt = _decode(out.jpeg)
        assert (dw, dh) == (w, h)
        if expect_fmt == "GRAY8":
            assert dfmt == "GRAY8"
        elif bayer:
            assert dfmt != "GRAY8"
    finally:
        enc.close()


def test_downscale_and_pipeline_reuse():
    enc = JpegEncoder(quality=70, max_width=32)
    f = _frame("GRAY8", 128, 96, bytes(128 * 96))
    a, b = enc.encode(f), enc.encode(f)
    assert (a.width, a.height) == (b.width, b.height) == (32, 24) and len(enc._pipes) == 1
    assert _decode(a.jpeg)[:2] == (32, 24)
    enc.close()


def test_bad_settings_and_bad_frames_are_refused():
    with pytest.raises(ValueError, match="quality"):
        JpegEncoder(quality=0)
    with pytest.raises(ValueError, match="max_width"):
        JpegEncoder(max_width=-1)
    with pytest.raises(ValueError, match="mono16"):
        JpegEncoder(mono16="log")
    fixed = JpegEncoder(mono16="fixed", mono16_range=(28000, 32000))
    assert _decode(fixed.encode(_frame("GRAY16_LE", 8, 8, bytes(128))).jpeg)[:2] == (8, 8)
    fixed.close()
    enc = JpegEncoder()
    with pytest.raises(EncodeError, match="mosaic"):
        enc.encode(_frame("RGB", 4, 4, bytes(48), "rggb"))
    with pytest.raises(EncodeError, match="truncated"):
        enc.encode(_frame("GRAY8", 64, 48, b"\x00" * 10))           # far too short for its caps
    enc.close()


# ---- encoder backends ----------------------------------------------------------------------
# nvjpegenc only exists on a Jetson, so the hardware PATH (I420 input, even-side crop, the probe, the
# runtime fallback) is exercised with jpegenc standing in for it via the injectable element name.
HW_STANDIN = "jpegenc"


def test_cpu_is_used_when_asked_or_when_the_hardware_element_is_missing():
    cpu = JpegEncoder(encoder="cpu", hw_element=HW_STANDIN)
    assert not cpu.hardware and cpu.backend == CPU_ELEMENT and cpu.fallback is None
    auto = JpegEncoder(encoder="auto", hw_element="nvjpegenc-not-here")
    assert not auto.hardware and auto.fallback is None          # auto: no hardware is not a problem
    hw = JpegEncoder(encoder="hw", hw_element="nvjpegenc-not-here")
    assert not hw.hardware and "not available" in hw.fallback   # hw: say why it is not used
    assert hw.describe() == {"backend": CPU_ELEMENT, "hardware": False, "requested": "hw",
                             "fallback": hw.fallback, "quality": 90, "max_width": 0}
    for e in (cpu, auto, hw):
        e.close()
    with pytest.raises(ValueError, match="jpeg_encoder"):
        JpegEncoder(encoder="gpu")


def test_a_hardware_element_that_fails_the_probe_is_not_used():
    enc = JpegEncoder(encoder="auto", hw_element="identity")     # exists, but is no JPEG encoder
    assert not enc.hardware and "probe failed" in enc.fallback
    assert _decode(enc.encode(_frame("GRAY8", 16, 16, bytes(256))).jpeg)[:2] == (16, 16)
    enc.close()


@pytest.mark.parametrize("fmt, w, h, nbytes, bayer, out", [
    ("GRAY8", 64, 48, 64 * 48, None, (64, 48)),
    ("GRAY8", 65, 49, 65 * 49, None, (64, 48)),                 # odd sides cropped, not resampled
    ("GRAY16_LE", 32, 24, 32 * 24 * 2, None, (32, 24)),
    ("GRAY8", 64, 48, 64 * 48, "rggb", (64, 48)),
    ("RGB", 33, 21, 33 * 21 * 3, None, (32, 20)),
    ("NV12", 64, 48, 64 * 48 * 3 // 2, None, (64, 48)),
])
def test_the_hardware_path_feeds_even_sided_i420(fmt, w, h, nbytes, bayer, out):
    enc = JpegEncoder(encoder="hw", hw_element=HW_STANDIN)
    try:
        assert enc.hardware and enc.backend == HW_STANDIN
        res = enc.encode(_frame(fmt, w, h, bytes(nbytes), bayer))
        assert (res.width, res.height) == out
        dw, dh, dfmt = _decode(res.jpeg)
        assert (dw, dh) == out and dfmt == "I420"               # colour JPEG even for mono input
    finally:
        enc.close()


def test_the_hardware_path_downscales_to_even_sides():
    enc = JpegEncoder(encoder="hw", hw_element=HW_STANDIN, max_width=40)
    res = enc.encode(_frame("GRAY8", 129, 97, bytes(129 * 97)))
    assert (res.width, res.height) == (40, 30) and _decode(res.jpeg)[:2] == (40, 30)
    enc.close()


def test_a_runtime_hardware_failure_moves_to_the_cpu_for_good(monkeypatch):
    enc = JpegEncoder(encoder="auto", hw_element=HW_STANDIN)
    assert enc.hardware
    real = JpegEncoder._encode

    def broken_hw(self, inp):
        if self.hardware:
            raise EncodeError("NVJPG engine hung")
        return real(self, inp)

    monkeypatch.setattr(JpegEncoder, "_encode", broken_hw)
    res = enc.encode(_frame("GRAY8", 64, 48, bytes(64 * 48)))
    assert (res.width, res.height) == (64, 48) and not enc.hardware
    assert "failed at runtime: NVJPG engine hung" in enc.fallback
    assert all(not key[0] for key in enc._pipes)                # the hardware pipelines are gone
    enc.close()


def test_a_truncated_frame_is_refused_before_either_encoder_sees_it():
    enc = JpegEncoder(encoder="auto", hw_element=HW_STANDIN)
    with pytest.raises(EncodeError, match="truncated frame: 10 bytes for GRAY8 64x48"):
        enc.encode(_frame("GRAY8", 64, 48, b"\x00" * 10))
    assert enc.hardware and enc.fallback is None
    enc.close()


def test_a_frame_neither_encoder_can_take_does_not_switch_encoders(monkeypatch):
    enc = JpegEncoder(encoder="auto", hw_element=HW_STANDIN)

    def always_fails(self, inp):
        raise EncodeError("unencodable")

    monkeypatch.setattr(JpegEncoder, "_encode", always_fails)
    with pytest.raises(EncodeError, match="unencodable"):
        enc.encode(_frame("GRAY8", 64, 48, bytes(64 * 48)))
    assert enc.hardware and enc.fallback is None                # the frame's fault, not the encoder's
    enc.close()


# ---- reader: pure parsing ------------------------------------------------------------------
def test_header_and_caps_parsing():
    hdr = transport.FrameHeader(timestamp_ns=123, frame_id=7, width=4, height=2, pixfmt="GRAY8",
                                ts_source="system").pack()
    d = from_header_bytes(hdr, b"\x01" * 8, "rggb", 99)
    assert (d.frame_id, d.stamp_ns, d.source, d.fmt, d.bayer, d.arrival_ns) == (7, 123, "system", "GRAY8", "rggb", 99)
    rgb = transport.FrameHeader(timestamp_ns=1, frame_id=1, width=1, height=1, pixfmt="RGB").pack()
    assert from_header_bytes(rgb, b"\x00" * 3, "rggb", 0).bayer is None   # colour is never a mosaic
    d = from_caps("video/x-bayer", "gbrg", 4, 2, 5, 456, b"", None, 1)
    assert (d.fmt, d.bayer, d.source) == ("GRAY8", "gbrg", None)
    assert from_caps("video/x-raw", "GRAY16_LE", 4, 2, 5, 456, b"", "bggr", 1).bayer == "bggr"
    assert from_caps("video/x-raw", "GRAY8", 4, 2, 5, 456, b"", "bggr", 1).bayer is None
    with pytest.raises(transport.TransportError):
        from_caps("video/x-bayer", "rggb16le", 4, 2, 5, 456, b"", None, 1)


def test_endpoint_selection_prefers_unixfd():
    with tempfile.TemporaryDirectory() as d:
        frames = os.path.join(d, "frames")
        assert select_endpoint(frames) == (None, None)
        open(frames, "w").close()
        assert select_endpoint(frames) == ("shm", frames)
        open(unixfd_socket(frames), "w").close()
        assert select_endpoint(frames) == ("unixfd", os.path.join(d, "unixfd"))
        assert select_endpoint(frames, "shm") == ("shm", frames)
    with pytest.raises(ValueError, match="transport"):
        TransportReader("/tmp/x", lambda d: None, prefer="tcp")


# ---- reader: live producers ----------------------------------------------------------------
class _Producer:
    """A stand-in for the core's plugin endpoint, pushing frames on a thread until stopped."""

    def __init__(self, kind, path, w=8, h=4):
        self.kind, self.w, self.h = kind, w, h
        if kind == "shm":
            desc = (f"appsrc name=src is-live=true format=time caps={transport.CAPS} ! "
                    f"shmsink socket-path={path} shm-size=1000000 wait-for-connection=false sync=false")
        else:
            desc = (f'appsrc name=src is-live=true format=time caps="video/x-raw,format=GRAY8,width={w},'
                    f'height={h},framerate=0/1" ! unixfdsink socket-path={path} sync=false')
        self.pipe = Gst.parse_launch(desc)
        self.src = self.pipe.get_by_name("src")
        self.pipe.set_state(Gst.State.PLAYING)
        self._stop = threading.Event()
        self.fid = 0
        self._t = threading.Thread(target=self._run, daemon=True)
        self._t.start()

    def _buffer(self):
        self.fid += 1
        pixels = bytes([self.fid % 256]) * (self.w * self.h)
        stamp = time.time_ns() - 5_000_000
        if self.kind == "shm":
            hdr = transport.FrameHeader(timestamp_ns=stamp, frame_id=self.fid, width=self.w, height=self.h,
                                        pixfmt="GRAY8", ts_source="ptp_chunk").pack()
            buf = Gst.Buffer.new_wrapped(hdr + pixels)
        else:
            gi.require_version("GstAllocators", "1.0")
            from gi.repository import GstAllocators
            fd = os.memfd_create("cam-test", 0)
            os.ftruncate(fd, len(pixels))
            os.pwrite(fd, pixels, 0)
            mem = GstAllocators.FdAllocator.alloc(GstAllocators.FdAllocator.new(), fd, len(pixels),
                                                  GstAllocators.FdMemoryFlags.NONE)
            buf = Gst.Buffer.new()
            buf.insert_memory(-1, mem)
            buf.offset = self.fid
            buf.offset_end = stamp
        buf.pts = self.fid * Gst.MSECOND
        return buf

    def _run(self):
        while not self._stop.is_set():
            self.src.emit("push-buffer", self._buffer())
            time.sleep(0.02)

    def stop(self):
        self._stop.set()
        self._t.join(2)
        self.pipe.set_state(Gst.State.NULL)


def _wait(cond, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


def _read_from(kind):
    with tempfile.TemporaryDirectory() as d:
        frames_sock = os.path.join(d, "frames")
        path = frames_sock if kind == "shm" else unixfd_socket(frames_sock)
        got = []
        prod = _Producer(kind, path)
        reader = TransportReader(frames_sock, got.append, prefer=kind, bayer_hint="rggb", retry_s=0.2)
        reader.start()
        try:
            assert _wait(lambda: len(got) >= 5), f"{kind}: no frames read ({reader.describe()})"
            d0 = got[-1]
            # The JP6 header cannot say "mosaic", so the config's hint applies; on unixfd an 8-bit
            # mosaic is always advertised as video/x-bayer, so a GRAY8 plane there is plain mono.
            assert (d0.width, d0.height, d0.fmt) == (8, 4, "GRAY8")
            assert d0.bayer == ("rggb" if kind == "shm" else None)
            assert len(d0.data) == 32 and d0.data == bytes([d0.frame_id % 256]) * 32
            assert 0 < d0.arrival_ns - d0.stamp_ns < 5_000_000_000
            assert d0.source == ("ptp_chunk" if kind == "shm" else None)
            assert reader.describe()["transport"] == kind and reader.connected
            # The core restarting: the endpoint goes away and comes back; the reader re-dials.
            prod.stop()
            if os.path.exists(path):
                os.unlink(path)
            assert _wait(lambda: not reader.connected), f"{kind}: the reader never noticed the producer leave"
            before = len(got)
            prod = _Producer(kind, path)
            assert _wait(lambda: len(got) > before + 3), f"{kind}: no frames after the producer came back"
        finally:
            reader.stop()
            prod.stop()


def test_reader_on_the_shm_header_endpoint():
    _read_from("shm")


def test_reader_on_the_unixfd_endpoint_where_the_runtime_has_it():
    if HAVE_UNIXFD:
        _read_from("unixfd")
    else:   # GStreamer < 1.24 (the legacy runtime): the core publishes shm only, and so do we
        assert Gst.version() < (1, 24, 0, 0)
