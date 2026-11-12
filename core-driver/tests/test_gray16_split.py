"""A 16-bit source stored through the 8-bit lossless recorder (recording.gray16_split).

The transform is checked over every 16-bit value in both byte orders. The plan is checked for the one
rule that matters -- the split only ever sits in front of a lossless 8-bit encoder that is either
software or PROVEN on the host -- and everything short of that records the 16-bit frame as before.
Then the whole way round with real GStreamer: recorded split (x265-lossless here, and through the
NV12 feed with a software stand-in for the hardware fragment), read back by the replay source and
by the exporter's decode, compared with what the camera delivered.

Run in cam-dev.
"""
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import gi
gi.require_version("Gst", "1.0")
from gi.repository import Gst
import numpy as np
from cam_driver import export_recordings, gray16_split, playback
from cam_driver.config import parse_config
from cam_driver.formats import FFV1_DECODER, LUMA_COLORIMETRY
from cam_driver.lifecycle import Lifecycle
from cam_driver.pipeline import CapturePipeline
from cam_driver.sources.replay import raw_decode_chain
from cam_driver.timestamps import FrameStamp, TimestampSource
from test_nv12_feed import _Source, _software_nv12_recorder
from test_recording_h264 import collect
from test_replay_source import _source, _pump

Gst.init(None)
W, H, IV = 64, 48, 40_000_000
T0 = 1_788_000_000_000_000_000


# ---- the transform -------------------------------------------------------------------------------
@pytest.mark.parametrize("big_endian", [False, True])
def test_split_and_join_are_exact_inverses_for_every_sample(big_endian):
    values = np.arange(65536, dtype=">u2" if big_endian else "<u2").reshape(256, 256)
    frame = values.tobytes()
    stored = gray16_split.split(frame, 256, 256, big_endian)
    assert len(stored) == len(frame)
    assert gray16_split.join(stored, 256, 256, big_endian) == frame
    a = np.frombuffer(stored, np.uint8).reshape(256, 512)
    hi, lo = a[:, :256], a[:, 256:]
    assert np.array_equal(hi, values >> 8)                                   # the high half is verbatim
    assert np.array_equal(lo, np.where(hi & 1, 255 - (values & 0xFF), values & 0xFF))
    # the point of the fold: along a ramp the low half never jumps, where the plain low byte drops
    # 255 -> 0 at every carry
    ramp = np.frombuffer(gray16_split.split(values.tobytes(), 65536, 1, big_endian), np.uint8)[65536:].astype(int)
    assert np.abs(np.diff(ramp)).max() == 1
    assert np.abs(np.diff((values.reshape(-1) & 0xFF).astype(int))).max() == 255


def test_random_frames_round_trip_and_the_two_byte_orders_store_the_same_picture():
    rng = np.random.default_rng(5)
    samples = rng.integers(0, 65536, (H, W), dtype=np.uint16)
    le, be = samples.astype("<u2").tobytes(), samples.astype(">u2").tobytes()
    assert gray16_split.split(le, W, H) == gray16_split.split(be, W, H, True)
    assert gray16_split.join(gray16_split.split(le, W, H), W, H) == le
    assert gray16_split.join(gray16_split.split(be, W, H, True), W, H, True) == be


def test_a_frame_of_the_wrong_size_is_refused():
    for fn in (gray16_split.split, gray16_split.join):
        with pytest.raises(ValueError):
            fn(bytes(W * H * 2 - 2), W, H)


def test_mode_names():
    assert [gray16_split.normalize_mode(v) for v in (None, False, "off", "", "fold", " FOLD ", True, "on", "plain", 3)] == \
        ["off", "off", "off", "off", "fold", "fold", "fold", "fold", "off", "off"]
    # a sidecar is not a wish: an unknown stored layout cannot be read as anything
    assert [gray16_split.stored_mode(v) for v in (None, "", "off", False, "fold")] == ["off"] * 4 + ["fold"]
    for unknown in ("plain", "planes", True, 1):
        with pytest.raises(ValueError):
            gray16_split.stored_mode(unknown)
    assert gray16_split.geometry_ok(640, 512) and not gray16_split.geometry_ok(641, 512)
    assert not gray16_split.geometry_ok(640, 511) and not gray16_split.geometry_ok(0, 512)


# ---- the plan ------------------------------------------------------------------------------------
def _pipe(tmp, *, recording=None, source=None, verdict=True, present=("hw-hevc-lossless",)):
    """A real CapturePipeline on a host that has the encoders in `present` (by decree: the recorder
    description is stubbed), whose NV12 proof answers `verdict`."""
    cfg = parse_config({"recording": {"encoder": "auto", "gray16_split": "fold", "output_dir": str(tmp),
                                      **(recording or {})},
                        "transport": {"plugin_endpoint": {"enabled": False}, "raw_endpoint": {"enabled": False}}})
    p = CapturePipeline(cfg, source or _Source("Mono16"))
    probes = []
    p._nv12_probe = lambda w, h, c: probes.append((w, h)) or verdict

    def recorder(rcfg, bits, loc, fps, color, parser, luma_feed="videoconvert"):
        want = rcfg.encoder if rcfg.encoder != "auto" else ("hw-hevc-lossless" if bits <= 8 else "ffv1")
        if want in ("hw-hevc-lossless", "x265-lossless", "x264") and bits > 8:
            want = "ffv1"                                   # the recorder's own depth guard
        if want not in present + ("ffv1",):
            want = "ffv1"
        return "fakesink name=rec_sink sync=false", want

    p._recorder_desc = recorder
    p.build()
    return p, probes


def test_split_rides_the_hardware_encoder_behind_a_proven_nv12_feed():
    with tempfile.TemporaryDirectory() as tmp:
        p, probes = _pipe(tmp)
        assert (p._rec_enc, p._split_mode, p._rec_feed) == ("hw-hevc-lossless", "fold", "nv12")
        assert probes == [(2 * W, H)]                       # proven at the geometry it will run at
        assert f"format=NV12,width={2 * W},height={H}" in p._session_desc_probe
        assert f"max-bytes={32 * (2 * W * H * 3 // 2)} " in p._session_desc_probe
        resolved = p.recording_settings()["resolved"]
        assert resolved["gray16_split"] == "fold" and resolved["nvenc_feed"] == "nv12" and resolved["encoder"] == "hw-hevc-lossless"


@pytest.mark.parametrize("why, kwargs", [
    ("the feed is not proven here", {"verdict": False}),
    ("the proof could not run", {"verdict": None}),
    ("the feed is switched off", {"recording": {"nvenc_feed": "videoconvert"}}),
    ("the feed is taken on trust, which the split never does", {"recording": {"nvenc_feed": "nv12"}, "verdict": False}),
    ("the feed is taken on trust and the proof could not run", {"recording": {"nvenc_feed": "nv12"}, "verdict": None}),
    ("no hardware encoder on this host", {"present": ()}),
    ("ffv1 was asked for", {"recording": {"encoder": "ffv1"}}),
    ("a lossy encoder was asked for", {"recording": {"encoder": "x264"}, "present": ("x264",)}),
    ("the knob is off", {"recording": {"gray16_split": "off"}}),
    ("odd width", {"source": _Source("Mono16", width=W + 1)}),
    ("odd height", {"source": _Source("Mono16", height=H + 1)}),
])
def test_anything_short_of_that_records_the_16_bit_frame_as_before(why, kwargs):
    with tempfile.TemporaryDirectory() as tmp:
        p, _ = _pipe(tmp, **kwargs)
        assert (p._rec_enc, p._split_mode, p._rec_feed, p._splitter) == ("ffv1", "off", None, None), why
        assert "format=GRAY16_LE" in p._session_desc_probe and f"width={p._width}," in p._session_desc_probe
        assert p.recording_settings()["resolved"]["gray16_split"] == "off"


def test_asking_for_the_nv12_feed_outright_does_not_waive_the_proof_for_the_split():
    """nvenc_feed: nv12 vouches for an 8-bit camera's geometry. The split runs at twice the width,
    where one wrong code is 256 counts: it is proven there, or it is not used."""
    with tempfile.TemporaryDirectory() as tmp:
        p, probes = _pipe(tmp, recording={"nvenc_feed": "nv12"})
        assert (p._rec_enc, p._split_mode, p._rec_feed) == ("hw-hevc-lossless", "fold", "nv12")
        assert probes == [(2 * W, H)]
    with tempfile.TemporaryDirectory() as tmp:               # a truthy answer that is not a pass
        p, _ = _pipe(tmp, recording={"nvenc_feed": "nv12"}, verdict="probably")
        assert (p._rec_enc, p._split_mode) == ("ffv1", "off")
    with tempfile.TemporaryDirectory() as tmp:               # switched off: the proof is not even run
        p, probes = _pipe(tmp, recording={"nvenc_feed": "videoconvert"})
        assert (p._rec_enc, p._split_mode) == ("ffv1", "off") and probes == []


def test_a_frame_the_split_cannot_take_is_a_counted_drop_not_an_exception():
    with tempfile.TemporaryDirectory() as tmp:
        p, _ = _pipe(tmp, recording={"encoder": "x265-lossless"}, present=("x265-lossless",))
        lc = Lifecycle(p, recording_enabled=True)
        p.pipeline.set_state(Gst.State.PLAYING)
        try:
            assert lc.request("activate")["ok"]
            sess = p._session
            stamp = lambda i: FrameStamp(i, T0 + i * IV, TimestampSource.SYSTEM, T0, T0, None)    # noqa: E731
            p._on_frame(stamp(0), bytes(W * H * 2))
            p._on_frame(stamp(1), bytes(W * H * 2 - 2))      # a short frame: the capture thread survives it
            p._on_frame(stamp(2), bytes(W * H * 2))
            assert sess.frames == 2 and not sess.error
            assert p.drops.enqueue_failures == 1
        finally:
            if p._session is not None:
                p.deactivate(wait_eos=False)
            p.pipeline.set_state(Gst.State.NULL)


def test_the_recorder_caps_take_the_frame_rate_the_pipeline_was_built_with():
    with tempfile.TemporaryDirectory() as tmp:
        p, _ = _pipe(tmp)
        assert p._framerate == "25/1" and p._raw_caps.endswith("framerate=25/1")
        assert f"format=NV12,width={2 * W},height={H},framerate=25/1" in p._rec_caps
        p._raw_caps += ",colorimetry=bt709"                  # whatever the source caps grow to carry
        caps, _, _ = p._feed_shape(SimpleNamespace(feed="nv12", split_mode="fold"))
        assert caps == p._rec_caps
        caps, frame_bytes, chroma = p._feed_shape(SimpleNamespace(feed=None, split_mode="fold"))
        assert caps == f"video/x-raw,format=GRAY8,width={2 * W},height={H},framerate=25/1"
        assert frame_bytes == 2 * W * H and chroma is None


def test_an_explicit_x265_lossless_takes_the_split_without_a_feed():
    with tempfile.TemporaryDirectory() as tmp:
        p, probes = _pipe(tmp, recording={"encoder": "x265-lossless"}, present=("x265-lossless",))
        assert (p._rec_enc, p._split_mode, p._rec_feed) == ("x265-lossless", "fold", None) and probes == []
        assert f"format=GRAY8,width={2 * W},height={H}" in p._session_desc_probe
        assert f"max-bytes={32 * 2 * W * H} " in p._session_desc_probe


def test_the_knob_says_nothing_about_8_bit_or_colour_sources():
    for pixel_format in ("Mono8", "BayerRG8", "NV12"):
        with tempfile.TemporaryDirectory() as tmp:
            p, _ = _pipe(tmp, source=_Source(pixel_format))
            assert p._split_mode == "off" and p._splitter is None, pixel_format


# ---- the whole way round ---------------------------------------------------------------------------
def _frames(n, dtype="<u2"):
    """Thermal-like: a smooth scene in the middle of the range, crossing many high-byte boundaries,
    plus noise; and one frame holding every 16-bit value this geometry can."""
    rng = np.random.default_rng(7)
    yy, xx = np.mgrid[0:H, 0:W]
    out = [np.clip(29500 + 40.0 * xx + 23.0 * yy + 400 * i + rng.normal(0, 6, (H, W)), 0, 65535).astype(dtype).tobytes()
           for i in range(n)]
    out.append((np.arange(W * H, dtype=np.uint32) * 21 % 65536).astype(dtype).tobytes())
    return out


def _record(tmp, recording, frames, pixel_format="Mono16", recorder=None):
    cfg = parse_config({"recording": {"gray16_split": "fold", "output_dir": str(tmp), **recording},
                        "transport": {"plugin_endpoint": {"enabled": False}, "raw_endpoint": {"enabled": False}}})
    p = CapturePipeline(cfg, _Source(pixel_format))
    p._nv12_probe = lambda w, h, c: True
    if recorder is not None:
        p._recorder_desc = recorder
    p.build()
    lc = Lifecycle(p, recording_enabled=True)
    p.pipeline.set_state(Gst.State.PLAYING)
    stamps = []
    try:
        assert lc.request("activate")["ok"]
        sess = p._session
        for i, data in enumerate(frames):
            ts = T0 + i * IV
            stamps.append(FrameStamp(i, ts, TimestampSource.SYSTEM, ts, ts, None))
            p._on_frame(stamps[-1], data)
        r = lc.request("deactivate")
        assert r["ok"] and r["session"]["frames"] == len(frames) and not r["session"]["error"], r
        assert p.drops.enqueue_failures == 0
        return json.loads(Path(sess.path_base + ".json").read_text()), r["session"]["files"], stamps
    finally:
        p.pipeline.set_state(Gst.State.NULL)


def _replayed(run_dir):
    src = _source(Path(run_dir))
    received = []
    try:
        src.start(lambda st, data: received.append((st, bytes(data), time.monotonic())))
        _pump(lambda: src.finished)
        assert not src.finished_error
        return src, [(st, data) for st, data, _ in received]
    finally:
        src.stop()


@pytest.mark.parametrize("pixel_format, dtype", [("Mono16", "<u2"), ("GRAY16_BE", ">u2")])
def test_split_recording_replays_and_exports_the_16_bit_frames(pixel_format, dtype):
    frames = _frames(10, dtype)
    with tempfile.TemporaryDirectory() as tmp:
        header, files, stamps = _record(tmp, {"encoder": "x265-lossless"}, frames, pixel_format)
        # the sidecar still describes the SOURCE frame; the split is the one extra thing a reader needs
        assert (header["pixel_format"], header["bits_per_pixel"], header["width"], header["height"]) == (pixel_format, 16, W, H)
        assert header["gray16_split_mode"] == "fold" and header["recording_encoder"] == "x265-lossless"
        assert header["recording_colorimetry"] == LUMA_COLORIMETRY and header["recording_lossy"] is False
        # what the track holds: one 8-bit frame, twice as wide
        stored = [d for part in files for _, d in collect(
            f'filesrc location="{part}" ! matroskademux ! {raw_decode_chain("video/x-h265", header)} ! videoconvert ! '
            f"video/x-raw,format=GRAY8,width={2 * W},height={H}")]
        assert [gray16_split.join(d, W, H, dtype == ">u2") for d in stored] == frames
        # replay hands consumers the 16-bit frame under its own stamp
        src, received = _replayed(tmp)
        assert src.pixel_format() == pixel_format and src.geometry()[2:] == (W, H)
        assert [d for _, d in received] == frames
        assert [st.timestamp_ns for st, _ in received] == [st.timestamp_ns for st in stamps]
        # ... and so does the exporter's decode
        run = playback.discover_sessions(tmp, Path(files[0]).name.rsplit("-", 1)[0])[0]
        samples = list(export_recordings._decode(run))
        assert [s.get_buffer().extract_dup(0, s.get_buffer().get_size()) for s in samples] == frames
        st = samples[0].get_caps().get_structure(0)
        fmt = "GRAY16_LE" if pixel_format == "Mono16" else pixel_format
        assert (st.get_string("format"), st.get_int("width")[1], st.get_int("height")[1]) == (fmt, W, H)


def test_split_through_the_nv12_feed_is_recorded_exactly():
    """The path a Jetson runs: the split frame plus the shared chroma block, into a lossless encode
    that reads NV12 through GStreamer's own layout (the stand-in for the hardware fragment)."""
    frames = _frames(8)
    with tempfile.TemporaryDirectory() as tmp:
        header, files, _ = _record(tmp, {"encoder": "auto"}, frames, recorder=_software_nv12_recorder)
        assert (header["recording_encoder"], header["recording_feed"], header["gray16_split_mode"]) == \
            ("hw-hevc-lossless", "nv12", "fold")
        _src, received = _replayed(tmp)
        assert [d for _, d in received] == frames


def test_a_run_can_change_between_split_and_ffv1_sessions():
    frames = _frames(5)
    with tempfile.TemporaryDirectory() as tmp:
        first, _, _ = _record(tmp, {"encoder": "x265-lossless"}, frames)
        time.sleep(1.1)                                     # run prefixes are stamped to the second
        second, files, _ = _record(tmp, {"encoder": "ffv1"}, frames)
        assert first["gray16_split_mode"] == "fold" and second["gray16_split_mode"] == "off"
        assert second["recording_encoder"] == "ffv1"
        assert [d for part in files for _, d in collect(
            f'filesrc location="{part}" ! matroskademux ! {FFV1_DECODER} ! videoconvert ! '
            f"video/x-raw,format=GRAY16_LE,width={W},height={H}")] == frames
        src = _source(Path(tmp), gap_max_s=0.01)
        received = []
        try:
            src.start(lambda st, data: received.append(bytes(data)))
            _pump(lambda: src.finished)
            assert received == frames + frames
        finally:
            src.stop()


def test_replay_refuses_a_split_it_cannot_undo():
    frames = _frames(3)
    with tempfile.TemporaryDirectory() as tmp:
        _record(tmp, {"encoder": "x265-lossless"}, frames)
        sidecar = next(p for p in Path(tmp).glob("*.json") if "recording-settings" not in p.name)
        header = json.loads(sidecar.read_text())
        sidecar.write_text(json.dumps({**header, "gray16_split_mode": "planes"}))
        with pytest.raises(ValueError, match="gray16_split_mode"):
            _source(Path(tmp))
        sidecar.write_text(json.dumps({**header, "pixel_format": "Mono8", "bits_per_pixel": 8}))
        with pytest.raises(ValueError, match="cannot have been stored that way"):
            _source(Path(tmp))
