"""The NV12 feed of the hardware lossless recorder, without the hardware.

What can be pinned off-Jetson: the fragment the recorder builds for each feed, the choice between
them (the proof decides, and anything short of a pass keeps the old path), the buffer the feeder
builds -- pushed through a REAL recording session into a lossless software encoder that reads NV12 the
way GStreamer lays it out, then decoded with the service's read path -- and the proof's own verdicts,
driven with software stand-ins for the hardware chain. What the VIC does to the plane is the one
thing only a Jetson can answer; that is what the probe is for (tools/nvenc_lossless_test.py --feed).

Run in cam-dev.
"""
import json
import os
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import gi
gi.require_version("Gst", "1.0")
from gi.repository import Gst
import cam_driver.recorder as rec
from cam_driver.config import RecordingConfig, parse_config
from cam_driver.formats import LUMA_COLORIMETRY
from cam_driver.lifecycle import Lifecycle
from cam_driver.pipeline import CapturePipeline
from cam_driver.sources.replay import raw_decode_chain
from cam_driver.timestamps import FrameStamp, TimestampSource
from test_recorder import _FakeGst, _JETSON, _DEV
from test_recording_h264 import collect

Gst.init(None)
W, H, IV = 64, 48, 40_000_000
T0 = 1_788_000_000_000_000_000
_REAL_GST = rec.Gst


def teardown_function():
    rec.Gst = _REAL_GST
    rec._probe_verdicts.clear()


def _fragment(present, feed, encoder="auto", bits=8, is_color=False, threads=4):
    rec.Gst = _FakeGst(present)
    try:
        return rec.build_recorder_description(RecordingConfig(encoder=encoder, videoconvert_threads=threads),
                                              bits, "/data/recordings/t", 25.0, is_color, None, luma_feed=feed)
    finally:
        rec.Gst = _REAL_GST


# ---- the fragment --------------------------------------------------------------------------------
def test_nv12_feed_starts_the_hardware_fragment_at_nvvidconv():
    desc, enc = _fragment(_JETSON, "nv12")
    assert enc == "hw-hevc-lossless"
    assert desc.startswith("queue max-size-buffers=12 name=rec_q ! nvvidconv ! video/x-raw(memory:NVMM),format=NV24 ! "
                           "nvv4l2h265enc enable-lossless=1")
    assert "videoconvert" not in desc


def test_the_two_feeds_share_everything_after_the_conversion():
    nv12, _ = _fragment(_JETSON, "nv12")
    conv, _ = _fragment(_JETSON, "videoconvert")
    assert conv.replace(f"videoconvert n-threads=1 ! video/x-raw,format=NV24,colorimetry={LUMA_COLORIMETRY} ! ", "") == nv12


def test_nv12_feed_never_applies_to_another_encoder_or_to_colour():
    for kwargs in ({"present": _DEV}, {"present": _JETSON, "encoder": "ffv1"},
                   {"present": _JETSON, "encoder": "x265-lossless"}, {"present": _JETSON, "encoder": "x264"},
                   {"present": _JETSON, "encoder": "hw-hevc-lossless", "is_color": True},
                   {"present": _JETSON, "bits": 16}):
        assert _fragment(feed="nv12", **kwargs) == _fragment(feed="videoconvert", **kwargs), kwargs


def test_the_luma_copy_runs_on_one_thread_and_a_real_conversion_keeps_the_knob():
    # mono/Bayer into its full-range container is a copy: more threads only cost CPU (measured)
    assert "videoconvert n-threads=1 ! video/x-raw,format=NV24" in _fragment(_JETSON, "videoconvert", threads=6)[0]
    assert "videoconvert n-threads=1 ! video/x-raw,format=I420" in _fragment(_DEV, "videoconvert", "x265-lossless", threads=6)[0]
    # a real conversion is not
    assert "videoconvert n-threads=6 ! video/x-raw,format=I420 ! x264enc" in _fragment(
        _DEV | {"x264enc", "h264parse"}, "videoconvert", "x264", threads=6)[0]
    assert "videoconvert n-threads=6 ! video/x-raw,format=NV24 ! nvvidconv" in _fragment(
        _JETSON, "videoconvert", "hw-hevc-lossless", is_color=True, threads=6)[0]
    assert "videoconvert n-threads=6 ! x265enc" in _fragment(_DEV, "videoconvert", "x265-lossless", is_color=True, threads=6)[0]


def test_feed_names_geometry_and_caps():
    assert [rec.normalize_feed(v) for v in (None, "auto", " NV12 ", "videoconvert", "vic", 7)] == \
        ["auto", "auto", "nv12", "videoconvert", "auto", "auto"]
    assert rec.nv12_geometry_ok(2448, 2048) and rec.nv12_geometry_ok(1280, 512)
    for bad in ((1626, 1236), (642, 512), (640, 513), (0, 480), (640, 0)):
        assert not rec.nv12_geometry_ok(*bad), bad
    assert rec.nv12_feed_caps(640, 512, "60/1") == \
        f"video/x-raw,format=NV12,width=640,height=512,framerate=60/1,colorimetry={LUMA_COLORIMETRY}"


def test_chroma_block_completes_a_plane_into_an_nv12_frame():
    chroma = rec.ChromaBlock(W, H)
    assert chroma.size == W * H // 2
    plane = bytes(range(256)) * (W * H // 256)
    buf = Gst.Buffer.new_wrapped(plane)
    buf.pts, buf.offset = 123, 7
    buf = chroma.tail(buf)
    assert buf.n_memory() == 2 and buf.get_size() == W * H * 3 // 2
    assert buf.extract_dup(0, W * H) == plane and buf.extract_dup(W * H, chroma.size) == bytes([128]) * chroma.size
    assert buf.pts == 123 and buf.offset == 7
    # every frame gets the same block: handing it out again neither consumes nor disturbs it
    again = chroma.tail(Gst.Buffer.new_wrapped(bytes(W * H)))
    del buf
    assert again.n_memory() == 2 and again.extract_dup(W * H, chroma.size) == bytes([128]) * chroma.size


# ---- the choice ----------------------------------------------------------------------------------
class _Source:
    encoded_caps = encoded_parser = None
    delivered_frame_rate = 25
    finite = True
    active_timestamp_source = "system"
    ptp_locked = False
    tick_frequency_hz = 0

    def __init__(self, pixel_format="Mono8", width=W, height=H):
        self._pf, self._w, self._h = pixel_format, width, height

    def geometry(self): return 0, 0, self._w, self._h
    def pixel_format(self): return self._pf
    def take_discontinuity(self): return False


def _pipe(tmp, *, recording=None, source=None, verdict=True, recorder=None):
    """A real CapturePipeline whose recorder resolves to the hardware encoder by decree (the registry
    of a dev host would turn it into ffv1) and whose proof answers `verdict`."""
    cfg = parse_config({"recording": {"encoder": "auto", "output_dir": str(tmp), **(recording or {})},
                        "transport": {"plugin_endpoint": {"enabled": False}, "raw_endpoint": {"enabled": False}}})
    p = CapturePipeline(cfg, source or _Source())
    probes = []
    p._nv12_probe = lambda w, h, c: probes.append((w, h)) or verdict
    p._recorder_desc = recorder or (lambda rcfg, bits, loc, fps, color, parser, luma_feed="videoconvert": (
        "fakesink name=rec_sink sync=false", "hw-hevc-lossless" if bits <= 8 and not color else "ffv1"))
    p.build()
    return p, probes


def test_auto_feeds_nv12_only_on_a_proven_host():
    for verdict, feed in ((True, "nv12"), (False, "videoconvert"), (None, "videoconvert")):
        with tempfile.TemporaryDirectory() as tmp:
            p, probes = _pipe(tmp, verdict=verdict)
            assert p._rec_feed == feed and probes == [(W, H)], (verdict, p._rec_feed, probes)
            assert p.recording_settings()["resolved"]["nvenc_feed"] == feed
            assert (p._rec_chroma is not None) == (feed == "nv12")
            assert ("format=NV12" in p._session_desc_probe) == (feed == "nv12")


def test_the_knob_overrides_the_probe_in_both_directions():
    with tempfile.TemporaryDirectory() as tmp:
        p, probes = _pipe(tmp, recording={"nvenc_feed": "videoconvert"}, verdict=True)
        assert p._rec_feed == "videoconvert" and probes == []
        p, probes = _pipe(tmp, recording={"nvenc_feed": "nv12"}, verdict=False)
        assert p._rec_feed == "nv12" and probes == []


def test_geometry_gstreamer_would_pad_keeps_the_old_feed():
    for width, height in ((W + 2, H), (W, H + 1)):
        with tempfile.TemporaryDirectory() as tmp:
            p, probes = _pipe(tmp, recording={"nvenc_feed": "nv12"}, source=_Source(width=width, height=height))
            assert p._rec_feed == "videoconvert" and probes == [], (width, height)


def test_the_question_does_not_arise_for_other_recorders():
    for source, recording in ((_Source("NV12"), {}), (_Source("Mono16"), {}), (_Source(), {"encoder": "ffv1"})):
        with tempfile.TemporaryDirectory() as tmp:
            recorder = lambda rcfg, bits, loc, fps, color, parser, luma_feed="videoconvert": (   # noqa: E731
                "fakesink name=rec_sink sync=false", "ffv1")
            p, probes = _pipe(tmp, recording=recording, source=source, recorder=recorder)
            assert p._rec_feed is None and p._rec_chroma is None and probes == []
            assert "format=NV12" not in p._session_desc_probe or source.pixel_format() == "NV12"
            assert p.recording_settings()["resolved"]["nvenc_feed"] is None


def test_session_pipeline_takes_nv12_caps_and_the_larger_frame():
    with tempfile.TemporaryDirectory() as tmp:
        p, _ = _pipe(tmp)
        desc = p._session_desc_probe
        assert f'caps="{rec.nv12_feed_caps(W, H, "25/1")}"' in desc
        assert f"max-bytes={32 * (W * H * 3 // 2)} " in desc
        # a candidate plan is dry-run with ITS feed, not the installed one
        assert "format=GRAY8" in p._session_desc("fakesink", "videoconvert")
        assert "format=NV12" in p._session_desc("fakesink", "nv12")


# ---- the buffer, through a real session ------------------------------------------------------------
def _software_nv12_recorder(rcfg, bits, loc, fps, color, parser, luma_feed="videoconvert"):
    """Stands in for the hardware fragment: a lossless 4:4:4 encode that reads its NV12 input through
    GStreamer's own layout, so a misplaced chroma block or a shifted plane cannot come back exact."""
    head = "queue name=rec_q ! " if luma_feed == "nv12" else \
        f"queue name=rec_q ! videoconvert ! video/x-raw,format=NV24,colorimetry={LUMA_COLORIMETRY} ! "
    return (head + f"videoconvert ! video/x-raw,format=Y444,colorimetry={LUMA_COLORIMETRY} ! "
            'x265enc option-string="lossless=1:bframes=0" speed-preset=ultrafast ! h265parse ! '
            + rec._splitmux(loc, 3600)), "hw-hevc-lossless"


def _record(tmp, recording, frames, pixel_format="BayerRG8"):
    p, _ = _pipe(tmp, recording=recording, source=_Source(pixel_format), recorder=_software_nv12_recorder)
    lc = Lifecycle(p, recording_enabled=True)
    p.pipeline.set_state(Gst.State.PLAYING)
    try:
        assert lc.request("activate")["ok"]
        sess = p._session
        for i, data in enumerate(frames):
            ts = T0 + i * IV
            p._on_frame(FrameStamp(i, ts, TimestampSource.SYSTEM, ts, ts, None), data)
        r = lc.request("deactivate")
        assert r["ok"] and r["session"]["frames"] == len(frames) and not r["session"]["error"], r
        header = json.loads(Path(sess.path_base + ".json").read_text())
        decode = raw_decode_chain("video/x-h265", header)
        decoded = [data for part in r["session"]["files"] for _, data in collect(
            f'filesrc location="{part}" ! matroskademux ! {decode} ! videoconvert ! '
            f"video/x-raw,format=GRAY8,width={W},height={H}")]
        assert p.drops.enqueue_failures == 0
        return header, decoded
    finally:
        p.pipeline.set_state(Gst.State.NULL)


def test_a_plane_fed_as_nv12_is_recorded_exactly():
    frames = [bytes((7 * x + 13 * y + 29 * i) % 256 for y in range(H) for x in range(W)) for i in range(12)]
    frames.append(bytes(range(256)) * (W * H // 256))      # every code
    with tempfile.TemporaryDirectory() as tmp:
        header, decoded = _record(tmp, {}, frames)
        assert header["recording_feed"] == "nv12" and header["recording_encoder"] == "hw-hevc-lossless"
        assert header["recording_colorimetry"] == LUMA_COLORIMETRY and header["cfa_tile_mode"] == "off"
        assert decoded == frames


def test_cfa_tiling_still_applies_in_front_of_the_nv12_feed():
    from cam_driver.bayer_tile import untile_cfa
    frames = [bytes((25 * (x % 2) + 55 * (y % 2) + x + y + i * 3) % 256 for y in range(H) for x in range(W))
              for i in range(6)]
    with tempfile.TemporaryDirectory() as tmp:
        header, decoded = _record(tmp, {"bayer_tile": "plain"}, frames)
        assert header["recording_feed"] == "nv12" and header["cfa_tile_mode"] == "plain"
        assert decoded != frames
        assert [untile_cfa(d, W, H, "plain", header["bayer_pattern"]) for d in decoded] == frames


def test_a_frame_of_the_wrong_size_ends_the_session_instead_of_shifting_the_plane():
    with tempfile.TemporaryDirectory() as tmp:
        p, _ = _pipe(tmp, recorder=_software_nv12_recorder)
        lc = Lifecycle(p, recording_enabled=True)
        p.pipeline.set_state(Gst.State.PLAYING)
        try:
            assert lc.request("activate")["ok"]
            sess = p._session
            p._on_frame(FrameStamp(0, T0, TimestampSource.SYSTEM, T0, T0, None), bytes(W * H))
            p._on_frame(FrameStamp(1, T0 + IV, TimestampSource.SYSTEM, T0, T0, None), bytes(W * H + 4))
            assert sess.frames == 1 and "refusing to record a misplaced plane" in sess.error
            assert p.drops.enqueue_failures == 1
        finally:
            if p._session is not None:
                p.deactivate(wait_eos=False)
            p.pipeline.set_state(Gst.State.NULL)


# ---- the proof -----------------------------------------------------------------------------------
_EXACT = (f"videoconvert ! video/x-raw,format=Y444,colorimetry={LUMA_COLORIMETRY} ! "
          'x265enc option-string="lossless=1:bframes=0" speed-preset=ultrafast ! h265parse')
# what a wrong range assumption does to the plane: a lossless codec behind a limited-range squeeze
_SQUEEZED = ("videoconvert ! video/x-raw,format=Y444,colorimetry=bt709 ! "
             'x265enc option-string="lossless=1:bframes=0" speed-preset=ultrafast ! h265parse')


def _probe(monkeypatch, chain, width=W, height=H, **kw):
    monkeypatch.setattr(rec, "_PROBE_ELEMENTS", ("x265enc", "h265parse", "avdec_h265", "capssetter"))
    monkeypatch.setattr(rec, "_hw_hevc_lossless", lambda cfg, gop, bframes: chain)
    return rec.probe_nv12_feed(width, height, **kw)


def test_probe_passes_an_exact_path_and_remembers_it(monkeypatch):
    assert _probe(monkeypatch, _EXACT) is True
    assert rec._probe_verdicts == {(W, H): True}
    assert _probe(monkeypatch, "this ! would ! not ! parse") is True     # not run again


def test_probe_fails_a_path_that_rescales_the_plane(monkeypatch):
    assert _probe(monkeypatch, _SQUEEZED) is False
    assert rec._probe_verdicts == {(W, H): False}


def test_probe_is_inconclusive_when_it_cannot_run(monkeypatch):
    assert _probe(monkeypatch, "identity ! no_such_element_here") is None      # does not even parse
    # frames go in, nothing comes back: a timeout is not a verdict
    assert _probe(monkeypatch, "fakesink name=swallow async=false   appsrc name=never ! h265parse", timeout_s=0.5) is None
    assert _probe(monkeypatch, _EXACT, width=W + 2) is None                    # geometry the feed cannot carry
    monkeypatch.setattr(rec, "_PROBE_ELEMENTS", ("no_such_element_here",))
    assert rec.probe_nv12_feed(W, H) is None                                   # no encoder on this host
    assert rec._probe_verdicts == {}
