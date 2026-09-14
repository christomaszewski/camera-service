"""Replay a run recorded through stop/configure/start with changing codecs and quality.

Real recorder and replay pipelines; no camera hardware or Zenoh router required.
Run in cam-dev, or as part of pytest.
"""
import json
import os
from pathlib import Path
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import gi
gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst
from cam_driver.bayer_tile import untile_cfa
from cam_driver.config import parse_config
from cam_driver.formats import parse_pixel_format
from cam_driver.gst_frame import GstFrame
from cam_driver.lifecycle import Lifecycle
from cam_driver.pipeline import CapturePipeline
from cam_driver.playback import load_stamps
from cam_driver.timestamps import FrameStamp, TimestampSource
from cam_driver.transport import HEADER_SIZE, unpack_header
from test_recording_h264 import collect
from test_recording_settings import Source, W, H, IV, version
from test_replay_source import _source, _pump

T0 = 1_788_000_000_000_000_000


def _record(root, pixel_format, changes, *, count=30, gap_frames=5, empty_between=False):
    source = Source()
    source.pixel_format = lambda: pixel_format
    cfg = parse_config({"recording": {"encoder": "ffv1", "output_dir": str(root)},
                        "preview": {"enabled": True, "sink": "appsink name=preview sync=false"},
                        "transport": {"plugin_endpoint": {"enabled": False}, "raw_endpoint": {"enabled": False}}})
    pipe = CapturePipeline(cfg, source)
    pipe.build()
    lc = Lifecycle(pipe, recording_enabled=True)
    pipe.pipeline.set_state(Gst.State.PLAYING)
    preview = pipe.pipeline.get_by_name("preview")
    fmt = parse_pixel_format(pixel_format)[0]
    caps = f"video/x-raw,format={fmt},width={W},height={H},framerate=25/1"
    frames = [data for _, data in collect(f"videotestsrc num-buffers={count} pattern=ball ! {caps}")]
    # Distinct CFA phases catch a stale un-tiler even when adjacent frames are very similar.
    if pixel_format.startswith("Bayer"):
        frames = [bytes((25 * (x % 2) + 55 * (y % 2) + x + y + i * 3) % 256
                        for y in range(H) for x in range(W)) for i in range(count)]
    expected, sessions = [], []
    fid = 0

    def feed(data):
        nonlocal fid
        ts = T0 + fid * IV
        stamp = FrameStamp(fid, ts, TimestampSource.SYSTEM, ts + 50, ts, None)
        pipe._on_frame(stamp, data)
        sample = preview.emit("try-pull-sample", 2 * Gst.SECOND)
        assert sample is not None
        buf = sample.get_buffer()
        assert buf.extract_dup(0, buf.get_size()) == data, "recording changes must not alter live preview"
        fid += 1
        return stamp

    try:
        for settings in changes:
            for _ in range(gap_frames):
                feed(frames[0])  # ingest continues while recording is inactive
            if empty_between:
                assert lc.request("activate")["ok"]
                empty = pipe._session
                r = lc.request("deactivate")
                assert r["ok"] and r["session"]["frames"] == 0 and not r["session"]["truncated"], r
                assert Path(empty.settings_path).is_file()
            r = lc.configure_recording({"expected": version(pipe), "settings": settings})
            assert r["ok"], r
            assert lc.request("activate", {"expected_recording_settings": version(pipe)})["ok"]
            sess = pipe._session
            stamps = [feed(data) for data in frames]
            r = lc.request("deactivate")
            assert r["ok"] and not r["session"]["truncated"] and r["session"]["frames"] == count, r
            header = json.loads(Path(sess.path_base + ".json").read_text())
            snapshot = json.loads(Path(sess.settings_path).read_text())
            assert all(snapshot["requested"][key] == value for key, value in settings.items())
            encoder = header["recording_encoder"]
            decoder = {"x264": "h264parse ! avdec_h264", "ffv1": "avdec_ffv1"}[encoder]
            decoded = [data for part in r["session"]["files"]
                       for _, data in collect(f'filesrc location="{part}" ! matroskademux ! {decoder} ! videoconvert ! {caps}')]
            tile = header["cfa_tile_mode"]
            if tile != "off":
                decoded = [untile_cfa(data, W, H, tile, header["bayer_pattern"]) for data in decoded]
            assert len(decoded) == len(stamps)
            if encoder == "ffv1":
                assert decoded == frames
            expected.extend(zip(stamps, decoded))
            sessions.append((sess, stamps, decoded))
        assert pipe.drops.frames_missing == pipe.drops.enqueue_failures == 0
    finally:
        if pipe._session:
            pipe.deactivate()
        pipe.pipeline.set_state(Gst.State.NULL)
    return expected, sessions


def _read(src, *, buffers=False):
    received = []
    start = src.start_buffers if buffers else src.start
    start(lambda st, data: received.append((st, data, time.monotonic())))
    _pump(lambda: src.finished)
    assert not src.finished_error
    return received


def _check(received, expected):
    assert [(st, bytes(data)) for st, data, _ in received] == expected


def _mixed_color(pixel_format):
    with tempfile.TemporaryDirectory() as tmp:
        expected, sessions = _record(Path(tmp), pixel_format, [
            {"encoder": "ffv1", "segment_seconds": 1},
            {"encoder": "x264", "x264_crf": 12, "x264_preset": "ultrafast", "segment_seconds": 1, "keyframe_interval_s": .24},
            {"encoder": "x264", "x264_crf": 36, "x264_preset": "veryfast", "segment_seconds": 2, "keyframe_interval_s": .08},
            {"encoder": "ffv1", "segment_seconds": 1},
        ])
        src = _source(Path(tmp))
        try:
            assert src.encoded_caps is None, "raw I420 recorded as H.264 must not become stream-copy"
            received = _read(src, buffers=True)
            _check(received, expected)
            assert all(isinstance(data, GstFrame) for _, data, _ in received)
            assert src.playback.descriptor()["sessions"] == 4
            assert src.provenance()["replay_of"] == [sess.path_base for sess, _, _ in sessions]
            # Restart crosses all decoder/settings boundaries again. Retain the old pool buffers
            # to catch pixels being overwritten when a different decoder starts.
            assert src.playback.request("restart")["ok"]
            _pump(lambda: src.finished)
            span = expected[-1][0].timestamp_ns - expected[0][0].timestamp_ns + IV
            assert len(received) == 2 * len(expected)
            _check(received[:len(expected)], expected)
            assert [st.timestamp_ns for st, _, _ in received[len(expected):]] == [st.timestamp_ns + span for st, _ in expected]
            assert [bytes(data) for _, data, _ in received[len(expected):]] == [data for _, data in expected]
        finally:
            src.stop()
        # A window that starts inside a later codec must initialize that session's decoder.
        origin = expected[0][0].timestamp_ns
        first = len(sessions[0][1]) + 2
        last = len(expected) - 2
        begin = (expected[first][0].timestamp_ns - origin) / 1e9
        end = (expected[last][0].timestamp_ns - origin) / 1e9
        src = _source(Path(tmp), epoch_unix_ns=origin, from_s=begin, to_s=end)
        try:
            _check(_read(src), expected[first:last])
        finally:
            src.stop()


def test_stop_change_encoder_and_quality_resume_replays_nv12():
    _mixed_color("NV12")


def test_stop_change_encoder_and_quality_resume_replays_raw_i420():
    _mixed_color("I420")


def test_bayer_tiling_can_change_between_sessions_and_through_lossy_h264():
    with tempfile.TemporaryDirectory() as tmp:
        expected, _ = _record(Path(tmp), "BayerRG8", [
            {"encoder": "ffv1", "bayer_tile": "plain"},
            {"bayer_tile": "green_diff"},
            {"encoder": "x264", "x264_crf": 18},  # recorder turns tiling OFF for lossy H.264
            {"encoder": "ffv1", "bayer_tile": "rct"},
            {"bayer_tile": "off"},
        ], count=4)
        src = _source(Path(tmp))
        try:
            _check(_read(src, buffers=True), expected)
        finally:
            src.stop()


def test_changed_encoder_preserves_the_recorded_gap_on_a_shared_timeline():
    with tempfile.TemporaryDirectory() as tmp:
        expected, _ = _record(Path(tmp), "NV12", [{"encoder": "ffv1"}, {"encoder": "x264"}], count=3, gap_frames=8)
        src = _source(Path(tmp), speed=1, gap_max_s=.01, epoch_unix_ns=expected[0][0].timestamp_ns)
        try:
            received = _read(src)
            _check(received, expected)
            recorded_gap = (expected[3][0].timestamp_ns - expected[2][0].timestamp_ns) / 1e9
            assert abs(recorded_gap - .36) < .001
            assert received[3][2] - received[2][2] >= .28, "a shared timeline must not collapse the stopped-recording gap"
            assert abs(src.playback.descriptor()["duration_s"] - .56) < .001
        finally:
            src.stop()


def test_empty_recording_attempts_do_not_block_mixed_run_replay():
    with tempfile.TemporaryDirectory() as tmp:
        expected, _ = _record(Path(tmp), "NV12", [{"encoder": "ffv1"}, {"encoder": "x264"}],
                              count=3, empty_between=True)
        assert len(list(Path(tmp).glob("*.recording-settings.json"))) == 4
        src = _source(Path(tmp))
        try:
            _check(_read(src), expected)
            assert src.playback.descriptor()["sessions"] == 2
        finally:
            src.stop()


def test_stop_invalidates_pending_end_of_session_callbacks():
    with tempfile.TemporaryDirectory() as tmp:
        _record(Path(tmp), "NV12", [{"encoder": "ffv1"}, {"encoder": "x264"}], count=2)
        src = _source(Path(tmp))
        gen = src._gen
        src._finish(gen)  # schedules a main-loop callback to pause the old reader
        src.stop()
        src._on_eos(None, None, gen)  # an EOS already queued before stop must also be ignored
        src._finish(src._gen)  # window-end callback racing stop can see the updated generation
        ctx = GLib.MainContext.default()
        while ctx.pending():
            ctx.iteration(False)
        assert src._session_idx == 0
        assert src._pipeline.get_state(0)[1] == Gst.State.NULL
        assert src._reader_bus is None
        src.stop()  # teardown can stop a source again after end-of-data


def test_mixed_replay_streams_previews_and_rerecords_every_frame():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        run = root / "input"
        expected, _ = _record(run, "NV12", [{"encoder": "ffv1"}, {"encoder": "x264"},
                                           {"encoder": "ffv1"}], count=6, gap_frames=2)
        cfg = parse_config({
            "camera": {"type": "replay"}, "replay": {"path": str(run), "speed": 1.0},
            "recording": {"encoder": "ffv1", "output_dir": str(root / "output")},
            "preview": {"enabled": True, "sink": "appsink name=preview emit-signals=true sync=false"},
            "transport": {"plugin_endpoint": {"enabled": True, "socket_path": tmp + "/frames"},
                          "raw_endpoint": {"enabled": False}}})
        src = _source(run, speed=1.0)
        pipe = CapturePipeline(cfg, src)
        pipe.build()
        consumer = None
        received, preview = [], []

        def on_preview(sink):
            buf = sink.emit("pull-sample").get_buffer()
            preview.append(buf.extract_dup(0, buf.get_size()))
            return Gst.FlowReturn.OK

        def on_sample(sink):
            buf = sink.emit("pull-sample").get_buffer()
            data = buf.extract_dup(0, buf.get_size())
            if pipe._have_unixfd:
                received.append((buf.offset, buf.offset_end, data))
            else:
                header = unpack_header(data)
                received.append((header.frame_id, header.timestamp_ns, data[HEADER_SIZE:]))
            return Gst.FlowReturn.OK

        try:
            pipe.pipeline.get_by_name("preview").connect("new-sample", on_preview)
            pipe.pipeline.set_state(Gst.State.PLAYING)
            endpoint = (f'unixfdsrc socket-path="{pipe._unixfd_path}"' if pipe._have_unixfd else
                        f'shmsrc socket-path="{tmp}/frames" is-live=true do-timestamp=true')
            consumer = Gst.parse_launch(endpoint + ' ! appsink name=sink emit-signals=true sync=false')
            consumer.get_by_name("sink").connect("new-sample", on_sample)
            consumer.set_state(Gst.State.PLAYING)
            time.sleep(.1)  # allow the transport subscriber to connect
            assert pipe.activate()["ok"]
            sess = pipe._session
            pipe._start_source()
            _pump(lambda: src.finished and len(received) >= len(expected) and len(preview) >= len(expected))
            assert not src.finished_error
            src.stop()
            r = pipe.deactivate()
            assert r["ok"] and not r["session"]["truncated"], r
            assert received == [(st.frame_id, st.timestamp_ns, data) for st, data in expected]
            assert preview == [data for _, data in expected]
            assert load_stamps(sess.path_base + ".csv") == [st for st, _ in expected]
            caps = f"video/x-raw,format=NV12,width={W},height={H},framerate=25/1"
            decoded = [data for part in r["session"]["files"] for _, data in collect(
                f'filesrc location="{part}" ! matroskademux ! avdec_ffv1 ! videoconvert ! {caps}')]
            assert decoded == [data for _, data in expected]
            assert pipe.drops.frames_missing == pipe.drops.enqueue_failures == 0
        finally:
            if consumer is not None:
                consumer.set_state(Gst.State.NULL)
            pipe.shutdown()


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name, flush=True)
